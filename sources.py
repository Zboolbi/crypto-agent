"""
Sources de données de marché — API publiques de la liste public-apis (section Cryptocurrency),
toutes utilisables sans clé :

  CoinGecko        classement top 100 par capitalisation (source principale)
  Coinpaprika      classement de secours
  Coinlore         classement de secours n°2
  Binance Futures  contrats perpétuels, bougies, prix mark, taux de financement (source de trading)
  Bybit            mêmes données, en secours de Binance (ou en principal si l'exchange est Bybit)
  DefiLlama        contrôle croisé du prix avant chaque ouverture
  Kraken           contrôle croisé de secours

Le flux temps réel (WebSocket Binance Futures : transactions et meilleur prix) alimente le scalping.

Chaque appel a un délai maximal, des nouvelles tentatives et un repli sur la source suivante.
"""

import base64
import json
import os
import socket
import ssl
import struct

import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = "crypto-agent/2.0"

INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}


class DataError(Exception):
    pass


def http_json(url, params=None, timeout=10, retries=3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    delay = 2
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (418, 429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(getattr(e, "reason", e))
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    raise DataError("{} : {}".format(url.split("?")[0], last))


# --------------------------------------------------------------------------- #
# Classement top 100 (CoinGecko → Coinpaprika → Coinlore)
# --------------------------------------------------------------------------- #


def _coin(cid, symbol, name, rank, price, mcap, volume, ch24, ch7):
    """Format commun, calqué sur les champs CoinGecko utilisés par l'agent."""
    return {
        "id": cid, "symbol": symbol.lower(), "name": name, "market_cap_rank": rank,
        "current_price": price, "market_cap": mcap, "total_volume": volume,
        "price_change_percentage_24h_in_currency": ch24,
        "price_change_percentage_7d_in_currency": ch7,
    }


def top_coingecko(limit, api):
    return [_coin(c["id"], c["symbol"], c["name"], c["market_cap_rank"], c["current_price"],
                  c["market_cap"], c["total_volume"], c.get("price_change_percentage_24h_in_currency"),
                  c.get("price_change_percentage_7d_in_currency"))
            for c in api.top_markets("usd", limit)]


def top_coinpaprika(limit):
    data = http_json("https://api.coinpaprika.com/v1/tickers", {"limit": limit})
    out = []
    for c in sorted(data, key=lambda x: x.get("rank") or 9999)[:limit]:
        q = c["quotes"]["USD"]
        out.append(_coin(c["id"], c["symbol"], c["name"], c["rank"], q["price"], q["market_cap"],
                         q["volume_24h"], q.get("percent_change_24h"), q.get("percent_change_7d")))
    return out


def top_coinlore(limit):
    data = http_json("https://api.coinlore.net/api/tickers/", {"start": 0, "limit": limit})["data"]

    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return [_coin(c["nameid"], c["symbol"], c["name"], c["rank"], f(c["price_usd"]), f(c["market_cap_usd"]),
                  f(c["volume24"]), f(c["percent_change_24h"]), f(c["percent_change_7d"])) for c in data]


def top_coins(limit=100, coingecko=None):
    errors = []
    sources = [("CoinGecko", lambda: top_coingecko(limit, coingecko))] if coingecko else []
    sources += [("Coinpaprika", lambda: top_coinpaprika(limit)), ("Coinlore", lambda: top_coinlore(limit))]
    for name, fn in sources:
        try:
            coins = fn()
            if coins:
                return coins, name
        except Exception as e:
            errors.append(f"{name} ({e})")
    raise DataError("Aucune source de classement disponible : " + ", ".join(errors))


# --------------------------------------------------------------------------- #
# Places de marché à terme (Binance Futures, Bybit)
# --------------------------------------------------------------------------- #


def _split_base(base):
    """1000PEPE → (PEPE, 1000) : certains contrats portent sur un lot de jetons."""
    for mult in (1000000, 10000, 1000, 100):
        p = str(mult)
        if base.startswith(p) and len(base) > len(p):
            return base[len(p):], mult
    return base, 1


class BinanceFutures:
    name = "binance"
    BASE = "https://fapi.binance.com/fapi/v1"

    def contracts(self):
        info = http_json(self.BASE + "/exchangeInfo")
        out = {}
        for s in info["symbols"]:
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
                token, mult = _split_base(s["baseAsset"])
                # On garde le contrat le plus simple (multiplicateur 1) s'il en existe plusieurs.
                if token not in out or mult < out[token]["mult"]:
                    tick = next((float(f["tickSize"]) for f in s.get("filters", [])
                                 if f.get("filterType") == "PRICE_FILTER"), 0.0)
                    out[token] = {"contract": s["symbol"], "base": s["baseAsset"], "mult": mult, "tick": tick}
        return out

    def volumes_24h(self):
        """{contrat: volume échangé sur 24 h, en $}"""
        return {d["symbol"]: float(d["quoteVolume"]) for d in http_json(self.BASE + "/ticker/24hr")}

    def tickers(self):
        """{contrat: {'price': prix mark, 'funding': taux de financement par période}}"""
        return {d["symbol"]: {"price": float(d["markPrice"]), "funding": float(d["lastFundingRate"] or 0)}
                for d in http_json(self.BASE + "/premiumIndex")}

    def candles(self, contract, interval="15m", limit=200, end_ms=None):
        params = {"symbol": contract, "interval": interval, "limit": min(limit, 1500)}
        if end_ms:
            params["endTime"] = end_ms
        rows = http_json(self.BASE + "/klines", params)
        return [{"t": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                 "v": float(r[7])} for r in rows]


class BybitFutures:
    name = "bybit"
    BASE = "https://api.bybit.com/v5/market"

    def contracts(self):
        out, cursor = {}, ""
        while True:
            params = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            res = http_json(self.BASE + "/instruments-info", params)["result"]
            for s in res["list"]:
                if s.get("contractType") == "LinearPerpetual" and s.get("quoteCoin") == "USDT" \
                        and s.get("status") == "Trading":
                    token, mult = _split_base(s["baseCoin"])
                    if token not in out or mult < out[token]["mult"]:
                        tick = float((s.get("priceFilter") or {}).get("tickSize") or 0)
                        out[token] = {"contract": s["symbol"], "base": s["baseCoin"], "mult": mult, "tick": tick}
            cursor = res.get("nextPageCursor")
            if not cursor:
                return out

    def volumes_24h(self):
        rows = http_json(self.BASE + "/tickers", {"category": "linear"})["result"]["list"]
        return {r["symbol"]: float(r.get("turnover24h") or 0) for r in rows}

    def tickers(self):
        rows = http_json(self.BASE + "/tickers", {"category": "linear"})["result"]["list"]
        return {r["symbol"]: {"price": float(r.get("markPrice") or r["lastPrice"]),
                              "funding": float(r.get("fundingRate") or 0)} for r in rows}

    def candles(self, contract, interval="15m", limit=200, end_ms=None):
        minutes = INTERVAL_MINUTES[interval]
        params = {"category": "linear", "symbol": contract, "interval": str(minutes), "limit": min(limit, 1000)}
        if end_ms:
            params["end"] = end_ms
        rows = http_json(self.BASE + "/kline", params)["result"]["list"]
        return [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                 "v": float(r[6])} for r in reversed(rows)]


VENUES = {"binance": BinanceFutures, "bybit": BybitFutures}


class MarketData:
    """Données de la place de marché principale, avec repli automatique sur l'autre."""

    def __init__(self, venue="binance"):
        primary = VENUES.get(venue, BinanceFutures)()
        others = [cls() for name, cls in VENUES.items() if name != primary.name]
        self.venues = [primary, *others]
        self._contracts = None
        self._contracts_ts = 0
        self._venue_of_contracts = None

    def _try(self, method, *args, **kwargs):
        errors = []
        for v in self.venues:
            try:
                return v, getattr(v, method)(*args, **kwargs)
            except Exception as e:
                errors.append(f"{v.name} ({e})")
        raise DataError("Données de marché indisponibles : " + ", ".join(errors))

    def contracts(self):
        if self._contracts is None or time.time() - self._contracts_ts > 3600:
            venue, self._contracts = self._try("contracts")
            self._venue_of_contracts = venue
            self._contracts_ts = time.time()
        return self._contracts

    @property
    def venue(self):
        return self._venue_of_contracts or self.venues[0]

    def tickers(self):
        # Les noms de contrats doivent venir de la même place que la liste des contrats.
        self.contracts()
        return self.venue.tickers()

    def volumes_24h(self):
        self.contracts()
        return self.venue.volumes_24h()

    def secondary_tickers(self):
        """Prix des contrats sur une autre place, pour contrôle croisé ({} si indisponible)."""
        for v in self.venues:
            if v is not self.venue:
                try:
                    return v.tickers()
                except Exception:
                    continue
        return {}

    def candles(self, contract, interval="15m", limit=200):
        self.contracts()
        return self.venue.candles(contract, interval, limit)

    def candles_many(self, contracts, interval="15m", limit=200, workers=8):
        def one(c):
            try:
                return c, self.candles(c, interval, limit)
            except Exception:
                return c, None
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return dict(pool.map(one, contracts))

    def history(self, contract, interval="1h", bars=3000):
        """Historique long, par pages successives (le plus ancien en premier)."""
        self.contracts()
        out, end = [], None
        while len(out) < bars:
            page = self.venue.candles(contract, interval, min(1000, bars - len(out)), end)
            if not page:
                break
            out = page + out
            end = page[0]["t"] - 1
            if len(page) < 2:
                break
        seen, dedup = set(), []
        for k in out:
            if k["t"] not in seen:
                seen.add(k["t"])
                dedup.append(k)
        return dedup[-bars:]


# --------------------------------------------------------------------------- #
# Contrôle croisé du prix (DefiLlama → Kraken)
# --------------------------------------------------------------------------- #


def reference_prices(coins, ranking_source="CoinGecko"):
    """Prix de référence indépendant par jeton {id: prix $}. Vide si indisponible.

    DefiLlama n'accepte que les identifiants CoinGecko ; sinon on interroge Kraken.
    """
    if ranking_source == "CoinGecko":
        ids = ",".join("coingecko:" + c["id"] for c in coins)
        try:
            data = http_json("https://coins.llama.fi/prices/current/" + ids, retries=2)["coins"]
            return {k.split(":", 1)[1]: v["price"] for k, v in data.items() if v.get("confidence", 1) >= 0.8}
        except Exception:
            pass
    out = {}
    for c in coins:
        sym = "XBT" if c["symbol"].upper() == "BTC" else c["symbol"].upper()
        try:
            res = http_json("https://api.kraken.com/0/public/Ticker", {"pair": sym + "USD"}, retries=1)
            if not res.get("error"):
                out[c["id"]] = float(next(iter(res["result"].values()))["c"][0])
        except Exception:
            continue
    return out




# --------------------------------------------------------------------------- #
# Flux temps réel (WebSocket)
# --------------------------------------------------------------------------- #


class WebSocket:
    """Client WebSocket minimal (RFC 6455) : réception de messages texte, ping/pong automatique."""

    def __init__(self, url, timeout=30):
        u = urllib.parse.urlparse(url)
        host, port = u.hostname, u.port or 443
        raw = socket.create_connection((host, port), timeout=timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + ("?" + u.query if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: {USER_AGENT}\r\n\r\n")
                          .encode())
        self.buf = bytearray()
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        end = self.buf.index(b"\r\n\r\n")
        status = bytes(self.buf[:end]).split(b"\r\n")[0]
        del self.buf[:end + 4]
        if b" 101 " not in status:
            raise DataError("WebSocket refusé : {}".format(status.decode(errors="replace")))

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("connexion WebSocket fermée")
        self.buf += chunk

    def _read(self, n):
        while len(self.buf) < n:
            self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _send(self, opcode, payload=b""):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", n)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", n)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self):
        message = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:
                self._send(0xA, payload)
            elif opcode == 0x8:
                raise ConnectionError("WebSocket fermé par le serveur")
            elif opcode in (0x0, 0x1, 0x2):
                message += payload
                if fin:
                    return message.decode("utf-8")

    def close(self):
        try:
            self._send(0x8)
            self.sock.close()
        except Exception:
            pass


STREAM_CHUNK = 50  # contrats par connexion WebSocket : charge et gigue réduites par connexion


def binance_stream_urls(contracts, chunk=STREAM_CHUNK):
    """Binance sépare ses flux : transactions sur /market, carnet sur /public.

    Le carnet passe par depth5@100ms (10 mises à jour par seconde, 5 niveaux) plutôt que
    bookTicker (chaque changement) : 4× moins de messages, et la taille des files d'attente.
    Les contrats sont répartis par paquets de `chunk`, une connexion par paquet et par flux.
    """
    names = [c.lower() for c in contracts]
    base = "wss://fstream.binance.com/{}/stream?streams="
    groups = [names[i:i + chunk] for i in range(0, len(names), chunk)]
    urls = {}
    for i, g in enumerate(groups, 1):
        tag = f" {i}/{len(groups)}" if len(groups) > 1 else ""
        urls["transactions" + tag] = base.format("market") + "/".join(n + "@aggTrade" for n in g)
        urls["carnet" + tag] = base.format("public") + "/".join(n + "@depth5@100ms" for n in g)
    return urls


def binance_clock_offset_ms(samples=5):
    """Décalage horloge serveur − horloge locale, pour mesurer la latence réelle du flux.

    Comme NTP : plusieurs mesures, on garde celle dont l'aller-retour est le plus court
    (la première, qui inclut l'établissement de la connexion, est souvent faussée).
    """
    best = None
    for _ in range(samples):
        t0 = time.time() * 1000
        server = http_json(BinanceFutures.BASE + "/time")["serverTime"]
        t1 = time.time() * 1000
        if best is None or t1 - t0 < best[0]:
            best = (t1 - t0, server - (t0 + t1) / 2)
    return best[1]
