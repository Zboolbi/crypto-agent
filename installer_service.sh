#!/bin/bash
# Installe l'agent et son tableau de bord comme services macOS (launchd) : ils démarrent à
# l'ouverture de session, redémarrent seuls s'ils s'arrêtent, et l'agent empêche la mise en veille.
# Usage : ./installer_service.sh [papier|testnet|reel]
set -e
MODE="${1:-papier}"
case "$MODE" in papier|testnet|reel) ;; *) echo "Usage : $0 [papier|testnet|reel]"; exit 1 ;; esac

DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -x "$DIR/.venv/bin/python" ]; then PY="$DIR/.venv/bin/python"; else PY="$(command -v python3)"; fi
AGENTS="$HOME/Library/LaunchAgents"
LABEL="com.cryptoagent.$MODE"
DASH="com.cryptoagent.dashboard"
EXTRA=""

if [ "$MODE" != "papier" ]; then
  [ -f "$DIR/.env" ] || { echo "Clés absentes : lancez d'abord « $PY trader.py cles »."; exit 1; }
  "$PY" -c "import ccxt" 2>/dev/null || { echo "ccxt manquant : $PY -m pip install -r requirements.txt"; exit 1; }
fi
if [ "$MODE" = "reel" ]; then
  echo "⚠  MODE RÉEL : l'agent va trader seul, en permanence, avec votre argent (short et levier compris)."
  echo "   Les pertes peuvent dépasser la mise."
  read -r -p "   Tapez « JE CONFIRME » : " ok
  [ "$ok" = "JE CONFIRME" ] || { echo "Annulé."; exit 1; }
  EXTRA="<string>--je-confirme-le-risque</string>"
fi
case "$DIR" in
  "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
    echo "ℹ  Dossier protégé par macOS : si le journal affiche « Operation not permitted »,"
    echo "   déplacez-le (ex. ~/crypto-agent) ou accordez l'accès complet au disque à $PY." ;;
esac

mkdir -p "$DIR/logs" "$AGENTS"
plist() {  # plist <label> <journal> <arguments XML...> ; variables d'environnement en plus : $EXTRA_ENV
  local label="$1" log="$2"; shift 2
  cat > "$AGENTS/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>$*</array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$log</string>
  <key>StandardErrorPath</key><string>$log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string>${EXTRA_ENV:-}</dict>
</dict>
</plist>
EOF
  launchctl unload "$AGENTS/$label.plist" 2>/dev/null || true
  launchctl load -w "$AGENTS/$label.plist"
}

plist "$LABEL" "$DIR/logs/agent_$MODE.log" \
  "<string>/usr/bin/caffeinate</string><string>-i</string><string>$PY</string><string>$DIR/trader.py</string>" \
  "<string>--mode</string><string>$MODE</string>$EXTRA<string>service</string>"
# Tableau de bord : Grafana local s'il est installé (grafana/installer_grafana.sh), sinon l'ancien tableau Python.
GF_BASE="$HOME/.local/share/crypto-grafana"
GF_HOME="$(ls -d "$GF_BASE"/grafana-* 2>/dev/null | tail -1 || true)"
plist "com.cryptoagent.metriques" "$DIR/logs/metriques.log" \
  "<string>$PY</string><string>$DIR/metriques.py</string><string>--mode</string><string>$MODE</string>"
# Envoi vers Grafana Cloud, si les identifiants ont été saisis (python3 grafana_cloud.py cles).
if grep -q '^GRAFANA_CLOUD_TOKEN=' "$DIR/.env" 2>/dev/null; then
  plist "com.cryptoagent.grafana-cloud" "$DIR/logs/grafana_cloud.log" \
    "<string>$PY</string><string>$DIR/grafana_cloud.py</string><string>lancer</string>"
fi
if [ -n "$GF_HOME" ] && [ -f "$GF_BASE/crypto-agent.ini" ]; then
  launchctl unload "$AGENTS/$DASH.plist" 2>/dev/null || true
  rm -f "$AGENTS/$DASH.plist"
  EXTRA_ENV="<key>CRYPTO_AGENT_DIR</key><string>$DIR</string><key>CRYPTO_AGENT_DB</key><string>$DIR/donnees/agent.db</string>" \
  plist "com.cryptoagent.grafana" "$DIR/logs/grafana.log" \
    "<string>$GF_HOME/bin/grafana</string><string>server</string><string>--homepath</string><string>$GF_HOME</string>" \
    "<string>--config</string><string>$GF_BASE/crypto-agent.ini</string>"
  DASH_URL="http://localhost:3000 (Grafana)"
else
  plist "$DASH" "$DIR/logs/dashboard.log" \
    "<string>$PY</string><string>$DIR/dashboard.py</string><string>--mode</string><string>$MODE</string>"
  DASH_URL="http://localhost:8050"
fi

if [ "$MODE" = "papier" ]; then
  # Stratégies comparées en parallèle, chacune avec son portefeuille fictif.
  plist "com.cryptoagent.funding" "$DIR/logs/funding_papier.log" \
    "<string>$PY</string><string>$DIR/funding.py</string><string>lancer</string>"
  plist "com.cryptoagent.tendance" "$DIR/logs/tendance_papier.log" \
    "<string>$PY</string><string>$DIR/trader.py</string><string>--mode</string><string>papier</string><string>auto</string>"
fi

echo "✓ Agent $LABEL et tableau de bord installés et démarrés (Python : $PY)."
echo "  Tableau de bord   : $DASH_URL"
echo "  Solde en direct   : $PY scalper.py solde --suivre"
echo "  Journal           : tail -f \"$DIR/logs/agent_$MODE.log\""
echo "  Arrêter           : ./desinstaller_service.sh $MODE"
