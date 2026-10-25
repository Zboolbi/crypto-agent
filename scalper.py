#!/usr/bin/env python3
"""
Scalping court terme qui apprend de ses erreurs : beaucoup de petits shorts de quelques
secondes à quelques minutes, déclenchés en temps réel par le flux d'ordres Binance Futures.

Fonctionnement :
  1. Flux temps réel : transactions (/market aggTrade) et 5 meilleurs niveaux du carnet
     (/public depth5@100ms), deux connexions qui se reconnectent seules.
  2. Pré-filtre : pression vendeuse et petite baisse → signal candidat.
  3. Chaque candidat lance des trades « fantômes » (un par profil : éclair, rapide, large),
     suivis virtuellement jusqu'à l'objectif, au stop ou à la durée maximale. Leurs résultats
     entraînent en continu un modèle par profil (learner.py) : l'agent apprend quels contextes
     mènent aux pertes, sans engager de capital.
  4. Trade réel seulement si le modèle prévoit un gain espéré positif après frais. Le profil
     retenu est celui dont l'espérance est la meilleure ; la taille dépend de la confiance.
  5. Entrée par ordre limite (frais maker), file d'attente simulée en mode papier ; objectif
     en ordre limite, stop au marché. Aucune pause imposée : l'agent peut reshorter la même
     crypto quelques millisecondes après une sortie si le modèle le justifie.

⚠️  La latence réseau (≈ 100 à 300 ms depuis un ordinateur personnel) reste incompressible.
    Le journal affiche le résultat net de frais : jugez sur ce chiffre.
"""

import collections
import csv
import fcntl
import json
import logging
import math
import os
import queue
import random
import sys
import threading
import time
from datetime import date, datetime

import trader as T
from crypto_agent import bold, cyan, dim, fmt_price, green, red, yellow
from learner import OnlineLogit
from sources import WebSocket, binance_clock_offset_ms, binance_stream_urls

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_MS = 250       # pas d'échantillonnage du prix
LONG_MS = 30000       # horizon de la baisse « 30 s » et de la volatilité
STATUS_S = 30
TREND_REFRESH_S = 300
SHADOW_SPACING_MS = 1000  # au plus un groupe de fantômes par seconde, par crypto et par famille
QUARANTINE_TRADES = 5     # trades réels examinés pour la quarantaine
QUARANTINE_MS = 3600000   # durée de la quarantaine d'une crypto
LIVE_FILE_S = 1           # fréquence d'écriture du solde temps réel
CURVE_S = 10              # fréquence d'enregistrement de la courbe de valeur (tableau de bord)


class AlreadyRunning(RuntimeError):
    pass

log = logging.getLogger("scalper")


def setup_logging():
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False


# --------------------------------------------------------------------------- #
# Carnet et flux d'un contrat
# --------------------------------------------------------------------------- #


class Book:
    __slots__ = ("bid", "ask", "bid_qty", "ask_qty", "bid_top", "ask_top", "last",
                 "trades", "buy", "sell", "samples")

    def __init__(self):
        self.bid = self.ask = self.last = None
        self.bid_qty = self.ask_qty = self.bid_top = self.ask_top = 0.0
        self.trades = collections.deque()   # (t, montant signé $) ; > 0 = achat agressif
        self.buy = self.sell = 0.0
        self.samples = collections.deque()  # (t, prix)

    def add_trade(self, t, price, usd, window_ms):
        self.last = price
        self.trades.append((t, usd))
        if usd > 0:
            self.buy += usd
        else:
            self.sell -= usd
        while self.trades and self.trades[0][0] < t - window_ms:
            _, old = self.trades.popleft()
            if old > 0:
                self.buy -= old
            else:
                self.sell += old
        if not self.samples or t - self.samples[-1][0] >= SAMPLE_MS:
            self.samples.append((t, price))
        while self.samples and self.samples[0][0] < t - LONG_MS - 1000:
            self.samples.popleft()

    def set_depth(self, bids, asks):
        if bids and asks:
            self.bid, self.bid_top = float(bids[0][0]), float(bids[0][1])
            self.ask, self.ask_top = float(asks[0][0]), float(asks[0][1])
            self.bid_qty = sum(float(q) for _, q in bids)
            self.ask_qty = sum(float(q) for _, q in asks)

    def flow(self):
        vol = self.buy + self.sell
        return (self.buy - self.sell) / vol if vol else 0.0

    def price_ago(self, t, ms):
        for ts, p in self.samples:
            if ts >= t - ms:
                return p
        return None

    def volatility_bps(self):
        pts = [p for _, p in self.samples]
        if len(pts) < 5:
            return 0.0
        rets = [(b / a - 1) * 1e4 for a, b in zip(pts[:-1], pts[1:])]
        m = sum(rets) / len(rets)
        return math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))


# --------------------------------------------------------------------------- #
# Trade simulé (fantôme ou papier) : ordre limite, file d'attente, sorties
# --------------------------------------------------------------------------- #


def model_key(family, profile, maker):
    return "{}/{}/{}".format(family, profile["nom"], "limite" if maker else "marche")


class SimTrade:
    __slots__ = ("side", "profile", "key", "features", "p", "notional", "maker_entry", "limit", "queue", "deadline",
                 "entry", "opened", "tp", "sl", "max_ms", "state", "exit", "reason", "fee_bps",
                 "maker_bps", "taker_bps", "flow_exit", "order_id", "market", "tp_id", "sl_id", "qty", "closed",
                 "explore", "lat", "active", "tp_active")

    def __init__(self, side, t, book, profile, cfg, maker, key=None, features=None, p=None, notional=0.0,
                 latency_ms=None):
        self.side, self.profile, self.features, self.p, self.notional = side, profile, features, p, notional
        self.key = key or model_key("elan", profile, maker)
        self.maker_bps, self.taker_bps = cfg["frais_maker_pct"] * 100, cfg["frais_pct"] * 100
        # Un short « rebond » est ouvert pendant une pression acheteuse : sortir dès que les achats
        # dominent le fermerait aussitôt. La sortie sur flux inversé ne vaut que pour « elan ».
        self.flow_exit = None if self.key.startswith("rebond/") else cfg["scalp_sortie_flux"]
        self.maker_entry = maker
        self.max_ms = profile["duree_s"] * 1000
        self.entry = self.opened = self.exit = self.reason = self.closed = None
        self.order_id = self.market = self.tp_id = self.sl_id = None
        self.qty = 0.0
        self.explore = False
        self.fee_bps = 0.0
        # Latence aller-retour : l'agent voit le marché en retard et son ordre met encore du temps à
        # atteindre l'exchange. Rien ne peut être exécuté avant `active`.
        self.lat = cfg["scalp_latence_ms"] if latency_ms is None else latency_ms
        self.active, self.tp_active = t + self.lat, None
        self.state = "envoi"
        if self.maker_entry:
            # Ordre limite au meilleur prix de notre côté : on rejoint la file d'attente existante.
            self.limit = book.ask if side == "short" else book.bid
            self.queue = book.ask_top if side == "short" else book.bid_top
            self.deadline = self.active + cfg["scalp_entree_delai_s"] * 1000
        else:
            self.limit, self.queue, self.deadline = None, 0.0, self.active
        if self.lat <= 0:
            self._activate(t, book)

    def _activate(self, t, book):
        """L'ordre arrive sur l'exchange : exécution au marché, ou mise en file de l'ordre limite."""
        short = self.side == "short"
        if self.maker_entry:
            # Post-only : rejeté si le prix a déjà traversé la limite (il serait exécuté au marché).
            crossed = (book.bid >= self.limit) if short else (book.ask <= self.limit)
            self.state = "annule" if crossed else "attente"
        else:
            # Au marché : au moins bon prix entre le carnet et la dernière transaction.
            last = book.last or (book.bid if short else book.ask)
            self._fill(t, min(book.bid, last) if short else max(book.ask, last), maker=False)

    def _fill(self, t, price, maker):
        self.entry, self.opened, self.state = price, t, "ouvert"
        self.tp_active = t + self.lat  # l'ordre d'objectif n'est déposé qu'après confirmation de l'entrée
        self.fee_bps = self.maker_bps if maker else self.taker_bps
        tp, sl = self.profile["objectif_bps"] / 1e4, self.profile["stop_bps"] / 1e4
        short = self.side == "short"
        self.tp = price * (1 - tp) if short else price * (1 + tp)
        self.sl = price * (1 + sl) if short else price * (1 - sl)

    def _close(self, t, price, maker, reason):
        self.exit, self.reason, self.closed, self.state = price, reason, t, "ferme"
        self.fee_bps += self.maker_bps if maker else self.taker_bps

    def gross_bps(self, price=None):
        price = self.exit if price is None else price
        d = (self.entry - price) if self.side == "short" else (price - self.entry)
        return d / self.entry * 1e4

    def net_bps(self):
        return self.gross_bps() - self.fee_bps

    def on_trade(self, t, price, qty, seller_aggressor, book):
        short = self.side == "short"
        if self.state == "envoi":
            if t < self.active:
                return
            self._activate(t, book)
            if self.state != "attente":
                return
        if self.state == "attente":
            if t > self.deadline:
                self.state = "annule"
            elif short and (price > self.limit or (price == self.limit and not seller_aggressor)):
                self.queue = 0.0 if price > self.limit else self.queue - qty
                if self.queue <= 0:
                    self._fill(t, self.limit, maker=True)
            elif not short and (price < self.limit or (price == self.limit and seller_aggressor)):
                self.queue = 0.0 if price < self.limit else self.queue - qty
                if self.queue <= 0:
                    self._fill(t, self.limit, maker=True)
        elif self.state == "ouvert":
            # L'objectif est un ordre limite au repos : exécuté si le prix le traverse.
            if t >= self.tp_active and ((short and price < self.tp) or (not short and price > self.tp)):
                self._close(t, self.tp, True, "objectif")
            elif self.flow_exit is not None and t - self.opened > 1000:
                f = book.flow()
                if (short and f >= self.flow_exit) or (not short and f <= -self.flow_exit):
                    self._close(t, book.ask if short else book.bid, False, "flux inversé")

    def on_book(self, t, book):
        short = self.side == "short"
        if self.state == "envoi":
            if t < self.active:
                return
            self._activate(t, book)
            return
        if self.state == "attente":
            if t > self.deadline:
                self.state = "annule"
        elif self.state == "ouvert" and book.bid:
            if short and book.ask >= self.sl:
                self._close(t, max(self.sl, book.ask), False, "stop")
            elif not short and book.bid <= self.sl:
                self._close(t, min(self.sl, book.bid), False, "stop")
            elif t - self.opened > self.max_ms:
                self._close(t, book.ask if short else book.bid, False, "durée max")


# --------------------------------------------------------------------------- #
# Agent de scalping
# --------------------------------------------------------------------------- #


class Scalper:
    def __init__(self, mode="papier", cfg=None, state_dir=HERE):
        setup_logging()
        self.state_dir = state_dir
        self.cfg = cfg or T.load_config()
        self.mode = mode
        self.trader = T.Trader(mode, self.cfg)
        self.broker = self.trader.broker
        self.state_path = os.path.join(state_dir, "portefeuille_scalp_{}.json".format(mode))
        self.journal_path = os.path.join(state_dir, "journal_scalp_{}.csv".format(mode))
        self.curve_path = os.path.join(state_dir, "courbe_scalp_{}.csv".format(mode))
        self.live_path = os.path.join(state_dir, "etat_scalp_{}.json".format(mode))
        self.state = self._load_state()
        modes = {"limite": [True], "marche": [False]}.get(self.cfg["scalp_entree"], [True, False])
        # « elan » : short pendant une chute ; « rebond » : short après une poussée trop rapide.
        self.families = list(self.cfg["scalp_familles"]) + (["elan_long"] if self.cfg["scalp_long"] else [])
        self.variants = [(f, p, m) for f in self.families for p in self.cfg["scalp_profils"] for m in modes]
        self.models = {model_key(f, p, m): OnlineLogit.from_dict(self.state["modeles"].get(model_key(f, p, m)))
                       for f, p, m in self.variants}
        self.books, self.coins, self.trend, self.funding = {}, {}, {}, {}
        self.shadows = collections.defaultdict(list)
        self.last_shadow = {}
        self.trades = {}   # trades réels en cours (attente ou ouvert), un par contrat
        self.latencies = collections.deque(maxlen=2000)
        self.msgs = self.reconnects = self.n_shadows = 0
        self.proc_us = 0.0                     # temps de traitement moyen d'un message
        self.sim_latency = self.cfg["scalp_latence_ms"]  # latence aller-retour simulée
        self.rate = (time.time(), 0, 0.0)      # (instant, messages cumulés, débit msg/s)
        self.msg_total = 0
        self.offset_ms = 0.0
        self.equity_cache = self.state["cash"]
        self.explore_times = collections.deque()  # instants des trades d'exploration (dernière heure)
        self.last_explore = {}                    # dernière exploration par crypto
        self.rng = random.Random()
        self._stop = threading.Event()

    # -- état ---------------------------------------------------------------------

    def _load_state(self):
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            if "modeles" in st:
                st.setdefault("remplissage", {})
                if st.get("version_modeles", 1) < 3:
                    # Avant la version 3, la simulation exécutait les ordres sans latence (gains irréalistes) :
                    # portefeuille, journal et modèles archivés, on repart d'un état propre.
                    for path in (self.state_path, self.journal_path, self.state_path.replace("portefeuille", "courbe")
                                 .replace(".json", ".csv")):
                        if os.path.exists(path):
                            os.replace(path, path + ".sans-latence")
                    return self._load_state()
                if st.get("version_modeles", 1) < 2:
                    # Modèles « rebond » entraînés avec une sortie sur flux inversé erronée : on repart de zéro.
                    for k in [k for k in st["modeles"] if k.startswith("rebond/")]:
                        del st["modeles"][k]
                    st["version_modeles"] = 2
                return st
            # ancien format, sans apprentissage : archivé
            os.replace(self.state_path, self.state_path + ".ancien")
            if os.path.exists(self.journal_path):
                os.replace(self.journal_path, self.journal_path + ".ancien")
        cap = self.cfg["capital_initial"]
        return {"cash": cap, "peak": cap, "day": None, "day_start": cap, "halted": False, "halted_day": None,
                "modeles": {}, "version_modeles": 3, "remplissage": {}, "cryptos": {}, "serie_pertes": 0,
                "stats": {"trades": 0, "gagnants": 0, "pnl": 0.0, "frais": 0.0, "volume": 0.0,
                          "ordres": 0, "executes": 0, "fantomes": 0, "debut": time.time(),
                          "exploration": 0, "exploration_pnl": 0.0}}

    def save(self):
        self.state["modeles"] = {k: m.to_dict() for k, m in self.models.items()}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)

    def journal(self, **row):
        new = not os.path.exists(self.journal_path)
        fields = ["date", "crypto", "sens", "profil", "proba", "entree", "sortie", "notionnel", "duree_s",
                  "brut_bps", "net_bps", "pnl", "raison"]
        with open(self.journal_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if new:
                w.writeheader()
            row["date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            w.writerow(row)

    # -- univers, tendance, financement ------------------------------------------

    def select_universe(self):
        """Tous les contrats perpétuels USDT assez échangés, des plus gros (BTC, ETH, XRP) aux plus petits."""
        cfg, md = self.cfg, self.trader.md
        volumes = md.volumes_24h()
        prices = {k: v["price"] for k, v in md.tickers().items()}
        rows = []
        for token, info in md.contracts().items():
            sym = token.lower()
            if sym in T.EXCLUDED_SYMBOLS or sym in cfg["exclure"] or sym.startswith("usd") or sym.endswith("usd"):
                continue  # stablecoins et jetons adossés : pas de mouvement propre à trader
            vol, price = volumes.get(info["contract"], 0.0), prices.get(info["contract"])
            if price and info.get("tick", 0) / price * 1e4 > cfg["scalp_pas_max_bps"]:
                continue  # pas de prix trop grossier pour viser quelques points de base
            if vol >= cfg["scalp_volume_24h_min_usd"]:
                rows.append((vol, token, info))
        rows.sort(key=lambda r: -r[0])
        if cfg["scalp_cryptos"]:
            rows = rows[:cfg["scalp_cryptos"]]
        self.coins = {}
        for vol, token, info in rows:
            coin = {"id": info["contract"], "symbol": token.lower(), "name": token, "base": info["base"],
                    "contract": info["contract"], "mult": info["mult"], "total_volume": vol}
            if self.broker.available(coin):
                self.coins[info["contract"]] = coin
        self.books = {c: self.books.get(c) or Book() for c in self.coins}

    def refresh_trend(self):
        try:
            self.trader.refresh()
            self.trend = {s["coin"]["contract"]: s["score"] for s in self.trader.scan()}
            self.funding = {c: v["funding"] for c, v in self.trader.tick.items()}
        except Exception as e:  # tendance indisponible : le scalping continue avec la dernière connue
            log.info(dim("Tendance non rafraîchie : {}".format(e)))

    def _trend_loop(self):
        while not self._stop.wait(TREND_REFRESH_S):
            self.refresh_trend()

    # -- variables du modèle -------------------------------------------------------

    def features(self, c, t, side):
        b = self.books[c]
        sign = 1 if side == "short" else -1
        mid = (b.bid + b.ask) / 2
        p5, p30 = b.price_ago(t, 5000) or b.last, b.price_ago(t, LONG_MS) or b.last
        depth = b.bid_qty + b.ask_qty
        sym = self.state["cryptos"].get(c, {})
        vol = b.buy + b.sell
        return [
            -sign * b.flow(),
            -sign * (b.last / p5 - 1) * 1e3,
            -sign * (b.last / p30 - 1) * 1e3,
            sign * ((b.ask_qty - b.bid_qty) / depth if depth else 0.0),
            (b.ask - b.bid) / mid * 1e4,
            b.volatility_bps(),
            math.log10(vol + 1),
            math.log10(len(b.trades) + 1),
            -sign * self.trend.get(c, 0.0) / 100,
            sign * self.funding.get(c, 0.0) * 1e4,
            sym.get("edge", 0.0) / 10,
        ]

    def expected(self, profile, p, maker_entry):
        """Gain espéré (pb) d'un trade exécuté : objectif net si gagné, stop + frais si perdu."""
        maker, taker = self.cfg["frais_maker_pct"] * 100, self.cfg["frais_pct"] * 100
        entry_fee = maker if maker_entry else taker
        win = profile["objectif_bps"] - entry_fee - maker
        loss = profile["stop_bps"] + entry_fee + taker
        return p * win - (1 - p) * loss, win, loss

    def ready(self, key=None):
        n = self.cfg["scalp_apprentissage_min"]
        if key:
            return self.models[key].seen >= n
        return any(m.seen >= n for m in self.models.values())

    def fill_rate(self, key):
        return self.state["remplissage"].get(key, 0.3)

    # -- flux --------------------------------------------------------------------------

    def on_trade(self, c, t, price, qty, seller_aggressor):
        b = self.books.get(c)
        if b is None:
            return
        b.add_trade(t, price, -price * qty if seller_aggressor else price * qty, self.cfg["scalp_fenetre_s"] * 1000)
        self._step_shadows(c, lambda s: s.on_trade(t, price, qty, seller_aggressor, b))
        real = self.trades.get(c)
        if real:
            self._step_real(c, real, t, lambda s: s.on_trade(t, price, qty, seller_aggressor, b))
        self.consider(c, t)

    def on_depth(self, c, t, bids, asks):
        b = self.books.get(c)
        if b is None:
            return
        b.set_depth(bids, asks)
        self._step_shadows(c, lambda s: s.on_book(t, b))
        real = self.trades.get(c)
        if real:
            self._step_real(c, real, t, lambda s: s.on_book(t, b))

    def _step_shadows(self, c, step):
        alive = []
        for s in self.shadows[c]:
            step(s)
            if s.state == "ferme":
                self.learn(c, s)
            elif s.state == "annule":
                self._fill_stat(s.key, 0.0)
            else:
                alive.append(s)
        self.n_shadows += len(alive) - len(self.shadows[c])
        self.shadows[c] = alive

    # -- apprentissage ---------------------------------------------------------------

    def _fill_stat(self, key, filled):
        rates = self.state["remplissage"]
        rates[key] = rates.get(key, 0.3) + 0.02 * (filled - rates.get(key, 0.3))

    def learn(self, c, sim):
        net = sim.net_bps()
        if sim.maker_entry:
            self._fill_stat(sim.key, 1.0)
        self.models[sim.key].update(sim.features, 1.0 if net > 0 else 0.0)
        sym = self.state["cryptos"].setdefault(c, {"edge": 0.0, "n": 0})
        sym["edge"] += 0.05 * (net - sym["edge"])
        sym["n"] += 1
        self.state["stats"]["fantomes"] += 1

    def paused(self, c, t):
        return t < self.state["cryptos"].get(c, {}).get("pause_jusqua", 0)

    # -- décisions --------------------------------------------------------------------

    def consider(self, c, t):
        cfg, b = self.cfg, self.books[c]
        if not b.bid or not b.ask or not b.last:
            return
        if (b.ask - b.bid) / ((b.bid + b.ask) / 2) * 1e4 > cfg["scalp_spread_max_bps"]:
            return
        if b.buy + b.sell < cfg["scalp_volume_min_usd"]:
            return
        p5 = b.price_ago(t, cfg["scalp_momentum_s"] * 1000)
        if not p5:
            return
        flow, mom, trend = b.flow(), (b.last / p5 - 1) * 1e4, self.trend.get(c, 0.0)
        seuil = cfg["scalp_seuil_desequilibre"]
        if cfg["autoriser_short"] and trend <= cfg["scalp_filtre_tendance"]:
            if "elan" in self.families and flow <= -seuil and mom <= -cfg["scalp_seuil_momentum_bps"]:
                self.evaluate(c, t, "elan", "short")
            if "rebond" in self.families and flow >= seuil and mom >= cfg["scalp_seuil_rebond_bps"]:
                self.evaluate(c, t, "rebond", "short")
        if "elan_long" in self.families and flow >= seuil and mom >= cfg["scalp_seuil_momentum_bps"] \
                and trend >= cfg["scalp_filtre_tendance"]:
            self.evaluate(c, t, "elan_long", "long")

    def evaluate(self, c, t, family, side):
        cfg, b = self.cfg, self.books[c]
        x = self.features(c, t, side)
        # Le stop doit laisser de la marge au-delà de l'écart achat/vente : sinon, vendre au meilleur
        # acheteur place déjà le prix de rachat contre le stop, et le trade est perdu dès l'entrée.
        spread = (b.ask - b.bid) / ((b.ask + b.bid) / 2) * 1e4
        variants = [v for v in self.variants
                    if v[0] == family and v[1]["stop_bps"] >= cfg["scalp_stop_min_ecarts"] * spread]
        if not variants:
            return

        # Trades fantômes : l'agent apprend de chaque signal, même ceux qu'il ne prend pas.
        sk = c + "/" + family
        if t - self.last_shadow.get(sk, 0) >= SHADOW_SPACING_MS and \
                self.n_shadows < cfg["scalp_fantomes_max"]:
            self.last_shadow[sk] = t
            for fam, prof, maker in variants:
                self.shadows[c].append(SimTrade(side, t, b, prof, cfg, maker, model_key(fam, prof, maker),
                                                features=x, latency_ms=self.sim_latency))
                self.n_shadows += 1

        if c in self.trades or not self.can_open() or self.paused(c, t):
            return
        best = None
        for fam, prof, maker in variants:
            key = model_key(fam, prof, maker)
            if not self.ready(key):
                continue
            p, _ = self.models[key].calibrated(x)  # probabilité corrigée par les résultats réels
            ev, win, loss = self.expected(prof, p, maker)
            score = ev * (self.fill_rate(key) if maker else 1.0)  # un ordre non exécuté ne rapporte rien
            if best is None or score > best[0]:
                best = (score, prof, maker, key, p, win, loss, ev)
        if best and best[0] >= cfg["scalp_ev_min_bps"]:
            _, prof, maker, key, p, win, loss, ev = best
            self.open(c, t, side, prof, maker, key, x, p, win, loss, ev)
        elif self.can_explore(t, c):
            # Exploration : petit trade réel sur une variante tirée au hasard, pour apprendre en tradant.
            fam, prof, maker = self.rng.choice(variants)
            key = model_key(fam, prof, maker)
            m = self.models[key]
            p = m.calibrated(x)[0] if m.seen >= 50 else m.base_rate
            ev, win, loss = self.expected(prof, p, maker)
            self.open(c, t, side, prof, maker, key, x, p, win, loss, ev, explore=True)

    def can_explore(self, t, c=None):
        cfg = self.cfg
        if not cfg["scalp_exploration"] or (self.mode != "papier" and not cfg["scalp_exploration_reel"]):
            return False
        budget = cfg["scalp_exploration_budget_jour_pct"] / 100 * cfg["capital_initial"]
        day = self.state.setdefault("exploration_jour", {"date": "", "pnl": 0.0})
        if day["date"] == str(date.today()) and budget and day["pnl"] <= -budget:
            return False  # budget de pertes du jour épuisé : l'agent continue d'apprendre par les fantômes
        if c and t - self.last_explore.get(c, -1e18) < cfg["scalp_exploration_espacement_s"] * 1000:
            return False  # pas deux explorations de suite sur la même crypto
        if not cfg["scalp_exploration_par_heure"]:
            return True   # 0 = pas de plafond horaire
        while self.explore_times and self.explore_times[0] < t - 3600000:
            self.explore_times.popleft()
        spacing = 3600000 / max(1, cfg["scalp_exploration_par_heure"])  # réparti sur l'heure, pas en rafale
        if self.explore_times and t - self.explore_times[-1] < spacing:
            return False
        return len(self.explore_times) < cfg["scalp_exploration_par_heure"]

    def can_open(self):
        st, cfg = self.state, self.cfg
        if st["halted"] or st["halted_day"] == str(date.today()):
            return False
        return not cfg["scalp_positions_max"] or len(self.trades) < cfg["scalp_positions_max"]

    def size_pct(self, p, win, loss):
        """Taille selon la confiance (Kelly fractionné), réduite après une série de pertes."""
        cfg = self.cfg
        kelly = p - (1 - p) * loss / win if win > 0 else 0.0
        frac = max(0.0, min(1.0, kelly / 0.3))
        pct = cfg["scalp_taille_min_pct"] + (cfg["scalp_taille_max_pct"] - cfg["scalp_taille_min_pct"]) * frac
        return pct * max(0.25, 0.75 ** self.state["serie_pertes"])

    def exposure(self):
        return sum(s.notional for s in self.trades.values())

    def open(self, c, t, side, prof, maker, key, x, p, win, loss, ev, explore=False):
        cfg = self.cfg
        eq = self.equity_cache
        pct = cfg["scalp_exploration_taille_pct"] if explore else self.size_pct(p, win, loss)
        notional = min(eq * pct / 100, eq * cfg["levier_max"] - self.exposure())
        if notional < 10:
            return
        sim = SimTrade(side, t, self.books[c], prof, cfg, maker, key, features=x, p=p, notional=notional,
                       latency_ms=self.sim_latency)
        sim.explore = explore
        self.state["stats"]["ordres"] += 1
        if self.mode != "papier":
            if not self._send_entry(c, sim):
                return
            if maker:
                sim.state = "attente"   # l'exchange gère la file ; suivi par _poll_live
            else:
                b = self.books[c]
                sim._fill(t, b.bid if side == "short" else b.ask, maker=False)
                self._place_brackets(sim)
        self.trades[c] = sim
        if explore:
            self.explore_times.append(t)
            self.last_explore[c] = t
        log.info("{}{} {} {} {} · {} · proba {:.0%} · espérance {:+.1f} pb · {:.0f}% du capital {}".format(
            cyan("[exploration] ") if explore else "", red("▼ SHORT") if side == "short" else green("▲ LONG"),
            self.coins[c]["base"],
            "limite" if sim.maker_entry else "marché",
            fmt_price(sim.limit or sim.entry or (self.books[c].bid if side == "short" else self.books[c].ask), "usd"),
            key, p, ev, pct, dim("(retard {:.0f} ms)".format(self.latencies[-1] if self.latencies else 0))))

    def _step_real(self, c, sim, t, step):
        before = sim.state
        if self.mode == "papier":
            step(sim)
        elif sim.state == "ouvert" and t - sim.opened > sim.max_ms:
            self._close_live(c, sim, t, "durée max")
        if sim.state == "ouvert" and before in ("envoi", "attente"):
            self.state["stats"]["executes"] += 1
            log.info(dim("  exécuté {} à {}".format(self.coins[c]["base"], fmt_price(sim.entry, "usd"))))
        elif sim.state == "annule":
            del self.trades[c]
        elif sim.state == "ferme":
            del self.trades[c]
            self.record(c, sim)

    def record(self, c, sim):
        st, s = self.state, self.state["stats"]
        net_bps = sim.net_bps()
        pnl = sim.notional * net_bps / 1e4
        st["cash"] += pnl
        s["trades"] += 1
        s["gagnants"] += 1 if pnl > 0 else 0
        s["pnl"] += pnl
        s["frais"] += sim.notional * sim.fee_bps / 1e4
        s["volume"] += sim.notional * 2
        self.learn(c, sim)
        sym = st["cryptos"].setdefault(c, {"edge": 0.0, "n": 0})
        sym["pause_jusqua"] = sim.closed + self.cfg["scalp_pause_s"] * 1000
        if sim.explore:
            # Les trades d'exploration sont pris sans espérance positive : ils ne doivent ni réduire la
            # taille des trades décidés par le modèle ni mettre une crypto en quarantaine.
            s["exploration"] = s.get("exploration", 0) + 1
            s["exploration_pnl"] = s.get("exploration_pnl", 0.0) + pnl
            day = st.setdefault("exploration_jour", {"date": "", "pnl": 0.0})
            if day["date"] != str(date.today()):
                day.update(date=str(date.today()), pnl=0.0)
            day["pnl"] += pnl
        else:
            st["serie_pertes"] = 0 if pnl > 0 else st["serie_pertes"] + 1
            sym["reel"] = (sym.get("reel") or [])[-(QUARANTINE_TRADES - 1):] + [net_bps]
        if not sim.explore and len(sym["reel"]) >= QUARANTINE_TRADES and \
                sum(sym["reel"]) / len(sym["reel"]) <= self.cfg["scalp_quarantaine_bps"]:
            sym["pause_jusqua"], sym["reel"] = sim.closed + QUARANTINE_MS, []
            log.info(yellow("  {} en quarantaine 1 h : ses {} derniers trades réels perdent en moyenne".format(
                self.coins[c]["base"], QUARANTINE_TRADES)))
        duration = (sim.closed - sim.opened) / 1000
        self.journal(crypto=self.coins[c]["base"], sens=sim.side,
                     profil=("explo:" if sim.explore else "") + sim.key, proba=round(sim.p, 3),
                     entree=sim.entry, sortie=sim.exit, notionnel=round(sim.notional, 2), duree_s=round(duration, 2),
                     brut_bps=round(sim.gross_bps(), 2), net_bps=round(net_bps, 2), pnl=round(pnl, 4),
                     raison=sim.reason)
        log.info("  ✕ {}{} {} {} en {:.1f} s → {} {}".format(
            "[exploration] " if sim.explore else "", sim.side.upper(), self.coins[c]["base"], sim.reason, duration,
            (green if pnl >= 0 else red)("{:+.2f} $".format(pnl)),
            dim("(brut {:+.1f} pb, net {:+.1f} pb)".format(sim.gross_bps(), net_bps))))
        self.save()

    # -- exchange réel (non testé sur un compte : à valider en testnet) -------------------

    def _send_entry(self, c, sim):
        ex, coin = self.broker.ex, self.coins[c]
        m = self.broker.market(coin)
        side = "sell" if sim.side == "short" else "buy"
        try:
            qty = float(ex.amount_to_precision(m, sim.notional / (sim.limit or sim.entry)))
            if sim.maker_entry:
                o = ex.create_order(m, "limit", side, qty, float(ex.price_to_precision(m, sim.limit)),
                                    {"postOnly": True})
            else:
                o = ex.create_order(m, "market", side, qty)
            sim.order_id, sim.market, sim.qty = o.get("id"), m, qty
            return True
        except Exception as e:
            log.info(red("  Ordre refusé sur {} : {}".format(c, e)))
            return False

    def _poll_live(self, now_ms):
        ex = self.broker.ex
        for c, sim in list(self.trades.items()):
            try:
                if sim.state == "attente":
                    o = ex.fetch_order(sim.order_id, sim.market)
                    if o.get("status") == "closed":
                        sim._fill(now_ms, float(o.get("average") or sim.limit), maker=True)
                        self._place_brackets(sim)
                        self.state["stats"]["executes"] += 1
                    elif now_ms > sim.deadline:
                        ex.cancel_order(sim.order_id, sim.market)
                        del self.trades[c]
            except Exception as e:
                log.info(yellow("  Suivi d'ordre {} : {}".format(c, e)))
        open_live = {k: v for k, v in self.trades.items() if v.state == "ouvert"}
        if not open_live:
            return
        positions = self.broker.positions()
        for c, sim in open_live.items():
            if sim.market in positions:
                continue
            hit_tp = False
            try:
                hit_tp = bool(sim.tp_id) and ex.fetch_order(sim.tp_id, sim.market).get("status") == "closed"
            except Exception:
                log.info(yellow("  Statut de l'objectif {} inconnu : compté comme stop.".format(c)))
            self._cancel_brackets(sim)
            sim._close(now_ms, sim.tp if hit_tp else sim.sl, hit_tp, "objectif" if hit_tp else "stop")
            del self.trades[c]
            self.record(c, sim)

    def _place_brackets(self, sim):
        ex, m = self.broker.ex, sim.market
        side = "buy" if sim.side == "short" else "sell"
        try:
            sim.tp_id = ex.create_order(m, "limit", side, sim.qty, float(ex.price_to_precision(m, sim.tp)),
                                        {"reduceOnly": True}).get("id")
        except Exception as e:
            log.info(yellow("  ⚠ Objectif non déposé sur {} : {}".format(m, e)))
        try:
            sim.sl_id = ex.create_order(m, "market", side, sim.qty, None,
                                        {"stopLossPrice": float(ex.price_to_precision(m, sim.sl)),
                                         "reduceOnly": True}).get("id")
        except Exception as e:
            log.info(yellow("  ⚠ Stop non déposé sur {} : {}".format(m, e)))

    def _cancel_brackets(self, sim):
        for oid in (sim.tp_id, sim.sl_id):
            if oid:
                try:
                    self.broker.ex.cancel_order(oid, sim.market)
                except Exception:
                    log.info(dim("  Annulation de l'ordre {} impossible (déjà exécuté ?)".format(oid)))

    def _close_live(self, c, sim, t, reason):
        self._cancel_brackets(sim)
        b = self.books[c]
        fill = self.broker.close({"side": sim.side, "qty": sim.qty, "market": sim.market,
                                  "symbol": self.coins[c]["base"]}, b.ask if sim.side == "short" else b.bid)
        sim._close(t, fill["price"], False, reason)

    # -- garde-fous et bilan -------------------------------------------------------

    def equity(self):
        if self.mode != "papier":
            return self.broker.equity(self.state, 0.0)
        open_pnl = 0.0
        for c, s in self.trades.items():
            b = self.books.get(c)
            if s.state == "ouvert" and b and b.bid:
                open_pnl += s.notional * s.gross_bps(b.ask if s.side == "short" else b.bid) / 1e4
        return self.state["cash"] + open_pnl

    def risk_guard(self, now_ms):
        st, cfg = self.state, self.cfg
        eq = self.equity()
        self.equity_cache = eq
        today = str(date.today())
        if st["day"] != today:
            st["day"], st["day_start"] = today, eq
        st["peak"] = max(st["peak"], eq)
        reason = None
        if cfg["drawdown_max_pct"] and not st["halted"] and eq <= st["peak"] * (1 - cfg["drawdown_max_pct"] / 100):
            st["halted"], reason = True, "drawdown max"
        elif cfg["perte_journaliere_max_pct"] and st["halted_day"] != today \
                and eq <= st["day_start"] * (1 - cfg["perte_journaliere_max_pct"] / 100):
            st["halted_day"], reason = today, "coupe-circuit journalier"
        if reason:
            log.info(bold(red("⛔ {} : positions fermées, plus d'ouverture.".format(reason))))
            for c, sim in list(self.trades.items()):
                b = self.books[c]
                if sim.state == "attente":
                    del self.trades[c]
                elif self.mode == "papier":
                    sim._close(now_ms, b.ask if sim.side == "short" else b.bid, False, reason)
                    del self.trades[c]
                    self.record(c, sim)
                else:
                    self._close_live(c, sim, now_ms, reason)
                    del self.trades[c]
                    self.record(c, sim)

    def snapshot(self):
        st, s = self.state, self.state["stats"]
        eq = self.equity() if self.mode == "papier" else self.equity_cache
        positions = []
        for c, sim in self.trades.items():
            b = self.books.get(c)
            row = {"crypto": self.coins[c]["base"], "variante": sim.key, "etat": sim.state,
                   "notionnel": round(sim.notional, 2)}
            if sim.state == "ouvert" and b and b.bid:
                now_price = b.ask if sim.side == "short" else b.bid
                row.update(entree=sim.entry, actuel=now_price,
                           pnl=round(sim.notional * (sim.gross_bps(now_price) - sim.fee_bps) / 1e4, 2))
            positions.append(row)
        hours = max((time.time() - s["debut"]) / 3600, 1 / 60)
        now = time.time()
        t0, n0, rate = self.rate
        if now - t0 >= 5:
            rate = (self.msg_total - n0) / (now - t0)
            self.rate = (now, self.msg_total, rate)
        lat = sorted(self.latencies)
        return {"maj": now, "mode": self.mode,
                "flux_msg_s": round(rate, 1), "retard_ms": round(lat[len(lat) // 2]) if lat else None,
                "reconnexions": self.reconnects, "traitement_us": round(self.proc_us, 1),
                "contrats": len(self.coins), "fantomes_actifs": self.n_shadows,
                "pertes_de_suite": st["serie_pertes"], "arret": st["halted"] or st["halted_day"] == str(date.today()), "capital_initial": self.cfg["capital_initial"],
                "valeur": round(eq, 2), "realise": round(s["pnl"], 2), "latent": round(eq - st["cash"], 2),
                "trades": s["trades"], "gagnants": s["gagnants"], "frais": round(s["frais"], 2),
                "trades_par_heure": round(s["trades"] / hours, 1), "debut": s["debut"],
                "apprentissage": self.ready(), "fantomes": s["fantomes"], "positions": positions,
                "exploration": s.get("exploration", 0), "exploration_pnl": round(s.get("exploration_pnl", 0.0), 2),
                "exploration_heure": len(self.explore_times)}

    def append_curve(self, snap):
        new = not os.path.exists(self.curve_path)
        with open(self.curve_path, "a", encoding="utf-8") as f:
            if new:
                f.write("t,valeur,realise,latent,trades\n")
            f.write("{:.0f},{},{},{},{}\n".format(snap["maj"], snap["valeur"], snap["realise"], snap["latent"],
                                                 snap["trades"]))

    def write_live(self):
        tmp = self.live_path + ".tmp"
        snap = self.snapshot()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        os.replace(tmp, self.live_path)
        return snap

    def _stdin_loop(self):
        """En terminal : Entrée (ou « s ») affiche le solde, « b » le bilan complet."""
        while not self._stop.is_set():
            try:
                cmd = input().strip().lower()
            except (EOFError, OSError):
                return
            if cmd in ("", "s", "solde"):
                print(format_balance(self.snapshot()))
            elif cmd in ("b", "bilan"):
                self.show()

    def status(self):
        lat = sorted(self.latencies)
        if lat:  # aller (données) + retour (ordre) ≈ 2 × le retard mesuré du flux
            self.sim_latency = max(self.cfg["scalp_latence_ms"], 2 * lat[len(lat) // 2])
        s = self.state["stats"]
        hours = max((time.time() - s["debut"]) / 3600, 1 / 60)
        lat = sorted(self.latencies)
        med = lat[len(lat) // 2] if lat else 0
        eq = self.equity_cache
        seen = max(m.seen for m in self.models.values())
        learning = "modèle prêt" if self.ready() else "apprentissage {}/{}".format(seen, self.cfg["scalp_apprentissage_min"])
        log.info(dim("── valeur {} ({:+.2f}%) · {} trades ({:.0f}/h) · gagnants {:.0f}% · net {:+.2f} $ · "
                     "ordres exécutés {}/{} · {} · {} fantômes appris · flux {:.0f} msg/s, retard {:.0f} ms"
                     .format(fmt_price(eq, "usd"), (eq / self.cfg["capital_initial"] - 1) * 100, s["trades"],
                             s["trades"] / hours, s["gagnants"] / s["trades"] * 100 if s["trades"] else 0, s["pnl"],
                             s["executes"], s["ordres"], learning, s["fantomes"], self.msgs / STATUS_S, med)))
        self.msgs = 0
        sys.stdout.flush()

    # -- boucle principale ---------------------------------------------------------

    def _reader(self, name, url, q):
        delay = 1
        while not self._stop.is_set():
            try:
                ws = WebSocket(url)
                delay = 1
                while not self._stop.is_set():
                    q.put(ws.recv())
            except Exception as e:  # coupure réseau ou serveur : reconnexion de ce flux seul
                if self._stop.is_set():
                    return
                self.reconnects += 1
                q.put(("info", "flux {} coupé ({}), reconnexion dans {} s".format(name, e, delay)))
                time.sleep(delay)
                delay = min(delay * 2, 30)

    def acquire_lock(self):
        self._lock = open(os.path.join(self.state_dir, ".scalper_{}.lock".format(self.mode)), "w")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise AlreadyRunning("un agent de scalping {} tourne déjà (service ou autre terminal)".format(
                self.mode)) from None

    def run(self):
        cfg = self.cfg
        self.acquire_lock()
        self.select_universe()
        self.refresh_trend()
        self.offset_ms = binance_clock_offset_ms()
        self.risk_guard(time.time() * 1000)
        threading.Thread(target=self._trend_loop, daemon=True).start()
        contracts = list(self.coins)
        log.info(bold(cyan("⚡ Scalping [{}] sur {} contrats : {}".format(
            self.mode.upper(), len(contracts), ", ".join(self.coins[c]["base"] for c in contracts)))))
        log.info(dim("Profils : {} · entrée {} · taille {}–{}% du capital selon la confiance · {}".format(
            ", ".join("{nom} ({objectif_bps}/{stop_bps} pb, {duree_s} s)".format(**p) for p in cfg["scalp_profils"]),
            {"limite": "par ordre limite", "marche": "au marché"}.get(cfg["scalp_entree"],
                                                                    "limite ou marché selon le gain espéré"),
            cfg["scalp_taille_min_pct"], cfg["scalp_taille_max_pct"],
            "modèle prêt" if self.ready() else "apprentissage en cours, aucun trade réel avant {} fantômes par profil"
            .format(cfg["scalp_apprentissage_min"]))))
        q = queue.Queue(maxsize=200000)
        for name, url in binance_stream_urls(contracts).items():
            threading.Thread(target=self._reader, args=(name, url, q), daemon=True).start()
        if sys.stdin.isatty():
            log.info(dim("Appuyez sur Entrée pour voir le solde en temps réel, « b » + Entrée pour le bilan."))
            threading.Thread(target=self._stdin_loop, daemon=True).start()
        last_status = last_poll = last_live = last_curve = time.time()
        try:
            while True:
                try:
                    item = q.get(timeout=60)
                except queue.Empty:
                    raise ConnectionError("aucune donnée reçue depuis 60 s") from None
                if isinstance(item, tuple):
                    log.info(yellow(item[1]))
                    continue
                started = time.perf_counter()
                d = json.loads(item).get("data") or {}
                c = d.get("s")
                self.msgs += 1
                self.msg_total += 1
                if d.get("e") == "aggTrade":
                    self.latencies.append(time.time() * 1000 + self.offset_ms - d["T"])
                    self.on_trade(c, d["T"], float(d["p"]), float(d["q"]), d["m"])
                elif d.get("e") == "depthUpdate":
                    self.on_depth(c, d["T"], d["b"], d["a"])
                self.proc_us += 0.001 * ((time.perf_counter() - started) * 1e6 - self.proc_us)
                now = time.time()
                if self.mode != "papier" and now - last_poll >= 1:
                    self._poll_live(now * 1000 + self.offset_ms)
                    last_poll = now
                if now - last_live >= LIVE_FILE_S:
                    snap = self.write_live()
                    last_live = now
                    if now - last_curve >= CURVE_S:
                        self.append_curve(snap)
                        last_curve = now
                if now - last_status >= STATUS_S:
                    self.risk_guard(now * 1000 + self.offset_ms)
                    self.status()
                    self.save()
                    last_status = now
        finally:
            self._stop.set()
            self.save()

    # -- bilan ---------------------------------------------------------------------

    def show(self):
        st, s = self.state, self.state["stats"]
        print(bold("\n  Scalping [{}]".format(self.mode.upper())))
        print("  Valeur : {} · trades {} · gagnants {:.0f}% · net {:+.2f} $ · frais {:.2f} $ · volume {}".format(
            fmt_price(st["cash"], "usd"), s["trades"], s["gagnants"] / s["trades"] * 100 if s["trades"] else 0,
            s["pnl"], s["frais"], T.fmt_big(s["volume"], "usd")))
        print("  Ordres limite exécutés : {}/{} · trades fantômes appris : {}".format(
            s["executes"], s["ordres"], s["fantomes"]))
        print(bold("\n  Ce que l'agent a appris"))
        for key, m in self.models.items():
            if m.seen < 50:
                continue
            good, bad = m.explain()
            fill = " · ordres exécutés {:.0%}".format(self.fill_rate(key)) if key.endswith("limite") else ""
            said, real = m.calibration()
            cal = " · annoncé {:.0%} → réel {:.0%}".format(said, real) if said is not None else ""
            print("  {} ({} exemples, {:.0%} gagnants{}{}) :".format(key, m.seen, m.base_rate, fill, cal))
            if good:
                print("    favorable : " + ", ".join("{} ({:+.2f})".format(n, w) for n, w in good))
            if bad:
                print("    piège     : " + ", ".join("{} ({:+.2f})".format(n, w) for n, w in bad))
        syms = sorted(((c, v) for c, v in st["cryptos"].items() if v["n"] >= 10), key=lambda kv: kv[1]["edge"])
        if syms:
            fmt = lambda kv: "{} {:+.1f} pb".format(kv[0].replace("USDT", ""), kv[1]["edge"])  # noqa: E731
            print("  Cryptos les plus piégeuses : " + ", ".join(fmt(kv) for kv in syms[:4]))
            print("  Cryptos les plus favorables : " + ", ".join(fmt(kv) for kv in reversed(syms[-4:])))
        now = time.time() * 1000
        q = [c.replace("USDT", "") for c, v in st["cryptos"].items() if v.get("pause_jusqua", 0) - now > 60000]
        if q:
            print(yellow("  En quarantaine : " + ", ".join(q)))
        if os.path.exists(self.journal_path):
            with open(self.journal_path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))[-10:]
            if rows:
                print(bold("\n  Derniers trades"))
            for r in rows:
                v = float(r["pnl"])
                print("  {}  {:<8} {:<14} {:>6}s  proba {:>5}  net {:>6} pb  {}  {}".format(
                    dim(r["date"]), r["crypto"], r.get("profil", ""), r["duree_s"], r.get("proba", ""), r.get("net_bps", ""),
                    (green if v >= 0 else red)("{:+.2f}$".format(v)), dim(r["raison"])))


def format_balance(snap):
    cap = snap["capital_initial"]
    total = snap["valeur"] - cap
    rate = snap["gagnants"] / snap["trades"] * 100 if snap["trades"] else 0
    lines = [
        bold("  Solde [{}] · {}".format(snap["mode"].upper(), datetime.fromtimestamp(snap["maj"]).strftime("%H:%M:%S"))),
        "  Valeur actuelle : {}  ({} depuis le lancement, départ {})".format(
            bold(fmt_price(snap["valeur"], "usd")),
            (green if total >= 0 else red)("{:+,.2f} $ / {:+.2f}%".format(total, total / cap * 100).replace(",", " ")),
            fmt_price(cap, "usd")),
        "  Réalisé {:+.2f} $ · en cours {:+.2f} $ · frais payés {:.2f} $".format(
            snap["realise"], snap["latent"], snap["frais"]),
        "  {} trades ({}/h) · gagnants {:.0f}% · {} exemples appris{}".format(
            snap["trades"], snap["trades_par_heure"], rate, snap["fantomes"],
            "" if snap["apprentissage"] else " · apprentissage en cours"),
        "  dont exploration : {} trades, {:+.2f} $".format(snap.get("exploration", 0), snap.get("exploration_pnl", 0.0)),
    ]
    for p in snap["positions"]:
        if p.get("pnl") is not None:
            lines.append("    {} {} {} → {} : {}".format(p["crypto"], p["variante"], fmt_price(p["entree"], "usd"),
                                                      fmt_price(p["actuel"], "usd"),
                                                      (green if p["pnl"] >= 0 else red)("{:+.2f} $".format(p["pnl"]))))
        else:
            lines.append("    {} {} : ordre limite en attente".format(p["crypto"], p["variante"]))
    return "\n".join(lines)


def show_balance(path, follow=False):
    while True:
        try:
            with open(path, encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError):
            print("Aucun solde disponible : l'agent n'a pas encore tourné avec cette version.")
            return
        out = format_balance(snap)
        age = time.time() - snap["maj"]
        if age > 15:
            out += "\n" + yellow("  ⚠ Agent arrêté : dernière mise à jour il y a {:.0f} min.".format(age / 60))
        if follow:
            sys.stdout.write("\033[H\033[J" + out + "\n" + dim("  Ctrl+C pour quitter") + "\n")
            sys.stdout.flush()
            try:
                time.sleep(1)
            except KeyboardInterrupt:
                return
        else:
            print(out)
            return


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Scalping temps réel qui apprend de ses erreurs.")
    parser.add_argument("--mode", choices=["papier", "testnet", "reel"], default="papier")
    parser.add_argument("--je-confirme-le-risque", action="store_true")
    parser.add_argument("cmd", nargs="?", default="lancer", choices=["lancer", "solde", "bilan", "reset"])
    parser.add_argument("--suivre", action="store_true", help="solde rafraîchi chaque seconde")
    args = parser.parse_args()
    if args.cmd == "solde":
        show_balance(os.path.join(HERE, "etat_scalp_{}.json".format(args.mode)), args.suivre)
        return
    T.load_env()
    if args.mode == "reel" and args.cmd == "lancer":
        T.confirm_real(args.je_confirme_le_risque)
    sc = Scalper(args.mode)
    if args.cmd == "bilan":
        sc.show()
    elif args.cmd == "reset":
        try:
            sc.acquire_lock()
        except AlreadyRunning as e:
            print("Remise à zéro impossible : {}.".format(e))
            return
        for p in (sc.state_path, sc.journal_path, sc.live_path, sc.curve_path):
            if os.path.exists(p):
                os.remove(p)
        print("Portefeuille et apprentissage du scalping remis à zéro.")
    else:
        try:
            sc.run()
        except AlreadyRunning as e:
            print(red("Impossible de lancer : {}. Voir le solde : python3 scalper.py solde".format(e)))
            sys.exit(1)
        except KeyboardInterrupt:
            print("\n  Scalping arrêté.")
            sc.show()


if __name__ == "__main__":
    main()
