#!/usr/bin/env python3
"""
Tableau de bord temps réel de l'agent (style Grafana) : http://localhost:8050

Lit les fichiers que l'agent de scalping écrit en continu (solde, courbe de valeur,
journal des trades, état des modèles) et les sert à une page web rafraîchie chaque
seconde. N'écoute que sur la machine locale. Bibliothèque standard uniquement.

    python3 dashboard.py              # puis ouvrir http://localhost:8050
    python3 dashboard.py --port 8051
"""

import argparse
import csv
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from learner import OnlineLogit

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "dashboard.html")
MAX_POINTS = 600   # points de courbe envoyés au navigateur
MAX_TRADES = 300
STALE_S = 15       # au-delà, l'agent est considéré arrêté
DEFAULT_LONG_PROFILES = ("trente_s", "une_min", "cinq_min", "dix_min")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_csv(path, last=None):
    try:
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    return rows[-last:] if last else rows


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def curve(path, since):
    pts = [(num(r["t"]), num(r["valeur"])) for r in read_csv(path) if num(r.get("t")) >= since]
    step = max(1, -(-len(pts) // MAX_POINTS))  # arrondi supérieur : au plus MAX_POINTS points
    out = pts[::step]
    if pts and out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def trade_stats(trades):
    nets = [num(t["net_bps"]) for t in trades]
    wins = [v for v in nets if v > 0]
    losses = [v for v in nets if v <= 0]
    reasons, variants = {}, {}
    for t in trades:
        reasons[t["raison"]] = reasons.get(t["raison"], 0) + 1
        v = variants.setdefault(t.get("profil", "?"), {"n": 0, "pnl": 0.0, "gagnants": 0})
        v["n"] += 1
        v["pnl"] += num(t["pnl"])
        v["gagnants"] += 1 if num(t["pnl"]) > 0 else 0
    probas = [num(t.get("proba")) for t in trades if t.get("proba")]
    return {
        "esperance_bps": sum(nets) / len(nets) if nets else None,
        "gain_moyen_bps": sum(wins) / len(wins) if wins else None,
        "perte_moyenne_bps": sum(losses) / len(losses) if losses else None,
        "duree_moyenne_s": sum(num(t["duree_s"]) for t in trades) / len(trades) if trades else None,
        "proba_annoncee": sum(probas) / len(probas) if probas else None,
        "raisons": reasons,
        "variantes": variants,
    }


def long_profiles(base):
    """Noms des profils de la stratégie longue, lus dans la configuration effective."""
    cfg = read_json(os.path.join(base, "config_trading.json")) or {}
    return {p["nom"] for p in cfg.get("scalp_profils_longs", [])} or set(DEFAULT_LONG_PROFILES)


def strategy_of(variant, longs):
    parts = variant.replace("explo:", "").split("/")
    return "longue" if len(parts) > 1 and parts[1] in longs else "court"


def models(state, longs):
    rows = []
    for key, d in (state or {}).get("modeles", {}).items():
        m = OnlineLogit.from_dict(d)
        if not m.seen:
            continue
        said, real = m.calibration()
        rows.append({"variante": key, "strategie": strategy_of(key, longs), "exemples": m.seen, "gagnants": m.base_rate, "annonce": said, "reel": real,
                     "execution": state.get("remplissage", {}).get(key) if key.endswith("limite") else None})
    rows.sort(key=lambda r: -r["gagnants"])
    return rows


def cryptos(state):
    now = time.time() * 1000
    rows = [{"crypto": c.replace("USDT", ""), "resultat_bps": v.get("edge", 0.0), "exemples": v.get("n", 0),
             "quarantaine_min": max(0.0, (v.get("pause_jusqua", 0) - now) / 60000)}
            for c, v in (state or {}).get("cryptos", {}).items() if v.get("n", 0) >= 10]
    rows.sort(key=lambda r: r["resultat_bps"])
    return rows


def build(mode, window_s, base=HERE):
    path = lambda name: os.path.join(base, name.format(mode))
    live = read_json(path("etat_scalp_{}.json"))
    state = read_json(path("portefeuille_scalp_{}.json"))
    trades = read_csv(path("journal_scalp_{}.csv"))
    longs = long_profiles(base)
    since = time.time() - window_s if window_s else 0
    age = time.time() - live["maj"] if live else None
    groups = {}
    for g in ("court", "longue"):
        rows = [t for t in trades if strategy_of(t.get("profil", ""), longs) == g]
        positions = [p for p in (live or {}).get("positions", []) if p.get("strategie", "court") == g]
        explo = [t for t in rows if t.get("profil", "").startswith("explo:")]
        groups[g] = {"trades": rows[-100:], "stats": trade_stats(rows[-MAX_TRADES:]), "positions": positions,
                     "total": {"trades": len(rows), "gagnants": sum(1 for t in rows if num(t["pnl"]) > 0),
                               "pnl": sum(num(t["pnl"]) for t in rows), "exploration": len(explo),
                               "exploration_pnl": sum(num(t["pnl"]) for t in explo)}}
    trades = trades[-MAX_TRADES:]
    return {
        "maintenant": time.time(),
        "mode": mode,
        "en_ligne": age is not None and age < STALE_S,
        "age_s": age,
        "solde": live,
        "courbe": curve(path("courbe_scalp_{}.csv"), since),
        "trades": trades[-100:],
        "stats": trade_stats(trades),
        "modeles": models(state, longs),
        "tendance": read_json(path("etat_tendance_{}.json")),
        "financement": read_json(path("etat_funding_{}.json")),
        "strategies": groups,
        "cryptos": cryptos(state),
    }


class Handler(BaseHTTPRequestHandler):
    mode = "papier"

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if url.path == "/api/etat":
            q = parse_qs(url.query)
            mode = q.get("mode", [self.mode])[0]
            if mode not in ("papier", "testnet", "reel"):
                return self._send(400, b'{"erreur": "mode inconnu"}', "application/json")
            window = num(q.get("fenetre", ["3600"])[0], 3600)
            body = json.dumps(build(mode, window), ensure_ascii=False).encode("utf-8")
            return self._send(200, body, "application/json; charset=utf-8")
        return self._send(404, b"introuvable", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        pass  # pas de ligne de journal à chaque rafraîchissement


def main():
    parser = argparse.ArgumentParser(description="Tableau de bord temps réel de l'agent crypto.")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--mode", choices=["papier", "testnet", "reel"], default="papier")
    args = parser.parse_args()
    Handler.mode = args.mode
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Tableau de bord : http://localhost:{args.port}  (Ctrl+C pour arrêter)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
