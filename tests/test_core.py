"""Tests des briques critiques du scalping. Lancer : python3 -m unittest discover -s tests"""

import json
import os
import random
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scalper  # noqa: E402
import trader  # noqa: E402
from learner import FEATURES, OnlineLogit  # noqa: E402
from sources import WebSocket  # noqa: E402

CFG = json.loads(json.dumps(trader.DEFAULT_CONFIG))
PROFILE = {"nom": "eclair", "objectif_bps": 10, "stop_bps": 8, "duree_s": 20}


def book(bid=100.0, ask=100.01, ask_top=5.0):
    b = scalper.Book()
    b.set_depth([[str(bid), "3"]] * 5, [[str(ask), str(ask_top)]] * 5)
    b.ask_top = ask_top
    return b


class BookTest(unittest.TestCase):
    def test_flow_window_and_price_ago(self):
        b = scalper.Book()
        b.add_trade(0, 100.0, 1000.0, 10000)     # achat agressif
        b.add_trade(5000, 99.9, -3000.0, 10000)  # vente agressive
        self.assertAlmostEqual(b.flow(), (1000 - 3000) / 4000)
        b.add_trade(12000, 99.8, -1000.0, 10000)  # le premier trade sort de la fenêtre de 10 s
        self.assertAlmostEqual(b.flow(), -1.0)
        self.assertEqual(b.price_ago(12000, 7000), 99.9)


class SimTradeTest(unittest.TestCase):
    def test_limit_order_waits_for_queue(self):
        b = book(ask_top=5.0)
        s = scalper.SimTrade("short", 0, b, PROFILE, CFG, True)
        self.assertEqual(s.state, "attente")
        s.on_trade(100, 100.01, 3.0, False, b)  # 3 achetés au prix limite : il reste 2 devant nous
        self.assertEqual(s.state, "attente")
        s.on_trade(200, 100.01, 2.5, False, b)
        self.assertEqual(s.state, "ouvert")
        self.assertEqual(s.entry, 100.01)

    def test_limit_order_filled_when_price_trades_through(self):
        b = book()
        s = scalper.SimTrade("short", 0, b, PROFILE, CFG, True)
        s.on_trade(50, 100.05, 0.1, False, b)
        self.assertEqual(s.state, "ouvert")

    def test_limit_order_cancelled_after_deadline(self):
        b = book()
        s = scalper.SimTrade("short", 0, b, PROFILE, CFG, True)
        s.on_book(CFG["scalp_entree_delai_s"] * 1000 + 1, b)
        self.assertEqual(s.state, "annule")

    def test_take_profit_net_of_maker_fees(self):
        b = book()
        s = scalper.SimTrade("short", 0, b, PROFILE, CFG, True)
        s.on_trade(1, 100.02, 10, False, b)
        s.on_trade(2, s.tp * 0.9999, 1, True, b)
        self.assertEqual(s.reason, "objectif")
        self.assertAlmostEqual(s.net_bps(), 10 - 2 * CFG["frais_maker_pct"] * 100, places=6)

    def test_stop_uses_worst_price(self):
        b = book()
        s = scalper.SimTrade("short", 0, b, PROFILE, CFG, True)
        s.on_trade(1, 100.02, 10, False, b)
        b.set_depth([["100.2", "1"]], [["100.3", "1"]])  # le marché saute au-dessus du stop
        s.on_book(2, b)
        self.assertEqual(s.reason, "stop")
        self.assertEqual(s.exit, 100.3)
        self.assertLess(s.net_bps(), -PROFILE["stop_bps"])

    def test_market_entry_pays_taker(self):
        cfg = CFG
        s = scalper.SimTrade("short", 0, book(), PROFILE, cfg, False)
        self.assertEqual(s.state, "ouvert")
        self.assertEqual(s.entry, 100.0)  # vendu au meilleur acheteur
        self.assertAlmostEqual(s.fee_bps, cfg["frais_pct"] * 100)


class LearnerTest(unittest.TestCase):
    def test_learns_a_simple_pattern_and_round_trips(self):
        rnd = random.Random(1)
        m = OnlineLogit()
        for _ in range(3000):
            x = [rnd.gauss(0, 1) for _ in FEATURES]
            m.update(x, 1.0 if x[0] + 0.3 * rnd.gauss(0, 1) > 0 else 0.0)
        self.assertGreater(m.accuracy, 0.8)
        self.assertGreater(m.predict([2.0] + [0.0] * (len(FEATURES) - 1)), 0.9)
        clone = OnlineLogit.from_dict(json.loads(json.dumps(m.to_dict())))
        x = [0.5] * len(FEATURES)
        self.assertAlmostEqual(clone.predict(x), m.predict(x))

    def test_resets_when_features_change(self):
        self.assertEqual(OnlineLogit.from_dict({"w": [1.0, 2.0]}).seen, 0)


class ScalperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sc = scalper.Scalper("papier", json.loads(json.dumps(CFG)), state_dir=self.tmp.name)
        self.sc.coins = {"BTCUSDT": {"base": "BTC"}}
        self.sc.books = {"BTCUSDT": scalper.Book()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_size_shrinks_after_losses_and_stays_in_bounds(self):
        hi = self.sc.size_pct(0.9, 6, 12)
        self.assertLessEqual(hi, CFG["scalp_taille_max_pct"])
        self.sc.state["serie_pertes"] = 3
        self.assertLess(self.sc.size_pct(0.9, 6, 12), hi)
        self.sc.state["serie_pertes"] = 0
        self.assertEqual(self.sc.size_pct(0.1, 6, 12), CFG["scalp_taille_min_pct"])

    def _sell_burst(self, t0, price=100.0):
        """6 s de ventes agressives (≈ 1 pb de baisse toutes les 200 ms, 100 k$ par transaction)."""
        sc = self.sc
        sc.on_depth("BTCUSDT", t0, [[str(price - 0.01), "5"]] * 5, [[str(price), "1"]] * 5)
        for i in range(30):
            sc.on_trade("BTCUSDT", t0 + i * 200, price * (1 - i * 0.0001), 1000.0, True)
        return t0 + 29 * 200

    def test_shadows_first_then_real_trade_when_model_confident(self):
        sc = self.sc
        self._sell_burst(10000)
        self.assertIn("BTCUSDT", sc.last_shadow, "des trades fantômes doivent être lancés")
        self.assertFalse(sc.trades, "aucun trade réel tant que le modèle apprend")

        for key, m in sc.models.items():  # modèle entraîné et confiant, en entrée limite
            m.seen, m.b = CFG["scalp_apprentissage_min"], (5.0 if key.endswith("limite") else -5.0)
            sc.state["remplissage"][key] = 0.9
        t = self._sell_burst(30000)
        self.assertIn("BTCUSDT", sc.trades)
        sim = sc.trades["BTCUSDT"]
        self.assertEqual(sim.state, "attente")
        self.assertLessEqual(sim.notional, sc.equity_cache * CFG["scalp_taille_max_pct"] / 100 + 1e-6)

        sc.on_trade("BTCUSDT", t + 100, sim.limit + 0.05, 1.0, False)  # exécution de l'ordre limite
        self.assertEqual(sim.state, "ouvert")
        cash = sc.state["cash"]
        sc.on_trade("BTCUSDT", t + 200, sim.tp * 0.999, 1.0, True)    # objectif atteint
        self.assertEqual(sim.reason, "objectif")
        self.assertGreater(sc.state["cash"], cash)
        self.assertEqual(sc.state["stats"]["trades"], 1)
        self.assertTrue(os.path.exists(sc.journal_path))


class FakeSock:
    def __init__(self, data):
        self.data, self.sent = data, b""

    def recv(self, n):
        out, self.data = self.data[:n], self.data[n:]
        return out

    def sendall(self, b):
        self.sent += b


class WebSocketTest(unittest.TestCase):
    def test_answers_ping_and_reassembles_fragments(self):
        ws = WebSocket.__new__(WebSocket)
        ping = bytes([0x89, 4]) + b"ping"
        part1 = bytes([0x01, 3]) + b'{"a'
        part2 = bytes([0x80, 4]) + b'":1}'
        big = b"x" * 300
        long_frame = bytes([0x81, 126]) + struct.pack("!H", len(big)) + big
        ws.sock, ws.buf = FakeSock(ping + part1 + part2 + long_frame), bytearray()
        self.assertEqual(ws.recv(), '{"a":1}')
        self.assertEqual(ws.sock.sent[0], 0x8A)  # pong renvoyé
        self.assertEqual(ws.recv(), "x" * 300)


if __name__ == "__main__":
    unittest.main()
