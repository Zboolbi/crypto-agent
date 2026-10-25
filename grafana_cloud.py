#!/usr/bin/env python3
"""
Envoie les données de l'agent vers Grafana Cloud et y crée le tableau de bord.

Source : la base SQLite remplie par metriques.py (donnees/agent.db).
  - métriques (soldes, gains, santé, positions, modèles, totaux par stratégie) → Prometheus
    de la pile Grafana Cloud, au format Influx, toutes les 15 s ;
  - chaque trade fermé → Loki (journaux), pour le tableau des derniers trades ;
  - tableau de bord « Crypto Agent » créé ou mis à jour par l'API de la pile.

Les identifiants sont saisis par l'utilisateur (commande « cles ») et rangés dans .env.

    python3 grafana_cloud.py cles      # saisie des identifiants (par vous)
    python3 grafana_cloud.py tester    # vérifie l'envoi et l'accès à l'API
    python3 grafana_cloud.py tableau   # crée / met à jour le tableau de bord
    python3 grafana_cloud.py           # envoi continu (service com.cryptoagent.grafana-cloud)
"""

import argparse
import base64
import getpass
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request

import metriques

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
STATE_PATH = os.path.join(HERE, "donnees", "grafana_cloud.json")
PERIOD_S = 15
KEYS = {
    "GRAFANA_CLOUD_URL": "Adresse de votre pile Grafana (ex. https://votre-nom.grafana.net)",
    "GRAFANA_CLOUD_INFLUX_URL": "Adresse d'envoi InfluxDB (…/api/v1/push/influx/write)",
    "GRAFANA_CLOUD_METRICS_USER": "Identifiant (« User » / « Instance ID ») de l'envoi InfluxDB",
    "GRAFANA_CLOUD_LOGS_URL": "Adresse Loki (ex. https://logs-prod-xxx.grafana.net)",
    "GRAFANA_CLOUD_LOGS_USER": "Identifiant (« User ») de Loki",
    "GRAFANA_CLOUD_TOKEN": "Jeton d'accès (droits metrics:write et logs:write)",
    "GRAFANA_CLOUD_SA_TOKEN": "Jeton de compte de service (rôle Editor), pour créer le tableau de bord",
}
SECRET_KEYS = {"GRAFANA_CLOUD_TOKEN", "GRAFANA_CLOUD_SA_TOKEN"}


class CloudError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Identifiants
# --------------------------------------------------------------------------- #


def read_env():
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


def setup_keys():
    """Saisie interactive par l'utilisateur ; les jetons ne s'affichent pas."""
    env = read_env()
    print("Identifiants Grafana Cloud (Entrée pour garder la valeur déjà enregistrée).\n")
    for key, label in KEYS.items():
        current = env.get(key)
        prompt = f"{label}{' [déjà enregistré]' if current else ''} : "
        value = (getpass.getpass(prompt) if key in SECRET_KEYS else input(prompt)).strip()
        if value:
            env[key] = value.rstrip("/")
    lines = [f"{k}={v}" for k, v in env.items()]
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(ENV_PATH, 0o600)
    print("\nEnregistré dans .env (lisible par vous seul). Vérifiez avec : python3 grafana_cloud.py tester")


def credentials():
    env = read_env()
    missing = [k for k in KEYS if not env.get(k)]
    if missing:
        raise CloudError("Identifiants manquants : " + ", ".join(missing) + ". Lancez « python3 grafana_cloud.py cles ».")
    return env


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def basic(user, token):
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


def request(url, data=None, auth=None, content_type="application/json", method=None, timeout=20):
    body = data if isinstance(data, (bytes, type(None))) else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method or ("POST" if body is not None else "GET"),
                                 headers={"Content-Type": content_type, "Authorization": auth or "",
                                          "User-Agent": "crypto-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw and resp.headers.get_content_type() == "application/json" else raw
    except urllib.error.HTTPError as e:
        raise CloudError(f"{url.split('?')[0]} → HTTP {e.code} : {e.read()[:200].decode(errors='replace')}") from e
    except urllib.error.URLError as e:
        raise CloudError(f"{url.split('?')[0]} injoignable : {e.reason}") from e


# --------------------------------------------------------------------------- #
# Format Influx (lignes) : mesure,étiquettes champs horodatage
# --------------------------------------------------------------------------- #


def _esc_tag(v):
    return str(v).replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def line(measurement, tags, fields, ts_ns):
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return None
    tag_part = "".join(f",{k}={_esc_tag(v)}" for k, v in sorted(tags.items()) if v not in (None, ""))
    field_part = ",".join(f"{k}={float(v)}" for k, v in fields.items())
    return f"{measurement}{tag_part} {field_part} {ts_ns}"


def collect_lines(db, now_ns):
    """Dernier état de chaque table → lignes Influx (une métrique Prometheus par champ)."""
    out = []
    last_ts = db.execute("SELECT MAX(ts) FROM valeurs").fetchone()[0]
    for strat, valeur, depart, realise, latent, trades, gagnants in db.execute(
            "SELECT strategie, valeur, depart, realise, latent, trades, gagnants FROM valeurs WHERE ts = ?", (last_ts,)):
        out.append(line("agent_valeur", {"strategie": strat},
                        {"valeur": valeur, "gain": valeur - depart, "realise": realise, "latent": latent,
                         "trades": trades, "gagnants": gagnants}, now_ns))
    row = db.execute("SELECT flux_msg_s, retard_ms, traitement_us, reconnexions, contrats, fantomes_actifs, "
                     "exemples, positions, en_ligne, ts FROM sante ORDER BY ts DESC LIMIT 1").fetchone()
    if row:
        names = ("flux_msg_s", "retard_ms", "traitement_us", "reconnexions", "contrats", "fantomes_actifs",
                 "exemples", "positions", "en_ligne")
        fields = dict(zip(names, row[:-1]))
        fields["en_ligne"] = int(bool(row[-2]) and time.time() - row[-1] < 60)
        out.append(line("agent_sante", {}, fields, now_ns))
    for strat, crypto, variante, etat, notionnel, pnl in db.execute(
            "SELECT strategie, crypto, variante, etat, notionnel, pnl FROM positions"):
        out.append(line("agent_position", {"strategie": strat, "crypto": crypto, "variante": variante, "etat": etat},
                        {"notionnel": notionnel, "pnl": pnl}, now_ns))
    for strat, n, wins, pnl, net, explo in db.execute(
            "SELECT strategie, COUNT(*), SUM(pnl > 0), SUM(pnl), AVG(net_bps), SUM(exploration) FROM trades "
            "WHERE ts >= strftime('%s', 'now', 'start of day', 'utc') GROUP BY strategie"):
        out.append(line("agent_jour", {"strategie": strat},
                        {"trades": n, "gagnants_pct": 100.0 * (wins or 0) / n if n else None, "resultat": pnl,
                         "net_bps": net, "exploration": explo}, now_ns))
    for row in db.execute("SELECT variante, strategie, exemples, gagnants, annonce, reel, net_moyen, execution, "
                          "validation_bps, valide FROM modeles"):
        variante, strat, *vals = row
        names = ("exemples", "gagnants", "annonce", "reel", "net_moyen", "execution", "validation_bps", "valide")
        out.append(line("agent_modele", {"variante": variante, "strategie": strat}, dict(zip(names, vals)), now_ns))
    return [x for x in out if x]


def collect_trades(db, since_ts):
    """Trades fermés depuis since_ts → flux Loki, regroupés par stratégie."""
    streams = {}
    last = since_ts
    for ts, strat, crypto, sens, variante, explo, duree, brut, net, pnl, raison in db.execute(
            "SELECT ts, strategie, crypto, sens, variante, exploration, duree_s, brut_bps, net_bps, pnl, raison "
            "FROM trades WHERE ts > ? ORDER BY ts LIMIT 5000", (since_ts,)):
        entry = {"crypto": crypto, "sens": sens, "variante": variante, "exploration": bool(explo), "duree_s": duree,
                 "brut_bps": brut, "net_bps": net, "pnl": pnl, "sortie": raison}
        streams.setdefault(strat, []).append([str(ts * 1_000_000_000), json.dumps(entry, ensure_ascii=False)])
        last = max(last, ts)
    payload = {"streams": [{"stream": {"app": "crypto-agent", "strategie": s}, "values": v}
                           for s, v in streams.items()]}
    return payload, last, sum(len(v) for v in streams.values())


# --------------------------------------------------------------------------- #
# Envoi
# --------------------------------------------------------------------------- #


class Pusher:
    def __init__(self, env, db_path=metriques.DB_PATH):
        self.env = env
        self.db = sqlite3.connect(db_path)
        self.state = {"trades_ts": 0}
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, encoding="utf-8") as f:
                self.state = json.load(f)

    def push_metrics(self):
        lines = collect_lines(self.db, time.time_ns())
        if lines:
            request(self.env["GRAFANA_CLOUD_INFLUX_URL"], "\n".join(lines).encode(),
                    basic(self.env["GRAFANA_CLOUD_METRICS_USER"], self.env["GRAFANA_CLOUD_TOKEN"]), "text/plain")
        return len(lines)

    def push_trades(self):
        payload, last, n = collect_trades(self.db, self.state["trades_ts"])
        if n:
            url = self.env["GRAFANA_CLOUD_LOGS_URL"] + "/loki/api/v1/push"
            request(url, payload, basic(self.env["GRAFANA_CLOUD_LOGS_USER"], self.env["GRAFANA_CLOUD_TOKEN"]))
            self.state["trades_ts"] = last
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
        return n

    def run(self):
        print(f"Envoi vers Grafana Cloud toutes les {PERIOD_S} s (Ctrl+C pour arrêter).", flush=True)
        while True:
            try:
                m, t = self.push_metrics(), self.push_trades()
                if t:
                    print(f"{time.strftime('%H:%M:%S')} {m} métriques, {t} trades envoyés", flush=True)
            except (CloudError, sqlite3.Error) as e:  # réseau ou quota : on retente au prochain tour
                print(f"{time.strftime('%H:%M:%S')} envoi impossible : {e}", flush=True)
            time.sleep(PERIOD_S)


# --------------------------------------------------------------------------- #
# Tableau de bord Cloud (Prometheus + Loki)
# --------------------------------------------------------------------------- #


DEFAULT_SOURCES = ({"type": "prometheus", "uid": "grafanacloud-prom"}, {"type": "loki", "uid": "grafanacloud-logs"})


def datasources(env):
    """Sources Prometheus et Loki de la pile. Si le jeton (rôle Editor) ne peut pas les lister,
    on utilise les identifiants standard de Grafana Cloud."""
    auth = "Bearer " + env["GRAFANA_CLOUD_SA_TOKEN"]
    try:
        sources = request(env["GRAFANA_CLOUD_URL"] + "/api/datasources", auth=auth)
    except CloudError as e:
        if "HTTP 403" not in str(e):
            raise
        return DEFAULT_SOURCES
    # Pile Grafana Cloud : « grafanacloud-<pile>-prom » et « grafanacloud-<pile>-logs » ; on écarte les
    # sources annexes (usage, historique des alertes…).
    prom = next((d for d in sources if d["type"] == "prometheus" and d["name"].endswith("-prom")), None)
    loki = next((d for d in sources if d["type"] == "loki" and d["name"].endswith("-logs")), None)
    if not prom or not loki:
        raise CloudError("Sources Prometheus ou Loki introuvables dans la pile Grafana Cloud.")
    return {"type": "prometheus", "uid": prom["uid"]}, {"type": "loki", "uid": loki["uid"]}


def check_query(env, prom):
    """Exécute une requête sur la source Prometheus : prouve qu'elle existe et reçoit nos données."""
    now = int(time.time() * 1000)
    body = {"queries": [{"refId": "A", "datasource": prom, "expr": "agent_sante_flux_msg_s", "instant": True,
                         "range": False}], "from": str(now - 600000), "to": str(now)}
    res = request(env["GRAFANA_CLOUD_URL"] + "/api/ds/query", body, "Bearer " + env["GRAFANA_CLOUD_SA_TOKEN"])
    return len(res["results"]["A"].get("frames", []))


def build_dashboard(prom, loki):
    ids = iter(range(1, 1000))
    green, red, blue = "#73BF69", "#F2495C", "#5794F2"
    pnl_steps = {"mode": "absolute", "steps": [{"color": red, "value": None}, {"color": green, "value": 0}]}

    def target(expr, *, ds=prom, ref="A", fmt="time_series", instant=False, legend=""):
        return {"refId": ref, "datasource": ds, "expr": expr, "format": fmt, "instant": instant,
                "range": not instant, "legendFormat": legend}

    def panel(kind, title, grid, targets, *, unit=None, steps=None, decimals=2, options=None, transformations=None,
              color=None, overrides=None):
        defaults = {"decimals": decimals}
        if unit:
            defaults["unit"] = unit
        if steps:
            defaults["thresholds"], defaults["color"] = steps, {"mode": "thresholds"}
        if color:
            defaults["color"] = {"mode": "fixed", "fixedColor": color}
        return {"id": next(ids), "type": kind, "title": title, "gridPos": dict(zip("xywh", grid)),
                "datasource": targets[0]["datasource"], "targets": targets,
                "fieldConfig": {"defaults": defaults, "overrides": overrides or []}, "options": options or {},
                "transformations": transformations or []}

    def stat(title, x, expr, *, steps=None, color=None):
        return panel("stat", title, (x, 0, 4, 4), [target(expr, instant=True)], unit="currencyUSD", steps=steps,
                     color=color, options={"reduceOptions": {"calcs": ["lastNotNull"]}, "graphMode": "area",
                                           "colorMode": "value"})

    def g(strategy, field="gain"):
        return f'agent_valeur_{field}{{strategie="{strategy}"}}'

    online = panel("stat", "Agent", (20, 0, 4, 4), [target("max(agent_sante_en_ligne)", instant=True)],
                   decimals=0, steps={"mode": "absolute", "steps": [{"color": red, "value": None},
                                                                    {"color": green, "value": 1}]},
                   options={"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background"})
    online["fieldConfig"]["defaults"]["mappings"] = [{"type": "value", "options": {
        "0": {"text": "ARRÊTÉ", "color": red}, "1": {"text": "EN LIGNE", "color": green}}}]
    panels = [
        stat("Valeur", 0, g("total", "valeur"), color=blue),
        stat("Gain total", 4, g("total"), steps=pnl_steps),
        stat("Scalping", 8, g("scalping"), steps=pnl_steps),
        stat("Tendance", 12, g("tendance"), steps=pnl_steps),
        stat("Financement", 16, g("financement"), steps=pnl_steps),
        online,
        panel("timeseries", "Gain / perte par stratégie ($)", (0, 4, 16, 9),
              [target("agent_valeur_gain", legend="{{strategie}}")], unit="currencyUSD",
              options={"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "min", "max"]}}),
        panel("stat", "Santé de l'agent", (16, 4, 8, 9), [
            target("agent_sante_flux_msg_s", instant=True, legend="Flux (msg/s)", ref="A"),
            target("agent_sante_retard_ms", instant=True, legend="Retard (ms)", ref="B"),
            target("agent_sante_reconnexions", instant=True, legend="Reconnexions", ref="C"),
            target("agent_sante_contrats", instant=True, legend="Contrats suivis", ref="D"),
            target("agent_sante_positions", instant=True, legend="Positions scalping", ref="E")],
              decimals=0, color=blue, options={"reduceOptions": {"calcs": ["lastNotNull"]}, "textMode": "value_and_name",
                                               "orientation": "horizontal", "graphMode": "none"}),
    ]
    y = 13
    for i, (s, label) in enumerate((("court", "⚡ Court terme"), ("longue", "⏱ Long terme"))):
        x = 12 * i
        panels.append(panel("stat", f"{label} · aujourd'hui", (x, y, 12, 4), [
            target(f'agent_jour_trades{{strategie="{s}"}}', instant=True, legend="Trades", ref="A"),
            target(f'agent_jour_gagnants_pct{{strategie="{s}"}}', instant=True, legend="Gagnants (%)", ref="B"),
            target(f'agent_jour_resultat{{strategie="{s}"}}', instant=True, legend="Résultat ($)", ref="C"),
            target(f'agent_jour_net_bps{{strategie="{s}"}}', instant=True, legend="Net / trade (pb)", ref="D"),
            target(f'agent_jour_exploration{{strategie="{s}"}}', instant=True, legend="Exploration", ref="E")],
            decimals=1, options={"reduceOptions": {"calcs": ["lastNotNull"]}, "textMode": "value_and_name",
                                 "graphMode": "none"}))
        panels.append(panel(
            "table", f"{label} · positions ouvertes", (x, y + 4, 12, 7),
            [target(f'max by (crypto, variante, etat) (agent_position_pnl{{strategie="{s}"}})', instant=True, fmt="table")],
            unit="currencyUSD",
            transformations=[{"id": "organize", "options": {
                "excludeByName": {"Time": True, "__name__": True, "strategie": True, "instance": True, "job": True, "__proxy_source__": True},
                "renameByName": {"Value": "P&L", "crypto": "Crypto", "variante": "Variante", "etat": "État"}}}],
            overrides=[{"matcher": {"id": "byName", "options": "P&L"}, "properties": [
                {"id": "custom.cellOptions", "value": {"type": "color-text"}}, {"id": "thresholds", "value": pnl_steps},
                {"id": "color", "value": {"mode": "thresholds"}}]}]))
    y += 11
    panels.append(panel(
        "table", "📈 Tendance et ⚖ financement · positions ouvertes", (0, y, 12, 9),
        [target('max by (strategie, crypto, variante) (agent_position_pnl{strategie=~"tendance|financement"})',
                instant=True, fmt="table")],
        unit="currencyUSD",
        transformations=[{"id": "organize", "options": {
            "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True, "__proxy_source__": True, "etat": True},
            "renameByName": {"Value": "P&L / financement", "crypto": "Crypto", "variante": "Sens",
                             "strategie": "Stratégie"}}}]))
    panels.append(panel(
        "table", "Derniers trades", (12, y, 12, 9),
        [{"refId": "A", "datasource": loki, "expr": '{app="crypto-agent"} | json', "queryType": "range"}],
        transformations=[{"id": "extractFields", "options": {"source": "Line"}},
                         {"id": "organize", "options": {"excludeByName": {
                             "Line": True, "id": True, "tsNs": True, "labels": True, "labelTypes": True,
                             "traceID": True, "traceID (field)": True}}}],
        decimals=1))
    y += 9
    model_fields = (("exemples", "Exemples"), ("gagnants", "Gagnants"), ("reel", "Réel"), ("net_moyen", "Net moyen (pb)"),
                    ("validation_bps", "Validation (pb)"), ("valide", "Trade réel autorisé"))
    panels.append(panel(
        "table", "Ce que l'agent a appris (une ligne par variante)", (0, y, 24, 12),
        # « max by » retire les étiquettes techniques (__name__…) : les 6 requêtes fusionnent en une ligne par variante.
        [target(f"max by (variante, strategie) (agent_modele_{f})", instant=True, fmt="table", ref=chr(65 + i))
         for i, (f, _) in enumerate(model_fields)],
        transformations=[{"id": "merge", "options": {}},
                         {"id": "organize", "options": {
                             "excludeByName": {"Time": True, "instance": True, "job": True, "__proxy_source__": True},
                             "renameByName": {f"Value #{chr(65 + i)}": label for i, (_, label) in enumerate(model_fields)}
                             | {"variante": "Variante", "strategie": "Stratégie"}}}],
        decimals=2))
    return {"uid": "crypto-agent", "title": "Crypto Agent", "tags": ["crypto", "agent"], "timezone": "browser",
            "refresh": "30s", "time": {"from": "now-6h", "to": "now"}, "schemaVersion": 39, "panels": panels,
            "description": "Agent de trading crypto en mode papier : données envoyées depuis le Mac (grafana_cloud.py)."}


def publish_dashboard(env):
    prom, loki = datasources(env)
    body = {"dashboard": build_dashboard(prom, loki), "overwrite": True,
            "message": "Mis à jour par grafana_cloud.py"}
    res = request(env["GRAFANA_CLOUD_URL"] + "/api/dashboards/db", body, "Bearer " + env["GRAFANA_CLOUD_SA_TOKEN"])
    return env["GRAFANA_CLOUD_URL"] + res.get("url", "/d/crypto-agent")


def main():
    p = argparse.ArgumentParser(description="Envoi des données de l'agent vers Grafana Cloud.")
    p.add_argument("cmd", nargs="?", default="lancer", choices=["lancer", "cles", "tester", "tableau"])
    args = p.parse_args()
    if args.cmd == "cles":
        setup_keys()
        return
    try:
        env = credentials()
        if args.cmd == "tester":
            pusher = Pusher(env)
            print(f"Métriques envoyées : {pusher.push_metrics()} séries")
            print(f"Trades envoyés : {pusher.push_trades()}")
            prom, loki = datasources(env)
            n = check_query(env, prom)
            print(f"API de la pile : OK (Prometheus {prom['uid']} : {n} série(s) agent_sante lues, Loki {loki['uid']})")
        elif args.cmd == "tableau":
            print("Tableau de bord publié :", publish_dashboard(env))
        else:
            Pusher(env).run()
    except CloudError as e:
        raise SystemExit(f"Erreur : {e}") from None


if __name__ == "__main__":
    main()
