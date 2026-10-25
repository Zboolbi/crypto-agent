#!/usr/bin/env python3
"""
Analyse comparée des stratégies sur une période : scalping, tendance, financement.

    python3 analyse.py point-de-depart        # enregistre la valeur de chaque portefeuille
    python3 analyse.py                        # analyse depuis le dernier point de départ
    python3 analyse.py --json                 # même chose, en JSON

Pour chaque stratégie : variation de valeur, nombre de trades, taux de gain, gain moyen,
répartition par variante, par crypto et par raison de sortie.
"""

import argparse
import csv
import json
import os
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE = os.path.join(HERE, "point_de_depart.json")


def read_json(name):
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_csv(name):
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def values():
    """Valeur actuelle (réalisé + latent connu) de chaque portefeuille papier."""
    scalp = read_json("etat_scalp_papier.json") or {}
    fund = read_json("etat_funding_papier.json") or {}
    trend = read_json("etat_tendance_papier.json") or {}
    return {
        "scalping": scalp.get("valeur"),
        "financement": fund.get("valeur"),
        "tendance": trend.get("valeur"),  # réalisé + latent des positions ouvertes
    }


def group(rows, key, pnl_key):
    out = {}
    for r in rows:
        g = out.setdefault(key(r), {"n": 0, "gagnants": 0, "pnl": 0.0})
        g["n"] += 1
        g["gagnants"] += 1 if num(r[pnl_key]) > 0 else 0
        g["pnl"] += num(r[pnl_key])
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["pnl"]))


def summarize(rows, pnl_key):
    n = len(rows)
    wins = sum(1 for r in rows if num(r[pnl_key]) > 0)
    pnl = sum(num(r[pnl_key]) for r in rows)
    return {"trades": n, "gagnants": wins, "taux_gain": wins / n if n else None, "pnl": pnl,
            "pnl_moyen": pnl / n if n else None}


def analyse(since_ts):
    since = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M:%S")
    scalp = [r for r in read_csv("journal_scalp_papier.csv") if r["date"] >= since]
    trend = [r for r in read_csv("journal_papier.csv") if r["date"] >= since and r["action"] == "FERMETURE"]
    fund = [r for r in read_csv("journal_funding_papier.csv") if r["date"] >= since and r["action"] == "FERMETURE"]
    model = [r for r in scalp if not r["profil"].startswith("explo:")]
    explo = [r for r in scalp if r["profil"].startswith("explo:")]
    out = {
        "depuis": since, "duree_h": (time.time() - since_ts) / 3600,
        "scalping": dict(summarize(scalp, "pnl"), modele=summarize(model, "pnl"), exploration=summarize(explo, "pnl"),
                         net_moyen_bps=sum(num(r["net_bps"]) for r in scalp) / len(scalp) if scalp else None,
                         brut_moyen_bps=sum(num(r["brut_bps"]) for r in scalp) / len(scalp) if scalp else None,
                         par_variante=group(scalp, lambda r: r["profil"].replace("explo:", ""), "pnl"),
                         par_crypto=group(scalp, lambda r: r["crypto"], "pnl"),
                         par_sortie=group(scalp, lambda r: r["raison"], "pnl")),
        "tendance": dict(summarize(trend, "pnl"), par_crypto=group(trend, lambda r: r["crypto"], "pnl"),
                         par_sortie=group(trend, lambda r: r["raison"], "pnl")),
        "financement": summarize(fund, "pnl"),
    }
    base = read_json(os.path.basename(BASELINE)) or {}
    now = values()
    out["valeurs"] = {k: {"depart": base.get("valeurs", {}).get(k), "actuel": v} for k, v in now.items()}
    fund_state = read_json("portefeuille_funding_papier.json") or {}
    out["financement"]["positions_ouvertes"] = len(fund_state.get("positions", {}))
    out["financement"]["financement_encaisse"] = fund_state.get("stats", {}).get("financement")
    return out


def pct(v):
    return "—" if v is None else f"{v * 100:.0f} %"


def report(a):
    print("Analyse depuis {} ({:.1f} h)".format(a["depuis"], a["duree_h"]))
    for k, v in a["valeurs"].items():
        if v["depart"] is not None and v["actuel"] is not None:
            print("  {:<12} {:>10.2f} $ → {:>10.2f} $  ({:+.2f} $)".format(k, v["depart"], v["actuel"],
                                                                            v["actuel"] - v["depart"]))
    s = a["scalping"]
    print("\nScalping : {} trades · gagnants {} · {:+.2f} $ · brut {:+.1f} pb, net {:+.1f} pb par trade".format(
        s["trades"], pct(s["taux_gain"]), s["pnl"], s["brut_moyen_bps"] or 0, s["net_moyen_bps"] or 0))
    print("  décidés par le modèle : {} trades, gagnants {}, {:+.2f} $ · exploration : {} trades, {:+.2f} $".format(
        s["modele"]["trades"], pct(s["modele"]["taux_gain"]), s["modele"]["pnl"], s["exploration"]["trades"],
        s["exploration"]["pnl"]))
    for title, key in (("variantes", "par_variante"), ("sorties", "par_sortie")):
        rows = list(s[key].items())
        print(f"  {title} : " + " · ".join("{} {}/{} {:+.2f} $".format(k, g["gagnants"], g["n"], g["pnl"])
                                                    for k, g in rows[:6]))
    t = a["tendance"]
    print("\nTendance : {} trades fermés · gagnants {} · {:+.2f} $".format(t["trades"], pct(t["taux_gain"]), t["pnl"]))
    f = a["financement"]
    print("Financement : {} positions ouvertes · {} fermées · financement encaissé {:+.4f} $".format(
        f["positions_ouvertes"], f["trades"], f["financement_encaisse"] or 0))


def main():
    p = argparse.ArgumentParser(description="Analyse comparée des stratégies.")
    p.add_argument("cmd", nargs="?", default="analyse", choices=["analyse", "point-de-depart"])
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    if args.cmd == "point-de-depart":
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "valeurs": values()}, f, indent=1)
        print("Point de départ enregistré :", values())
        return
    base = read_json(os.path.basename(BASELINE))
    a = analyse(base["ts"] if base else time.time() - 4 * 3600)
    if args.json:
        print(json.dumps(a, indent=1, ensure_ascii=False))
    else:
        report(a)


if __name__ == "__main__":
    main()
