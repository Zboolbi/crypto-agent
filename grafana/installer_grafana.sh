#!/bin/bash
# Installe Grafana OSS en local (sans droits administrateur) avec le greffon SQLite,
# branché sur la base de l'agent (donnees/agent.db, remplie par metriques.py).
# Téléchargements officiels, empreintes SHA-256 vérifiées. Grafana n'écoute que sur ce Mac.
set -euo pipefail
GF_VERSION="13.2.3"
PLUGIN_VERSION="4.0.6"
PLUGIN_SHA256="e5ce2354dd26ba9bea56b37f9ed4050cc917a46b858a0a191e1cafda382bf18a"

DIR="$(cd "$(dirname "$0")/.." && pwd)"
BASE="$HOME/.local/share/crypto-grafana"
DL="$BASE/telechargements"
mkdir -p "$DL" "$BASE/data" "$BASE/logs" "$BASE/plugins"

ARCHIVE="grafana-$GF_VERSION.darwin-arm64.tar.gz"
if [ ! -d "$BASE/grafana-$GF_VERSION" ]; then
  echo "Téléchargement de Grafana $GF_VERSION…"
  curl -fsSL -o "$DL/$ARCHIVE" "https://dl.grafana.com/oss/release/$ARCHIVE"
  curl -fsSL -o "$DL/$ARCHIVE.sha256" "https://dl.grafana.com/oss/release/$ARCHIVE.sha256"
  echo "$(cat "$DL/$ARCHIVE.sha256" | awk '{print $1}')  $DL/$ARCHIVE" | shasum -a 256 -c -
  mkdir -p "$DL/extrait" && tar -xzf "$DL/$ARCHIVE" -C "$DL/extrait"
  mv "$DL/extrait"/grafana* "$BASE/grafana-$GF_VERSION" && rmdir "$DL/extrait"
fi

if [ ! -d "$BASE/plugins/frser-sqlite-datasource" ]; then
  echo "Téléchargement du greffon SQLite $PLUGIN_VERSION…"
  curl -fsSL -o "$DL/sqlite-$PLUGIN_VERSION.zip" \
    "https://grafana.com/api/plugins/frser-sqlite-datasource/versions/$PLUGIN_VERSION/download"
  echo "$PLUGIN_SHA256  $DL/sqlite-$PLUGIN_VERSION.zip" | shasum -a 256 -c -
  mkdir -p "$DL/greffon" && unzip -q -o "$DL/sqlite-$PLUGIN_VERSION.zip" -d "$DL/greffon"
  mv "$DL/greffon"/frser-sqlite-datasource "$BASE/plugins/" && rm -rf "$DL/greffon"
fi

# Mot de passe administrateur aléatoire, conservé dans .env (lecture libre en local sans connexion).
touch "$DIR/.env" && chmod 600 "$DIR/.env"
if ! grep -q '^GRAFANA_ADMIN_PASSWORD=' "$DIR/.env"; then
  echo "GRAFANA_ADMIN_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')" >> "$DIR/.env"
fi
PASSWORD="$(grep '^GRAFANA_ADMIN_PASSWORD=' "$DIR/.env" | cut -d= -f2-)"

cat > "$BASE/crypto-agent.ini" <<EOF
[paths]
data = $BASE/data
logs = $BASE/logs
plugins = $BASE/plugins
provisioning = $DIR/grafana/provisioning

[server]
http_addr = 127.0.0.1
http_port = 3000

[security]
admin_user = admin
admin_password = $PASSWORD

[auth.anonymous]
enabled = true
org_role = Viewer

[dashboards]
default_home_dashboard_path = $DIR/grafana/tableaux/agent.json

[analytics]
reporting_enabled = false
check_for_updates = false
check_for_plugin_updates = false

[news]
news_feed_enabled = false
EOF
chmod 600 "$BASE/crypto-agent.ini"
echo "✓ Grafana $GF_VERSION prêt dans $BASE (configuration : crypto-agent.ini)."
