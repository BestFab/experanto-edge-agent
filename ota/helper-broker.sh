#!/usr/bin/env bash
# =============================================================================
# Experanto Edge — helper broker (root). Canale privilegiato SENZA sudo.
#
# Il sandbox systemd dell'agente (NoNewPrivileges=true) blocca `sudo -n
# ota-helper.sh` — constatato live sulla flotta: OTA, update di sistema e
# reboot remoti impossibili. Questo broker chiude il gap SENZA indebolire il
# sandbox dell'agente:
#
#   agente (unprivileged)  --scrive-->  {state}/helper.request
#   experanto-edge-helper.path (root)  --PathExists-->  helper.service -> QUESTO script
#     consuma la request -> valida (whitelist azioni + anti-replay + argomenti) ->
#     lancia ota-helper.sh in una unit TRANSIENTE (systemd-run, detached) ->
#     risponde su {state}/helper.response e ritorna subito.
#
# Formato request  (una riga):  <epoch> <nonce> <azione> [arg...]
#   azioni ammesse: agent <version> <artifact> | system <security|full> | reboot | ping
# Formato response (una riga):  <epoch> <nonce> <accepted|done|rejected> rc=<n> <detail>
#
# Perche' systemd-run e non `exec`: se il broker eseguisse ota-helper DENTRO il
# proprio job oneshot, (a) il TimeoutStartSec ucciderebbe un apt/pip lungo a
# meta' transazione (dpkg rotto), e (b) la path-unit non ri-triggererebbe fino
# a fine run (comandi persi). Con systemd-run l'azione gira in un cgroup a se',
# senza timeout ereditato, e il broker si libera in <1s.
#
# Sicurezza: chi scrive helper.request possiede gia' la state dir (utente
# sandboxato experanto-edge) e nel modello sudo storico poteva gia' invocare
# ota-helper.sh con arg arbitrari. Il broker deve restare <= permissivo:
#   - la RESPONSE (e i suoi temp) vive in una dir ROOT-OWNED ($RESP_DIR, parent
#     /run non scrivibile dall'attaccante): root non scrive MAI per-path dentro
#     la dir dell'attaccante, quindi niente symlink-swap/TOCTOU su cui far
#     scrivere/chmodare root (il vecchio temp nella state dir era racciabile);
#   - la REQUEST puo' essere un symlink piazzato dall'attaccante (-> /etc/shadow):
#     dopo il consumo si RIFIUTA se e' un symlink, cosi' root non ne legge mai il
#     bersaglio (niente info-disclosure nella response);
#   - version/mode/nonce passano da whitelist di caratteri; artifact confinato
#     alla state dir via realpath (niente '..').
# =============================================================================
set -euo pipefail

APP_DIR="${EXPERANTO_EDGE_APP_DIR:-/opt/experanto-edge}"
STATE_DIR="${EXPERANTO_EDGE_STATE_DIR:-/var/lib/experanto-edge}"
# Dir della response: ROOT-OWNED. Default /run/experanto-edge (RuntimeDirectory
# della unit); il parent /run e' di root, quindi l'utente sandboxato non puo'
# crearci dentro symlink. La REQUEST invece deve restare nella state dir
# scrivibile dall'agente.
RESP_DIR="${EXPERANTO_EDGE_RESP_DIR:-/run/experanto-edge}"
REQ="$STATE_DIR/helper.request"
RESP="$RESP_DIR/helper.response"
OTA_HELPER="$APP_DIR/ota-helper.sh"
SVC_USER="${EXPERANTO_EDGE_SVC_USER:-experanto-edge}"   # owner atteso della request
MAX_AGE_S=120

log() { echo "[helper-broker] $*"; logger -t experanto-edge-helper "$*" 2>/dev/null || true; }

# La dir della response deve esistere ed essere root-only (belt-and-suspenders
# rispetto a RuntimeDirectory=). mkdir dentro /run (root) e' sicuro.
mkdir -p -m 755 "$RESP_DIR" 2>/dev/null || true

respond() {  # $1 epoch, $2 nonce, $3 stato, $4 rc, $5 detail
    # Temp + rename ENTRAMBI in $RESP_DIR (root-owned): nessuna scrittura per-path
    # di root nella dir dell'attaccante, quindi il TOCTOU symlink-swap non esiste.
    # Niente chown: root scrive, l'agente legge (file world-readable).
    local tmp
    tmp="$(mktemp "$RESP_DIR/.resp.XXXXXX")" || return 0
    printf '%s %s %s rc=%s %s\n' "$1" "$2" "$3" "$4" "$5" > "$tmp"
    chmod 644 "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$RESP"
}

launch_detached() {  # $@ = argomenti per ota-helper.sh
    # Unit transiente: cgroup proprio, nessun TimeoutStartSec ereditato, sopravvive
    # al restart dell'agente. --collect la rimuove a fine run. Il nome include il
    # nonce (hex+dash, valido come unit name) per non collidere fra azioni.
    systemd-run --collect --no-block \
        --unit="experanto-edge-priv-${nonce}" \
        --description="Experanto Edge azione privilegiata: $1" \
        "$OTA_HELPER" "$@"
}

reject()  { respond "$ts" "$nonce" rejected "$1" "$2"; log "rifiutata (nonce ${nonce:-?}): $2"; exit 0; }
accept()  { respond "$ts" "$nonce" accepted 0 "$1"; log "accettata (nonce $nonce): $1"; exit 0; }

[[ -e "$REQ" ]] || exit 0            # trigger spurio: nulla da fare
WORK="$(mktemp "$STATE_DIR/.helper.req.XXXXXX")"
mv -f "$REQ" "$WORK"                 # consuma subito: la path unit puo' ri-triggerare
# La request potrebbe essere un symlink piazzato dall'attaccante (es. -> /etc/shadow):
# mv sposta il symlink (rename non deref), ma NON dobbiamo leggerne il bersaglio come
# root. Si scarta senza leggere se: e' un symlink / non e' un file regolare, OPPURE non
# e' di proprieta' di chi la scrive legittimamente ($SVC_USER). Quest'ultimo chiude anche
# la variante hardlink (helper.request -> /etc/shadow, owner root:shadow): su Debian
# protected_hardlinks=1 la blocca gia' a monte, questo e' difesa-in-profondita'.
owner="$(stat -c '%U' "$WORK" 2>/dev/null || echo '?')"
if [[ -L "$WORK" || ! -f "$WORK" || "$owner" != "$SVC_USER" ]]; then
    rm -f "$WORK"
    log "request non fidata (symlink/hardlink/owner=$owner): scartata senza leggerla"
    exit 0
fi
line="$(head -n1 "$WORK" | tr -d '\r')"
rm -f "$WORK"

ts=""; nonce=""; action=""; args=""
read -r ts nonce action args <<<"$line" || true
if [[ -z "$ts" || -z "$nonce" || -z "$action" ]]; then
    log "request malformata: '$line'"
    respond "${ts:-0}" "${nonce:-x}" rejected 64 "request malformata"
    exit 0
fi
# nonce: solo caratteri sicuri (entra in un nome di unit systemd)
if ! [[ "$nonce" =~ ^[A-Za-z0-9._-]+$ ]]; then
    respond "$ts" "x" rejected 64 "nonce non valido"; exit 0
fi
now="$(date +%s)"
if ! [[ "$ts" =~ ^[0-9]+$ ]] || (( now - ts > MAX_AGE_S )) || (( ts - now > MAX_AGE_S )); then
    reject 65 "request stantia o futura (ts=$ts now=$now)"
fi

case "$action" in
    ping)
        respond "$ts" "$nonce" done 0 "pong"; log "ping ok (nonce $nonce)"; exit 0 ;;
    reboot)
        [[ -x "$OTA_HELPER" ]] || reject 69 "ota-helper assente"
        launch_detached reboot || reject 70 "launch reboot fallito"
        accept "reboot avviato" ;;
    system)
        mode=""; read -r mode _ <<<"$args" || true; mode="${mode:-security}"
        [[ "$mode" == "security" || "$mode" == "full" ]] || reject 66 "mode non valido: $mode"
        [[ -x "$OTA_HELPER" ]] || reject 69 "ota-helper assente"
        launch_detached system "$mode" || reject 70 "launch system fallito"
        accept "aggiornamento sistema ($mode) avviato" ;;
    agent)
        version=""; artifact=""
        read -r version artifact _ <<<"$args" || true
        [[ -n "$version" && -n "$artifact" ]] || reject 66 "agent: versione/artifact mancanti"
        # version finisce in `rm -rf "$RELEASES/$version"` dentro ota-helper: niente
        # traversal, niente metacaratteri.
        [[ "$version" =~ ^[A-Za-z0-9._-]+$ && "$version" != "." && "$version" != ".." ]] \
            || reject 66 "version non valida: $version"
        # artifact confinato alla state dir via realpath (il glob del case matcha
        # anche '/', quindi '..' lo bypasserebbe): canonicalizza e confronta il prefisso.
        real="$(realpath -m -- "$artifact")"
        case "$real/" in
            "$STATE_DIR"/*) ;;
            *) reject 67 "artifact fuori dalla state dir" ;;
        esac
        [[ -f "$real" ]] || reject 67 "artifact assente: $real"
        [[ -x "$OTA_HELPER" ]] || reject 69 "ota-helper assente"
        launch_detached agent "$version" "$real" || reject 70 "launch agent fallito"
        accept "install $version avviata" ;;
    *)
        reject 64 "azione non ammessa: $action" ;;
esac
