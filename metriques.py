#!/usr/bin/env python3
"""
Exporte en continu les données de l'agent dans une base SQLite lue par Grafana.

Toutes les 5 secondes : valeur de chaque portefeuille (scalping, tendance, financement, total),
santé du flux temps réel, positions ouvertes, nouveaux trades fermés et état des modèles.
La base garde l'historique (30 jours), ce qui permet à Grafana de tracer des courbes.

    python3 metriques.py              # boucle continue (service com.cryptoagent.metriques)
    python3 metriques.py --une-fois   # un seul export, pour tester
"""

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime

from learner import OnlineLogit

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "donnees", "agent.db")
PERIOD_S = 5
RETENTION_DAYS = 30
DEFAULT_LONG_PROFILES = ("trente_s", "une_min", "cinq_min", "dix_min")

SCHEMA = """
CREATE TABLE IF NOT EXISTS valeurs (
    ts INTEGER NOT NULL, strategie TEXT NOT NULL, valeur REAL, depart REAL, realise REAL, latent REAL,
    trades INTEGER, gagnants INTEGER, disponible REAL, PRIMARY KEY (ts, strategie));
CREATE TABLE IF NOT EXISTS sante (
    ts INTEGER PRIMARY KEY, flux_msg_s REAL, retard_ms REAL, traitement_us REAL, reconnexions INTEGER,
    contrats INTEGER, fantomes_actifs INTEGER, exemples INTEGER, positions INTEGER, en_ligne INTEGER);
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY, ts INTEGER NOT NULL, strategie TEXT, crypto TEXT, sens TEXT, variante TEXT,
    exploration INTEGER, duree_s REAL, brut_bps REAL, net_bps REAL, pnl REAL, raison TEXT);
CREATE INDEX IF NOT EXISTS trades_ts ON trades (ts);
CREATE TABLE IF NOT EXISTS positions (
    strategie TEXT, crypto TEXT, variante TEXT, etat TEXT, notionnel REAL, entree REAL, actuel REAL,
    pnl REAL, maj INTEGER);
CREATE TABLE IF NOT EXISTS modeles (
    variante TEXT PRIMARY KEY, strategie TEXT, exemples INTEGER, gagnants REAL, annonce REAL, reel REAL,
    net_moyen REAL, execution REAL, validation_bps REAL, validation_n INTEGER, valide INTEGER, maj INTEGER);
"""


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


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def epoch(local_date):
    """« 2026-10-25 10:20:00 » (heure locale du journal) → secondes depuis 1970."""
    return int(datetime.strptime(local_date, "%Y-%m-%d %H:%M:%S").timestamp())


def row_id(source, row):
    return hashlib.sha1(f"{source}|{sorted(row.items())}".encode(), usedforsecurity=False).hexdigest()


def long_profiles(cfg):
    return {p["nom"] for p in cfg.get("scalp_profils_longs", [])} or set(DEFAULT_LONG_PROFILES)


def scalp_strategy(variant, longs):
    parts = variant.replace("explo:", "").split("/")
    return "longue" if len(parts) > 1 and parts[1] in longs else "court"


class Exporter:
    def __init__(self, db_path=DB_PATH, mode="papier"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)
        cols = {row[1] for row in self.db.execute("PRAGMA table_info(valeurs)")}
        if "disponible" not in cols:  # base créée avant l'ajout des fonds disponibles
            self.db.execute("ALTER TABLE valeurs ADD COLUMN disponible REAL")
        self.mode = mode
        self.last_purge = 0.0

    def export(self, now=None):
        now = int(now or time.time())
        cfg = read_json("config_trading.json") or {}
        longs = long_profiles(cfg)
        scalp = read_json(f"etat_scalp_{self.mode}.json")
        trend = read_json(f"etat_tendance_{self.mode}.json")
        fund = read_json(f"etat_funding_{self.mode}.json")
        with self.db:
            self._values(now, scalp, trend, fund, cfg)
            self._health(now, scalp)
            self._positions(now, scalp, trend, fund)
            self._trades(longs)
            self._models(now, longs, cfg)
            if now - self.last_purge > 3600:
                limit = now - RETENTION_DAYS * 86400
                for table in ("valeurs", "sante", "trades"):
                    self.db.execute(f"DELETE FROM {table} WHERE ts < ?", (limit,))  # nom de table fixe, pas d'entrée externe
                self.last_purge = now

    def _values(self, now, scalp, trend, fund, cfg):
        """Une ligne par portefeuille, plus le total. « disponible » = valeur moins le capital immobilisé."""
        rows = []
        if scalp:
            engaged = sum(p["notionnel"] for p in scalp.get("positions", []))  # positions et ordres en cours
            rows.append(("scalping", scalp["valeur"], scalp["capital_initial"], scalp["realise"], scalp["latent"],
                         scalp["trades"], scalp["gagnants"], max(0.0, scalp["valeur"] - engaged)))
        if trend:
            # Positions à levier : seule la marge (taille / levier) est immobilisée.
            margin = sum(p["notionnel"] for p in trend.get("positions", [])) / max(1.0, cfg.get("levier_max", 1.0))
            rows.append(("tendance", trend["valeur"], trend["capital_initial"], trend["realise"], trend["latent"],
                         trend["trades"], trend["gagnants"], max(0.0, trend["valeur"] - margin)))
        if fund:
            st = fund["stats"]
            latent = fund["valeur"] - fund["capital_initial"] - st["pnl"]  # positions ouvertes, financement compris
            rows.append(("financement", fund["valeur"], fund["capital_initial"], st["pnl"], latent,
                         st["trades"], st["gagnants"], fund["cash"]))
        if rows:
            total = [sum(r[i] for r in rows) for i in range(1, 8)]
            rows.append(("total", *total))
        self.db.executemany("INSERT OR REPLACE INTO valeurs (ts, strategie, valeur, depart, realise, latent, trades, "
                            "gagnants, disponible) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [(now, *r) for r in rows])

    def _health(self, now, scalp):
        if not scalp:
            return
        online = int(time.time() - scalp["maj"] < 15)
        self.db.execute("INSERT OR REPLACE INTO sante VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            now, scalp.get("flux_msg_s"), scalp.get("retard_ms"), scalp.get("traitement_us"),
            scalp.get("reconnexions"), scalp.get("contrats"), scalp.get("fantomes_actifs"),
            scalp.get("fantomes"), len(scalp.get("positions", [])), online))

    def _positions(self, now, scalp, trend, fund):
        rows = [(p.get("strategie", "court"), p["crypto"], p["variante"], p["etat"], p["notionnel"],
                 p.get("entree"), p.get("actuel"), p.get("pnl"), now) for p in (scalp or {}).get("positions", [])]
        rows += [("tendance", p["crypto"], p["sens"], "ouvert", p["notionnel"], p["entree"], p["actuel"], p["pnl"], now)
                 for p in (trend or {}).get("positions", [])]
        rows += [("financement", p["symbole"], "comptant + perpétuel", "ouvert", p["notionnel"], None, None,
                  p["financement"], now) for p in (fund or {}).get("positions", [])]
        self.db.execute("DELETE FROM positions")
        self.db.executemany("INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def _trades(self, longs):
        rows = []
        for r in read_csv(f"journal_scalp_{self.mode}.csv"):
            variant = r.get("profil", "")
            rows.append((row_id("scalp", r), epoch(r["date"]), scalp_strategy(variant, longs), r["crypto"], r["sens"],
                         variant.replace("explo:", ""), int(variant.startswith("explo:")), num(r["duree_s"]),
                         num(r["brut_bps"]), num(r["net_bps"]), num(r["pnl"]), r["raison"]))
        rows.extend((row_id("tendance", r), epoch(r["date"]), "tendance", r["crypto"], r["sens"], "", 0,
                     None, None, None, num(r["pnl"]), r["raison"])
                    for r in read_csv(f"journal_{self.mode}.csv") if r["action"] == "FERMETURE")
        rows.extend((row_id("financement", r), epoch(r["date"]), "financement", r["symbole"], "carry", "", 0,
                     None, None, None, num(r["pnl"]), r["raison"])
                    for r in read_csv(f"journal_funding_{self.mode}.csv") if r["action"] == "FERMETURE")
        self.db.executemany("INSERT OR IGNORE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def _models(self, now, longs, cfg):
        state = read_json(f"portefeuille_scalp_{self.mode}.json") or {}
        n_val = cfg.get("scalp_validation_min", 50)
        min_val = cfg.get("scalp_validation_min_bps", 1.0)
        rows = []
        for key, d in state.get("modeles", {}).items():
            m = OnlineLogit.from_dict(d)
            if not m.seen:
                continue
            said, real = m.calibration()
            hist = state.get("validation", {}).get(key, [])
            recent = hist[-n_val:]
            val = sum(recent) / len(recent) if recent else None
            rows.append((key, scalp_strategy(key, longs), m.seen, m.base_rate, said, real, m.avg_net,
                         state.get("remplissage", {}).get(key) if key.endswith("limite") else None,
                         val, len(hist), int(len(hist) >= n_val and val is not None and val >= min_val), now))
        self.db.execute("DELETE FROM modeles")
        self.db.executemany("INSERT INTO modeles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def main():
    p = argparse.ArgumentParser(description="Exporte les données de l'agent vers SQLite pour Grafana.")
    p.add_argument("--une-fois", action="store_true")
    p.add_argument("--mode", choices=["papier", "testnet", "reel"], default="papier")
    args = p.parse_args()
    exp = Exporter(mode=args.mode)
    if args.une_fois:
        exp.export()
        print(f"Exporté dans {DB_PATH}")
        return
    print(f"Export vers {DB_PATH} toutes les {PERIOD_S} s (Ctrl+C pour arrêter).", flush=True)
    while True:
        try:
            exp.export()
        except (sqlite3.Error, KeyError, ValueError, OSError) as e:  # fichier en cours d'écriture : prochain tour
            print(f"Export ignoré : {e}", flush=True)
        time.sleep(PERIOD_S)


if __name__ == "__main__":
    main()
