#!/usr/bin/env bash
# Test dei helper puri di wg_provision.sh (parsing + validazione risposta server).
# Include il test di CONTRATTO: la risposta ESATTA che l'endpoint Python
# (_handle_wg_provision) emette deve essere parsata/validata correttamente qui.
# Compatibile bash 3.2.  Uso:  ./wg_provision.test.sh
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
. "$DIR/wg_provision.sh"

PUB="iDYG0COgniHQeIlLjU8bYq8NSMtFqPVo/8677IWN+1E="
CKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMBErY3I+TV6heGkw/asfkSyPzddNIPhThbockVjv34/ experanto-edge-ttyd"
fails=0
_ok(){   if "$@"; then printf 'ok   %s\n' "$*"; else printf 'FAIL %s (atteso vero)\n' "$*"; fails=$((fails+1)); fi; }
_no(){   if "$@"; then printf 'FAIL %s (atteso falso)\n' "$*"; fails=$((fails+1)); else printf 'ok   ! %s\n' "$*"; fi; }
_eq(){   if [ "$2" = "$3" ]; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s: atteso [%s] ott [%s]\n' "$1" "$3" "$2"; fails=$((fails+1)); fi; }

# --- CONTRATTO: risposta come la emette json.dumps del worker Python -----------
RESP='{"status": "ok", "wg_address": "10.8.0.5/32", "hub_pubkey": "'"$PUB"'", "hub_endpoint": "hub.experanto.it:51820", "wg_interface": "wg-experanto", "console_ssh_key": "'"$CKEY"'", "reused": false}'
_eq "extract wg_address"  "$(wgp_extract "$RESP" wg_address)"  "10.8.0.5/32"
_eq "extract console_ssh_key" "$(wgp_extract "$RESP" console_ssh_key)" "$CKEY"
_ok wgp_valid_ssh_key   "$(wgp_extract "$RESP" console_ssh_key)"
_eq "extract hub_pubkey"  "$(wgp_extract "$RESP" hub_pubkey)"  "$PUB"
_eq "extract hub_endpoint" "$(wgp_extract "$RESP" hub_endpoint)" "hub.experanto.it:51820"
_no wgp_has_error "$RESP"
_ok wgp_valid_address  "$(wgp_extract "$RESP" wg_address)"
_ok wgp_valid_pubkey   "$(wgp_extract "$RESP" hub_pubkey)"
_ok wgp_valid_endpoint "$(wgp_extract "$RESP" hub_endpoint)"

# risposta d'errore
ERR='{"error": "credenziali non valide"}'
_ok wgp_has_error "$ERR"
_eq "extract error" "$(wgp_extract "$ERR" error)" "credenziali non valide"

# --- validatori: casi validi ---
_ok wgp_valid_pubkey   "$PUB"
_ok wgp_valid_pubkey   "$(printf 'A%.0s' $(seq 1 43))="
_ok wgp_valid_address  "10.8.0.2/32"
_ok wgp_valid_address  "10.8.0.254/32"
_ok wgp_valid_endpoint "1.2.3.4:51820"
_ok wgp_valid_endpoint "mqtt.experanto.it:443"

# --- chiave SSH console ---
_ok wgp_valid_ssh_key "$CKEY"
_ok wgp_valid_ssh_key "ssh-rsa AAAAB3NzaC1yc2E user@host"
_no wgp_valid_ssh_key "not-a-key"
_no wgp_valid_ssh_key ""
_no wgp_valid_ssh_key "ssh-ed25519 AAAA
iniezione-riga2"

# --- validatori: casi invalidi (injection / MITM / malformato) ---
_no wgp_valid_pubkey   "; rm -rf / #"
_no wgp_valid_pubkey   "$PUB extra"
_no wgp_valid_pubkey   "AAAA=AAAAshort"                # '=' interno + lunghezza errata
_no wgp_valid_pubkey   ""
_no wgp_valid_address  "10.8.0.5"                      # senza /32
_no wgp_valid_address  "10.8.0.5/32
evil"                                                  # newline (no bypass ancora ^$)
_no wgp_valid_address  "192.168.0.5/32"               # altra subnet
_no wgp_valid_address  "10.8.0.5/32; reboot"
_no wgp_valid_endpoint "hub:51820; rm -rf /"
_no wgp_valid_endpoint "hub 51820"
_no wgp_valid_endpoint "hub:"

# --- wgp_conf produce una conf sana ---
CONF="$(wgp_conf "PRIVKEY==" "10.8.0.5/32" "$PUB" "hub.experanto.it:51820")"
case "$CONF" in
  *"[Interface]"*"Address = 10.8.0.5/32"*"[Peer]"*"PublicKey = $PUB"*"Endpoint = hub.experanto.it:51820"*"AllowedIPs = 10.8.0.0/24"*) printf 'ok   wgp_conf shape\n' ;;
  *) printf 'FAIL wgp_conf shape\n'; fails=$((fails+1)) ;;
esac

echo "---"
if [ "$fails" -eq 0 ]; then echo "TUTTI I TEST OK"; exit 0; else echo "$fails TEST FALLITI"; exit 1; fi
