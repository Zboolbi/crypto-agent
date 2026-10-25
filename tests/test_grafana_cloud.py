"""Tests de l'envoi vers Grafana Cloud, contre un faux serveur local."""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grafana_cloud as gc
import metriques


class FakeCloud(BaseHTTPRequestHandler):
    received: ClassVar[list] = []

    def _reply(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/datasources" and self.headers["Authorization"] == "Bearer sa-test":
            body = [{"type": "prometheus", "name": "grafanacloud-x-prom", "uid": "prom1"},
                    {"type": "prometheus", "name": "grafanacloud-x-usage-insights", "uid": "usage"},
                    {"type": "loki", "name": "grafanacloud-x-logs", "uid": "loki1"}]
            return self._reply(200, json.dumps(body).encode())
        return self._reply(401)

    def do_POST(self):
        data = self.rfile.read(int(self.headers["Content-Length"]))
        FakeCloud.received.append((self.path, self.headers["Authorization"], data))
        if self.path == "/api/dashboards/db":
            return self._reply(200, json.dumps({"url": "/d/crypto-agent/crypto-agent"}).encode())
        return self._reply(204)

    def log_message(self, *args):
        pass


class CloudTest(unittest.TestCase):
    def setUp(self):
        FakeCloud.received = []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeCloud)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.env = {"GRAFANA_CLOUD_URL": base, "GRAFANA_CLOUD_INFLUX_URL": base + "/api/v1/push/influx/write",
                    "GRAFANA_CLOUD_METRICS_USER": "123", "GRAFANA_CLOUD_LOGS_URL": base,
                    "GRAFANA_CLOUD_LOGS_USER": "456", "GRAFANA_CLOUD_TOKEN": "tok-test", "GRAFANA_CLOUD_SA_TOKEN": "sa-test"}
        self.db_path = os.path.join(d, "agent.db")
        db = sqlite3.connect(self.db_path)
        db.executescript(metriques.SCHEMA)
        db.execute("INSERT INTO valeurs VALUES (100, 'total', 29900, 30000, -50, -50, 10, 4)")
        db.execute("INSERT INTO valeurs VALUES (100, 'scalping', 9900, 10000, -100, 0, 10, 4)")
        db.execute("INSERT INTO sante VALUES (100, 300, 200, 30, 0, 100, 5, 1000, 1, 1)")
        db.execute("INSERT INTO positions VALUES ('tendance', 'DOT', 'short', 'ouvert', 1000, 1, 1, 12.5, 100)")
        db.execute("INSERT INTO trades VALUES ('a', 2000000000, 'court', 'BTC', 'short', 'elan/eclair/limite', 1, 10, "
                   "20, 16, 1.6, 'objectif')")
        db.execute("INSERT INTO modeles VALUES ('elan/eclair/limite', 'court', 500, 0.4, 0.4, 0.38, -3.2, 0.5, -4, 60, 0, 100)")
        db.commit()
        self.patches = [mock.patch.object(gc, "STATE_PATH", os.path.join(d, "etat.json"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.srv.shutdown()
        self.tmp.cleanup()

    def test_metrics_trades_and_dashboard(self):
        pusher = gc.Pusher(self.env, self.db_path)
        self.assertGreaterEqual(pusher.push_metrics(), 5)
        self.assertEqual(pusher.push_trades(), 1)
        self.assertEqual(pusher.push_trades(), 0, "un trade n'est envoyé qu'une fois")
        url = gc.publish_dashboard(self.env)
        self.assertTrue(url.endswith("/d/crypto-agent/crypto-agent"))

        paths = [p for p, _, _ in FakeCloud.received]
        self.assertEqual(paths, ["/api/v1/push/influx/write", "/loki/api/v1/push", "/api/dashboards/db"])
        influx = FakeCloud.received[0][2].decode()
        self.assertIn("agent_valeur,strategie=total valeur=29900.0,gain=-100.0", influx)
        self.assertIn("agent_position,crypto=DOT,etat=ouvert,strategie=tendance,variante=short", influx)
        self.assertEqual(FakeCloud.received[0][1], gc.basic("123", "tok-test"))
        loki = json.loads(FakeCloud.received[1][2])
        self.assertEqual(loki["streams"][0]["stream"], {"app": "crypto-agent", "strategie": "court"})
        dash = json.loads(FakeCloud.received[2][2])["dashboard"]
        uids = {t["datasource"]["uid"] for p in dash["panels"] for t in p["targets"]}
        self.assertEqual(uids, {"prom1", "loki1"}, "sources de la pile, hors « usage insights »")

    def test_tag_escaping(self):
        self.assertEqual(gc.line("m", {"a": "x y,z"}, {"v": 1}, 5), "m,a=x\\ y\\,z v=1.0 5")
        self.assertIsNone(gc.line("m", {}, {"v": None}, 5))


if __name__ == "__main__":
    unittest.main()
