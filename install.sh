#!/usr/bin/env bash
# Experanto Edge agent installer (Raspberry Pi / Debian).
# Usage:
#   sudo ./install.sh --code EXP-XXXX-XXXX --secret <SECRET> [--station <UUID>] \
#                     [--host-code EXP-YYYY] [--broker mqtt.experanto.it] [--datalogger-ip 192.168.1.50] \
#                     [--reader-type solarlog_getjp] \
#                     [--wg-endpoint vps:51820 --wg-hub-pubkey <KEY> --wg-address 10.8.0.5/32 [--wg-persistent]]
# --reader-type = tipo di reader del datalogger (dal DB via enroll-exchange sui Pi
# nuovi: il server e' autorevole). Assente -> resta il default del config template.
# --host-code = device_code of the Pi HOST this instance runs on (its own code on a
# self-host): goes into up/status as a self-report the server cross-checks against the
# operator-set host link (never an authority). Pass `--host-code -` to CLEAR a stale
# self-report (e.g. after moving the datalogger to another Pi).
# With --wg-endpoint the installer joins the Pi to YOUR WireGuard hub (no third party): it
# generates a keypair, writes /etc/wireguard/wg-experanto.conf, and prints the Pi's PUBLIC key
# to register as a peer on the hub. --wg-persistent keeps the link always up (for the bring-up);
# otherwise it's on-demand via `open_ssh`. See WG_HUB.md for the server side.
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

# Self-bootstrap: se install.sh viene eseguito da solo (curl … | sudo bash), il pacchetto
# non è accanto → scarica l'ULTIMA RELEASE (fallback su main) e ri-esegui da lì.
if [[ ! -f "$SRC_DIR/pyproject.toml" ]]; then
  echo "==> bootstrap: scarico l'ultima release dell'agente"
  command -v tar >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq tar; }
  TMP="$(mktemp -d)"
  TARBALL="$(curl -fsSL https://api.github.com/repos/BestFab/experanto-edge-agent/releases/latest 2>/dev/null | grep -oE '"tarball_url"[^,]+' | cut -d'"' -f4)"
  curl -fsSL "${TARBALL:-https://github.com/BestFab/experanto-edge-agent/archive/refs/heads/main.tar.gz}" | tar -xz -C "$TMP" --strip-components=1
  exec bash "$TMP/install.sh" "$@"
fi

# helper puri per l'onboarding WG automatico (parsing/validazione risposta server).
# shellcheck source=/dev/null
[[ -f "$SRC_DIR/wg_provision.sh" ]] && source "$SRC_DIR/wg_provision.sh"

CODE=""; SECRET=""; STATION=""; HOST_CODE=""; BROKER=""; DL_IP=""; UPD_URL=""; UPD_KEY=""
RTYPE=""; WG_ENDPOINT=""; WG_HUB_PUBKEY=""; WG_ADDRESS=""; WG_SSH_USER=""; WG_PERSISTENT=""
WG_AUTO=""; SERVER="${EXPERANTO_SERVER:-https://web.experanto.it}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --code) CODE="$2"; shift 2;;
    --secret) SECRET="$2"; shift 2;;
    --station) STATION="$2"; shift 2;;
    --host-code) HOST_CODE="$2"; shift 2;;
    --broker) BROKER="$2"; shift 2;;
    --datalogger-ip) DL_IP="$2"; shift 2;;
    --reader-type) RTYPE="$2"; shift 2;;
    --update-base-url) UPD_URL="$2"; shift 2;;
    --update-pubkey) UPD_KEY="$2"; shift 2;;
    --wg-endpoint) WG_ENDPOINT="$2"; shift 2;;
    --wg-hub-pubkey) WG_HUB_PUBKEY="$2"; shift 2;;
    --wg-address) WG_ADDRESS="$2"; shift 2;;
    --wg-ssh-user) WG_SSH_USER="$2"; shift 2;;
    --wg-persistent) WG_PERSISTENT="1"; shift;;
    --wg-auto) WG_AUTO="1"; shift;;
    --server) SERVER="$2"; shift 2;;
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
# CFG_DIR owned by the service user too: the agent rewrites config.yaml atomically
# (config.yaml.tmp -> rename) when it persists runtime state (interval, last_command_id,
# ssh_open_until). root:root here => PermissionError on every save.
install -d -o "$SVC_USER" -g "$SVC_USER" "$CFG_DIR"
install -d "$RELEASES"

# apt ASPETTA il lock invece di fallire: al primo boot unattended-upgrades tiene
# /var/lib/dpkg/lock-frontend, e install.sh ha piu' apt-get (venv qui + wireguard-tools
# piu' avanti) che possono collidere anche DOPO il lock-wait del firstboot, se
# unattended-upgrades riparte a meta' install (visto in campo: FATAL rc=100 sul
# wireguard-tools). DPkg::Lock::Timeout fa attendere ogni apt fino a N secondi.
mkdir -p /etc/apt/apt.conf.d
printf 'DPkg::Lock::Timeout "900";\n' > /etc/apt/apt.conf.d/99experanto-lock-timeout

echo "==> venv + pacchetto (release 'bootstrap')"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip unattended-upgrades
BOOT="$RELEASES/bootstrap"
rm -rf "$BOOT"; mkdir -p "$BOOT"
python3 -m venv "$BOOT/venv"
"$BOOT/venv/bin/pip" install --upgrade pip -q
"$BOOT/venv/bin/pip" install -q "$SRC_DIR"
# Il release e' root-owned ma l'agente gira come utente di servizio non-root:
# DEVE poter attraversare/eseguire il venv. `mkdir`/`venv` ereditano l'umask del
# chiamante -> con umask 077 (es. cloud-init/systemd al primo boot) le dir
# nascerebbero 700 e il servizio morirebbe con 203/EXEC "Permission denied".
# Forziamo la traversabilita' a prescindere dall'umask (a+rX = +x sulle dir e
# sugli eseguibili, +r ovunque; nessun permesso di scrittura aggiunto).
chmod -R a+rX "$BOOT"
ln -sfn "$BOOT" "$APP_DIR/current"

echo "==> OTA helper + sudoers (NOPASSWD, solo questo script)"
install -m 750 -o root -g root "$SRC_DIR/ota/ota-helper.sh" "$APP_DIR/ota-helper.sh"
cat >/etc/sudoers.d/experanto-edge <<EOF
$SVC_USER ALL=(root) NOPASSWD: $APP_DIR/ota-helper.sh
EOF
chmod 440 /etc/sudoers.d/experanto-edge
visudo -cf /etc/sudoers.d/experanto-edge >/dev/null

echo "==> helper broker (canale privilegiato col sandbox intatto) + watchdog WireGuard"
# Il sandbox dell'agente (NoNewPrivileges) blocca `sudo -n`: le azioni privilegiate
# passano dalla path-unit root che valida helper.request e delega a ota-helper.sh.
install -m 750 -o root -g root "$SRC_DIR/ota/helper-broker.sh" "$APP_DIR/helper-broker.sh"
install -m 750 -o root -g root "$SRC_DIR/ota/wg-watchdog.sh"  "$APP_DIR/wg-watchdog.sh"
install -m 644 "$SRC_DIR/systemd/experanto-edge-helper.service" /etc/systemd/system/experanto-edge-helper.service
install -m 644 "$SRC_DIR/systemd/experanto-edge-helper.path"    /etc/systemd/system/experanto-edge-helper.path
install -m 644 "$SRC_DIR/systemd/wg-watchdog.service"           /etc/systemd/system/wg-watchdog.service
install -m 644 "$SRC_DIR/systemd/wg-watchdog.timer"             /etc/systemd/system/wg-watchdog.timer
systemctl daemon-reload
systemctl enable --now experanto-edge-helper.path
# Il watchdog e' un no-op finche' wg-quick@ non e' enabled (overlay system-managed).
systemctl enable --now wg-watchdog.timer

echo "==> baseline unattended-upgrades (security)"
systemctl enable --now unattended-upgrades.service 2>/dev/null || true

echo "==> config + enrollment"
CFG="$CFG_DIR/config.yaml"
[[ -f "$CFG" ]] || install -m 640 "$SRC_DIR/config.example.yaml" "$CFG"
# HOST_CODE = device_code del Pi host (se stesso sui self-host): 4a parte
# dell'enroll; senza station la 3a resta vuota (CODE:SECRET::HOST).
ENROLL="$CODE:$SECRET"
if [[ -n "$HOST_CODE" ]]; then ENROLL="$ENROLL:$STATION:$HOST_CODE"
elif [[ -n "$STATION" ]]; then ENROLL="$ENROLL:$STATION"; fi
EXPERANTO_EDGE_CONFIG="$CFG" "$APP_DIR/current/venv/bin/experanto-edge" --enroll "$ENROLL" || true
if [[ -n "$BROKER" ]]; then sed -i "s/^broker_host:.*/broker_host: \"$BROKER\"/" "$CFG"; fi
if [[ -n "$DL_IP" ]]; then sed -i "s/^datalogger_ip:.*/datalogger_ip: \"$DL_IP\"/" "$CFG"; fi
# reader_type dal server (enroll-exchange/console): il DB e' autorevole gia' al
# primo boot (G7). Assente -> resta il default del config template.
if [[ -n "$RTYPE" ]]; then sed -i "s/^reader_type:.*/reader_type: \"$RTYPE\"/" "$CFG"; fi
# OTA config (delimitatore | perche' URL/base64 contengono /)
if [[ -n "$UPD_URL" ]]; then sed -i "s|^update_base_url:.*|update_base_url: \"$UPD_URL\"|" "$CFG"; fi
if [[ -n "$UPD_KEY" ]]; then sed -i "s|^update_public_key:.*|update_public_key: \"$UPD_KEY\"|" "$CFG"; fi
# WireGuard verso il TUO hub (nessun servizio terzo).
#   --wg-auto : provisioning AUTOMATICO all'install (approccio A) — genera la keypair,
#               chiede al server IP overlay + coordinate hub, scrive la conf, alza wg-quick@.
#   --wg-endpoint ... : modalita' MANUALE (flag espliciti), fallback offline.
WG_PUBKEY=""
if [[ -n "$WG_AUTO" ]]; then
  WG_IF="wg-experanto"; WG_CONF="/etc/wireguard/$WG_IF.conf"
  # INVARIANTE lifeline: se l'interfaccia WG e' gia' istanziata (Pi vivo), NON la tocco.
  if ip link show "$WG_IF" >/dev/null 2>&1; then
    echo "==> $WG_IF gia' presente: NON la tocco (lifeline)"
  else
    command -v wgp_valid_pubkey >/dev/null 2>&1 || { echo "wg_provision.sh mancante accanto a install.sh" >&2; exit 1; }
    echo "==> WireGuard AUTO: provisioning dal server $SERVER"
    apt-get install -y -qq wireguard-tools curl ca-certificates
    install -d -m 700 /etc/wireguard
    # keypair: riusa quella esistente (re-run idempotente) o generane una nuova
    if [[ -f "$WG_CONF" ]]; then WG_PRIV="$(awk -F' = ' '/^PrivateKey/{print $2}' "$WG_CONF")"
    else WG_PRIV="$(wg genkey)"; fi
    WG_PUB="$(printf '%s' "$WG_PRIV" | wg pubkey)"
    # POST code+secret+pubkey. TLS VERIFICATO (mai -k): un MITM non deve poter
    # dirottare il Pi su un hub ostile.
    WG_BODY="$(printf '{"device_code":"%s","secret":"%s","wg_pubkey":"%s"}' "$CODE" "$SECRET" "$WG_PUB")"
    RESP="$(curl -fsS --max-time 25 -X POST "$SERVER/api/edge/wg-provision" \
             -H 'Content-Type: application/json' --data "$WG_BODY")" \
      || { echo "provision WG fallito: server irraggiungibile o certificato non valido ($SERVER)" >&2; exit 1; }
    if wgp_has_error "$RESP"; then
      echo "provision WG rifiutato dal server: $(wgp_extract "$RESP" error)" >&2; exit 1
    fi
    WG_ADDRESS="$(wgp_extract "$RESP" wg_address)"
    WG_HUB_PUBKEY="$(wgp_extract "$RESP" hub_pubkey)"
    WG_ENDPOINT="$(wgp_extract "$RESP" hub_endpoint)"
    # VALIDA prima di scrivere in /etc/wireguard (difesa MITM/malformato).
    wgp_valid_address  "$WG_ADDRESS"    || { echo "wg_address non valido dal server: [$WG_ADDRESS]" >&2; exit 1; }
    wgp_valid_pubkey   "$WG_HUB_PUBKEY" || { echo "hub_pubkey non valido dal server" >&2; exit 1; }
    wgp_valid_endpoint "$WG_ENDPOINT"   || { echo "hub_endpoint non valido dal server: [$WG_ENDPOINT]" >&2; exit 1; }
    ( umask 077; wgp_conf "$WG_PRIV" "$WG_ADDRESS" "$WG_HUB_PUBKEY" "$WG_ENDPOINT" > "$WG_CONF" )
    chmod 600 "$WG_CONF"
    sed -i "s|^wg_interface:.*|wg_interface: \"$WG_IF\"|" "$CFG"
    sed -i "s|^wg_address:.*|wg_address: \"$WG_ADDRESS\"|" "$CFG"
    [[ -n "$WG_SSH_USER" ]] && sed -i "s|^wg_ssh_user:.*|wg_ssh_user: \"$WG_SSH_USER\"|" "$CFG"
    # Chiave pubblica della console (ttyd/id_edge) -> authorized_keys dell'utente SSH,
    # cosi' shell/SSH a chiave dalla console funziona subito. Utente = --wg-ssh-user oppure
    # chi ha lanciato sudo (deve combaciare con l'utente SSH della console, di norma 'fabri').
    CONSOLE_KEY="$(wgp_extract "$RESP" console_ssh_key)"
    SSH_LOGIN_USER="${WG_SSH_USER:-${SUDO_USER:-}}"
    if [[ -n "$CONSOLE_KEY" ]] && wgp_valid_ssh_key "$CONSOLE_KEY"; then
      SSH_HOME="$(getent passwd "$SSH_LOGIN_USER" 2>/dev/null | cut -d: -f6)"
      if [[ -n "$SSH_LOGIN_USER" && -n "$SSH_HOME" ]]; then
        install -d -m 700 -o "$SSH_LOGIN_USER" -g "$SSH_LOGIN_USER" "$SSH_HOME/.ssh"
        AK="$SSH_HOME/.ssh/authorized_keys"; touch "$AK"
        grep -qF "$CONSOLE_KEY" "$AK" 2>/dev/null || printf '%s\n' "$CONSOLE_KEY" >> "$AK"
        chown "$SSH_LOGIN_USER":"$SSH_LOGIN_USER" "$AK"; chmod 600 "$AK"
        echo "==> chiave console -> authorized_keys di $SSH_LOGIN_USER (shell da console ok)"
      else
        echo "==> chiave console ricevuta ma utente SSH ignoto: passa --wg-ssh-user <utente>. authorized_keys NON aggiornato." >&2
      fi
    fi
    cat >/etc/sudoers.d/experanto-edge-wg <<EOF
$SVC_USER ALL=(root) NOPASSWD: /usr/bin/wg-quick up $WG_IF, /usr/bin/wg-quick down $WG_IF
EOF
    chmod 440 /etc/sudoers.d/experanto-edge-wg
    visudo -cf /etc/sudoers.d/experanto-edge-wg >/dev/null
    # Onboarding = lifeline sempre su: wg-quick@ persistente (il peer e' gia' registrato).
    systemctl enable --now "wg-quick@$WG_IF" || true
    echo "==> WireGuard AUTO ok: $WG_ADDRESS via hub $WG_ENDPOINT (peer registrato dal server)"
  fi
elif [[ -n "$WG_ENDPOINT" ]]; then
  [[ -n "$WG_ADDRESS" && -n "$WG_HUB_PUBKEY" ]] || { echo "--wg-endpoint richiede --wg-address e --wg-hub-pubkey" >&2; exit 1; }
  echo "==> WireGuard (hub $WG_ENDPOINT, IP overlay $WG_ADDRESS)"
  apt-get install -y -qq wireguard-tools
  WG_IF="wg-experanto"; WG_CONF="/etc/wireguard/$WG_IF.conf"
  install -d -m 700 /etc/wireguard
  if [[ ! -f "$WG_CONF" ]]; then
    WG_PRIV="$(wg genkey)"
    ( umask 077; cat > "$WG_CONF" <<WGEOF
[Interface]
PrivateKey = $WG_PRIV
Address = $WG_ADDRESS

[Peer]
PublicKey = $WG_HUB_PUBKEY
Endpoint = $WG_ENDPOINT
AllowedIPs = 10.8.0.0/24
PersistentKeepalive = 25
WGEOF
    )
  fi
  chmod 600 "$WG_CONF"
  WG_PUBKEY="$(awk -F' = ' '/^PrivateKey/{print $2}' "$WG_CONF" | wg pubkey)"
  sed -i "s|^wg_interface:.*|wg_interface: \"$WG_IF\"|" "$CFG"
  sed -i "s|^wg_address:.*|wg_address: \"$WG_ADDRESS\"|" "$CFG"
  if [[ -n "$WG_SSH_USER" ]]; then sed -i "s|^wg_ssh_user:.*|wg_ssh_user: \"$WG_SSH_USER\"|" "$CFG"; fi
  # sudoers: l'agente (non-root) puo' alzare/abbassare SOLO questa interfaccia WireGuard
  cat >/etc/sudoers.d/experanto-edge-wg <<EOF
$SVC_USER ALL=(root) NOPASSWD: /usr/bin/wg-quick up $WG_IF, /usr/bin/wg-quick down $WG_IF
EOF
  chmod 440 /etc/sudoers.d/experanto-edge-wg
  visudo -cf /etc/sudoers.d/experanto-edge-wg >/dev/null
  if [[ -n "$WG_PERSISTENT" ]]; then
    echo "==> WireGuard persistente (wg-quick@$WG_IF sempre su — per il bring-up)"
    systemctl enable --now "wg-quick@$WG_IF" || true   # sale quando il peer e' registrato sull'hub
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

if [[ -n "$WG_PUBKEY" ]]; then
  echo
  echo "==> AZIONE sull'hub WireGuard: registra questo Pi come peer (vedi WG_HUB.md)"
  echo "    PublicKey  = $WG_PUBKEY"
  echo "    AllowedIPs = $WG_ADDRESS"
  echo
fi
echo "==> fatto. Stato:"; systemctl --no-pager status experanto-edge.service || true
