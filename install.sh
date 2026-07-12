#!/usr/bin/env bash
# Experanto Edge agent installer (Raspberry Pi / Debian).
# Usage:
#   sudo ./install.sh --code EXP-XXXX-XXXX --secret <SECRET> [--station <UUID>] \
#                     [--broker mqtt.experanto.it] [--datalogger-ip 192.168.1.50] \
#                     [--ssh-bastion vps.tuo.it --ssh-reverse-port 22016 [--ssh-persistent]]
# With --ssh-bastion the installer sets up remote SSH via a reverse tunnel to YOUR bastion
# (no third party): it generates a key and prints the public key to authorize on the bastion.
# --ssh-persistent keeps the tunnel always up (for the bring-up); otherwise it's on-demand
# via the `open_ssh` command. See BASTION.md for the server side.
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
SSH_BASTION=""; SSH_BASTION_USER="edge-tunnel"; SSH_BASTION_PORT=22
SSH_REVERSE_PORT=""; SSH_LOCAL_PORT=22; SSH_PERSISTENT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --code) CODE="$2"; shift 2;;
    --secret) SECRET="$2"; shift 2;;
    --station) STATION="$2"; shift 2;;
    --broker) BROKER="$2"; shift 2;;
    --datalogger-ip) DL_IP="$2"; shift 2;;
    --update-base-url) UPD_URL="$2"; shift 2;;
    --update-pubkey) UPD_KEY="$2"; shift 2;;
    --ssh-bastion) SSH_BASTION="$2"; shift 2;;
    --ssh-bastion-user) SSH_BASTION_USER="$2"; shift 2;;
    --ssh-bastion-port) SSH_BASTION_PORT="$2"; shift 2;;
    --ssh-reverse-port) SSH_REVERSE_PORT="$2"; shift 2;;
    --ssh-local-port) SSH_LOCAL_PORT="$2"; shift 2;;
    --ssh-persistent) SSH_PERSISTENT="1"; shift;;
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
# Reverse SSH tunnel verso il TUO bastion (nessun servizio terzo). Genera la chiave,
# scrive la config e stampa la PUBBLICA da autorizzare sul bastion. Opzionale.
TUNNEL_PUBKEY=""
if [[ -n "$SSH_BASTION" ]]; then
  [[ -n "$SSH_REVERSE_PORT" ]] || { echo "--ssh-bastion richiede --ssh-reverse-port" >&2; exit 1; }
  echo "==> reverse SSH tunnel (bastion $SSH_BASTION:$SSH_BASTION_PORT, porta remota $SSH_REVERSE_PORT)"
  apt-get install -y -qq autossh openssh-client
  KEY="$CFG_DIR/tunnel_key"
  [[ -f "$KEY" ]] || ssh-keygen -t ed25519 -f "$KEY" -N "" -C "experanto-edge@${CODE:-pi}" -q
  chown "$SVC_USER":"$SVC_USER" "$KEY" "$KEY.pub"; chmod 600 "$KEY"
  sed -i "s|^ssh_bastion_host:.*|ssh_bastion_host: \"$SSH_BASTION\"|" "$CFG"
  sed -i "s|^ssh_bastion_user:.*|ssh_bastion_user: \"$SSH_BASTION_USER\"|" "$CFG"
  sed -i "s|^ssh_bastion_port:.*|ssh_bastion_port: $SSH_BASTION_PORT|" "$CFG"
  sed -i "s|^ssh_reverse_port:.*|ssh_reverse_port: $SSH_REVERSE_PORT|" "$CFG"
  sed -i "s|^ssh_local_port:.*|ssh_local_port: $SSH_LOCAL_PORT|" "$CFG"
  sed -i "s|^ssh_identity:.*|ssh_identity: \"$KEY\"|" "$CFG"
  TUNNEL_PUBKEY="$(cat "$KEY.pub")"
  if [[ -n "$SSH_PERSISTENT" ]]; then
    echo "==> tunnel persistente (systemd, sempre su — per il bring-up)"
    cat >/etc/systemd/system/experanto-edge-tunnel.service <<EOF
[Unit]
Description=Experanto Edge reverse SSH tunnel (persistent)
After=network-online.target
Wants=network-online.target
[Service]
User=$SVC_USER
Environment=AUTOSSH_GATETIME=0
ExecStart=/usr/bin/autossh -M 0 -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$STATE_DIR/known_hosts_bastion -o IdentitiesOnly=yes -i $KEY -R $SSH_REVERSE_PORT:localhost:$SSH_LOCAL_PORT -p $SSH_BASTION_PORT $SSH_BASTION_USER@$SSH_BASTION
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now experanto-edge-tunnel.service || true   # ritenta finche' la pubkey non e' sul bastion
  fi
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

if [[ -n "$TUNNEL_PUBKEY" ]]; then
  echo
  echo "==> AZIONE sul bastion $SSH_BASTION: autorizza questa chiave pubblica"
  echo "    per l'utente '$SSH_BASTION_USER' (~/.ssh/authorized_keys). Hardening in BASTION.md."
  echo
  echo "$TUNNEL_PUBKEY"
  echo
fi
echo "==> fatto. Stato:"; systemctl --no-pager status experanto-edge.service || true
