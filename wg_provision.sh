# wg_provision.sh — helper PURI per l'onboarding WireGuard automatico (approccio A).
#
# Sourceable e testabile (wg_provision.test.sh). NESSUNA I/O di sistema qui: solo
# parsing e VALIDAZIONE della risposta del server prima di scriverla in
# /etc/wireguard. La validazione a formato fisso e' la difesa contro risposte
# malformate / MITM: nulla finisce nella conf WireGuard (o in wg-quick) senza
# combaciare esattamente col formato atteso. Compatibile bash 3.2.

# wgp_extract <json> <field> -> stampa il valore stringa del campo ("" se assente).
# Risposta piatta {"a":"x","b":"y"}; tollera spazi dopo i due punti (json.dumps).
wgp_extract() {
  printf '%s' "$1" | sed -n 's/.*"'"$2"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1
}

# wgp_has_error <json> -> 0 se contiene un campo "error".
wgp_has_error() { printf '%s' "$1" | grep -q '"error"[[:space:]]*:'; }

# --- validatori a formato fisso ---------------------------------------------
# Il `case` con glob-negato scarta PRIMA qualsiasi carattere fuori dall'insieme
# atteso (newline/spazi inclusi): evita il bypass "valore\naltro" che le ancore
# ^$ di grep avrebbero (grep matcha riga per riga).

# pubkey WireGuard = 32 byte base64 = 43 char base64 + 1 padding '=' finale.
wgp_valid_pubkey() {
  [ "${#1}" -eq 44 ] || return 1
  case "$1" in *=) ;; *) return 1 ;; esac      # deve finire con '='
  case "${1%=}" in ""|*[!A-Za-z0-9+/]*) return 1 ;; esac  # i 43 prima: solo base64
  return 0
}

# IP overlay + /32, es. 10.8.0.5/32
wgp_valid_address() {
  case "$1" in ""|*[!0-9./]*) return 1 ;; esac
  printf '%s' "$1" | grep -Eq '^10\.8\.0\.[0-9]{1,3}/32$'
}

# endpoint hub host:porta
wgp_valid_endpoint() {
  case "$1" in ""|*[!A-Za-z0-9.:-]*) return 1 ;; esac
  printf '%s' "$1" | grep -Eq '^[A-Za-z0-9.-]+:[0-9]{1,5}$'
}

# wgp_conf <privkey> <address> <hub_pubkey> <endpoint> -> stampa la conf WireGuard.
wgp_conf() {
  printf '[Interface]\nPrivateKey = %s\nAddress = %s\n\n[Peer]\nPublicKey = %s\nEndpoint = %s\nAllowedIPs = 10.8.0.0/24\nPersistentKeepalive = 25\n' \
    "$1" "$2" "$3" "$4"
}
