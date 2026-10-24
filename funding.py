#!/usr/bin/env python3
"""
Arbitrage du taux de financement (« cash and carry »), sans pari sur la direction du prix.

Sur un contrat perpétuel, quand le taux de financement est positif, les acheteurs paient
les vendeurs à chaque échéance (toutes les 8 h, 4 h ou 1 h selon le contrat). La stratégie :
  - acheter la crypto au comptant, et
  - vendre à découvert la même quantité en contrat perpétuel.
Les deux jambes se compensent : la position ne gagne ni ne perd quand le prix bouge, mais
elle encaisse le financement tant qu'il reste positif.

Règles :
  - entrée si le taux moyen des dernières `funding_fenetre_h` heures ET le taux en cours
    dépassent `funding_entree_annuel_pct` (annualisé), et si le gain attendu sur
    `funding_horizon_j` jours couvre les frais aller-retour avec une marge ;
  - sortie quand le taux moyen récent tombe sous `funding_sortie_annuel_pct`, ou si la
    marge de la jambe à découvert s'épuise (sécurité).

Risques réels : taux qui s'inverse, écart entre comptant et perpétuel à l'entrée et à la
sortie, liquidation de la jambe à découvert si le prix s'envole et qu'on ne rééquilibre pas.

    python3 funding.py scan          # opportunités du moment
    python3 funding.py backtest      # rendement sur l'historique réel des taux
    python3 funding.py               # moteur papier (une décision toutes les 5 min)
    python3 funding.py bilan
"""

import argparse
import csv
import json
import os
import time
from datetime import datetime

import trader as T
from crypto_agent import bold, cyan, dim, fmt_price, green, red, yellow
from sources import http_json

HERE = os.path.dirname(os.path.abspath(__file__))
FAPI = "https://fapi.binance.com/fapi/v1"
SPOT = "https://api.binance.com/api/v3"
CYCLE_S = 300
DAY_MS = 86400000


# --------------------------------------------------------------------------- #
# Données
# --------------------------------------------------------------------------- #


def annual(rate, interval_h):
    """Taux par échéance → rendement annualisé en %."""
    return rate * 24 / interval_h * 365 * 100


class FundingData:
    def __init__(self):
        self._spot = None

    def spot_symbols(self):
        if self._spot is None:
            info = http_json(SPOT + "/exchangeInfo", {"permissions": "SPOT"})
            self._spot = {s["symbol"] for s in info["symbols"] if s["status"] == "TRADING"}
        return self._spot

    def intervals(self):
        """Contrats dont l'échéance n'est pas de 8 h (les autres sont absents de la liste)."""
        return {d["symbol"]: d["fundingIntervalHours"] for d in http_json(FAPI + "/fundingInfo")}

    def premium(self):
        return {d["symbol"]: d for d in http_json(FAPI + "/premiumIndex")}

    def volumes(self):
        return {d["symbol"]: float(d["quoteVolume"]) for d in http_json(FAPI + "/ticker/24hr")}

    def perp_books(self):
        return {d["symbol"]: (float(d["bidPrice"]), float(d["askPrice"]))
                for d in http_json(FAPI + "/ticker/bookTicker")}

    def spot_books(self):
        return {d["symbol"]: (float(d["bidPrice"]), float(d["askPrice"]))
                for d in http_json(SPOT + "/ticker/bookTicker")}

    def history(self, symbol, start_ms, end_ms=None):
        """Taux réellement appliqués depuis start_ms : [(instant ms, taux, prix mark)]."""
        out, cursor = [], start_ms
        while True:
            params = {"symbol": symbol, "startTime": int(cursor), "limit": 1000}
            if end_ms:
                params["endTime"] = int(end_ms)
            page = http_json(FAPI + "/fundingRate", params)
            out += [(r["fundingTime"], float(r["fundingRate"]), float(r.get("markPrice") or 0)) for r in page]
            if len(page) < 1000:
                return out
            cursor = page[-1]["fundingTime"] + 1


# --------------------------------------------------------------------------- #
# Règles (communes au backtest et au moteur)
# --------------------------------------------------------------------------- #


def round_trip_cost_pct(cfg):
    """Frais et glissement d'un aller-retour complet (deux jambes, entrée et sortie), en %."""
    per_leg = cfg["frais_comptant_pct"] + cfg["frais_pct"] + 2 * cfg["glissement_pct"]
    return 2 * per_leg


def should_enter(avg_annual, current_annual, cfg):
    """Entrer si le taux est durablement élevé et si l'horizon couvre les frais avec marge."""
    if avg_annual < cfg["funding_entree_annuel_pct"] or current_annual < cfg["funding_entree_annuel_pct"]:
        return False
    expected = min(avg_annual, current_annual) / 365 * cfg["funding_horizon_j"]
    return expected >= cfg["funding_marge_frais"] * round_trip_cost_pct(cfg)


def should_exit(avg_annual, cfg):
    return avg_annual < cfg["funding_sortie_annuel_pct"]


def trailing_avg(rates, n):
    tail = rates[-n:]
    return sum(tail) / len(tail) if tail else 0.0


# --------------------------------------------------------------------------- #
# Backtest sur l'historique réel des taux
# --------------------------------------------------------------------------- #


def backtest(cfg, days=180, top=40, verbose=True):
    data = FundingData()
    vols, intervals = data.volumes(), data.intervals()
    spot = data.spot_symbols()
    symbols = sorted((s for s in vols if s.endswith("USDT") and s in spot
                      and vols[s] >= cfg["funding_volume_min_usd"]), key=lambda s: -vols[s])[:top]
    start = time.time() * 1000 - days * DAY_MS
    series = {}
    for i, s in enumerate(symbols, 1):
        if verbose:
            print(dim("  [{}/{}] historique des taux de {}…".format(i, len(symbols), s)), end="\r", flush=True)
        h = data.history(s, start - cfg["funding_fenetre_h"] * 3600000)
        if len(h) > 10:
            series[s] = h
    if verbose:
        print(" " * 70, end="\r")

    # Chronologie commune : chaque échéance de chaque contrat est un événement.
    events = sorted((t, s, r) for s, h in series.items() for t, r, _ in h if t >= start)
    hist = {s: [] for s in series}
    for s, h in series.items():
        hist[s] = [r for t, r, _ in h if t < start]
    cost = round_trip_cost_pct(cfg) / 100
    slots = cfg["funding_positions_max"]
    slot_share = 1 / slots
    capital, positions, trades = 1.0, {}, []
    for t, s, rate in events:
        h_s = intervals.get(s, 8)
        if s in positions:  # financement encaissé par la jambe à découvert
            positions[s]["funding"] += rate * positions[s]["notional"]
        hist[s].append(rate)
        n = max(1, int(cfg["funding_fenetre_h"] / h_s))
        avg = annual(trailing_avg(hist[s], n), h_s)
        cur = annual(rate, h_s)
        if s in positions and should_exit(avg, cfg):
            p = positions.pop(s)
            pnl = p["funding"] - cost * p["notional"]
            capital += pnl
            trades.append((s, (t - p["t"]) / DAY_MS, pnl / p["notional"] * 100))
        elif s not in positions and len(positions) < slots and should_enter(avg, cur, cfg):
            # Capital immobilisé par position : comptant (1) + marge du perpétuel (1/levier).
            notional = capital * slot_share / (1 + 1 / cfg["funding_levier"])
            positions[s] = {"t": t, "notional": notional, "funding": 0.0}
    end_t = events[-1][0] if events else start
    for s, p in positions.items():  # positions encore ouvertes : soldées à la fin
        capital += p["funding"] - cost * p["notional"]
        trades.append((s, (end_t - p["t"]) / DAY_MS, (p["funding"] / p["notional"] - cost) * 100))
    total = (capital - 1) * 100
    years = days / 365
    result = {"jours": days, "contrats": len(series), "rendement_pct": total,
              "annualise_pct": ((capital) ** (1 / years) - 1) * 100 if years and capital > 0 else None,
              "trades": len(trades), "gagnants": sum(1 for *_, r in trades if r > 0),
              "duree_moyenne_j": sum(d for _, d, _ in trades) / len(trades) if trades else 0,
              "meilleurs": sorted(trades, key=lambda x: -x[2])[:5], "pires": sorted(trades, key=lambda x: x[2])[:3]}
    return result


# --------------------------------------------------------------------------- #
# Moteur papier
# --------------------------------------------------------------------------- #


class FundingArb:
    def __init__(self, mode="papier", cfg=None, state_dir=HERE):
        if mode != "papier":
            raise SystemExit("Seul le mode papier existe pour l'arbitrage de financement à ce stade.")
        self.cfg = cfg or T.load_config()
        self.mode = mode
        self.data = FundingData()
        self.state_path = os.path.join(state_dir, "portefeuille_funding_{}.json".format(mode))
        self.journal_path = os.path.join(state_dir, "journal_funding_{}.csv".format(mode))
        self.live_path = os.path.join(state_dir, "etat_funding_{}.json".format(mode))
        self.state = self._load()
        self.market = {}

    def _load(self):
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        cap = self.cfg["funding_capital"]
        return {"cash": cap, "positions": {}, "debut": time.time(),
                "stats": {"trades": 0, "gagnants": 0, "pnl": 0.0, "frais": 0.0, "financement": 0.0}}

    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_path)

    # -- marché ---------------------------------------------------------------------

    def scan(self):
        """Opportunités classées : {symbole: {taux, annualisé, moyenne, prix…}}."""
        cfg, d = self.cfg, self.data
        prem, vols, intervals = d.premium(), d.volumes(), d.intervals()
        spot, sbooks, pbooks = d.spot_symbols(), d.spot_books(), d.perp_books()
        rows = {}
        for s, p in prem.items():
            if not s.endswith("USDT") or s not in spot or s not in sbooks or s not in pbooks:
                continue
            if vols.get(s, 0) < cfg["funding_volume_min_usd"] and s not in self.state["positions"]:
                continue
            h = intervals.get(s, 8)
            rate = float(p["lastFundingRate"] or 0)
            sb, pb = sbooks[s], pbooks[s]
            if min(sb + pb) <= 0:
                continue
            rows[s] = {"symbole": s, "taux": rate, "intervalle_h": h, "annuel": annual(rate, h),
                       "spot_bid": sb[0], "spot_ask": sb[1], "perp_bid": pb[0], "perp_ask": pb[1],
                       "mark": float(p["markPrice"]), "prochaine": p["nextFundingTime"],
                       "ecart_pct": (pb[0] / sb[1] - 1) * 100, "volume": vols.get(s, 0)}
        self.market = rows
        return rows

    def trailing(self, s, h):
        hours = self.cfg["funding_fenetre_h"]
        hist = self.data.history(s, time.time() * 1000 - hours * 3600000)
        return annual(trailing_avg([r for _, r, _ in hist], max(1, int(hours / h))), h), hist

    def equity(self):
        total = self.state["cash"]
        for s, p in self.state["positions"].items():
            m = self.market.get(s)
            if m:
                total += self.position_value(p, m)
        return total

    def position_value(self, p, m):
        """Valeur de liquidation : comptant vendu au bid, perpétuel racheté à l'ask, financement encaissé."""
        spot_leg = p["qty"] * m["spot_bid"]
        perp_pnl = p["qty"] * (p["perp_entry"] - m["perp_ask"])
        return spot_leg + p["marge"] + perp_pnl + p["financement"]

    # -- décisions --------------------------------------------------------------------

    def open(self, s, m):
        cfg, st = self.cfg, self.state
        eq = self.equity()
        budget = min(eq / cfg["funding_positions_max"], st["cash"])
        notional = budget / (1 + 1 / cfg["funding_levier"])  # comptant + marge du perpétuel
        if notional < 20:
            return
        slip = cfg["glissement_pct"] / 100
        spot_px, perp_px = m["spot_ask"] * (1 + slip), m["perp_bid"] * (1 - slip)
        qty = notional / spot_px
        fees = qty * spot_px * cfg["frais_comptant_pct"] / 100 + qty * perp_px * cfg["frais_pct"] / 100
        marge = qty * perp_px / cfg["funding_levier"]
        st["cash"] -= qty * spot_px + marge + fees
        st["positions"][s] = {"qty": qty, "spot_entry": spot_px, "perp_entry": perp_px, "marge": marge,
                              "frais": fees, "financement": 0.0, "ouvert": time.time() * 1000,
                              "annuel_entree": m["annuel"]}
        st["stats"]["frais"] += fees
        self.log("OUVERTURE", s, qty, spot_px, perp_px, fees, None, "taux {:+.1f} %/an".format(m["annuel"]))
        print("  {} {} : achat comptant {} + vente perpétuel {} · {} · taux {:+.1f} %/an".format(
            green("▶ CARRY"), s, fmt_price(spot_px, "usd"), fmt_price(perp_px, "usd"),
            T.fmt_big(qty * spot_px, "usd"), m["annuel"]))

    def close(self, s, m, reason):
        cfg, st = self.cfg, self.state
        p = st["positions"].pop(s)
        slip = cfg["glissement_pct"] / 100
        spot_px, perp_px = m["spot_bid"] * (1 - slip), m["perp_ask"] * (1 + slip)
        fees = p["qty"] * spot_px * cfg["frais_comptant_pct"] / 100 + p["qty"] * perp_px * cfg["frais_pct"] / 100
        proceeds = p["qty"] * spot_px + p["marge"] + p["qty"] * (p["perp_entry"] - perp_px) + p["financement"] - fees
        st["cash"] += proceeds
        cost_in = p["qty"] * p["spot_entry"] + p["marge"] + p["frais"]
        pnl = proceeds - cost_in
        s_ = st["stats"]
        s_["trades"] += 1
        s_["gagnants"] += 1 if pnl > 0 else 0
        s_["pnl"] += pnl
        s_["frais"] += fees
        self.log("FERMETURE", s, p["qty"], spot_px, perp_px, fees, pnl, reason)
        days = (time.time() * 1000 - p["ouvert"]) / DAY_MS
        print("  {} {} ({}) après {:.1f} j : financement {:+.2f} $ → {}".format(
            yellow("■ FIN"), s, reason, days, p["financement"], (green if pnl >= 0 else red)("{:+.2f} $".format(pnl))))

    def log(self, action, s, qty, spot_px, perp_px, fees, pnl, reason):
        new = not os.path.exists(self.journal_path)
        with open(self.journal_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["date", "action", "symbole", "quantite", "comptant", "perpetuel", "frais", "pnl", "raison"])
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, s, round(qty, 8), spot_px, perp_px,
                        round(fees, 4), "" if pnl is None else round(pnl, 4), reason])

    def accrue(self):
        """Financement réellement versé depuis l'ouverture de chaque position (historique Binance)."""
        for s, p in self.state["positions"].items():
            hist = self.data.history(s, p["ouvert"] + 1)
            earned = sum(p["qty"] * (mark or self.market.get(s, {}).get("mark", 0)) * rate for _, rate, mark in hist)
            delta = earned - p["financement"]
            p["financement"] = earned
            self.state["stats"]["financement"] += delta

    def cycle(self):
        cfg = self.cfg
        self.scan()
        self.accrue()
        # 1. Sorties : taux retombé, ou marge de la jambe à découvert presque épuisée
        for s in list(self.state["positions"]):
            m = self.market.get(s)
            if not m:
                continue
            p = self.state["positions"][s]
            avg, _ = self.trailing(s, m["intervalle_h"])
            perp_loss = p["qty"] * (m["perp_ask"] - p["perp_entry"])
            if perp_loss > 0.8 * p["marge"]:
                self.close(s, m, "marge du perpétuel épuisée")
            elif should_exit(avg, cfg):
                self.close(s, m, "taux retombé ({:+.1f} %/an en moyenne)".format(avg))
        # 2. Entrées : meilleurs taux d'abord, écart comptant/perpétuel raisonnable
        free = cfg["funding_positions_max"] - len(self.state["positions"])
        candidates = sorted((m for s, m in self.market.items() if s not in self.state["positions"]
                             and m["annuel"] >= cfg["funding_entree_annuel_pct"]
                             and abs(m["ecart_pct"]) <= cfg["funding_ecart_max_pct"]),
                            key=lambda m: -m["annuel"])
        for m in candidates:
            if free <= 0:
                break
            avg, _ = self.trailing(m["symbole"], m["intervalle_h"])
            if should_enter(avg, m["annuel"], cfg):
                self.open(m["symbole"], m)
                free -= 1
        self.save()
        self.write_live()
        self.status()

    def snapshot(self):
        eq = self.equity()
        positions = []
        for s, p in self.state["positions"].items():
            m = self.market.get(s, {})
            positions.append({"symbole": s, "notionnel": round(p["qty"] * p["spot_entry"], 2),
                              "financement": round(p["financement"], 4),
                              "valeur": round(self.position_value(p, m), 2) if m else None,
                              "taux_annuel": m.get("annuel"), "jours": (time.time() * 1000 - p["ouvert"]) / DAY_MS})
        top = sorted(self.market.values(), key=lambda m: -m["annuel"])[:10]
        return {"maj": time.time(), "capital_initial": self.cfg["funding_capital"], "valeur": round(eq, 2),
                "cash": round(self.state["cash"], 2), "stats": self.state["stats"], "positions": positions,
                "opportunites": [{"symbole": m["symbole"], "annuel": round(m["annuel"], 1),
                                  "ecart_pct": round(m["ecart_pct"], 3)} for m in top]}

    def write_live(self):
        tmp = self.live_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.snapshot(), f)
        os.replace(tmp, self.live_path)

    def status(self):
        snap = self.snapshot()
        cap = snap["capital_initial"]
        print(dim("  ── {} · valeur {} ({:+.2f} %) · {} position(s) · financement encaissé {:+.2f} $ · frais {:.2f} $".format(
            datetime.now().strftime("%H:%M"), fmt_price(snap["valeur"], "usd"), (snap["valeur"] / cap - 1) * 100,
            len(snap["positions"]), self.state["stats"]["financement"], self.state["stats"]["frais"])), flush=True)

    def run(self):
        print(bold(cyan("  ⚖ Arbitrage de financement [PAPIER] · capital {} · entrée ≥ {} %/an · sortie < {} %/an".format(
            fmt_price(self.cfg["funding_capital"], "usd"), self.cfg["funding_entree_annuel_pct"],
            self.cfg["funding_sortie_annuel_pct"]))), flush=True)
        while True:
            try:
                self.cycle()
            except Exception as e:  # réseau ou API : on retente au cycle suivant
                print(red("  Erreur : {}".format(e)), flush=True)
            time.sleep(CYCLE_S)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def show_scan(fa):
    rows = sorted(fa.scan().values(), key=lambda m: -m["annuel"])[:15]
    print(bold("\n  Contrat        Taux/échéance   Annualisé   Échéance   Écart perp/comptant"))
    for m in rows:
        print("  {:<14} {:>+12.4%}   {:>8.1f} %   {:>5} h   {:>+10.3f} %".format(
            m["symbole"], m["taux"], m["annuel"], m["intervalle_h"], m["ecart_pct"]))
    print(dim("  Frais d'un aller-retour complet : {:.2f} % · seuil d'entrée {} %/an".format(
        round_trip_cost_pct(fa.cfg), fa.cfg["funding_entree_annuel_pct"])))


def main():
    parser = argparse.ArgumentParser(description="Arbitrage du taux de financement (comptant + perpétuel).")
    parser.add_argument("cmd", nargs="?", default="lancer", choices=["lancer", "scan", "backtest", "bilan", "reset"])
    parser.add_argument("--jours", type=int, default=180)
    parser.add_argument("--contrats", type=int, default=40)
    args = parser.parse_args()
    cfg = T.load_config()
    if args.cmd == "backtest":
        r = backtest(cfg, args.jours, args.contrats)
        print(bold("\n  Backtest arbitrage de financement · {} jours · {} contrats".format(r["jours"], r["contrats"])))
        print("  Rendement : {:+.2f} %  (≈ {:+.1f} %/an) · {} positions · {} gagnantes · durée moyenne {:.1f} j".format(
            r["rendement_pct"], r["annualise_pct"] or 0, r["trades"], r["gagnants"], r["duree_moyenne_j"]))
        for s, d, ret in r["meilleurs"]:
            print("    + {:<12} {:5.1f} j  {:+.2f} %".format(s, d, ret))
        for s, d, ret in r["pires"]:
            print("    − {:<12} {:5.1f} j  {:+.2f} %".format(s, d, ret))
        print(dim("  Financement réellement versé, frais et glissement inclus ; écart comptant/perpétuel ignoré."))
        return
    fa = FundingArb("papier", cfg)
    if args.cmd == "scan":
        show_scan(fa)
    elif args.cmd == "bilan":
        fa.scan()
        snap = fa.snapshot()
        print(json.dumps(snap, indent=1, ensure_ascii=False))
    elif args.cmd == "reset":
        for p in (fa.state_path, fa.journal_path, fa.live_path):
            if os.path.exists(p):
                os.remove(p)
        print("Arbitrage de financement remis à zéro.")
    else:
        try:
            fa.run()
        except KeyboardInterrupt:
            print("\n  Arrêté.")


if __name__ == "__main__":
    main()
