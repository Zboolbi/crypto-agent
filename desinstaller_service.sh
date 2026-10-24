#!/bin/bash
# Arrête et retire l'agent et le tableau de bord. Les positions ouvertes restent sur l'exchange (avec leurs stops).
MODE="${1:-papier}"
for label in "com.cryptoagent.$MODE" "com.cryptoagent.dashboard"; do
  PLIST="$HOME/Library/LaunchAgents/$label.plist"
  [ -f "$PLIST" ] || continue
  launchctl unload -w "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✓ $label arrêté et retiré."
done
