#!/usr/bin/env python3
"""
Agent de trading crypto autonome — tendances, long / short, gestion du risque,
auto-optimisation et fonctionnement en service.

Données : CoinGecko / Coinpaprika / Coinlore (classement), Binance / Bybit Futures
(bougies, prix, financement), DefiLlama / Kraken (contrôle croisé). Voir sources.py.

Modes d'exécution :
  papier   (défaut) simulation complète avec un capital fictif, aucun ordre envoyé
  testnet  ordres sur le réseau de test d'un exchange (via ccxt), argent fictif
  reel     ordres réels avec de l'argent réel (via ccxt), confirmation exigée

⚠️  Aucune stratégie ne garantit de gain. Le short et le levier peuvent faire perdre
    plus que la mise. Testez longtemps en mode papier avant tout passage en réel.
"""

import argparse
import csv
import getpass
import json
import math
import os
import signal
import sys
import time
from datetime import date, datetime

from crypto_agent import (CoinGecko, CoinGeckoError, bold, cyan, dim, ensemble_forecast,
                          fmt_big, fmt_pct, fmt_price, green, linear_regression, mean, pad,
                          red, rsi, stdev, yellow)
from sources import INTERVAL_MINUTES, DataError, MarketData, reference_prices, top_coins

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config_trading.json")
ENV_PATH = os.path.join(HERE, ".env")
LOG_DIR = os.path.join(HERE, "logs")
CUR = "usd"  # les contrats perpétuels sont cotés en USDT

PROFILES = {
    "prudent": {
        "risque_par_trade_pct": 1.0, "levier_max": 2.0, "positions_max": 5,
        "score_entree_long": 45, "score_entree_short": 45, "score_sortie": 15,
        "multiplicateur_stop": 2.0, "ratio_gain_risque": 2.0,
        "perte_journaliere_max_pct": 5.0, "drawdown_max_pct": 20.0,
        "bougies": "1h", "intervalle_auto_s": 900,
    },
    # Recherche du rendement : plus de risque par trade, pas de limite de nombre de
    # positions (seul le levier borne l'exposition), entrée short plus facile que long.
    "agressif": {
        "risque_par_trade_pct": 3.0, "levier_max": 5.0, "positions_max": 0,
        "score_entree_long": 60, "score_entree_short": 30, "score_sortie": 15,
        "multiplicateur_stop": 2.0, "ratio_gain_risque": 2.5,
        "perte_journaliere_max_pct": 15.0, "drawdown_max_pct": 50.0,
        "bougies": "15m", "intervalle_auto_s": 120,
    },
}

DEFAULT_CONFIG = {
    "strategie": "scalping",           # « scalping » (secondes, flux temps réel) ou « tendance » (minutes)
    "profil": "agressif",
    "capital_initial": 10000,          # capital fictif du mode papier ($)
    "stop_min_pct": 1.0,
    "stop_max_pct": 12.0,
    "frais_pct": 0.05,
    "glissement_pct": 0.05,
    "autoriser_short": True,
    "bonus_financement": True,         # favorise le côté qui encaisse le financement
    "ecart_prix_max_pct": 1.5,         # refuse d'ouvrir si les sources de prix divergent
    "volume_min_usd": 20000000,
    "reoptimisation_h": 24,            # ré-optimisation automatique (0 = jamais)
    "optim_cryptos": 8,
    "exclure": [],
    "exchange": "binanceusdm",
    # -- scalping (scalper.py) --
    "frais_maker_pct": 0.02,           # frais des ordres limite (frais_pct = ordres au marché)
    "scalp_cryptos": 200,              # contrats suivis au plus (les plus échangés d'abord, 0 = tous)
    "scalp_volume_24h_min_usd": 10000000,  # volume quotidien minimal d'un contrat pour être suivi
    "scalp_familles": ["elan", "rebond"],  # short pendant une chute / short après une poussée trop rapide
    "scalp_entree": "auto",            # « auto » (le modèle choisit), « limite » (maker) ou « marche »
    "scalp_entree_delai_s": 3,
    "scalp_latence_ms": 300,           # latence aller-retour simulée minimale (2 × le retard mesuré si plus grand)         # ordre limite annulé s'il n'est pas exécuté à temps
    "scalp_profils": [                 # chaque signal est évalué sur ces profils, le meilleur est retenu
        {"nom": "precis", "objectif_bps": 8, "stop_bps": 20, "duree_s": 60},
        {"nom": "eclair", "objectif_bps": 10, "stop_bps": 8, "duree_s": 20},
        {"nom": "rapide", "objectif_bps": 20, "stop_bps": 15, "duree_s": 60},
        {"nom": "large", "objectif_bps": 35, "stop_bps": 25, "duree_s": 180},
    ],
    "scalp_fenetre_s": 10,             # fenêtre de mesure du flux d'ordres
    "scalp_momentum_s": 5,
    "scalp_seuil_desequilibre": 0.3,   # pré-filtre large : c'est le modèle qui décide ensuite
    "scalp_seuil_momentum_bps": 2,
    "scalp_seuil_rebond_bps": 4,       # hausse minimale sur 5 s pour un short « rebond »
    "scalp_volume_min_usd": 20000,
    "scalp_spread_max_bps": 5,
    "scalp_stop_min_ecarts": 3,        # stop d'au moins 3 fois l'écart achat/vente du moment
    "scalp_pas_max_bps": 3,            # contrat exclu si son pas de prix dépasse 3 pb
    "scalp_sortie_flux": 0.8,          # sortie anticipée si les achats dominent nettement
    "scalp_pause_s": 0,                # 0 = peut reshorter immédiatement après une sortie
    "scalp_taille_min_pct": 5,         # taille selon la confiance du modèle, en % du capital
    "scalp_taille_max_pct": 60,
    "scalp_ev_min_bps": 1.0,           # gain espéré minimal après frais pour un trade réel
    "scalp_validation_min": 50,        # trades fantômes « choisis » requis pour valider une variante
    "scalp_validation_min_bps": 1.0,   # leur résultat net moyen doit dépasser ce seuil
    "scalp_apprentissage_min": 200,    # trades fantômes par profil avant le premier trade réel
    "scalp_quarantaine_bps": -10,      # pause 1 h si les 5 derniers trades réels perdent plus en moyenne
    "scalp_fantomes_max": 10000,
    "scalp_positions_max": 0,          # 0 = pas de limite : seul le levier borne l'exposition
    "scalp_long": False,               # short uniquement
    "scalp_exploration": True,         # trades réels de petite taille pendant et après l'apprentissage
    "scalp_exploration_taille_pct": 2,  # taille d'un trade d'exploration, en % du capital
    "scalp_exploration_par_heure": 0,  # 0 = pas de plafond horaire
    "scalp_exploration_espacement_s": 10,  # délai minimal entre deux explorations sur une même crypto
    "scalp_exploration_reel": False,   # exploration interdite en mode réel sauf activation explicite
    "scalp_exploration_budget_jour_pct": 1,  # pertes d'exploration tolérées par jour, en % du capital (0 = sans limite)
    # -- arbitrage du taux de financement (funding.py) --
    "funding_capital": 10000,          # capital fictif dédié ($)
    "funding_positions_max": 5,        # réglages issus du balayage sur 11 mois de taux réels
    "funding_levier": 2,               # levier de la jambe perpétuelle (marge = notionnel / levier)
    "funding_entree_annuel_pct": 9,   # taux annualisé minimal pour entrer (moyenne récente et taux en cours)
    "funding_sortie_annuel_pct": 0,    # sortie quand la moyenne récente tombe sous ce seuil
    "funding_fenetre_h": 168,          # fenêtre de la moyenne des taux
    "funding_horizon_j": 30,           # durée de détention envisagée pour couvrir les frais
    "funding_marge_frais": 1.0,        # gain attendu ≥ les frais aller-retour
    "funding_ecart_max_pct": 0.3,      # écart perpétuel/comptant maximal à l'entrée
    "funding_volume_min_usd": 20000000,
    "frais_comptant_pct": 0.1,         # frais d'un ordre au comptant (Binance, sans réduction BNB)
    "scalp_filtre_tendance": 20,       # pas de short si le score 15 min dépasse ce seuil
}
DEFAULT_CONFIG.update(PROFILES["agressif"])

# Paramètres de l'analyse selon la taille des bougies : (courte, longue, momentum, horizon)
BAR_PARAMS = {"5m": (36, 144, 36, 36), "15m": (24, 96, 16, 16), "30m": (16, 72, 12, 12),
              "1h": (24, 72, 24, 24), "4h": (12, 42, 6, 6)}
CANDLES_NEEDED = 250

OPTIM_GRID = {
    "score_entree_long": [45, 60, 75, 101],   # 101 = jamais de long
    "score_entree_short": [20, 30, 45, 60],
    "multiplicateur_stop": [1.0, 1.5, 2.0, 3.0],
    "ratio_gain_risque": [1.5, 2.5, 4.0],
    "score_sortie": [10, 30],
}

# Stablecoins, or tokenisé et jetons « enveloppés » : pas de tendance propre à trader.
EXCLUDED_SYMBOLS = {
    "usdt", "usdc", "dai", "fdusd", "tusd", "usde", "susde", "usds", "pyusd", "usd1", "usdd",
    "bsc-usd", "busd", "frax", "rlusd", "usdtb", "usdf", "usdx", "eurc", "xaut", "paxg",
    "wbtc", "weth", "steth", "wsteth", "weeth", "cbbtc", "reth", "wbeth", "cbeth", "lbtc",
    "solvbtc", "meth", "ezeth", "rseth", "jitosol", "msol", "bnsol", "jupsol", "stkaave",
    "wbnb", "bbtc", "clbtc", "tbtc", "oseth", "lseth",
}
EXCLUDED_NAME_HINTS = ("wrapped", "staked", "bridged", "restaked", "liquid staking", "usd", "gold")


class TradingError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Analyse de tendance
# --------------------------------------------------------------------------- #


def bar_params(interval):
    fast, slow, mom, horizon = BAR_PARAMS[interval]
    return {"fast": fast, "slow": slow, "mom": mom, "horizon": horizon,
            "steps_per_day": 1440 // INTERVAL_MINUTES[interval]}


def analyse(prices, fast=24, slow=72, mom=24, horizon=24, steps_per_day=24):
    """Score de tendance de -100 (forte baisse) à +100 (forte hausse).

    Combine, normalisés par la volatilité :
      30 % croisement moyenne courte / longue
      30 % pente de la régression linéaire
      20 % momentum
      20 % prévision de l'ensemble de modèles
    Le RSI sert de filtre : un mouvement déjà en surachat / survente est pénalisé.
    """
    if not prices or len(prices) < slow + 2 or min(prices) <= 0:
        return None
    logs = [math.log(p) for p in prices]
    rets = [b - a for a, b in zip(logs[:-1], logs[1:])]
    sigma = stdev(rets[-slow:]) or 1e-9

    cross = (mean(prices[-fast:]) / mean(prices[-slow:]) - 1) / (sigma * math.sqrt(fast))
    _, b = linear_regression(logs[-slow:])
    slope = b * slow / (sigma * math.sqrt(slow))
    momentum = (logs[-1] - logs[-1 - mom]) / (sigma * math.sqrt(mom))
    fc, _, _ = ensemble_forecast(prices, horizon)
    forecast = math.log(fc[-1]["mid"] / prices[-1]) / (sigma * math.sqrt(horizon))

    def squash(x):
        return math.tanh(x / 1.5)

    score = 100 * (0.3 * squash(cross) + 0.3 * squash(slope)
                   + 0.2 * squash(momentum) + 0.2 * squash(forecast))
    r = rsi(prices, 14)
    if r is not None and ((score > 0 and r > 75) or (score < 0 and r < 25)):
        score *= 0.5
    return {
        "score": score,
        "rsi": r,
        "sigma_jour": sigma * math.sqrt(steps_per_day),
        "prevision": fc[-1]["mid"],
    }


def funding_adjust(score, funding, cfg):
    """Financement positif = les longs paient les shorts : on penche vers le short (±10 pts)."""
    if not cfg["bonus_financement"] or not funding:
        return score
    adj = max(-10.0, min(10.0, -funding / 0.0005 * 10))
    return max(-100.0, min(100.0, score + adj))


def signal_of(score, cfg):
    if score is None:
        return None
    if cfg["autoriser_short"] and score <= -cfg["score_entree_short"]:
        return "short"
    if score >= cfg["score_entree_long"]:
        return "long"
    return None


def stop_pct_from(sigma_jour, cfg):
    raw = cfg["multiplicateur_stop"] * sigma_jour * 100 if sigma_jour else 5.0
    return min(max(raw, cfg["stop_min_pct"]), cfg["stop_max_pct"])


def unrealized(pos, price):
    diff = price - pos["entry"] if pos["side"] == "long" else pos["entry"] - price
    return pos["qty"] * diff


def new_position(side, entry, qty, stop_pct, cfg):
    d, rr = stop_pct / 100, cfg["ratio_gain_risque"]
    return {
        "side": side, "qty": qty, "entry": entry, "best": entry, "stop_pct": stop_pct,
        "stop": entry * (1 - d) if side == "long" else entry * (1 + d),
        "tp": entry * (1 + d * rr) if side == "long" else entry * (1 - d * rr),
    }


def check_exit(pos, price, score, cfg):
    """Met à jour le stop suiveur et renvoie la raison de sortie, ou None."""
    long = pos["side"] == "long"
    pos["best"] = max(pos["best"], price) if long else min(pos["best"], price)
    dist = pos["stop_pct"] / 100
    gain = (price - pos["entry"]) if long else (pos["entry"] - price)

    # À partir de +1R : stop au point mort, puis stop suiveur à la même distance.
    if gain >= pos["entry"] * dist:
        if long:
            pos["stop"] = max(pos["stop"], pos["best"] * (1 - dist), pos["entry"])
        else:
            pos["stop"] = min(pos["stop"], pos["best"] * (1 + dist), pos["entry"])

    if (long and price <= pos["stop"]) or (not long and price >= pos["stop"]):
        return "stop"
    if (long and price >= pos["tp"]) or (not long and price <= pos["tp"]):
        return "objectif"
    if score is not None and ((long and score <= -cfg["score_sortie"])
                              or (not long and score >= cfg["score_sortie"])):
        return "retournement"
    return None


def tradable(coin, cfg):
    sym, name = coin["symbol"].lower(), coin["name"].lower()
    if sym in EXCLUDED_SYMBOLS or sym in cfg["exclure"] or coin["id"] in cfg["exclure"]:
        return False
    if any(h in name for h in EXCLUDED_NAME_HINTS):
        return False
    return (coin.get("total_volume") or 0) >= cfg["volume_min_usd"]


def score_bar(score, width=10):
    n = int(round(abs(score) / 100 * width))
    bar = ("█" * n).ljust(width, "·")
    return green(bar) if score > 0 else red(bar)


# --------------------------------------------------------------------------- #
# Simulation (backtest et optimisation)
# --------------------------------------------------------------------------- #


def precompute(candles, interval):
    """Analyse de chaque bougie une seule fois : (score, volatilité quotidienne) ou None."""
    bp = bar_params(interval)
    closes = [k["c"] for k in candles]
    out = [None] * len(closes)
    for i in range(max(bp["slow"] + 2, 100), len(closes)):
        a = analyse(closes[max(0, i - 199):i + 1], **bp)
        if a:
            out[i] = (a["score"], a["sigma_jour"])
    return out


def simulate(candles, pre, p, start=0, end=None):
    """Rejoue la stratégie bougie par bougie (stops et objectifs testés sur haut / bas)."""
    end = end or len(candles)
    cost = (p["frais_pct"] + p["glissement_pct"]) / 100
    eq, peak, mdd, pos = 1.0, 1.0, 0.0, None
    trades = wins = 0
    for i in range(start, end):
        k, a = candles[i], pre[i]
        score = a[0] if a else None
        if pos:
            long = pos["side"] == "long"
            exit_price = None
            if long and k["l"] <= pos["stop"]:
                exit_price = min(pos["stop"], k["o"])
            elif not long and k["h"] >= pos["stop"]:
                exit_price = max(pos["stop"], k["o"])
            elif long and k["h"] >= pos["tp"]:
                exit_price = max(pos["tp"], k["o"])
            elif not long and k["l"] <= pos["tp"]:
                exit_price = min(pos["tp"], k["o"])
            elif check_exit(pos, k["c"], score, p):
                exit_price = k["c"]
            if exit_price is not None:
                pnl = unrealized(pos, exit_price) - pos["qty"] * exit_price * cost
                eq += pnl
                trades += 1
                wins += 1 if pnl - pos["cost"] > 0 else 0
                pos = None
        if pos is None and a:
            side = signal_of(score, p)
            if side:
                sp = stop_pct_from(a[1], p)
                notional = min(eq * p["risque_par_trade_pct"] / sp, eq * p["levier_max"])
                pos = new_position(side, k["c"], notional / k["c"], sp, p)
                pos["cost"] = notional * cost
                eq -= pos["cost"]
        mark = eq + (unrealized(pos, k["c"]) if pos else 0)
        if mark <= 0:
            return {"ret": -100.0, "dd": 100.0, "trades": trades + 1, "wins": wins}
        peak = max(peak, mark)
        mdd = max(mdd, (1 - mark / peak) * 100)
    if pos:
        last = candles[end - 1]["c"]
        eq += unrealized(pos, last) - pos["qty"] * last * cost
        trades += 1
    return {"ret": (eq - 1) * 100, "dd": mdd, "trades": trades, "wins": wins}


def evaluate(datasets, p, part):
    """Moyenne sur plusieurs cryptos. part = 'train' (70 % ancien) ou 'test' (30 % récent)."""
    res = []
    for candles, pre in datasets:
        cut = int(len(candles) * 0.7)
        res.append(simulate(candles, pre, p, 0, cut) if part == "train" else simulate(candles, pre, p, cut))
    ret, dd = mean([r["ret"] for r in res]), mean([r["dd"] for r in res])
    trades = sum(r["trades"] for r in res)
    wins = sum(r["wins"] for r in res)
    return {"ret": ret, "dd": dd, "trades": trades, "win": wins / trades * 100 if trades else 0,
            "objectif": ret - 0.5 * dd}


# --------------------------------------------------------------------------- #
# Courtiers
# --------------------------------------------------------------------------- #


class PaperBroker:
    """Exécution simulée au prix mark de l'exchange, avec frais et glissement."""

    name = "papier"

    def __init__(self, cfg):
        self.cfg = cfg

    def available(self, coin):
        return True

    def _slip(self, price, buying):
        s = self.cfg["glissement_pct"] / 100
        return price * (1 + s) if buying else price * (1 - s)

    def open(self, coin, side, notional, price, leverage):
        fill = self._slip(price, side == "long")
        return {"qty": notional / fill, "price": fill, "fee": notional * self.cfg["frais_pct"] / 100}

    def close(self, pos, price):
        fill = self._slip(price, pos["side"] == "short")
        return {"price": fill, "fee": pos["qty"] * fill * self.cfg["frais_pct"] / 100}

    def protect(self, pos):
        pass

    def positions(self):
        return None  # rien à synchroniser

    def equity(self, state, open_pnl):
        return state["cash"] + open_pnl


class CcxtBroker:
    """Exécution réelle sur un exchange de contrats perpétuels via la bibliothèque ccxt.

    Clés lues dans EXCHANGE_API_KEY / EXCHANGE_API_SECRET (fichier .env ou environnement).
    Un stop-loss est aussi déposé côté exchange (reduceOnly), pour rester protégé
    même si l'agent s'arrête.
    """

    def __init__(self, cfg, testnet):
        try:
            import ccxt
        except ImportError as e:
            raise TradingError("Le mode {} nécessite ccxt : pip3 install ccxt".format(
                "testnet" if testnet else "réel")) from e
        key, secret = os.environ.get("EXCHANGE_API_KEY"), os.environ.get("EXCHANGE_API_SECRET")
        if not key or not secret:
            raise TradingError("Clés API absentes : lancez « python3 trader.py cles ».")
        if not hasattr(ccxt, cfg["exchange"]):
            raise TradingError("Exchange inconnu de ccxt : {}".format(cfg["exchange"]))
        self.cfg = cfg
        self.name = "testnet" if testnet else "reel"
        self.ex = getattr(ccxt, cfg["exchange"])({
            "apiKey": key, "secret": secret, "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        if testnet:
            try:
                self.ex.set_sandbox_mode(True)
            except Exception as e:
                raise TradingError("Testnet indisponible pour {} : {}".format(cfg["exchange"], e)) from e
        self.ex.load_markets()

    def market(self, coin):
        m = "{}/USDT:USDT".format(coin["base"])
        return m if m in self.ex.markets else None

    def available(self, coin):
        return self.market(coin) is not None

    def open(self, coin, side, notional, price, leverage):
        m = self.market(coin)
        try:
            self.ex.set_leverage(max(1, int(math.ceil(leverage))), m)
        except Exception:
            pass  # certains exchanges gèrent le levier autrement
        qty = float(self.ex.amount_to_precision(m, notional / price))
        min_qty = ((self.ex.markets[m].get("limits") or {}).get("amount") or {}).get("min") or 0
        if qty <= 0 or qty < min_qty:
            raise TradingError("Montant trop faible pour {} (minimum {}).".format(m, min_qty))
        order = self.ex.create_order(m, "market", "buy" if side == "long" else "sell", qty)
        fill = float(order.get("average") or order.get("price") or price)
        fee = (order.get("fee") or {}).get("cost")
        return {"qty": qty, "price": fill, "market": m,
                "fee": float(fee) if fee else qty * fill * self.cfg["frais_pct"] / 100}

    def close(self, pos, price):
        self._cancel_stop(pos)
        order = self.ex.create_order(pos["market"], "market", "sell" if pos["side"] == "long" else "buy",
                                     pos["qty"], None, {"reduceOnly": True})
        fill = float(order.get("average") or order.get("price") or price)
        fee = (order.get("fee") or {}).get("cost")
        return {"price": fill, "fee": float(fee) if fee else pos["qty"] * fill * self.cfg["frais_pct"] / 100}

    def protect(self, pos):
        """(Re)place le stop côté exchange quand il a bougé de plus de 0,2 %."""
        old = pos.get("stop_order_price")
        if old and abs(pos["stop"] / old - 1) < 0.002:
            return
        self._cancel_stop(pos)
        side = "sell" if pos["side"] == "long" else "buy"
        try:
            stop = float(self.ex.price_to_precision(pos["market"], pos["stop"]))
            order = self.ex.create_order(pos["market"], "market", side, pos["qty"], None,
                                         {"stopLossPrice": stop, "reduceOnly": True})
            pos["stop_order_id"], pos["stop_order_price"] = order.get("id"), pos["stop"]
        except Exception as e:
            print(yellow("  ⚠ Stop non déposé sur l'exchange pour {} ({}). "
                         "Il ne sera appliqué que pendant que l'agent tourne.".format(pos["symbol"], e)))

    def _cancel_stop(self, pos):
        if pos.get("stop_order_id"):
            try:
                self.ex.cancel_order(pos["stop_order_id"], pos["market"])
            except Exception:
                pass
            pos["stop_order_id"] = pos["stop_order_price"] = None

    def positions(self):
        """Positions réellement ouvertes sur l'exchange : {marché: {side, qty, entry}}."""
        out = {}
        for p in self.ex.fetch_positions():
            qty = abs(float(p.get("contracts") or 0))
            if qty > 0:
                out[p["symbol"]] = {"side": p.get("side") or "long", "qty": qty,
                                    "entry": float(p.get("entryPrice") or 0)}
        return out

    def equity(self, state, open_pnl):
        bal = self.ex.fetch_balance()
        wallet = float((bal.get("total") or {}).get("USDT") or 0)
        try:
            upnl = sum(float(p.get("unrealizedPnl") or 0) for p in self.ex.fetch_positions())
        except Exception:
            upnl = open_pnl
        return wallet + upnl


# --------------------------------------------------------------------------- #
# Configuration et clés
# --------------------------------------------------------------------------- #


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        cfg.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_env():
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def setup_keys():
    """Enregistre les clés API dans .env (lisible par vous seul)."""
    cfg = load_config()
    print(bold("\n  Connexion à l'exchange"))
    print(dim("  Créez une clé API avec le droit « Futures / trading » et SANS droit de retrait."))
    ex = input("  Exchange ccxt [{}] : ".format(cfg["exchange"])).strip() or cfg["exchange"]
    key = getpass.getpass("  Clé API (masquée) : ").strip()
    secret = getpass.getpass("  Secret API (masqué) : ").strip()
    if not key or not secret:
        print("  Annulé.")
        return
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("EXCHANGE_API_KEY={}\nEXCHANGE_API_SECRET={}\n".format(key, secret))
    os.chmod(ENV_PATH, 0o600)
    cfg["exchange"] = ex
    save_config(cfg)
    print(green("  Clés enregistrées dans .env (droits 600). L'agent se connectera seul à {}.".format(ex)))


# --------------------------------------------------------------------------- #
# Agent de trading
# --------------------------------------------------------------------------- #


class Trader:
    def __init__(self, mode="papier", cfg=None):
        self.cfg = cfg or load_config()
        self.mode = mode
        self.cg = CoinGecko()
        self.md = MarketData("bybit" if "bybit" in self.cfg["exchange"] else "binance")
        self.broker = PaperBroker(self.cfg) if mode == "papier" else CcxtBroker(self.cfg, mode == "testnet")
        self.state_path = os.path.join(HERE, "portefeuille_{}.json".format(mode))
        self.journal_path = os.path.join(HERE, "journal_{}.csv".format(mode))
        self.state = self._load_state()
        self.coins, self.tick, self.candles = {}, {}, {}
        self.rank_source = None
        self._universe_ts = 0
        self._secondary = None

    # -- état ---------------------------------------------------------------------

    def _load_state(self):
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        cap = self.cfg["capital_initial"]
        return {"cash": cap, "positions": {}, "peak": cap, "day": None, "day_start": cap,
                "halted_day": None, "halted": False, "derniere_optim": 0,
                "stats": {"trades": 0, "gagnants": 0, "pnl": 0.0, "frais": 0.0}}

    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_path)

    def journal(self, **row):
        new = not os.path.exists(self.journal_path)
        fields = ["date", "action", "crypto", "sens", "quantite", "prix", "notionnel", "frais", "pnl", "raison"]
        with open(self.journal_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if new:
                w.writeheader()
            row["date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            w.writerow(row)

    # -- marché -------------------------------------------------------------------

    def refresh(self, full=False):
        """Classement toutes les 30 min, prix et financement à chaque appel."""
        if full or not self.coins or time.time() - self._universe_ts > 1800:
            coins, self.rank_source = top_coins(100, self.cg)
            contracts = self.md.contracts()
            self.coins = {}
            for c in coins:
                c.update(contracts.get(c["symbol"].upper()) or {"contract": None, "base": None, "mult": 1})
                self.coins[c["id"]] = c
            self._universe_ts = time.time()
        self.tick = self.md.tickers()
        self.candles = {}
        self._secondary = None

    def resolve(self, query):
        q = query.strip().lower()
        if not self.coins:
            self.refresh()
        for key in ("symbol", "id", "name", "base"):
            for coin in self.coins.values():
                if (coin.get(key) or "").lower() == q:
                    return coin
        raise TradingError("« {} » ne fait pas partie du top 100.".format(query))

    def universe(self):
        return [c for c in self.coins.values()
                if c["contract"] in self.tick and tradable(c, self.cfg) and self.broker.available(c)]

    def load_candles(self, contracts):
        missing = [c for c in contracts if c and c not in self.candles]
        if missing:
            self.candles.update(self.md.candles_many(missing, self.cfg["bougies"], CANDLES_NEEDED))

    def analyse_contract(self, contract):
        self.load_candles([contract])
        k = self.candles.get(contract)
        if not k:
            return None
        a = analyse([x["c"] for x in k], **bar_params(self.cfg["bougies"]))
        if a:
            a["funding"] = self.tick.get(contract, {}).get("funding", 0.0)
            a["score"] = funding_adjust(a["score"], a["funding"], self.cfg)
        return a

    def price(self, contract):
        if contract not in self.tick:
            raise TradingError("Pas de prix pour {}.".format(contract))
        return self.tick[contract]["price"]

    def open_pnl(self):
        return sum(unrealized(p, self.tick[p["contract"]]["price"])
                   for p in self.state["positions"].values() if p["contract"] in self.tick)

    def equity(self):
        return self.broker.equity(self.state, self.open_pnl())

    def exposure(self):
        return sum(p["qty"] * p["entry"] for p in self.state["positions"].values())

    def scan(self):
        coins = self.universe()
        self.load_candles([c["contract"] for c in coins])
        results = []
        for coin in coins:
            a = self.analyse_contract(coin["contract"])
            if a:
                a["coin"] = coin
                a["signal"] = signal_of(a["score"], self.cfg)
                results.append(a)
        results.sort(key=lambda x: -abs(x["score"]))
        return results

    def price_check(self, coin, price):
        """Compare le prix à une autre source. Renvoie l'écart en %, ou None si invérifiable."""
        if self._secondary is None:
            self._secondary = self.md.secondary_tickers()
        ref = (self._secondary.get(coin["contract"]) or {}).get("price")
        if not ref:
            token = reference_prices([coin], self.rank_source).get(coin["id"])
            ref = token * coin["mult"] if token else None
        return abs(price / ref - 1) * 100 if ref else None

    # -- positions ------------------------------------------------------------------

    def open_position(self, coin, side, notional=None, reason="manuel"):
        cfg, st = self.cfg, self.state
        if st["halted"]:
            raise TradingError("Trading arrêté (drawdown max atteint). Utilisez « reprendre ».")
        if st["halted_day"] == str(date.today()):
            raise TradingError("Coupe-circuit journalier actif : plus d'ouverture aujourd'hui.")
        if not coin.get("contract") or coin["contract"] not in self.tick or not self.broker.available(coin):
            raise TradingError("{} n'a pas de contrat perpétuel disponible.".format(coin["symbol"].upper()))
        if coin["id"] in st["positions"]:
            raise TradingError("Une position est déjà ouverte sur {}.".format(coin["symbol"].upper()))
        if side == "short" and not cfg["autoriser_short"]:
            raise TradingError("Le short est désactivé (autoriser_short = false).")
        if cfg["positions_max"] and len(st["positions"]) >= cfg["positions_max"]:
            raise TradingError("Nombre maximal de positions atteint ({}).".format(cfg["positions_max"]))

        a = self.analyse_contract(coin["contract"])
        stop_pct = stop_pct_from(a["sigma_jour"] if a else None, cfg)
        equity = self.equity()
        if notional is None:
            notional = equity * cfg["risque_par_trade_pct"] / 100 / (stop_pct / 100)
        notional = min(notional, equity * cfg["levier_max"] - self.exposure())
        if notional < 10:
            raise TradingError("Plus de marge disponible (exposition max = {:.1f}× le capital).".format(
                cfg["levier_max"]))

        price = self.price(coin["contract"])
        gap = self.price_check(coin, price)
        if gap is not None and gap > cfg["ecart_prix_max_pct"]:
            raise TradingError("Prix de {} incohérent entre les sources ({:.1f}% d'écart) : ordre annulé.".format(
                coin["symbol"].upper(), gap))

        fill = self.broker.open(coin, side, notional, price, cfg["levier_max"])
        pos = new_position(side, fill["price"], fill["qty"], stop_pct, cfg)
        pos.update({"id": coin["id"], "symbol": coin["base"], "contract": coin["contract"],
                    "market": fill.get("market"), "fees": fill["fee"],
                    "opened": datetime.now().isoformat(timespec="seconds")})
        st["positions"][coin["id"]] = pos
        st["cash"] -= fill["fee"]
        st["stats"]["frais"] += fill["fee"]
        self.broker.protect(pos)
        self.save()
        self.journal(action="OUVERTURE", crypto=pos["symbol"], sens=side, quantite=round(pos["qty"], 8),
                     prix=round(pos["entry"], 8), notionnel=round(pos["qty"] * pos["entry"], 2),
                     frais=round(fill["fee"], 4), pnl="", raison=reason)
        print("  {} {} {} : {} à {} · stop {} · objectif {}".format(
            green("▲ LONG") if side == "long" else red("▼ SHORT"), pos["symbol"],
            dim("(" + reason + ")"), fmt_big(pos["qty"] * pos["entry"], CUR), fmt_price(pos["entry"], CUR),
            fmt_price(pos["stop"], CUR), fmt_price(pos["tp"], CUR)))
        return pos

    def _record_close(self, key, pos, exit_price, fee, reason):
        pnl = unrealized(pos, exit_price) - fee
        net = pnl - pos["fees"]
        st = self.state
        st["cash"] += pnl
        st["stats"]["trades"] += 1
        st["stats"]["gagnants"] += 1 if net > 0 else 0
        st["stats"]["pnl"] += net
        st["stats"]["frais"] += fee
        del st["positions"][key]
        self.save()
        self.journal(action="FERMETURE", crypto=pos["symbol"], sens=pos["side"], quantite=round(pos["qty"], 8),
                     prix=round(exit_price, 8), notionnel=round(pos["qty"] * exit_price, 2),
                     frais=round(fee, 4), pnl=round(net, 2), raison=reason)
        print("  ✕ Fermeture {} {} ({}) à {} → {}".format(
            pos["side"].upper(), pos["symbol"], reason, fmt_price(exit_price, CUR),
            (green if net >= 0 else red)("{:+,.2f} $".format(net).replace(",", " "))))
        return net

    def close_position(self, key, reason="manuel"):
        pos = self.state["positions"].get(key)
        if not pos:
            raise TradingError("Aucune position ouverte sur cette crypto.")
        fill = self.broker.close(pos, self.price(pos["contract"]))
        return self._record_close(key, pos, fill["price"], fill["fee"], reason)

    def close_all(self, reason):
        for key in list(self.state["positions"]):
            try:
                self.close_position(key, reason)
            except Exception as e:
                print(red("  Échec de fermeture {} : {}".format(key, e)))

    def sync_exchange(self):
        """Aligne le portefeuille local sur l'exchange (stops exécutés, positions ouvertes ailleurs)."""
        live = self.broker.positions()
        if live is None:
            return
        tracked = {p["market"]: k for k, p in self.state["positions"].items()}
        for market, key in tracked.items():
            if market not in live:
                pos = self.state["positions"][key]
                price = self.tick.get(pos["contract"], {}).get("price", pos["stop"])
                print(yellow("  Position {} fermée côté exchange (stop exécuté).".format(pos["symbol"])))
                self._record_close(key, pos, pos["stop"] if pos.get("stop_order_id") else price,
                                   0.0, "stop exchange")
        for market, lp in live.items():
            if market in tracked:
                continue
            coin = next((c for c in self.coins.values()
                         if c.get("base") and "{}/USDT:USDT".format(c["base"]) == market), None)
            if not coin:
                print(yellow("  Position {} inconnue sur l'exchange, laissée telle quelle.".format(market)))
                continue
            a = self.analyse_contract(coin["contract"])
            pos = new_position(lp["side"], lp["entry"] or self.price(coin["contract"]), lp["qty"],
                               stop_pct_from(a["sigma_jour"] if a else None, self.cfg), self.cfg)
            pos.update({"id": coin["id"], "symbol": coin["base"], "contract": coin["contract"],
                        "market": market, "fees": 0.0, "opened": datetime.now().isoformat(timespec="seconds")})
            self.state["positions"][coin["id"]] = pos
            self.broker.protect(pos)
            print(yellow("  Position {} {} trouvée sur l'exchange : reprise en gestion.".format(
                lp["side"].upper(), coin["base"])))
        self.save()

    # -- boucle autonome ------------------------------------------------------------

    def risk_guard(self):
        """Coupe-circuits. Renvoie True si l'ouverture de positions est autorisée."""
        st, cfg = self.state, self.cfg
        eq = self.equity()
        today = str(date.today())
        if st["day"] != today:
            st["day"], st["day_start"] = today, eq
        st["peak"] = max(st["peak"], eq)

        if st["halted"]:
            return False
        if cfg["drawdown_max_pct"] and eq <= st["peak"] * (1 - cfg["drawdown_max_pct"] / 100):
            print(bold(red("  ⛔ Drawdown max de {}% atteint : tout est fermé, trading arrêté.".format(
                cfg["drawdown_max_pct"]))))
            self.close_all("drawdown max")
            st["halted"] = True
            self.save()
            return False
        if cfg["perte_journaliere_max_pct"] and st["halted_day"] != today \
                and eq <= st["day_start"] * (1 - cfg["perte_journaliere_max_pct"] / 100):
            print(bold(red("  ⛔ Perte journalière max de {}% atteinte : tout est fermé jusqu'à demain.".format(
                cfg["perte_journaliere_max_pct"]))))
            self.close_all("coupe-circuit journalier")
            st["halted_day"] = today
            self.save()
        return st["halted_day"] != today

    def cycle(self, verbose=True):
        self.refresh()
        self.sync_exchange()
        can_open = self.risk_guard()

        # 1. Gestion des positions ouvertes
        self.load_candles([p["contract"] for p in self.state["positions"].values()])
        for key, pos in list(self.state["positions"].items()):
            if pos["contract"] not in self.tick:
                continue
            a = self.analyse_contract(pos["contract"])
            reason = check_exit(pos, self.price(pos["contract"]), a["score"] if a else None, self.cfg)
            if reason:
                self.close_position(key, reason)
            else:
                self.broker.protect(pos)
        self.save()

        # 2. Nouvelles positions : shorts en priorité, puis par force du signal
        signals = self.scan()
        if can_open:
            candidates = sorted((s for s in signals if s["signal"] and s["coin"]["id"] not in self.state["positions"]),
                                key=lambda s: (s["signal"] != "short", -abs(s["score"])))
            for s in candidates:
                if self.cfg["positions_max"] and len(self.state["positions"]) >= self.cfg["positions_max"]:
                    break
                try:
                    self.open_position(s["coin"], s["signal"], reason="auto score {:+.0f}".format(s["score"]))
                except TradingError as e:
                    print(dim("  " + str(e)))
                    if "marge" in str(e):
                        break
        if verbose:
            self.show_portfolio()
        return signals

    def maybe_reoptimize(self):
        h = self.cfg["reoptimisation_h"]
        if h and time.time() - self.state.get("derniere_optim", 0) > h * 3600:
            print(cyan("  ⚙ Ré-optimisation automatique des paramètres…"))
            self.optimize(apply=True)

    def run(self, interval=None, cycles=None):
        print(bold(cyan("  🤖 Mode autonome [{}] — profil {} — Ctrl+C pour arrêter.".format(
            self.mode.upper(), self.cfg["profil"]))))
        print(dim("  Les positions restent ouvertes à l'arrêt ; relancez « auto » pour reprendre leur gestion."))
        n = 0
        try:
            while cycles is None or n < cycles:
                print(dim("\n  ── Cycle {} · {}".format(n + 1, datetime.now().strftime("%d/%m %H:%M:%S"))))
                try:
                    self.maybe_reoptimize()
                    self.cycle()
                except (CoinGeckoError, DataError, TradingError) as e:
                    print(red("  Erreur : {}".format(e)))
                n += 1
                if cycles is None or n < cycles:
                    time.sleep(max(interval or self.cfg["intervalle_auto_s"], 30))
        except KeyboardInterrupt:
            print("\n  Mode autonome arrêté.")

    # -- optimisation et backtest -------------------------------------------------

    def _datasets(self, coins, bars):
        out = []
        for i, coin in enumerate(coins, 1):
            print(dim("  [{}/{}] Historique et analyse de {}…".format(i, len(coins), coin["base"])))
            candles = self.md.history(coin["contract"], self.cfg["bougies"], bars)
            if len(candles) > 400:
                out.append((candles, precompute(candles, self.cfg["bougies"])))
        return out

    def optimize(self, n_coins=None, bars=3000, apply=False):
        """Recherche des meilleurs réglages : calibrage sur 70 % de l'historique, validation sur 30 %."""
        self.refresh(full=True)
        coins = sorted(self.universe(), key=lambda c: c["market_cap_rank"] or 999)[:n_coins or self.cfg["optim_cryptos"]]
        datasets = self._datasets(coins, bars)
        if not datasets:
            raise TradingError("Historique insuffisant pour optimiser.")
        keys = list(OPTIM_GRID)
        combos = [{}]
        for k in keys:
            combos = [dict(c, **{k: v}) for c in combos for v in OPTIM_GRID[k]]
        print(dim("  Test de {} combinaisons sur {} cryptos ({} bougies {})…".format(
            len(combos), len(datasets), bars, self.cfg["bougies"])))

        scored = []
        for combo in combos:
            p = dict(self.cfg, **combo)
            scored.append((evaluate(datasets, p, "train")["objectif"], combo))
        scored.sort(key=lambda x: -x[0])

        current = {k: self.cfg[k] for k in keys}
        cur_test = evaluate(datasets, self.cfg, "test")
        print()
        print(bold("  " + "  ".join([pad("Réglages", 44), pad("Calibrage", 10, True), pad("Validation", 11, True),
                                     pad("Drawdown", 9, True), pad("Trades", 7, True), pad("Gagnants", 9, True)])))
        best, best_test = None, None
        for obj, combo in scored[:5]:
            t = evaluate(datasets, dict(self.cfg, **combo), "test")
            if best is None or t["objectif"] > best_test["objectif"]:
                best, best_test = combo, t
            label = "L{score_entree_long} S{score_entree_short} stop×{multiplicateur_stop} " \
                    "gain×{ratio_gain_risque} sortie{score_sortie}".format(**combo)
            print("  " + "  ".join([pad(label, 44), pad("{:+.1f}".format(obj), 10, True),
                                    pad(fmt_pct(t["ret"], 0), 11, True), pad("-{:.1f}%".format(t["dd"]), 9, True),
                                    pad(str(t["trades"]), 7, True), pad("{:.0f}%".format(t["win"]), 9, True)]))
        print(dim("  Réglages actuels en validation : {} · drawdown -{:.1f}% · {} trades".format(
            fmt_pct(cur_test["ret"], 0), cur_test["dd"], cur_test["trades"])))

        self.state["derniere_optim"] = time.time()
        if best_test["objectif"] > cur_test["objectif"] and best != current:
            if apply:
                self.cfg.update(best)
                save_config(self.cfg)
                print(green("  ✓ Nouveaux réglages appliqués : {}".format(best)))
            else:
                print(yellow("  Meilleurs réglages trouvés : {} (« optimiser --appliquer » pour les adopter)".format(best)))
        else:
            print(dim("  Les réglages actuels restent les meilleurs en validation : aucun changement."))
        self.save()
        print(dim("  Rendements moyens par crypto, frais et glissement inclus, financement ignoré."))

    def backtest(self, query, bars=3000):
        self.refresh()
        coin = self.resolve(query)
        if not coin.get("contract"):
            raise TradingError("{} n'a pas de contrat perpétuel.".format(coin["symbol"].upper()))
        data = self._datasets([coin], bars)
        if not data:
            raise TradingError("Historique insuffisant.")
        candles, pre = data[0]
        r = simulate(candles, pre, self.cfg)
        start = next(i for i, x in enumerate(pre) if x)
        hold = (candles[-1]["c"] / candles[start]["c"] - 1) * 100
        days = (candles[-1]["t"] - candles[start]["t"]) / 86400000
        print()
        print(bold("  Backtest {} — {:.0f} jours de bougies {} — profil {}".format(
            coin["base"], days, self.cfg["bougies"], self.cfg["profil"])))
        print("  Rendement de la stratégie : {}".format(fmt_pct(r["ret"], 0)))
        print("  Achat-conservation        : {}".format(fmt_pct(hold, 0)))
        print("  Drawdown maximal          : {}".format(red("-{:.1f}%".format(r["dd"]))))
        if r["trades"]:
            print("  Trades : {} · gagnants {:.0f}%".format(r["trades"], r["wins"] / r["trades"] * 100))
        print(dim("  Frais et glissement inclus, financement ignoré. Le passé ne préjuge pas du futur."))

    # -- affichage ---------------------------------------------------------------

    def show_trends(self, n=20):
        self.refresh()
        all_signals = self.scan()
        print()
        print(bold("  " + "  ".join([pad("Crypto", 10), pad("Prix", 13, True), pad("24h", 9, True),
                                     pad("Financ.", 8, True), pad("Score", 6, True), pad("", 10),
                                     pad("RSI", 5, True), pad("Stop", 6, True), "Signal"])))
        print(dim("  " + "─" * 90))
        for s in all_signals[:n]:
            coin = s["coin"]
            sig = {"long": green("LONG ▲"), "short": red("SHORT ▼")}.get(s["signal"], dim("neutre"))
            print("  " + "  ".join([
                pad(coin["base"], 10),
                pad(fmt_price(self.price(coin["contract"]), CUR), 13, True),
                pad(fmt_pct(coin.get("price_change_percentage_24h_in_currency")), 9, True),
                pad("{:+.3f}%".format(s["funding"] * 100), 8, True),
                pad("{:+.0f}".format(s["score"]), 6, True),
                score_bar(s["score"]),
                pad("{:.0f}".format(s["rsi"]) if s["rsi"] is not None else "—", 5, True),
                pad("{:.1f}%".format(stop_pct_from(s["sigma_jour"], self.cfg)), 6, True),
                sig,
            ]))
        longs = sum(1 for s in all_signals if s["signal"] == "long")
        shorts = sum(1 for s in all_signals if s["signal"] == "short")
        print(dim("  " + "─" * 90))
        print("  Signaux : {} long · {} short sur {} cryptos · seuils long ≥ {} / short ≤ -{}".format(
            green(longs), red(shorts), len(all_signals), self.cfg["score_entree_long"],
            self.cfg["score_entree_short"]))
        print(dim("  Bougies {} {} · classement {} · profil {}".format(
            self.cfg["bougies"], self.md.venue.name, self.rank_source, self.cfg["profil"])))

    def show_portfolio(self):
        if not self.tick:
            self.refresh()
        st = self.state
        eq = self.equity()
        cap = self.cfg["capital_initial"]
        print()
        print(bold("  Portefeuille [{}] · profil {}".format(self.mode.upper(), self.cfg["profil"])))
        line = "  Valeur : {}".format(bold(fmt_price(eq, CUR)))
        if self.mode == "papier":
            line += "  ({} depuis le départ, capital initial {})".format(fmt_pct((eq / cap - 1) * 100, 0),
                                                                      fmt_price(cap, CUR))
        print(line)
        print("  Exposition : {} ({:.2f}× le capital, max {}×) · positions : {}{}".format(
            fmt_big(self.exposure(), CUR), self.exposure() / eq if eq else 0, self.cfg["levier_max"],
            len(st["positions"]), "/{}".format(self.cfg["positions_max"]) if self.cfg["positions_max"] else ""))
        s = st["stats"]
        if s["trades"]:
            print("  Trades clôturés : {} · gagnants {:.0f}% · résultat {} · frais {}".format(
                s["trades"], s["gagnants"] / s["trades"] * 100,
                (green if s["pnl"] >= 0 else red)("{:+,.2f} $".format(s["pnl"]).replace(",", " ")),
                fmt_price(s["frais"], CUR)))
        if st["halted"]:
            print(bold(red("  ⛔ Trading arrêté (drawdown max). « reprendre » pour réactiver.")))
        elif st["halted_day"] == str(date.today()):
            print(bold(yellow("  ⏸ Coupe-circuit journalier actif jusqu'à demain.")))
        if not st["positions"]:
            print(dim("  Aucune position ouverte."))
            return
        print()
        print(bold("  " + "  ".join([pad("Crypto", 10), pad("Sens", 6), pad("Taille", 10, True),
                                     pad("Entrée", 13, True), pad("Actuel", 13, True), pad("P&L", 20, True),
                                     pad("Stop", 13, True), pad("Objectif", 13, True)])))
        for pos in st["positions"].values():
            price = self.tick.get(pos["contract"], {}).get("price", pos["entry"])
            pnl = unrealized(pos, price)
            pct = pnl / (pos["qty"] * pos["entry"]) * 100
            print("  " + "  ".join([
                pad(pos["symbol"], 10),
                pad(green("LONG") if pos["side"] == "long" else red("SHORT"), 6),
                pad(fmt_big(pos["qty"] * pos["entry"], CUR), 10, True),
                pad(fmt_price(pos["entry"], CUR), 13, True),
                pad(fmt_price(price, CUR), 13, True),
                pad("{} {}".format((green if pnl >= 0 else red)("{:+.2f}$".format(pnl)), fmt_pct(pct, 0)), 20, True),
                pad(fmt_price(pos["stop"], CUR), 13, True),
                pad(fmt_price(pos["tp"], CUR), 13, True),
            ]))

    def show_history(self, n=15):
        if not os.path.exists(self.journal_path):
            print(dim("  Aucun trade enregistré."))
            return
        with open(self.journal_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))[-n:]
        print()
        for r in rows:
            pnl = ""
            if r["pnl"]:
                v = float(r["pnl"])
                pnl = (green if v >= 0 else red)("{:+.2f}$".format(v))
            print("  {}  {:<10} {:<9} {:<5} {:>14}  {:>10}  {}".format(
                dim(r["date"]), r["action"], r["crypto"], r["sens"], fmt_price(float(r["prix"]), CUR),
                pnl, dim(r["raison"])))

    # -- réglages ------------------------------------------------------------------

    def set_profile(self, name):
        if name not in PROFILES:
            raise TradingError("Profils disponibles : {}".format(", ".join(PROFILES)))
        self.cfg.update(PROFILES[name])
        self.cfg["profil"] = name
        save_config(self.cfg)
        print("  Profil {} appliqué.".format(bold(name)))

    def config_cmd(self, args):
        if len(args) >= 2:
            key, val = args[0], " ".join(args[1:])
            if key not in DEFAULT_CONFIG:
                raise TradingError("Clé inconnue : {}".format(key))
            ref = DEFAULT_CONFIG[key]
            if isinstance(ref, bool):
                val = val.lower() in ("1", "true", "oui", "vrai")
            elif isinstance(ref, (int, float)):
                val = float(val)
            elif isinstance(ref, list):
                val = [v.strip().lower() for v in val.split(",") if v.strip()]
            self.cfg[key] = val
            save_config(self.cfg)
        print()
        for k, v in self.cfg.items():
            print("  {:<28} {}".format(k, v))

    def reset(self):
        if self.mode != "papier":
            raise TradingError("Le reset n'est possible qu'en mode papier.")
        if self.state["positions"]:
            raise TradingError("Fermez d'abord les positions (« fermer tout »).")
        for p in (self.state_path, self.journal_path):
            if os.path.exists(p):
                os.remove(p)
        self.state = self._load_state()
        self.save()
        print("  Portefeuille papier remis à {}.".format(fmt_price(self.cfg["capital_initial"], CUR)))

    # -- mode interactif -------------------------------------------------------------

    HELP = """
  Commandes :
    tendances [n]            Scan du top 100 : score de tendance, financement, signaux
    long <crypto> [$]        Ouvre un long (taille auto selon le risque si montant omis)
    short <crypto> [$]       Ouvre un short
    fermer <crypto|tout>     Ferme une position ou toutes
    portefeuille             Valeur, exposition et positions ouvertes
    cycle                    Un cycle de décision autonome (gestion + ouvertures)
    auto [secondes]          Mode autonome en boucle
    optimiser [appliquer]    Cherche les meilleurs réglages sur l'historique
    backtest <crypto>        Teste les réglages actuels sur l'historique
    profil <prudent|agressif>
    historique [n]           Derniers trades du journal
    config [clé valeur]      Affiche ou modifie les réglages
    reprendre                Réactive le trading après un arrêt de sécurité
    reset                    Remet le portefeuille papier à zéro
    quitter
"""

    def repl(self):
        print(bold(cyan("\n  🤖 Agent de trading crypto — mode {} — profil {}".format(
            self.mode.upper(), self.cfg["profil"]))))
        print(self.HELP)
        while True:
            try:
                raw = input(cyan("trader> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw:
                continue
            parts = raw.split()
            cmd, args = parts[0].lower(), parts[1:]
            try:
                if cmd in ("quitter", "exit", "q"):
                    break
                elif cmd in ("aide", "help", "?"):
                    print(self.HELP)
                elif cmd in ("tendances", "scan"):
                    self.show_trends(int(args[0]) if args else 20)
                elif cmd in ("long", "short"):
                    if not args:
                        print("  Usage : {} <crypto> [montant $]".format(cmd))
                        continue
                    self.refresh()
                    self.open_position(self.resolve(args[0]), cmd, float(args[1]) if len(args) > 1 else None)
                elif cmd == "fermer":
                    self.refresh()
                    if args and args[0] == "tout":
                        self.close_all("manuel")
                    elif args:
                        self.close_position(self.resolve(args[0])["id"])
                elif cmd in ("portefeuille", "positions", "pf"):
                    self.refresh()
                    self.show_portfolio()
                elif cmd == "cycle":
                    self.cycle()
                elif cmd == "auto":
                    self.run(int(args[0]) if args else None)
                elif cmd == "optimiser":
                    self.optimize(apply=bool(args and args[0].startswith("appl")))
                elif cmd == "backtest" and args:
                    self.backtest(args[0])
                elif cmd == "profil" and args:
                    self.set_profile(args[0])
                elif cmd == "historique":
                    self.show_history(int(args[0]) if args else 15)
                elif cmd == "config":
                    self.config_cmd(args)
                elif cmd == "reprendre":
                    self.state["halted"], self.state["halted_day"] = False, None
                    self.state["peak"] = self.equity()
                    self.save()
                    print("  Trading réactivé.")
                elif cmd == "reset":
                    self.reset()
                else:
                    print("  Commande inconnue. Tapez « aide ».")
            except (TradingError, CoinGeckoError, DataError) as e:
                print(red("  Erreur : {}".format(e)))
            except ValueError as e:
                print(red("  Paramètre invalide : {}".format(e)))
            except Exception as e:
                print(red("  Erreur de l'exchange : {}".format(e)))
        print("  À bientôt 👋")


# --------------------------------------------------------------------------- #
# Service autonome
# --------------------------------------------------------------------------- #


class Tee:
    def __init__(self, stream, path):
        self.stream, self.file = stream, open(path, "a", encoding="utf-8", buffering=1)

    def write(self, data):
        self.stream.write(data)
        self.file.write(data)

    def flush(self):
        self.stream.flush()
        self.file.flush()

    def isatty(self):
        return self.stream.isatty()


def service(mode):
    """Boucle sans fin : se connecte seul, se reconnecte après une panne, se ré-optimise."""
    os.makedirs(LOG_DIR, exist_ok=True)
    if sys.stdout.isatty():  # sous launchd, la sortie est déjà redirigée vers le journal
        log = os.path.join(LOG_DIR, "agent_{}.log".format(mode))
        sys.stdout = Tee(sys.stdout, log)
        sys.stderr = Tee(sys.stderr, log)

    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)

    trader, backoff = None, 30
    print(bold("  🤖 Service démarré [{}] · {}".format(mode.upper(), datetime.now().strftime("%d/%m/%Y %H:%M:%S"))))
    try:
        while True:
            try:
                if load_config()["strategie"] == "scalping":
                    from scalper import Scalper  # import différé : inutile en mode tendance
                    try:
                        Scalper(mode).run()  # chaque flux se reconnecte seul ; sortie = panne durable
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        busy = type(e).__name__ == "AlreadyRunning"
                        print(red("  Scalping interrompu : {} — nouvel essai dans {} s.".format(e, 60 if busy else 5)))
                        sys.stdout.flush()
                        time.sleep(60 if busy else 5)
                    continue
                if trader is None:
                    trader = Trader(mode)
                    print(green("  Connecté ({}, données {}).".format(trader.broker.name, trader.md.venues[0].name)))
                    backoff = 30
                print(dim("\n  ── {} · cycle".format(datetime.now().strftime("%d/%m %H:%M:%S"))))
                trader.maybe_reoptimize()
                trader.cycle()
                sys.stdout.flush()
                time.sleep(max(trader.cfg["intervalle_auto_s"], 30))
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(red("  Erreur : {} — nouvelle connexion dans {} s.".format(e, backoff)))
                sys.stdout.flush()
                trader = None
                time.sleep(backoff)
                backoff = min(backoff * 2, 600)
    except KeyboardInterrupt:
        if trader:
            trader.save()
        print("  Service arrêté. Les positions restent ouvertes (stops déposés sur l'exchange en mode réel).")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def confirm_real(skip):
    print(bold(red("\n  ⚠  MODE RÉEL : l'agent va passer des ordres avec votre argent, y compris")))
    print(bold(red("     des ventes à découvert avec levier. Les pertes peuvent dépasser la mise.")))
    if skip:
        return
    answer = input("  Tapez « JE CONFIRME » pour continuer : ").strip()
    if answer != "JE CONFIRME":
        print("  Annulé.")
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Agent de trading crypto autonome (tendances, long, short).")
    parser.add_argument("--mode", choices=["papier", "testnet", "reel"], default="papier")
    parser.add_argument("--je-confirme-le-risque", action="store_true",
                        help="Saute la confirmation interactive du mode réel (requis pour le service)")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("tendances")
    p.add_argument("-n", type=int, default=20)
    for side in ("long", "short"):
        p = sub.add_parser(side)
        p.add_argument("crypto")
        p.add_argument("montant", type=float, nargs="?")
    p = sub.add_parser("fermer")
    p.add_argument("crypto", help="symbole ou « tout »")
    sub.add_parser("portefeuille")
    sub.add_parser("cycle")
    p = sub.add_parser("auto")
    p.add_argument("--intervalle", type=int)
    p.add_argument("--cycles", type=int, help="Nombre de cycles (défaut : infini)")
    sub.add_parser("service", help="Fonctionnement autonome permanent (reconnexion automatique)")
    p = sub.add_parser("optimiser")
    p.add_argument("--appliquer", action="store_true")
    p.add_argument("--cryptos", type=int)
    p.add_argument("--bougies", type=int, default=3000)
    p = sub.add_parser("backtest")
    p.add_argument("crypto")
    p.add_argument("--bougies", type=int, default=3000)
    p = sub.add_parser("profil")
    p.add_argument("nom", choices=list(PROFILES))
    p = sub.add_parser("historique")
    p.add_argument("-n", type=int, default=15)
    sub.add_parser("cles", help="Enregistre les clés API de l'exchange")
    sub.add_parser("reset")
    args = parser.parse_args()

    load_env()
    if args.cmd == "cles":
        setup_keys()
        return
    if args.mode == "reel":
        confirm_real(args.je_confirme_le_risque)
    if args.cmd == "service":
        service(args.mode)
        return
    try:
        t = Trader(args.mode)
        if args.cmd == "tendances":
            t.show_trends(args.n)
        elif args.cmd in ("long", "short"):
            t.refresh()
            t.open_position(t.resolve(args.crypto), args.cmd, args.montant)
        elif args.cmd == "fermer":
            t.refresh()
            if args.crypto == "tout":
                t.close_all("manuel")
            else:
                t.close_position(t.resolve(args.crypto)["id"])
        elif args.cmd == "portefeuille":
            t.refresh()
            t.show_portfolio()
        elif args.cmd == "cycle":
            t.cycle()
        elif args.cmd == "auto":
            t.run(args.intervalle, args.cycles)
        elif args.cmd == "optimiser":
            t.optimize(args.cryptos, args.bougies, args.appliquer)
        elif args.cmd == "backtest":
            t.backtest(args.crypto, args.bougies)
        elif args.cmd == "profil":
            t.set_profile(args.nom)
        elif args.cmd == "historique":
            t.show_history(args.n)
        elif args.cmd == "reset":
            t.reset()
        else:
            t.repl()
    except (TradingError, CoinGeckoError, DataError) as e:
        print(red("Erreur : {}".format(e)), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
