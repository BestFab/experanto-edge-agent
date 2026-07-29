#!/usr/bin/env bash
# =============================================================================
# Experanto Edge — WireGuard watchdog (root, FUORI dal sandbox dell'agente).
#
# Self-heal per un overlay che muore "in place": interfaccia su ma handshake
# fermi (successo reale a Curinga, 2026-07-26 — sospetto unattended-upgrade),
# oppure interfaccia sparita. Se l'ultimo handshake del peer e' piu' vecchio
# di STALE_S, riporta su wg-quick@<iface>. Conservativo by design:
#   - agisce SOLO se wg-quick@<iface> e' enabled (overlay system-managed);
#   - hs==0 (mai un handshake) e' trattato come STALE SOLO se l'interfaccia
#     esiste da piu' di STALE_S: evita il churn al boot mentre il peer non e'
#     ancora registrato sull'hub;
#   - il ripristino e' `down` esplicito + `start` (NON un bare `restart`: su una
#     unit inactive il restart non esegue ExecStop e `up` fallisce con
#     "already exists" se l'iface fu alzata a mano fuori dall'unit);
#   - cooldown fra due ripristini (niente flapping se l'hub e' giu');
#   - --check stampa la decisione SENZA agire (per la validazione al deploy).
# L'agente non c'entra: gira da wg-watchdog.timer come root, NNP resta intatto.
# =============================================================================
set -u

IFACE="${WG_WATCHDOG_IFACE:-wg-experanto}"
STALE_S="${WG_WATCHDOG_STALE_S:-2100}"          # 35 min senza handshake = tunnel morto
COOLDOWN_S="${WG_WATCHDOG_COOLDOWN_S:-1800}"    # >=30 min fra due ripristini
STATE="/run/wg-watchdog.${IFACE}.last"
CHECK=0; [[ "${1:-}" == "--check" ]] && CHECK=1

log() { echo "[wg-watchdog] $*"; logger -t wg-watchdog "$*" 2>/dev/null || true; }

# Da quanti secondi l'unit wg-quick@<iface> e' entrata in stato active (0 se non
# disponibile). Serve per la grazia su hs==0: un'interfaccia appena creata che non
# ha ancora un handshake non e' "morta", sta solo aspettando il primo contatto.
unit_active_age() {
    local mono now_mono
    mono="$(systemctl show "wg-quick@${IFACE}" -p ActiveEnterTimestampMonotonic --value 2>/dev/null)"
    [[ "$mono" =~ ^[0-9]+$ ]] && (( mono > 0 )) || { echo 0; return; }
    now_mono="$(awk '{printf "%d", $1*1000000}' /proc/uptime 2>/dev/null || echo 0)"
    (( now_mono > mono )) && echo $(( (now_mono - mono) / 1000000 )) || echo 0
}

decide() {  # stdout: "OK|STALE|SKIP <motivo>"
    if ! systemctl is-enabled --quiet "wg-quick@${IFACE}" 2>/dev/null; then
        echo "SKIP wg-quick@${IFACE} non enabled (overlay non system-managed)"; return
    fi
    local out now hs age
    if ! out="$(wg show "$IFACE" latest-handshakes 2>/dev/null)"; then
        echo "STALE interfaccia ${IFACE} assente (wg show fallito)"; return
    fi
    now="$(date +%s)"
    hs="$(awk 'BEGIN{m=0} {if ($2>m) m=$2} END{print m}' <<<"$out")"
    if (( hs == 0 )); then
        local uage; uage="$(unit_active_age)"
        if (( uage > 0 && uage <= STALE_S )); then
            echo "OK nessun handshake ma interfaccia su da soli ${uage}s (grazia)"; return
        fi
        echo "STALE nessun handshake e interfaccia su da >${STALE_S}s (o eta' ignota)"; return
    fi
    age=$(( now - hs ))
    if (( age > STALE_S )); then
        echo "STALE ultimo handshake ${age}s fa (> ${STALE_S}s)"; return
    fi
    echo "OK ultimo handshake ${age}s fa"
}

verdict="$(decide)"
reason="${verdict#* }"
case "$verdict" in
    OK*)   (( CHECK )) && log "check: tunnel sano — $reason"; exit 0 ;;
    SKIP*) (( CHECK )) && log "check: $reason"; exit 0 ;;
esac

# STALE — rispetta il cooldown, poi ripristina l'interfaccia di sistema.
now="$(date +%s)"
last="$(cat "$STATE" 2>/dev/null || echo 0)"
[[ "$last" =~ ^[0-9]+$ ]] || last=0
if (( now - last < COOLDOWN_S )); then
    (( CHECK )) && { log "check: stantio ma in cooldown — $reason"; exit 0; }
    log "tunnel stantio ($reason) ma cooldown attivo ($((now - last))s < ${COOLDOWN_S}s): non agisco"
    exit 0
fi
if (( CHECK )); then
    log "check: RIPRISTINEREI wg-quick@${IFACE} (down+start) — $reason"
    exit 0
fi

log "ripristino wg-quick@${IFACE} — $reason"
echo "$now" > "$STATE"   # prima del ripristino: il cooldown vale anche se fallisce
# down esplicito (idempotente) prima dello start: copre sia l'iface su fuori-unit
# ("already exists") sia l'unit inactive dopo un bring-up manuale. `|| true` perche'
# un down su un'iface gia' assente e' un successo per noi.
systemctl stop "wg-quick@${IFACE}" >/dev/null 2>&1 || true
wg-quick down "$IFACE" >/dev/null 2>&1 || true
ip link del "$IFACE" >/dev/null 2>&1 || true
systemctl reset-failed "wg-quick@${IFACE}" >/dev/null 2>&1 || true
if systemctl start "wg-quick@${IFACE}"; then
    log "ripristino eseguito"
else
    log "ripristino FALLITO (systemctl start rc=$?) — ritento al prossimo giro"
fi
