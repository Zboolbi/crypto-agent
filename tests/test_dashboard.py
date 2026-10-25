"""Tests du tableau de bord : assemblage des données et routes HTTP."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        now = time.time()
        with open(os.path.join(d, "etat_scalp_papier.json"), "w") as f:
            json.dump({"maj": now, "capital_initial": 10000, "valeur": 10012.5, "realise": 10.0, "latent": 2.5,
                       "trades": 2, "gagnants": 1, "frais": 3.0, "trades_par_heure": 4.0, "positions": []}, f)
        with open(os.path.join(d, "courbe_scalp_papier.csv"), "w") as f:
            f.write("t,valeur,realise,latent,trades\n")
            for i in range(2000):
                f.write(f"{now - 7200 + i * 3.6},{10000 + i},0,0,0\n")
        with open(os.path.join(d, "journal_scalp_papier.csv"), "w") as f:
            f.write("date,crypto,sens,profil,proba,entree,sortie,notionnel,duree_s,brut_bps,net_bps,pnl,raison\n")
            f.write("2026-10-25 00:00:00,BTC,short,elan/rapide/limite,0.6,100,99.8,1000,12,20,16,1.6,objectif\n")
            f.write("2026-10-25 00:01:00,ETH,short,elan/rapide/marche,0.55,100,100.2,1000,30,-20,-27,-2.7,stop\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_reads_live_files(self):
        out = dashboard.build("papier", 3600, base=self.tmp.name)
        self.assertTrue(out["en_ligne"])
        self.assertEqual(out["solde"]["valeur"], 10012.5)
        self.assertLessEqual(len(out["courbe"]), dashboard.MAX_POINTS + 1)
        self.assertGreater(out["courbe"][0][0], time.time() - 3700)  # fenêtre d'une heure respectée
        self.assertEqual(out["stats"]["raisons"], {"objectif": 1, "stop": 1})
        self.assertAlmostEqual(out["stats"]["esperance_bps"], (16 - 27) / 2)

    def test_trades_split_by_strategy(self):
        with open(os.path.join(self.tmp.name, "journal_scalp_papier.csv"), "a") as f:
            f.write("2026-10-25 00:02:00,SOL,short,explo:elan/cinq_min/marche,0.5,100,99,1000,300,100,93,9.3,objectif\n")
        out = dashboard.build("papier", 3600, base=self.tmp.name)
        court, longue = out["strategies"]["court"], out["strategies"]["longue"]
        self.assertEqual((court["total"]["trades"], longue["total"]["trades"]), (2, 1))
        self.assertEqual(longue["total"]["exploration"], 1)
        self.assertAlmostEqual(longue["total"]["pnl"], 9.3)

    def test_missing_files_are_reported_not_fatal(self):
        out = dashboard.build("testnet", 3600, base=self.tmp.name)
        self.assertFalse(out["en_ligne"])
        self.assertIsNone(out["solde"])
        self.assertEqual(out["trades"], [])


class ServerTest(unittest.TestCase):
    def test_routes(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            page = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn("Crypto Agent", page)
            api = json.loads(urllib.request.urlopen(base + "/api/etat?mode=papier&fenetre=900").read())
            self.assertIn("en_ligne", api)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/api/etat?mode=autre")
            self.assertEqual(e.exception.code, 400)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
