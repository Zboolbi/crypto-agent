#!/usr/bin/env python3
"""
Génère le tableau de bord Grafana de l'agent (grafana/tableaux/agent.json).

Toutes les requêtes lisent la base SQLite remplie par metriques.py (source « Agent crypto »).
Relancer ce script après toute modification, Grafana recharge le fichier en 30 s.

    python3 grafana/generer_tableau.py
"""

import itertools
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "tableaux", "agent.json")
DS = {"type": "frser-sqlite-datasource", "uid": "agent-sqlite"}
RANGE = "ts >= $__from / 1000 AND ts <= $__to / 1000"
# Regroupement adaptatif : au plus ~600 points par courbe quelle que soit la période.
BUCKET = "MAX(5, ($__to - $__from) / 600000)"

GREEN, RED, BLUE, PURPLE, ORANGE = "#73BF69", "#F2495C", "#5794F2", "#B877D9", "#FF9830"
PNL_STEPS = [{"color": RED, "value": None}, {"color": GREEN, "value": 0}]

_ids = itertools.count(1)  # identifiants uniques des panneaux


def panel(kind, title, grid, sql, *, fmt="table", time_cols=(), desc="", unit=None, decimals=None,
          steps=None, color_mode=None, options=None, overrides=None, custom=None, transformations=None):
    defaults = {"color": {"mode": color_mode or ("thresholds" if steps else "palette-classic")}}
    if steps:
        defaults["thresholds"] = {"mode": "absolute", "steps": steps}
    if unit:
        defaults["unit"] = unit
    if decimals is not None:
        defaults["decimals"] = decimals
    if custom:
        defaults["custom"] = custom
    p = {"id": next(_ids), "type": kind, "title": title, "description": desc, "datasource": DS,
         "gridPos": dict(zip(("x", "y", "w", "h"), grid)),
         "targets": [{"refId": "A", "datasource": DS, "queryType": fmt, "rawQueryText": sql, "queryText": sql,
                      "timeColumns": list(time_cols)}],
         "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
         "options": options or {}}
    if transformations:
        p["transformations"] = transformations
    return p


def row(title, y):
    return {"id": next(_ids), "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}


def stat(title, grid, sql, *, unit="currencyUSD", steps=None, desc="", decimals=2, fixed=None):
    color = {"mode": "fixed", "fixedColor": fixed} if fixed else None
    p = panel("stat", title, grid, sql, unit=unit, steps=steps, desc=desc, decimals=decimals,
              options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                       "colorMode": "value", "graphMode": "none", "textMode": "value_and_name" if fixed is None and
                       steps is None else "auto", "justifyMode": "center"})
    if color:
        p["fieldConfig"]["defaults"]["color"] = color
    return p


def pnl_color(field):
    return {"matcher": {"id": "byName", "options": field},
            "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                           {"id": "thresholds", "value": {"mode": "absolute", "steps": PNL_STEPS}},
                           {"id": "color", "value": {"mode": "thresholds"}}]}


def unit_of(field, unit, decimals=2):
    return {"matcher": {"id": "byName", "options": field},
            "properties": [{"id": "unit", "value": unit}, {"id": "decimals", "value": decimals}]}


def latest(strategy, expr):
    return f"SELECT {expr} AS v FROM valeurs WHERE strategie = '{strategy}' ORDER BY ts DESC LIMIT 1"


def build():
    panels = [row("Vue d'ensemble · portefeuilles fictifs (mode papier)", 0)]
    y = 1
    panels += [
        stat("Valeur", (0, y, 4, 4), latest("total", "valeur"), fixed=BLUE,
             desc="Somme des trois portefeuilles fictifs (scalping, tendance, financement)."),
        stat("Gain total", (4, y, 4, 4), latest("total", "valeur - depart"), steps=PNL_STEPS,
             desc="Valeur actuelle moins capital de départ, positions ouvertes comprises."),
        stat("Scalping", (8, y, 4, 4), latest("scalping", "valeur - depart"), steps=PNL_STEPS,
             desc="Court terme et long terme (30 s à 10 min) réunis."),
        stat("Tendance", (12, y, 4, 4), latest("tendance", "valeur - depart"), steps=PNL_STEPS),
        stat("Financement", (16, y, 4, 4), latest("financement", "valeur - depart"), steps=PNL_STEPS),
        stat("Agent", (20, y, 4, 4),
             "SELECT CASE WHEN en_ligne = 1 AND ts > strftime('%s','now') - 30 THEN 'EN LIGNE' ELSE 'ARRÊTÉ' END AS v "
             "FROM sante ORDER BY ts DESC LIMIT 1", unit="none", decimals=None,
             steps=[{"color": RED, "value": None}], desc="État du flux temps réel du scalping."),
    ]
    agent = panels[-1]
    agent["options"]["reduceOptions"]["fields"] = "/^v$/"  # valeur texte : à inclure explicitement
    agent["fieldConfig"]["defaults"]["mappings"] = [
        {"type": "value", "options": {"EN LIGNE": {"color": GREEN, "index": 0}, "ARRÊTÉ": {"color": RED, "index": 1}}}]
    y += 4

    pnl_curve = (f"SELECT CAST(ts / {BUCKET} AS INTEGER) * {BUCKET} AS time, "
                 + ", ".join(f"MAX(CASE WHEN strategie = '{s}' THEN valeur - depart END) AS \"{label}\""
                             for s, label in (("scalping", "Scalping"), ("tendance", "Tendance"),
                                              ("financement", "Financement"), ("total", "Total")))
                 + f" FROM valeurs WHERE {RANGE} GROUP BY 1 ORDER BY 1")
    panels.append(panel("timeseries", "Gain / perte par stratégie ($)", (0, y, 16, 9), pnl_curve, time_cols=["time"],
                        unit="currencyUSD", decimals=2, custom={"lineWidth": 2, "fillOpacity": 8, "spanNulls": True},
                        options={"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "min", "max"]},
                                 "tooltip": {"mode": "multi"}},
                        overrides=[{"matcher": {"id": "byName", "options": n},
                                    "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
                                   for n, c in (("Scalping", BLUE), ("Tendance", ORANGE), ("Financement", PURPLE),
                                                ("Total", GREEN))]))
    panels.append(panel(
        "stat", "Santé de l'agent", (16, y, 8, 9),
        "SELECT flux_msg_s AS \"Flux (msg/s)\", retard_ms AS \"Retard (ms)\", traitement_us AS \"Traitement (µs)\", "
        "reconnexions AS \"Reconnexions\", contrats AS \"Contrats suivis\", fantomes_actifs AS \"Fantômes en cours\", "
        "positions AS \"Positions scalping\" FROM sante ORDER BY ts DESC LIMIT 1",
        decimals=0, color_mode="fixed",
        options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "colorMode": "value",
                 "graphMode": "none", "textMode": "value_and_name", "orientation": "horizontal"}))
    panels[-1]["fieldConfig"]["defaults"]["color"] = {"mode": "fixed", "fixedColor": BLUE}
    y += 9

    panels.append(row("Scalping · ⚡ court terme (20 s à 3 min) et ⏱ long terme (30 s à 10 min)", y))
    y += 1
    for i, (strategy, label, color) in enumerate((("court", "⚡ Court terme", BLUE), ("longue", "⏱ Long terme", PURPLE))):
        x = 12 * i
        summary = (f"SELECT COUNT(*) AS \"Trades\", ROUND(100.0 * SUM(pnl > 0) / MAX(COUNT(*), 1), 1) AS \"Gagnants (%)\", "
                   f"ROUND(SUM(pnl), 2) AS \"Résultat ($)\", ROUND(AVG(net_bps), 1) AS \"Net / trade (pb)\", "
                   f"SUM(exploration) AS \"Exploration\" FROM trades WHERE strategie = '{strategy}' AND {RANGE}")
        p = panel("stat", f"{label} · synthèse sur la période", (x, y, 12, 4), summary, decimals=None,
                  options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                           "colorMode": "value", "graphMode": "none", "textMode": "value_and_name"},
                  overrides=[{"matcher": {"id": "byRegexp", "options": "Résultat.*|Net.*"},
                              "properties": [{"id": "thresholds", "value": {"mode": "absolute", "steps": PNL_STEPS}},
                                             {"id": "color", "value": {"mode": "thresholds"}}]}])
        p["fieldConfig"]["defaults"]["color"] = {"mode": "fixed", "fixedColor": color}
        panels.append(p)
        panels.append(panel(
            "timeseries", f"{label} · résultat de chaque trade ($)", (x, y + 4, 12, 8),
            f"SELECT ts AS time, pnl AS \"Résultat\" FROM trades WHERE strategie = '{strategy}' AND {RANGE} ORDER BY ts",
            time_cols=["time"], unit="currencyUSD", decimals=2, steps=PNL_STEPS, color_mode="thresholds",
            custom={"drawStyle": "bars", "fillOpacity": 80, "lineWidth": 1,
                    "thresholdsStyle": {"mode": "off"}, "gradientMode": "scheme"},
            options={"legend": {"showLegend": False}, "tooltip": {"mode": "single"}}))
        panels.append(panel(
            "table", f"{label} · positions ouvertes", (x, y + 12, 12, 6),
            f"SELECT crypto AS \"Crypto\", variante AS \"Variante\", etat AS \"État\", notionnel AS \"Taille\", "
            f"entree AS \"Entrée\", actuel AS \"Actuel\", pnl AS \"P&L\" FROM positions WHERE strategie = '{strategy}'",
            overrides=[pnl_color("P&L"), unit_of("Taille", "currencyUSD", 0), unit_of("P&L", "currencyUSD")]))
    y += 18

    panels.append(row("📈 Tendance et ⚖ financement (positions de plusieurs heures à plusieurs jours)", y))
    y += 1
    panels.append(panel(
        "table", "📈 Tendance · positions ouvertes", (0, y, 12, 8),
        "SELECT crypto AS \"Crypto\", variante AS \"Sens\", notionnel AS \"Taille\", entree AS \"Entrée\", "
        "actuel AS \"Actuel\", pnl AS \"P&L\" FROM positions WHERE strategie = 'tendance' ORDER BY pnl DESC",
        overrides=[pnl_color("P&L"), unit_of("Taille", "currencyUSD", 0), unit_of("P&L", "currencyUSD")]))
    panels.append(panel(
        "table", "⚖ Financement · positions ouvertes", (12, y, 12, 8),
        "SELECT crypto AS \"Contrat\", notionnel AS \"Taille\", pnl AS \"Financement encaissé\" "
        "FROM positions WHERE strategie = 'financement'",
        overrides=[pnl_color("Financement encaissé"), unit_of("Taille", "currencyUSD", 0),
                   unit_of("Financement encaissé", "currencyUSD", 4)]))
    y += 8

    panels.append(row("Trades, sorties et cryptos", y))
    y += 1
    panels.append(panel(
        "table", "Derniers trades (toutes stratégies)", (0, y, 24, 9),
        f"SELECT ts AS \"Heure\", strategie AS \"Stratégie\", crypto AS \"Crypto\", "
        f"CASE WHEN exploration = 1 THEN 'exploration · ' ELSE '' END || variante AS \"Variante\", "
        f"duree_s AS \"Durée (s)\", net_bps AS \"Net (pb)\", pnl AS \"P&L\", raison AS \"Sortie\" "
        f"FROM trades WHERE {RANGE} ORDER BY ts DESC LIMIT 200",
        time_cols=["Heure"],
        overrides=[pnl_color("P&L"), pnl_color("Net (pb)"), unit_of("P&L", "currencyUSD"),
                   unit_of("Durée (s)", "s", 1), unit_of("Net (pb)", "none", 1)]))
    y += 9
    panels.append(panel(
        "barchart", "Résultat par raison de sortie ($)", (0, y, 12, 8),
        f"SELECT CASE WHEN raison LIKE 'durée max%' THEN 'durée max' ELSE raison END AS \"Sortie\", "
        f"ROUND(SUM(pnl), 2) AS \"Résultat\", COUNT(*) AS \"Trades\" FROM trades WHERE {RANGE} GROUP BY 1 ORDER BY 2",
        unit="currencyUSD", steps=PNL_STEPS, color_mode="thresholds",
        options={"orientation": "horizontal", "xField": "Sortie", "showValue": "always", "legend": {"showLegend": False}},
        overrides=[{"matcher": {"id": "byName", "options": "Trades"},
                    "properties": [{"id": "custom.hideFrom", "value": {"viz": True, "legend": True, "tooltip": False}}]}]))
    panels.append(panel(
        "barchart", "Résultat par crypto : 8 meilleures et 8 pires ($)", (12, y, 12, 8),
        f"WITH r AS (SELECT crypto, ROUND(SUM(pnl), 2) AS total FROM trades WHERE {RANGE} GROUP BY crypto) "
        f"SELECT * FROM (SELECT crypto AS \"Crypto\", total AS \"Résultat\" FROM r ORDER BY total DESC LIMIT 8) "
        f"UNION SELECT * FROM (SELECT crypto, total FROM r ORDER BY total ASC LIMIT 8) ORDER BY 2 DESC",
        unit="currencyUSD", steps=PNL_STEPS, color_mode="thresholds",
        options={"orientation": "horizontal", "xField": "Crypto", "showValue": "auto", "legend": {"showLegend": False}}))
    y += 8

    panels.append(row("Apprentissage", y))
    y += 1
    panels.append(panel(
        "table", "Ce que l'agent a appris : une ligne par variante (famille / profil / entrée)", (0, y, 24, 12),
        "SELECT strategie AS \"Stratégie\", variante AS \"Variante\", exemples AS \"Exemples\", "
        "gagnants * 100 AS \"Gagnants (%)\", annonce * 100 AS \"Annoncé (%)\", reel * 100 AS \"Réel (%)\", "
        "net_moyen AS \"Net moyen (pb)\", execution * 100 AS \"Ordres limite exécutés (%)\", "
        "validation_bps AS \"Validation (pb)\", validation_n AS \"Cas validés\", "
        "CASE WHEN valide = 1 THEN 'oui' ELSE 'non' END AS \"Trade réel autorisé\" "
        "FROM modeles ORDER BY valide DESC, validation_bps DESC",
        desc="« Annoncé → réel » : calibrage des probabilités. « Net moyen » : résultat net réel moyen des trades "
             "fantômes. Une variante ne trade avec du capital que si sa validation hors échantillon dépasse 1 pb.",
        overrides=[pnl_color("Net moyen (pb)"), pnl_color("Validation (pb)"),
                   *[unit_of(f, "none", 1) for f in ("Gagnants (%)", "Annoncé (%)", "Réel (%)", "Net moyen (pb)",
                                                      "Ordres limite exécutés (%)", "Validation (pb)")]]))

    # Seconde rangée : fonds disponibles, performance, réussite, échec et détail par stratégie.
    for p in panels:
        if p["gridPos"]["y"] >= 5:
            p["gridPos"]["y"] += 5
    rate_steps_up = [{"color": RED, "value": None}, {"color": ORANGE, "value": 50}, {"color": GREEN, "value": 55}]
    rate_steps_down = [{"color": GREEN, "value": None}, {"color": ORANGE, "value": 45}, {"color": RED, "value": 50}]
    panels += [
        stat("Fonds disponibles", (0, 5, 4, 5), latest("total", "disponible"), fixed=BLUE,
             desc="Valeur des trois portefeuilles moins le capital immobilisé dans les positions ouvertes "
                  "(taille pour le scalping, marge pour la tendance, trésorerie pour le financement)."),
        stat("Performance", (4, 5, 4, 5), latest("total", "100.0 * (valeur - depart) / depart"), unit="percent",
             steps=PNL_STEPS, decimals=1, desc="Gain ou perte total rapporté au capital de départ."),
        stat("Taux de réussite", (8, 5, 4, 5), latest("total", "100.0 * gagnants / MAX(trades, 1)"), unit="percent",
             steps=rate_steps_up, decimals=1, desc="Part des trades fermés gagnants, frais compris."),
        stat("Taux d'échec", (12, 5, 4, 5), latest("total", "100.0 * (trades - gagnants) / MAX(trades, 1)"),
             unit="percent", steps=rate_steps_down, decimals=1, desc="Part des trades fermés perdants ou nuls."),
        panel("table", "Par stratégie", (16, 5, 8, 5),
              "SELECT strategie AS \"Stratégie\", ROUND(100.0 * (valeur - depart) / depart, 2) AS \"Performance (%)\", "
              "ROUND(100.0 * gagnants / MAX(trades, 1), 1) AS \"Réussite (%)\", "
              "ROUND(100.0 * (trades - gagnants) / MAX(trades, 1), 1) AS \"Échec (%)\", "
              "ROUND(disponible, 2) AS \"Disponible ($)\" FROM valeurs WHERE ts = (SELECT MAX(ts) FROM valeurs) "
              "AND strategie != 'total'",
              overrides=[pnl_color("Performance (%)"), unit_of("Disponible ($)", "currencyUSD")]),
    ]
    return {"uid": "crypto-agent", "title": "Crypto Agent", "tags": ["crypto", "agent"], "timezone": "browser",
            "refresh": "5s", "time": {"from": "now-6h", "to": "now"}, "schemaVersion": 39, "version": 1,
            "editable": False, "graphTooltip": 1, "panels": panels,
            "description": "Agent de trading crypto en mode papier : données exportées par metriques.py."}


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(build(), f, indent=1, ensure_ascii=False)
    print(f"Tableau de bord écrit : {OUT}")
