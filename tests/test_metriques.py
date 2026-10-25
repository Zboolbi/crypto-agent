"""Tests de l'export SQLite lu par Grafana."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import metriques


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name

        def put(name, obj):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                json.dump(obj, f)

        put("etat_scalp_papier.json", {"maj": 1e10, "capital_initial": 10000, "valeur": 9900.0, "realise": -100.0,
                                       "latent": 0.0, "trades": 3, "gagnants": 1, "flux_msg_s": 300, "retard_ms": 200,
                                       "traitement_us": 30, "reconnexions": 0, "contrats": 100, "fantomes_actifs": 5,
                                       "fantomes": 1000,
                                       "positions": [{"crypto": "BTC", "variante": "elan/cinq_min/marche",
                                                      "strategie": "longue", "etat": "ouvert", "notionnel": 1000,
                                                      "entree": 1.0, "actuel": 1.0, "pnl": -1.0}]})
        put("etat_tendance_papier.json", {"capital_initial": 10000, "valeur": 10300.0, "realise": 0.0, "latent": 300.0,
                                          "trades": 0, "gagnants": 0, "positions": []})
        put("etat_funding_papier.json", {"capital_initial": 10000, "valeur": 9990.0, "cash": 8000.0, "positions": [],
                                         "stats": {"pnl": 0.0, "trades": 0, "gagnants": 0}})
        with open(os.path.join(d, "journal_scalp_papier.csv"), "w", encoding="utf-8") as f:
            f.write("date,crypto,sens,profil,proba,entree,sortie,notionnel,duree_s,brut_bps,net_bps,pnl,raison\n")
            f.write("2026-10-25 10:00:00,BTC,short,explo:elan/cinq_min/marche,0.4,1,1,1000,300,20,13,1.3,objectif\n")
            f.write("2026-10-25 10:01:00,ETH,short,elan/eclair/limite,0.5,1,1,1000,10,-8,-12,-1.2,stop\n")
        self.patch = mock.patch.object(metriques, "HERE", d)
        self.patch.start()
        self.db = os.path.join(d, "agent.db")

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_export_is_complete_and_idempotent(self):
        exp = metriques.Exporter(self.db)
        exp.export(now=1000)
        exp.export(now=1005)  # les trades ne sont pas dupliqués
        db = sqlite3.connect(self.db)
        values = dict(db.execute("SELECT strategie, valeur - depart FROM valeurs WHERE ts = 1005").fetchall())
        self.assertEqual(values, {"scalping": -100.0, "tendance": 300.0, "financement": -10.0, "total": 190.0})
        latent = db.execute("SELECT latent FROM valeurs WHERE ts = 1005 AND strategie = 'financement'").fetchone()[0]
        self.assertEqual(latent, -10.0, "latent du financement = valeur − départ − réalisé")
        trades = db.execute("SELECT strategie, exploration FROM trades ORDER BY ts").fetchall()
        self.assertEqual(trades, [("longue", 1), ("court", 0)])
        self.assertEqual(db.execute("SELECT strategie FROM positions").fetchall(), [("longue",)])


if __name__ == "__main__":
    unittest.main()
