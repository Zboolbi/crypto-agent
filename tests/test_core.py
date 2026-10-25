"""Tests des briques critiques du scalping. Lancer : python3 -m unittest discover -s tests"""

import json
import os
import random
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scalper
import trader
from learner import FEATURES, OnlineLogit
from sources import WebSocket

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
        s = scalper.SimTrade("short", 0, b, PROFILE, CFG, True, latency_ms=0)
        self.assertEqual(s.state, "attente")
        s.on_trade(100, 100.01, 3.0, False, b)  # 3 achetés au prix limite : il reste 2 devant nous
        self.assertEqual(s.state, "attente")
        s.on_trade(200, 100.01, 2.5, False, b)
        self.assertEqual(s.state, "ouvert")
        self.assertEqual(s.entry, 100.01)

    def test_limit_order_filled_when_price_trades_through(self):
        b = book()
        s = scalper.SimTrade("short", 0, b, PROFILE, CFG, True, latency_ms=0)
        s.on_trade(50, 100.05, 0.1, False, b)
        self.assertEqual(s.state, "ouvert")

    def test_limit_order_cancelled_after_deadline(self):
        b = book()
        s = scalper.SimTrade("short", 0, b, PROFILE, CFG, True, latency_ms=0)
        s.on_book(CFG["scalp_entree_delai_s"] * 1000 + 1, b)
        self.assertEqual(s.state, "annule")

    def test_take_profit_net_of_maker_fees(self):
        b = book()
        s = scalper.SimTrade("short", 0, b, PROFILE, CFG, True, latency_ms=0)
        s.on_trade(1, 100.02, 10, False, b)
        s.on_trade(2, s.tp * 0.9999, 1, True, b)
        self.assertEqual(s.reason, "objectif")
        self.assertAlmostEqual(s.net_bps(), 10 - 2 * CFG["frais_maker_pct"] * 100, places=6)

    def test_stop_uses_worst_price(self):
        b = book()
        s = scalper.SimTrade("short", 0, b, PROFILE, CFG, True, latency_ms=0)
        s.on_trade(1, 100.02, 10, False, b)
        b.set_depth([["100.2", "1"]], [["100.3", "1"]])  # le marché saute au-dessus du stop
        s.on_book(2, b)
        self.assertEqual(s.reason, "stop")
        self.assertEqual(s.exit, 100.3)
        self.assertLess(s.net_bps(), -PROFILE["stop_bps"])

    def test_market_entry_pays_taker(self):
        cfg = CFG
        s = scalper.SimTrade("short", 0, book(), PROFILE, cfg, False, latency_ms=0)
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
        self.assertGreater(m.predict([2.0] + [0.0] * (len(FEATURES) - 1)), 0.9)
        clone = OnlineLogit.from_dict(json.loads(json.dumps(m.to_dict())))
        x = [0.5] * len(FEATURES)
        self.assertAlmostEqual(clone.predict(x), m.predict(x))

    def test_resets_when_features_change(self):
        self.assertEqual(OnlineLogit.from_dict({"w": [1.0, 2.0]}).seen, 0)


class ScalperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = json.loads(json.dumps(CFG))
        cfg["scalp_exploration"] = False  # ces tests vérifient les décisions du modèle seul
        self.sc = scalper.Scalper("papier", cfg, state_dir=self.tmp.name)
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
        self.assertIn("BTCUSDT/elan/court", sc.last_shadow, "des trades fantômes doivent être lancés")
        self.assertIn("BTCUSDT/elan/longue", sc.last_shadow, "et pour la stratégie longue aussi")
        self.assertFalse(sc.trades, "aucun trade réel tant que le modèle apprend")

        for key, m in sc.models.items():  # modèle entraîné, confirmé par ses résultats, en entrée limite
            good = key.startswith("elan/") and key.endswith("limite")
            m.seen, m.b, m.base_rate = CFG["scalp_apprentissage_min"], (5.0 if good else -5.0), 0.5
            m.cal_n, m.cal_w = [100.0] * 10, [(90.0 if good else 1.0)] * 10
            m.cal_net, m.avg_net = [(800.0 if good else -1500.0)] * 10, (8.0 if good else -15.0)
            m.scaler.count, m.scaler.m2 = 1000, [1e9] * len(FEATURES)  # contextes déjà vus
            sc.state["remplissage"][key] = 0.9
            sc.state["validation"][key] = [5.0] * 60 if good else [-5.0] * 60  # validé hors échantillon
        t = self._sell_burst(30000)
        self.assertIn(("BTCUSDT", "court"), sc.trades)
        sim = sc.trades[("BTCUSDT", "court")]
        self.assertEqual(sim.state, "attente")
        self.assertLessEqual(sim.notional, sc.equity_cache * CFG["scalp_taille_max_pct"] / 100 + 1e-6)

        sc.on_trade("BTCUSDT", t + 100, sim.limit + 0.05, 1.0, False)  # exécution de l'ordre limite
        self.assertEqual(sim.state, "ouvert")
        cash = sc.state["cash"]
        sc.on_trade("BTCUSDT", t + 200, sim.tp * 0.999, 1.0, True)    # trop tôt : l'ordre d'objectif n'est pas arrivé
        self.assertEqual(sim.state, "ouvert")
        sc.on_trade("BTCUSDT", t + 500, sim.tp * 0.999, 1.0, True)    # objectif atteint
        self.assertEqual(sim.reason, "objectif")
        self.assertGreater(sc.state["cash"], cash)
        self.assertEqual(sc.state["stats"]["trades"], 1)
        self.assertTrue(os.path.exists(sc.journal_path))


    def test_overconfident_model_is_corrected_by_calibration(self):
        sc = self.sc
        for m in sc.models.values():  # le modèle brut se croit sûr de lui, mais n'a gagné que 5 % du temps
            m.seen, m.b, m.base_rate = CFG["scalp_apprentissage_min"], 5.0, 0.05
            m.cal_n, m.cal_w = [100.0] * 10, [5.0] * 10
            m.cal_net, m.avg_net = [-1500.0] * 10, -15.0       # en réalité : -15 pb net en moyenne
            m.scaler.count, m.scaler.m2 = 1000, [1e9] * len(FEATURES)
        self._sell_burst(30000)
        self.assertFalse(sc.trades, "aucun trade quand les résultats réels démentent le modèle")

    def test_quarantine_after_real_losses(self):
        sc = self.sc
        b = book()
        for i in range(5):
            sim = scalper.SimTrade("short", i * 1000, b, PROFILE, CFG, False, features=[0.0] * len(FEATURES), latency_ms=0,
                                   p=0.6, notional=1000.0)
            sim._close(i * 1000 + 500, sim.entry * 1.002, False, "stop")
            sc.record("BTCUSDT", sim)
        self.assertTrue(sc.paused("BTCUSDT", 10000))
        self.assertFalse(sc.paused("BTCUSDT", 10000 + scalper.QUARANTINE_MS))

    def test_single_instance_lock(self):
        self.sc.acquire_lock()
        other = scalper.Scalper("papier", json.loads(json.dumps(CFG)), state_dir=self.tmp.name)
        with self.assertRaises(scalper.AlreadyRunning):
            other.acquire_lock()

    def test_live_balance_file(self):
        self.sc.write_live()
        with open(self.sc.live_path, encoding="utf-8") as f:
            snap = json.load(f)
        self.assertEqual(snap["valeur"], CFG["capital_initial"])
        self.assertIn("Valeur actuelle", scalper.format_balance(snap))


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = json.loads(json.dumps(CFG))
        cfg["scalp_exploration"] = False
        self.sc = scalper.Scalper("papier", cfg, state_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_variant_needs_positive_out_of_sample_results(self):
        sc, key = self.sc, "elan/rapide/marche"
        self.assertFalse(sc.validated(key), "aucun historique : pas de trade réel")
        sc.state["validation"][key] = [-5.0] * 60
        self.assertFalse(sc.validated(key), "les trades choisis perdaient")
        sc.state["validation"][key] = [3.0] * 60
        self.assertTrue(sc.validated(key))

    def test_only_chosen_shadows_feed_validation(self):
        sc = self.sc
        for would in (True, False):
            sim = scalper.SimTrade("short", 0, book(), PROFILE, CFG, False, "elan/eclair/marche",
                                   features=[0.0] * len(FEATURES), latency_ms=0)
            sim.would = would
            sim._close(10, sim.entry * 0.999, True, "objectif")
            sc.learn("BTCUSDT", sim)
        self.assertEqual(len(sc.state["validation"]["elan/eclair/marche"]), 1)


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sc = scalper.Scalper("papier", json.loads(json.dumps(CFG)), state_dir=self.tmp.name)
        self.sc.coins = {c: {"base": c} for c in ("A", "B", "C", "D")}
        self.sc.books = {c: book() for c in self.sc.coins}

    def tearDown(self):
        self.tmp.cleanup()

    def test_ten_percent_per_trade_and_caps_per_strategy(self):
        sc = self.sc
        args = ("short", PROFILE, False, "elan/eclair/marche", [0.0] * len(FEATURES), 0.9, 6, 12, 5)
        for c in sc.coins:
            sc.open(c, 0, args[0], *args[1:], group="court")
        court = [s.notional for k, s in sc.trades.items() if k[1] == "court"]
        self.assertEqual(court, [1000.0, 500.0], "10 % par trade, 15 % au plus pour le court terme")
        for c in sc.coins:
            sc.open(c, 0, args[0], *args[1:], group="longue")
        longue = [s.notional for k, s in sc.trades.items() if k[1] == "longue"]
        self.assertEqual(longue, [1000.0], "10 % au plus pour la stratégie longue")
        self.assertAlmostEqual(sc.exposure(), 2500.0, msg="25 % engagés au total")
        self.assertIn(("A", "court"), sc.trades)
        self.assertIn(("A", "longue"), sc.trades, "les deux stratégies peuvent tenir la même crypto")

    def test_new_session_keeps_knowledge(self):
        sc = self.sc
        sc.state["cash"], sc.state["stats"]["trades"] = 9000.0, 42
        sc.state["validation"]["elan/eclair/marche"] = [1.0] * 60
        sc.state["cryptos"]["BTCUSDT"] = {"edge": 2.0, "n": 50, "pause_jusqua": 9e15, "reel": [-20.0]}
        sc.new_session()
        self.assertEqual((sc.state["cash"], sc.state["stats"]["trades"]), (CFG["capital_initial"], 0))
        self.assertEqual(len(sc.state["validation"]["elan/eclair/marche"]), 60, "validation conservée")
        self.assertFalse(sc.paused("BTCUSDT", 0), "quarantaine levée")


class ExplorationOffTest(unittest.TestCase):
    def test_no_real_trade_without_validation_but_shadows_keep_learning(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sc = scalper.Scalper("papier", json.loads(json.dumps(CFG)), state_dir=tmp.name)  # réglages par défaut
        sc.coins, sc.books = {"BTCUSDT": {"base": "BTC"}}, {"BTCUSDT": scalper.Book()}
        sc.on_depth("BTCUSDT", 10000, [["99.99", "5"]] * 5, [["100", "1"]] * 5)
        for i in range(30):
            sc.on_trade("BTCUSDT", 10000 + i * 200, 100 * (1 - i * 0.0001), 1000.0, True)
        self.assertFalse(sc.trades, "exploration coupée et aucune variante validée : aucun trade réel")
        self.assertTrue(any(sc.shadows.values()), "les trades fantômes continuent")


class ExplorationTest(unittest.TestCase):
    def make(self, mode="papier", **over):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = json.loads(json.dumps(CFG))
        cfg["scalp_exploration"] = True  # coupée par défaut depuis le 25/10 ; ces tests vérifient son fonctionnement
        cfg.update(over)
        sc = scalper.Scalper(mode, cfg, state_dir=self.tmp.name)
        sc.coins = {"BTCUSDT": {"base": "BTC"}}
        sc.books = {"BTCUSDT": scalper.Book()}
        return sc

    def burst(self, sc, t0):
        sc.on_depth("BTCUSDT", t0, [["99.99", "5"]] * 5, [["100", "1"]] * 5)
        for i in range(30):
            sc.on_trade("BTCUSDT", t0 + i * 200, 100 * (1 - i * 0.0001), 1000.0, True)

    def test_trades_while_learning_with_small_size(self):
        sc = self.make()
        opened = []
        real_open = sc.open
        sc.open = lambda *a, **k: (opened.append(k.get("explore")), real_open(*a, **k))
        self.burst(sc, 10000)
        self.assertFalse(sc.ready(), "le modèle est encore en apprentissage")
        self.assertEqual(opened, [True, True], "un trade d'exploration par stratégie (court et longue), puis attente")
        self.assertEqual(sc.state["stats"]["ordres"], 2)
        sim = sc.trades.get(("BTCUSDT", "court"))
        if sim:  # l'ordre limite peut avoir expiré pendant la chute simulée
            self.assertTrue(sim.explore)
            self.assertAlmostEqual(sim.notional, sc.equity_cache * CFG["scalp_exploration_taille_pct"] / 100)

    def test_hourly_cap(self):
        sc = self.make(scalp_exploration_par_heure=1)
        self.burst(sc, 10000)
        sc.trades.clear()
        self.burst(sc, 20000)
        self.assertFalse(sc.trades, "plafond horaire d'exploration respecté")

    def test_unlimited_with_per_crypto_spacing(self):
        sc = self.make(scalp_exploration_par_heure=0, scalp_exploration_espacement_s=10)
        sc.last_explore[("BTCUSDT", "court")] = 0
        self.assertFalse(sc.can_explore(5000, "BTCUSDT"), "même crypto : 10 s d'écart minimum")
        self.assertTrue(sc.can_explore(5000, "ETHUSDT"), "autre crypto : pas d'attente")
        sc.explore_times.extend(range(0, 5000, 10))  # 500 explorations dans l'heure
        self.assertTrue(sc.can_explore(11000, "BTCUSDT"), "pas de plafond horaire")

    def test_unlimited_simultaneous_positions(self):
        sc = self.make(scalp_positions_max=0)
        sc.trades = {str(i): None for i in range(100)}
        self.assertTrue(sc.can_open())

    def test_universe_from_exchange_volume(self):
        sc = self.make(scalp_volume_24h_min_usd=10e6, scalp_cryptos=0)

        class FakeMD:
            def contracts(self):
                ticks = {"BTC": 0.1, "XRP": 0.0001, "PEPE": 1e-7, "USDC": 0.0001, "TINY": 0.01, "COARSE": 0.01}
                return {t: {"contract": t + "USDT", "base": t, "mult": 1, "tick": k} for t, k in ticks.items()}

            def tickers(self):
                prices = {"BTC": 80000, "XRP": 1.3, "PEPE": 0.004, "USDC": 1, "TINY": 1, "COARSE": 2.0}
                return {t + "USDT": {"price": p, "funding": 0.0} for t, p in prices.items()}

            def volumes_24h(self):
                return {"BTCUSDT": 9e9, "XRPUSDT": 5e8, "PEPEUSDT": 2e7, "USDCUSDT": 1e9, "TINYUSDT": 1e5,
                        "COARSEUSDT": 1e8}

        sc.trader.md = FakeMD()
        sc.select_universe()
        self.assertEqual(list(sc.coins), ["BTCUSDT", "XRPUSDT", "PEPEUSDT"],
                         "stablecoin, contrat peu échangé et pas de prix trop grossier (50 pb) exclus")

    def test_stop_must_exceed_spread(self):
        sc = self.make()
        opened = []
        sc.open = lambda *a, **k: opened.append(a[3]["stop_bps"])
        sc.on_depth("BTCUSDT", 10000, [["99.96", "5"]] * 5, [["100", "1"]] * 5)  # écart de 4 pb
        for i in range(30):
            sc.on_trade("BTCUSDT", 10000 + i * 200, 100 * (1 - i * 0.0001), 1000.0, True)
        self.assertTrue(opened)
        self.assertTrue(all(stop >= 3 * 4 for stop in opened), "pas de stop plus serré que 3 écarts")
        shadow_stops = {s.profile["stop_bps"] for s in sc.shadows["BTCUSDT"]}
        self.assertNotIn(8, shadow_stops, "le profil éclair (stop 8 pb) est écarté")

    def test_spread_over_the_hour(self):
        sc = self.make(scalp_exploration_par_heure=60)
        self.assertTrue(sc.can_explore(0))
        sc.explore_times.append(0)
        self.assertFalse(sc.can_explore(30000), "pas deux trades d'exploration à moins d'une minute")
        self.assertTrue(sc.can_explore(60000))

    def test_exploration_loss_does_not_quarantine_or_shrink(self):
        sc = self.make()
        for i in range(6):
            sim = scalper.SimTrade("short", i * 1000, book(), PROFILE, CFG, False, features=[0.0] * len(FEATURES), latency_ms=0,
                                   p=0.3, notional=200.0)
            sim.explore = True
            sim._close(i * 1000 + 500, sim.entry * 1.003, False, "stop")
            sc.record("BTCUSDT", sim)
        self.assertFalse(sc.paused("BTCUSDT", 10000))
        self.assertEqual(sc.state["serie_pertes"], 0)
        self.assertEqual(sc.state["stats"]["exploration"], 6)
        self.assertLess(sc.state["stats"]["exploration_pnl"], 0)

    def test_daily_loss_budget_stops_exploration(self):
        sc = self.make(scalp_exploration_budget_jour_pct=1)  # 1 % de 10 000 $ = 100 $
        self.assertTrue(sc.can_explore(0, "BTCUSDT"))
        sc.state["exploration_jour"] = {"date": str(scalper.date.today()), "pnl": -100.0}
        self.assertFalse(sc.can_explore(0, "BTCUSDT"), "budget du jour épuisé")
        sc.state["exploration_jour"]["date"] = "2000-01-01"
        self.assertTrue(sc.can_explore(0, "BTCUSDT"), "nouveau jour, nouveau budget")

    def test_disabled_in_real_mode_by_default(self):
        sc = self.make()
        sc.mode = "reel"
        self.assertFalse(sc.can_explore(0))


class LatencyTest(unittest.TestCase):
    def test_no_fill_or_target_before_the_order_reaches_the_exchange(self):
        b = book(bid=100.0, ask=100.01)
        s = scalper.SimTrade("short", 0, b, PROFILE, CFG, False, latency_ms=300)
        b.add_trade(100, 99.0, -1000.0, 10000)
        s.on_trade(100, 99.0, 1.0, True, b)                 # chute avant l'arrivée de l'ordre
        self.assertEqual(s.state, "envoi", "rien n'est exécuté pendant la latence")
        b.add_trade(400, 99.0, -1000.0, 10000)
        s.on_trade(400, 99.0, 1.0, True, b)                 # l'ordre arrive : vendu au prix réel, pas au carnet périmé
        self.assertEqual((s.state, s.entry), ("ouvert", 99.0))
        s.on_trade(500, s.tp * 0.999, 1.0, True, b)
        self.assertEqual(s.state, "ouvert", "l'objectif n'est pas encore déposé")
        s.on_trade(800, s.tp * 0.999, 1.0, True, b)
        self.assertEqual(s.reason, "objectif")

    def test_post_only_rejected_if_price_crossed(self):
        b = book(bid=100.0, ask=100.01)
        s = scalper.SimTrade("short", 0, b, PROFILE, CFG, True, latency_ms=300)
        b.set_depth([["100.02", "3"]], [["100.03", "3"]])     # le prix monte au-dessus de notre limite
        s.on_book(400, b)
        self.assertEqual(s.state, "annule")


LONG_PROFILE = {"nom": "une_min", "objectif_bps": 45, "stop_bps": 35, "duree_s": 60}


class ExtensionTest(unittest.TestCase):

    def open_short(self, b):
        s = scalper.SimTrade("short", 0, b, LONG_PROFILE, CFG, False, "elan/une_min/marche", latency_ms=0)
        self.assertEqual((s.state, s.entry), ("ouvert", 100.0))
        return s

    def test_losing_recoverable_position_is_extended_then_closed(self):
        b = book(bid=100.0, ask=100.01)
        s = self.open_short(b)
        b.set_depth([["100.04", "3"]], [["100.05", "3"]])     # perte légère (5 pb), flux neutre
        s.on_book(61000, b)
        self.assertEqual((s.state, s.extensions), ("ouvert", 1), "prolongée au lieu de fermer en perte")
        s.on_book(121000, b)
        s.on_book(181000, b)
        self.assertEqual(s.state, "ferme", "2 prolongations au plus")
        self.assertEqual(s.reason, "durée max (prolongée ×2)")

    def test_no_extension_when_flow_is_against(self):
        b = book(bid=100.0, ask=100.01)
        s = self.open_short(b)
        b.set_depth([["100.04", "3"]], [["100.05", "3"]])
        b.add_trade(60000, 100.05, 50000.0, 10000)          # forte pression acheteuse contre le short
        s.on_book(61000, b)
        self.assertEqual(s.state, "ferme")

    def test_no_extension_when_loss_is_too_close_to_stop(self):
        b = book(bid=100.0, ask=100.01)
        s = self.open_short(b)
        b.set_depth([["100.24", "3"]], [["100.25", "3"]])     # perte de 25 pb > 50 % du stop de 35 pb
        s.on_book(61000, b)
        self.assertEqual(s.state, "ferme")

    def test_winning_position_closes_at_deadline(self):
        b = book(bid=100.0, ask=100.01)
        s = self.open_short(b)
        b.set_depth([["99.79", "3"]], [["99.80", "3"]])       # gain de 20 pb, frais couverts
        s.on_book(61000, b)
        self.assertEqual((s.state, s.reason), ("ferme", "durée max"))
        self.assertGreater(s.net_bps(), 0)


class RebondExitTest(unittest.TestCase):
    def test_rebound_short_is_not_closed_by_buy_pressure(self):
        b = book()
        for key, expect_exit in (("rebond/eclair/marche", False), ("elan/eclair/marche", True)):
            s = scalper.SimTrade("short", 0, b, PROFILE, CFG, False, key, latency_ms=0)
            b.add_trade(500, 100.0, 50000.0, 10000)   # forte pression acheteuse
            s.on_trade(2000, 100.0, 1.0, False, b)
            self.assertEqual(s.state == "ferme", expect_exit, key)


class TargetedExplorationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sc = scalper.Scalper("papier", {**json.loads(json.dumps(CFG)), "scalp_exploration": True},
                                  state_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def set_model(self, key, net, n):
        m = self.sc.models[key]
        m.seen, m.base_rate, m.avg_net = 1000, 0.4, net
        m.cal_n, m.cal_w, m.cal_net = [n] * 10, [0.4 * n] * 10, [net * n] * 10
        m.scaler.count, m.scaler.m2 = 1000, [1e9] * len(FEATURES)

    def test_picks_most_promising_variant_and_skips_hopeless_ones(self):
        variants = [v for v in self.sc.variants if v[0] == "elan" and v[3] == "court" and not v[2]]
        keys = [scalper.model_key(f, p, m) for f, p, m, _ in variants]
        for k in keys:
            self.set_model(k, -20.0, 400)           # bien connues et nettement perdantes
        x = [0.0] * len(FEATURES)
        self.assertIsNone(self.sc.explore_pick(variants, x), "rien d'assez prometteur : pas d'exploration")
        self.set_model(keys[1], 2.0, 400)
        self.assertEqual(self.sc.explore_pick(variants, x)[4], keys[1], "la variante la plus prometteuse")
        self.set_model(keys[2], -5.0, 0)            # inconnue : le bonus lui donne sa chance
        self.assertEqual(self.sc.explore_pick(variants, x)[4], keys[2])


class CalibrationTest(unittest.TestCase):
    def test_net_result_is_calibrated_on_real_outcomes(self):
        rnd = random.Random(2)
        m = OnlineLogit()
        for _ in range(3000):
            x = [rnd.gauss(0, 1) for _ in FEATURES]
            win = rnd.random() < 0.6                 # 60 % de gains… mais de +4 pb contre -27 pb
            m.update(x, 1.0 if win else 0.0, 4.0 if win else -27.0)
        est = m.estimate([0.0] * len(FEATURES))
        self.assertGreater(est["p"], 0.5, "souvent gagnant")
        self.assertLess(est["net"], -5, "mais perdant en moyenne : c'est ce chiffre qui décide")


    def test_calibrated_matches_reality_and_ignores_unknown_contexts(self):
        rnd = random.Random(0)
        m = OnlineLogit()
        for _ in range(3000):
            x = [rnd.gauss(0, 1) for _ in FEATURES]
            m.update(x, 1.0 if rnd.random() < 0.1 else 0.0)  # 10 % de gains quoi qu'il arrive
        p = m.estimate([2.0] * len(FEATURES))["p"]
        self.assertLess(abs(p - 0.1), 0.08)
        p_unknown = m.estimate([50.0] * len(FEATURES))["p"]
        self.assertAlmostEqual(p_unknown, m.base_rate)


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
