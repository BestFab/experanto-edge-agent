#!/usr/bin/env bash
# Experanto Edge agent installer (Raspberry Pi / Debian).
# Usage:
#   sudo ./install.sh --code EXP-XXXX-XXXX --secret <SECRET> [--station <UUID>] \
#                     [--broker mqtt.experanto.it] [--datalogger-ip 192.168.1.50] \
#                     [--tailscale-authkey tskey-…] [--tailscale-login-server URL]
# With a Tailscale auth key the installer enables on-demand remote SSH (operator mode,
# no sudo): the agent brings a tunnel up only on the `open_ssh` command, then down.
#
# Layout (rollback-friendly, phase E5):
#   /opt/experanto-edge/releases/<name>/venv        one venv per release
#   /opt/experanto-edge/current -> releases/<name>  atomic symlink swapped by OTA
#   /opt/experanto-edge/ota-helper.sh               root helper (NOPASSWD sudoers)
# systemd runs current/venv/bin/experanto-edge; OTA installs a new release, probes
# it (--selfcheck), repoints `current`, restarts, health-checks, rolls back on fail.
set -euo pipefail

APP_DIR=/opt/experanto-edge
RELEASES="$APP_DIR/releases"
CFG_DIR=/etc/experanto-edge
STATE_DIR=/var/lib/experanto-edge
SVC_USER=experanto-edge
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

CODE=""; SECRET=""; STATION=""; BROKER=""; DL_IP=""; UPD_URL=""; UPD_KEY=""
TS_KEY=""; TS_LOGIN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --code) CODE="$2"; shift 2;;
    --secret) SECRET="$2"; shift 2;;
    --station) STATION="$2"; shift 2;;
    --broker) BROKER="$2"; shift 2;;
    --datalogger-ip) DL_IP="$2"; shift 2;;
    --update-base-url) UPD_URL="$2"; shift 2;;
    --update-pubkey) UPD_KEY="$2"; shift 2;;
    --tailscale-authkey) TS_KEY="$2"; shift 2;;
    --tailscale-login-server) TS_LOGIN="$2"; shift 2;;
    *) echo "arg sconosciuto: $1" >&2; exit 1;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "esegui con sudo/root" >&2; exit 1; }
if [[ -z "$CODE" ]]; then read -rp "Device code: " CODE; fi
if [[ -z "$SECRET" ]]; then read -rsp "Secret: " SECRET; echo; fi

echo "==> utente di sistema $SVC_USER"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"

echo "==> directory"
install -d -o "$SVC_USER" -g "$SVC_USER" "$STATE_DIR"
install -d "$CFG_DIR"
install -d "$RELEASES"

echo "==> venv + pacchetto (release 'bootstrap')"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip unattended-upgrades
BOOT="$RELEASES/bootstrap"
rm -rf "$BOOT"; mkdir -p "$BOOT"
python3 -m venv "$BOOT/venv"
"$BOOT/venv/bin/pip" install --upgrade pip -q
"$BOOT/venv/bin/pip" install -q "$SRC_DIR"
ln -sfn "$BOOT" "$APP_DIR/current"

echo "==> OTA helper + sudoers (NOPASSWD, solo questo script)"
install -m 750 -o root -g root "$SRC_DIR/ota/ota-helper.sh" "$APP_DIR/ota-helper.sh"
cat >/etc/sudoers.d/experanto-edge <<EOF
$SVC_USER ALL=(root) NOPASSWD: $APP_DIR/ota-helper.sh
EOF
chmod 440 /etc/sudoers.d/experanto-edge
visudo -cf /etc/sudoers.d/experanto-edge >/dev/null

echo "==> baseline unattended-upgrades (security)"
systemctl enable --now unattended-upgrades.service 2>/dev/null || true

echo "==> config + enrollment"
CFG="$CFG_DIR/config.yaml"
[[ -f "$CFG" ]] || install -m 640 "$SRC_DIR/config.example.yaml" "$CFG"
ENROLL="$CODE:$SECRET"; [[ -n "$STATION" ]] && ENROLL="$ENROLL:$STATION"
EXPERANTO_EDGE_CONFIG="$CFG" "$APP_DIR/current/venv/bin/experanto-edge" --enroll "$ENROLL" || true
if [[ -n "$BROKER" ]]; then sed -i "s/^broker_host:.*/broker_host: \"$BROKER\"/" "$CFG"; fi
if [[ -n "$DL_IP" ]]; then sed -i "s/^datalogger_ip:.*/datalogger_ip: \"$DL_IP\"/" "$CFG"; fi
# OTA config (delimitatore | perche' URL/base64 contengono /)
if [[ -n "$UPD_URL" ]]; then sed -i "s|^update_base_url:.*|update_base_url: \"$UPD_URL\"|" "$CFG"; fi
if [[ -n "$UPD_KEY" ]]; then sed -i "s|^update_public_key:.*|update_public_key: \"$UPD_KEY\"|" "$CFG"; fi
# Tailscale per l'accesso SSH remoto on-demand (solo se fornita una authkey)
if [[ -n "$TS_KEY" ]]; then
  echo "==> Tailscale (SSH remoto on-demand, operator mode per $SVC_USER)"
  command -v tailscale >/dev/null 2>&1 || curl -fsSL https://tailscale.com/install.sh | sh
  systemctl enable --now tailscaled 2>/dev/null || true
  # operator: l'utente di servizio (non-root) puo' fare `tailscale up/down/ip` senza sudo
  tailscale set --operator="$SVC_USER" 2>/dev/null || true
  sed -i "s|^tailscale_authkey:.*|tailscale_authkey: \"$TS_KEY\"|" "$CFG"
  if [[ -n "$TS_LOGIN" ]]; then sed -i "s|^tailscale_login_server:.*|tailscale_login_server: \"$TS_LOGIN\"|" "$CFG"; fi
fi
chown "$SVC_USER":"$SVC_USER" "$CFG"; chmod 640 "$CFG"

echo "==> servizio systemd"
install -m 644 "$SRC_DIR/systemd/experanto-edge.service" /etc/systemd/system/experanto-edge.service
# The unit runs the console script via the `current` symlink; OTA swaps the target.
mkdir -p /etc/systemd/system/experanto-edge.service.d
cat >/etc/systemd/system/experanto-edge.service.d/env.conf <<EOF
[Service]
Environment=EXPERANTO_EDGE_CONFIG=$CFG
ExecStart=
ExecStart=$APP_DIR/current/venv/bin/experanto-edge
EOF
systemctl daemon-reload
systemctl enable --now experanto-edge.service

echo "==> fatto. Stato:"; systemctl --no-pager status experanto-edge.service || true
