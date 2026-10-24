#!/bin/bash
# Arrête et retire le service. Les positions ouvertes restent sur l'exchange (avec leurs stops).
MODE="${1:-papier}"
PLIST="$HOME/Library/LaunchAgents/com.cryptoagent.$MODE.plist"
[ -f "$PLIST" ] || { echo "Aucun service $MODE installé."; exit 0; }
launchctl unload -w "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "✓ Service com.cryptoagent.$MODE arrêté et retiré."
echo "  Pour tout fermer : python3 trader.py --mode $MODE fermer tout"
