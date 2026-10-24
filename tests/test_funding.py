"""Tests des règles de l'arbitrage de financement."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funding  # noqa: E402
import trader  # noqa: E402

CFG = json.loads(json.dumps(trader.DEFAULT_CONFIG))


class RulesTest(unittest.TestCase):
    def test_annualisation(self):
        self.assertAlmostEqual(funding.annual(0.0001, 8), 10.95)
        self.assertAlmostEqual(funding.annual(0.00005, 4), 10.95)

    def test_entry_needs_durable_rate_covering_costs(self):
        cfg = dict(CFG, funding_entree_annuel_pct=9, funding_horizon_j=30, funding_marge_frais=1.0)
        self.assertTrue(funding.should_enter(11, 11, cfg))
        self.assertFalse(funding.should_enter(11, 5, cfg), "taux en cours trop bas")
        self.assertFalse(funding.should_enter(5, 20, cfg), "moyenne récente trop basse")
        cfg_short = dict(cfg, funding_horizon_j=3)
        self.assertFalse(funding.should_enter(11, 11, cfg_short), "3 jours à 11 %/an ne couvrent pas 0,5 % de frais")

    def test_exit(self):
        self.assertTrue(funding.should_exit(-1, dict(CFG, funding_sortie_annuel_pct=0)))
        self.assertFalse(funding.should_exit(4, dict(CFG, funding_sortie_annuel_pct=0)))


class PaperEngineTest(unittest.TestCase):
    def test_open_close_accounting(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fa = funding.FundingArb("papier", json.loads(json.dumps(CFG)), state_dir=tmp.name)
        m = {"symbole": "BTCUSDT", "annuel": 11.0, "spot_bid": 99.99, "spot_ask": 100.0, "perp_bid": 100.0,
             "perp_ask": 100.01, "mark": 100.0}
        fa.market = {"BTCUSDT": m}
        fa.open("BTCUSDT", m)
        self.assertIn("BTCUSDT", fa.state["positions"])
        self.assertLess(fa.equity(), CFG["funding_capital"], "frais d'entrée payés")
        fa.state["positions"]["BTCUSDT"]["financement"] = 50.0
        moved = dict(m, spot_bid=150.0, spot_ask=150.01, perp_bid=150.0, perp_ask=150.02)  # +50 % : couvert
        fa.market = {"BTCUSDT": moved}
        fa.close("BTCUSDT", moved, "test")
        pnl = fa.state["stats"]["pnl"]
        self.assertGreater(pnl, 40, "le financement est encaissé, le mouvement de prix est neutralisé")
        self.assertLess(pnl, 50)


if __name__ == "__main__":
    unittest.main()
