#!/bin/bash
# Installe l'agent comme service macOS (launchd) : il démarre à l'ouverture de session,
# redémarre seul s'il s'arrête et empêche la mise en veille pendant qu'il tourne.
# Usage : ./installer_service.sh [papier|testnet|reel]
set -e
MODE="${1:-papier}"
case "$MODE" in papier|testnet|reel) ;; *) echo "Usage : $0 [papier|testnet|reel]"; exit 1 ;; esac

DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
LABEL="com.cryptoagent.$MODE"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$DIR/logs/agent_$MODE.log"
EXTRA=""

if [ "$MODE" != "papier" ]; then
  [ -f "$DIR/.env" ] || { echo "Clés absentes : lancez d'abord « python3 trader.py cles »."; exit 1; }
  "$PY" -c "import ccxt" 2>/dev/null || { echo "Installez d'abord ccxt : pip3 install ccxt"; exit 1; }
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
    echo "ℹ  Le dossier est dans un emplacement protégé par macOS. Si le journal affiche"
    echo "   « Operation not permitted », déplacez le dossier (ex. ~/crypto-agent) puis réinstallez,"
    echo "   ou accordez « Accès complet au disque » à $PY (Réglages Système > Confidentialité)." ;;
esac

mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$PY</string><string>$DIR/trader.py</string>
    <string>--mode</string><string>$MODE</string>$EXTRA
    <string>service</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"
echo "✓ Service $LABEL installé et démarré."
echo "  Suivre l'activité : tail -f \"$LOG\""
echo "  Portefeuille      : python3 trader.py --mode $MODE portefeuille"
echo "  Arrêter           : ./desinstaller_service.sh $MODE"
