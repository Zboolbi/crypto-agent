#!/usr/bin/env python3
"""
Agent Crypto — suivi des 100 cryptomonnaies les plus capitalisées + prévisions.

Données : API publique CoinGecko (aucune clé requise ; une clé "demo" optionnelle
peut être fournie via la variable d'environnement COINGECKO_API_KEY).

Aucune dépendance externe : bibliothèque standard Python uniquement (3.8+).

⚠️  Les prévisions sont des estimations statistiques, pas un conseil financier.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_BASE = "https://api.coingecko.com/api/v3"
USER_AGENT = "crypto-agent/1.0"
CACHE_TTL = 60  # secondes (cours)
HISTORY_TTL = 600  # secondes (historique pour les prévisions)
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "crypto_agent")

# --------------------------------------------------------------------------- #
# Affichage
# --------------------------------------------------------------------------- #

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text, code):
    return "\033[{}m{}\033[0m".format(code, text) if USE_COLOR else str(text)


def green(t):
    return c(t, "32")


def red(t):
    return c(t, "31")


def bold(t):
    return c(t, "1")


def dim(t):
    return c(t, "2")


def cyan(t):
    return c(t, "36")


def yellow(t):
    return c(t, "33")


CURRENCY_SYMBOLS = {"eur": "€", "usd": "$", "gbp": "£", "chf": "CHF ", "jpy": "¥"}


def fmt_price(value, currency):
    if value is None:
        return "—"
    sym = CURRENCY_SYMBOLS.get(currency, currency.upper() + " ")
    if value >= 1000:
        s = "{:,.0f}".format(value)
    elif value >= 1:
        s = "{:,.2f}".format(value)
    elif value >= 0.01:
        s = "{:.4f}".format(value)
    else:
        s = "{:.8f}".format(value).rstrip("0")
    s = s.replace(",", " ")
    return s + " " + sym.strip() if currency == "eur" else sym + s


def fmt_big(value, currency):
    if value is None:
        return "—"
    sym = CURRENCY_SYMBOLS.get(currency, currency.upper() + " ").strip()
    for limit, suffix in ((1e12, "T"), (1e9, "Md"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= limit:
            return "{:.2f} {}{}".format(value / limit, suffix, sym)
    return "{:.0f} {}".format(value, sym)


def fmt_pct(value, width=8):
    if value is None:
        return "—".rjust(width)
    s = "{:+.2f}%".format(value).rjust(width)
    return green(s) if value >= 0 else red(s)


def visible_len(s):
    """Longueur sans les séquences ANSI."""
    out, skip = 0, False
    for ch in s:
        if ch == "\033":
            skip = True
        elif skip and ch == "m":
            skip = False
        elif not skip:
            out += 1
    return out


def pad(s, width, right=False):
    gap = max(0, width - visible_len(s))
    return (" " * gap + s) if right else (s + " " * gap)


def sparkline(values, width=30):
    blocks = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    step = max(1, len(values) // width)
    pts = values[::step][-width:]
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1
    line = "".join(blocks[int((v - lo) / span * (len(blocks) - 1))] for v in pts)
    return green(line) if pts[-1] >= pts[0] else red(line)


# --------------------------------------------------------------------------- #
# Client CoinGecko
# --------------------------------------------------------------------------- #


class CoinGeckoError(Exception):
    pass


class CoinGecko:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get("COINGECKO_API_KEY")
        self._cache = {}

    def _cache_file(self, url):
        return os.path.join(CACHE_DIR, hashlib.sha1(url.encode(), usedforsecurity=False).hexdigest() + ".json")

    def _read_cache(self, url, ttl):
        cached = self._cache.get(url)
        if cached and time.time() - cached[0] < ttl:
            return cached[1]
        try:
            path = self._cache_file(url)
            if time.time() - os.path.getmtime(path) < ttl:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        except (OSError, ValueError):
            pass
        return None

    def _write_cache(self, url, data):
        self._cache[url] = (time.time(), data)
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(self._cache_file(url), "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError:
            pass

    def _get(self, path, params=None, ttl=CACHE_TTL, retries=5):
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        cached = self._read_cache(url, ttl)
        if cached is not None:
            return cached

        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self.api_key:
            headers["x-cg-demo-api-key"] = self.api_key

        delay = 8
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                self._write_cache(url, data)
                return data
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < retries - 1:
                    stale = self._read_cache(url, float("inf"))
                    if stale is not None:
                        print(dim("  Limite de requêtes atteinte : données en cache utilisées."),
                              file=sys.stderr)
                        return stale
                    print(dim("  Limite de requêtes atteinte, nouvel essai dans {}s…".format(delay)),
                          file=sys.stderr)
                    time.sleep(delay)
                    delay *= 2
                    continue
                if e.code == 429:
                    raise CoinGeckoError("Limite de requêtes CoinGecko atteinte, réessayez dans une minute "
                                         "(ou définissez COINGECKO_API_KEY).") from e
                if e.code == 404:
                    raise CoinGeckoError("Ressource introuvable : " + path) from e
                raise CoinGeckoError("Erreur HTTP {} sur {}".format(e.code, path)) from e
            except urllib.error.URLError as e:
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                raise CoinGeckoError("Connexion impossible : {}".format(e.reason)) from e
        raise CoinGeckoError("Échec après {} tentatives".format(retries))

    def top_markets(self, currency="eur", limit=100):
        return self._get("/coins/markets", {
            "vs_currency": currency,
            "order": "market_cap_desc",
            "per_page": min(limit, 250),
            "page": 1,
            "sparkline": "true",
            "price_change_percentage": "1h,24h,7d,30d",
        })[:limit]

    def history(self, coin_id, currency="eur", days=180):
        data = self._get("/coins/{}/market_chart".format(coin_id), {
            "vs_currency": currency,
            "days": days,
            "interval": "daily",
        }, ttl=HISTORY_TTL)
        # Un point par jour ; le dernier point est le prix "live", on le garde.
        series = [(datetime.fromtimestamp(ts / 1000, tz=timezone.utc), p) for ts, p in data.get("prices", [])]
        dedup = {}
        for dt, p in series:
            dedup[dt.date()] = (dt, p)
        return [dedup[k] for k in sorted(dedup)]


# --------------------------------------------------------------------------- #
# Indicateurs & modèles de prévision
# --------------------------------------------------------------------------- #


def mean(xs):
    return sum(xs) / len(xs)


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def sma(values, n):
    return mean(values[-n:]) if len(values) >= n else None


def rsi(values, n=14):
    if len(values) <= n:
        return None
    gains, losses = [], []
    for a, b in zip(values[-n - 1:-1], values[-n:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain, avg_loss = mean(gains), mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def linear_regression(ys):
    """Régression linéaire y = a + b·x avec x = 0..n-1."""
    n = len(ys)
    xs = range(n)
    mx, my = (n - 1) / 2, mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx if sxx else 0.0
    a = my - b * mx
    return a, b


def forecast_linear(log_prices, horizon, window=30):
    """Tendance linéaire sur les `window` derniers jours (en log)."""
    ys = log_prices[-window:]
    a, b = linear_regression(ys)
    n = len(ys)
    return [a + b * (n - 1 + h) for h in range(1, horizon + 1)]


def holt_fit(ys, alpha, beta):
    level, trend = ys[0], ys[1] - ys[0]
    sse = 0.0
    for y in ys[1:]:
        pred = level + trend
        sse += (y - pred) ** 2
        new_level = alpha * y + (1 - alpha) * (level + trend)
        trend = beta * (new_level - level) + (1 - beta) * trend
        level = new_level
    return level, trend, sse


def forecast_holt(log_prices, horizon, damping=0.9):
    """Lissage exponentiel de Holt (tendance amortie), paramètres optimisés par grille."""
    best = None
    grid = [i / 10 for i in range(1, 10)]
    for alpha in grid:
        for beta in grid:
            level, trend, sse = holt_fit(log_prices, alpha, beta)
            if best is None or sse < best[2]:
                best = (level, trend, sse)
    level, trend, _ = best
    out, acc = [], 0.0
    for h in range(1, horizon + 1):
        acc += damping ** h
        out.append(level + acc * trend)
    return out


def forecast_drift(log_prices, horizon, window=90):
    """Marche aléatoire avec dérive (rendement moyen historique)."""
    rets = [b - a for a, b in zip(log_prices[-window - 1:-1], log_prices[-window:])]
    mu = mean(rets) if rets else 0.0
    last = log_prices[-1]
    return [last + mu * h for h in range(1, horizon + 1)]


MODELS = {
    "linear": forecast_linear,
    "holt": forecast_holt,
    "drift": forecast_drift,
}


def ensemble_forecast(prices, horizon):
    logs = [math.log(p) for p in prices]
    preds = {name: fn(logs, horizon) for name, fn in MODELS.items()}
    combined = [mean([preds[m][h] for m in preds]) for h in range(horizon)]

    # Intervalle de confiance : volatilité quotidienne des rendements log (≈ GBM)
    rets = [b - a for a, b in zip(logs[-91:-1], logs[-90:])]
    sigma = stdev(rets)
    z = 1.645  # intervalle à 90 %
    result = []
    for h, mu in enumerate(combined, start=1):
        spread = z * sigma * math.sqrt(h)
        result.append({
            "h": h,
            "mid": math.exp(mu),
            "low": math.exp(mu - spread),
            "high": math.exp(mu + spread),
        })
    return result, {k: [math.exp(v) for v in vs] for k, vs in preds.items()}, sigma


def backtest(prices, horizon, folds=5):
    """Validation glissante : on prévoit `horizon` jours à partir de points passés."""
    errors, direction_hits, naive_errors = [], 0, []
    total = 0
    for f in range(folds, 0, -1):
        cut = len(prices) - f * horizon
        if cut < 60:
            continue
        train, actual = prices[:cut], prices[cut + horizon - 1]
        fc, _, _ = ensemble_forecast(train, horizon)
        pred = fc[-1]["mid"]
        errors.append(abs(pred - actual) / actual * 100)
        naive_errors.append(abs(train[-1] - actual) / actual * 100)
        if (pred - train[-1]) * (actual - train[-1]) > 0:
            direction_hits += 1
        total += 1
    if not total:
        return None
    return {
        "mape": mean(errors),
        "naive_mape": mean(naive_errors),
        "direction": direction_hits / total * 100,
        "folds": total,
    }


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #


class CryptoAgent:
    def __init__(self, currency="eur", limit=100, api_key=None):
        self.currency = currency.lower()
        self.limit = limit
        self.api = CoinGecko(api_key)
        self._markets = None
        self._markets_ts = 0

    # -- données --------------------------------------------------------------

    def markets(self, refresh=False):
        if refresh or self._markets is None or time.time() - self._markets_ts > CACHE_TTL:
            self._markets = self.api.top_markets(self.currency, self.limit)
            self._markets_ts = time.time()
        return self._markets

    def resolve(self, query):
        q = query.strip().lower()
        coins = self.markets()
        for key in ("symbol", "id", "name"):
            for coin in coins:
                if coin[key].lower() == q:
                    return coin
        for coin in coins:
            if q in coin["name"].lower() or q in coin["id"]:
                return coin
        raise CoinGeckoError("« {} » ne fait pas partie du top {}.".format(query, self.limit))

    # -- commandes ------------------------------------------------------------

    def cmd_top(self, n=None, sort="rank", export=None):
        coins = list(self.markets(refresh=True))
        keys = {
            "rank": lambda x: x["market_cap_rank"] or 9999,
            "gain": lambda x: -(x.get("price_change_percentage_24h_in_currency") or 0),
            "loss": lambda x: (x.get("price_change_percentage_24h_in_currency") or 0),
            "volume": lambda x: -(x.get("total_volume") or 0),
            "7d": lambda x: -(x.get("price_change_percentage_7d_in_currency") or 0),
        }
        coins.sort(key=keys.get(sort, keys["rank"]))
        if n:
            coins = coins[:n]

        if export:
            self._export(coins, export)

        cur = self.currency
        header = [("#", 4, True), ("Crypto", 26, False), ("Prix", 16, True),
                  ("1h", 9, True), ("24h", 9, True), ("7j", 9, True),
                  ("Capitalisation", 16, True), ("Volume 24h", 14, True), ("7 jours", 22, False)]
        print()
        print(bold("  ".join(pad(h, w, r) for h, w, r in header)))
        print(dim("─" * 140))
        for coin in coins:
            name = "{} {}".format(coin["name"][:18], dim(coin["symbol"].upper()))
            spark = (coin.get("sparkline_in_7d") or {}).get("price") or []
            row = [
                pad(str(coin["market_cap_rank"] or "—"), 4, True),
                pad(name, 26),
                pad(fmt_price(coin["current_price"], cur), 16, True),
                pad(fmt_pct(coin.get("price_change_percentage_1h_in_currency")), 9, True),
                pad(fmt_pct(coin.get("price_change_percentage_24h_in_currency")), 9, True),
                pad(fmt_pct(coin.get("price_change_percentage_7d_in_currency")), 9, True),
                pad(fmt_big(coin["market_cap"], cur), 16, True),
                pad(fmt_big(coin["total_volume"], cur), 14, True),
                sparkline(spark, 22),
            ]
            print("  ".join(row))
        print(dim("─" * 140))
        self._summary(self.markets())
        print(dim("  Source : CoinGecko · {} · devise : {}".format(
            datetime.now().strftime("%d/%m/%Y %H:%M:%S"), cur.upper())))

    def _summary(self, coins):
        total = sum(x["market_cap"] or 0 for x in coins)
        changes = [x.get("price_change_percentage_24h_in_currency") for x in coins]
        changes = [v for v in changes if v is not None]
        up = sum(1 for v in changes if v > 0)
        btc = next((x for x in coins if x["id"] == "bitcoin"), None)
        print("  Capitalisation cumulée du top {} : {}".format(len(coins), bold(fmt_big(total, self.currency))))
        if btc and total:
            print("  Dominance BTC (dans le top {}) : {:.1f}%".format(len(coins), btc["market_cap"] / total * 100))
        if changes:
            print("  En hausse sur 24h : {} · en baisse : {} · variation médiane : {}".format(
                green(up), red(len(changes) - up), fmt_pct(sorted(changes)[len(changes) // 2], 0)))

    def _export(self, coins, path):
        fields = ["market_cap_rank", "id", "symbol", "name", "current_price", "market_cap",
                  "total_volume", "price_change_percentage_1h_in_currency",
                  "price_change_percentage_24h_in_currency", "price_change_percentage_7d_in_currency",
                  "price_change_percentage_30d_in_currency", "ath", "ath_change_percentage"]
        if path.endswith(".json"):
            with open(path, "w", encoding="utf-8") as f:
                json.dump([{k: x.get(k) for k in fields} for x in coins], f, indent=2, ensure_ascii=False)
        else:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                w.writerows(coins)
        print(cyan("  Export enregistré : {}".format(path)))

    def cmd_info(self, query):
        coin = self.resolve(query)
        cur = self.currency
        print()
        print(bold("  {} ({}) — rang #{}".format(coin["name"], coin["symbol"].upper(), coin["market_cap_rank"])))
        print("  Prix actuel      : {}".format(bold(fmt_price(coin["current_price"], cur))))
        print("  Variation 1h     : {}".format(fmt_pct(coin.get("price_change_percentage_1h_in_currency"), 0)))
        print("  Variation 24h    : {}".format(fmt_pct(coin.get("price_change_percentage_24h_in_currency"), 0)))
        print("  Variation 7j     : {}".format(fmt_pct(coin.get("price_change_percentage_7d_in_currency"), 0)))
        print("  Variation 30j    : {}".format(fmt_pct(coin.get("price_change_percentage_30d_in_currency"), 0)))
        print("  Plus haut / bas 24h : {} / {}".format(
            fmt_price(coin.get("high_24h"), cur), fmt_price(coin.get("low_24h"), cur)))
        print("  Capitalisation   : {}".format(fmt_big(coin["market_cap"], cur)))
        print("  Volume 24h       : {}".format(fmt_big(coin["total_volume"], cur)))
        if coin.get("ath"):
            print("  Record (ATH)     : {} ({} depuis)".format(
                fmt_price(coin["ath"], cur), fmt_pct(coin.get("ath_change_percentage"), 0)))
        spark = (coin.get("sparkline_in_7d") or {}).get("price") or []
        if spark:
            print("  7 derniers jours : {}".format(sparkline(spark, 40)))

    def cmd_forecast(self, query, horizon=7, history_days=180, show_models=False):
        if not 1 <= horizon <= 90:
            raise CoinGeckoError("L'horizon doit être compris entre 1 et 90 jours.")
        coin = self.resolve(query)
        cur = self.currency
        hist = self.api.history(coin["id"], cur, history_days)
        prices = [p for _, p in hist]
        if len(prices) < 40:
            raise CoinGeckoError("Historique insuffisant pour {} ({} points).".format(coin["name"], len(prices)))

        fc, per_model, sigma = ensemble_forecast(prices, horizon)
        last = prices[-1]
        bt = backtest(prices, horizon)

        print()
        print(bold("  Prévision {} ({}) sur {} jour(s)".format(coin["name"], coin["symbol"].upper(), horizon)))
        print(dim("  Historique : {} jours · {}".format(len(prices), sparkline(prices, 50))))
        print("  Prix actuel : {}".format(bold(fmt_price(last, cur))))
        print()

        # Indicateurs techniques
        s20, s50 = sma(prices, 20), sma(prices, 50)
        r = rsi(prices)
        print(bold("  Indicateurs"))
        if s20 and s50:
            trend = green("haussière") if s20 > s50 else red("baissière")
            print("  Moyennes mobiles 20j / 50j : {} / {} → tendance {}".format(
                fmt_price(s20, cur), fmt_price(s50, cur), trend))
        if r is not None:
            zone = red("surachat") if r > 70 else green("survente") if r < 30 else "neutre"
            print("  RSI 14j : {:.1f} ({})".format(r, zone))
        print("  Volatilité quotidienne : {:.2f}% (annualisée ≈ {:.0f}%)".format(
            sigma * 100, sigma * math.sqrt(365) * 100))
        print()

        # Tableau de prévision
        print(bold("  " + "  ".join([pad("Date", 12), pad("Estimation", 16, True), pad("Variation", 10, True),
                                     pad("Bas (90%)", 16, True), pad("Haut (90%)", 16, True)])))
        print(dim("  " + "─" * 78))
        today = hist[-1][0].date()
        rows = fc if horizon <= 14 else [x for x in fc if x["h"] % 7 == 0 or x["h"] == horizon]
        for x in rows:
            d = today + timedelta(days=x["h"])
            print("  " + "  ".join([
                pad(d.strftime("%d/%m/%Y"), 12),
                pad(fmt_price(x["mid"], cur), 16, True),
                pad(fmt_pct((x["mid"] / last - 1) * 100), 10, True),
                pad(dim(fmt_price(x["low"], cur)), 16, True),
                pad(dim(fmt_price(x["high"], cur)), 16, True),
            ]))
        print()

        final = fc[-1]["mid"]
        change = (final / last - 1) * 100
        verdict = green("HAUSSE") if change > 1 else red("BAISSE") if change < -1 else yellow("STABLE")
        print("  Tendance estimée à {} j : {} ({})".format(horizon, verdict, fmt_pct(change, 0)))

        if show_models:
            print()
            print(bold("  Détail par modèle (J+{})".format(horizon)))
            labels = {"linear": "Régression linéaire 30j", "holt": "Holt (tendance amortie)",
                      "drift": "Marche aléatoire + dérive"}
            for name, vals in per_model.items():
                print("  {:<28} {}  {}".format(labels[name], pad(fmt_price(vals[-1], cur), 16, True),
                                               fmt_pct((vals[-1] / last - 1) * 100)))

        if bt:
            print()
            print(bold("  Fiabilité historique (backtest sur {} périodes de {} j)".format(bt["folds"], horizon)))
            better = bt["mape"] < bt["naive_mape"]
            print("  Erreur moyenne du modèle : {:.1f}%  (modèle naïf « prix inchangé » : {:.1f}%) {}".format(
                bt["mape"], bt["naive_mape"], green("✓") if better else red("✗")))
            print("  Bonne direction : {:.0f}% des cas".format(bt["direction"]))

        print()
        print(dim("  ⚠  Estimation statistique (ensemble linéaire + Holt + dérive) basée uniquement sur les prix"))
        print(dim("     passés. Les cryptomonnaies sont très volatiles : ceci n'est pas un conseil financier."))

    def cmd_watch(self, symbols, interval=60, threshold=2.0):
        print(bold("  Surveillance toutes les {}s — alerte si variation ≥ {}% entre deux relevés.".format(
            interval, threshold)))
        print(dim("  Ctrl+C pour arrêter."))
        prev = {}
        try:
            while True:
                coins = self.markets(refresh=True)
                watched = [self.resolve(s) for s in symbols] if symbols else coins[:10]
                stamp = datetime.now().strftime("%H:%M:%S")
                print()
                print(dim("  [{}]".format(stamp)))
                for coin in watched:
                    p = coin["current_price"]
                    line = "  {:<8} {}  24h {}".format(
                        coin["symbol"].upper(), pad(fmt_price(p, self.currency), 16, True),
                        fmt_pct(coin.get("price_change_percentage_24h_in_currency")))
                    old = prev.get(coin["id"])
                    if old:
                        delta = (p / old - 1) * 100
                        line += "   Δ {}".format(fmt_pct(delta))
                        if abs(delta) >= threshold:
                            line += "  " + bold(yellow("⚡ ALERTE"))
                            sys.stdout.write("\a")
                    prev[coin["id"]] = p
                    print(line)
                time.sleep(max(interval, 30))
        except KeyboardInterrupt:
            print("\n  Surveillance arrêtée.")

    # -- mode interactif --------------------------------------------------------

    HELP = """
  Commandes disponibles :
    top [n] [tri]           Classement (tri : rank, gain, loss, volume, 7d)
    info <crypto>           Détail d'une crypto (symbole, nom ou id : btc, ethereum…)
    prevision <crypto> [j]  Prévision sur j jours (défaut 7, max 90)
    modeles <crypto> [j]    Prévision + détail de chaque modèle
    surveiller [cryptos…]   Surveillance en continu avec alertes
    export <fichier>        Exporte le top en .csv ou .json
    devise <code>           Change la devise (eur, usd, gbp…)
    aide                    Affiche cette aide
    quitter                 Quitte l'agent
"""

    def repl(self):
        print(bold(cyan("\n  🤖 Agent Crypto — top {} par capitalisation".format(self.limit))))
        print(self.HELP)
        while True:
            try:
                raw = input(cyan("crypto> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw:
                continue
            parts = raw.split()
            cmd, args = parts[0].lower(), parts[1:]
            try:
                if cmd in ("quitter", "exit", "quit", "q"):
                    break
                elif cmd in ("aide", "help", "?"):
                    print(self.HELP)
                elif cmd == "top":
                    n = next((int(a) for a in args if a.isdigit()), None)
                    sort = next((a for a in args if not a.isdigit()), "rank")
                    self.cmd_top(n, sort)
                elif cmd in ("info", "prix", "price"):
                    self.cmd_info(" ".join(args))
                elif cmd in ("prevision", "prévision", "forecast", "modeles", "modèles"):
                    if not args:
                        print("  Usage : prevision <crypto> [jours]")
                        continue
                    days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 7
                    self.cmd_forecast(args[0], days, show_models=cmd.startswith("mod"))
                elif cmd in ("surveiller", "watch"):
                    self.cmd_watch(args)
                elif cmd == "export":
                    self._export(self.markets(), args[0] if args else "top100.csv")
                elif cmd == "devise" and args:
                    self.currency = args[0].lower()
                    self._markets = None
                    print("  Devise : {}".format(self.currency.upper()))
                else:
                    # Raccourci : taper directement un symbole affiche ses infos
                    self.cmd_info(raw)
            except CoinGeckoError as e:
                print(red("  Erreur : {}".format(e)))
            except ValueError as e:
                print(red("  Paramètre invalide : {}".format(e)))
        print("  À bientôt 👋")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main():
    parser = argparse.ArgumentParser(
        description="Agent crypto : cours du top 100 par capitalisation et prévisions.")
    parser.add_argument("--devise", "-d", default="eur", help="Devise de cotation (eur, usd…)")
    parser.add_argument("--limite", type=int, default=100, help="Taille du classement (défaut 100)")
    sub = parser.add_subparsers(dest="cmd")

    p_top = sub.add_parser("top", help="Afficher le classement")
    p_top.add_argument("-n", type=int, help="Nombre de lignes affichées")
    p_top.add_argument("--tri", default="rank", choices=["rank", "gain", "loss", "volume", "7d"])
    p_top.add_argument("--export", help="Fichier .csv ou .json")

    p_info = sub.add_parser("info", help="Détail d'une crypto")
    p_info.add_argument("crypto")

    p_fc = sub.add_parser("prevision", help="Prévision de prix")
    p_fc.add_argument("crypto")
    p_fc.add_argument("--jours", "-j", type=int, default=7, help="Horizon (1-90 jours)")
    p_fc.add_argument("--historique", type=int, default=180, help="Jours d'historique utilisés")
    p_fc.add_argument("--modeles", action="store_true", help="Afficher le détail par modèle")

    p_w = sub.add_parser("surveiller", help="Surveillance en continu avec alertes")
    p_w.add_argument("cryptos", nargs="*")
    p_w.add_argument("--intervalle", type=int, default=60)
    p_w.add_argument("--seuil", type=float, default=2.0, help="Seuil d'alerte en %%")

    args = parser.parse_args()
    agent = CryptoAgent(args.devise, args.limite)

    try:
        if args.cmd == "top":
            agent.cmd_top(args.n, args.tri, args.export)
        elif args.cmd == "info":
            agent.cmd_info(args.crypto)
        elif args.cmd == "prevision":
            agent.cmd_forecast(args.crypto, args.jours, args.historique, args.modeles)
        elif args.cmd == "surveiller":
            agent.cmd_watch(args.cryptos, args.intervalle, args.seuil)
        else:
            agent.repl()
    except CoinGeckoError as e:
        print(red("Erreur : {}".format(e)), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
