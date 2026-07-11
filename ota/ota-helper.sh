#!/usr/bin/env bash
# =============================================================================
# Experanto Edge — OTA helper (runs as ROOT via a one-line NOPASSWD sudoers rule).
#
# The agent runs unprivileged/sandboxed and cannot write /opt or run apt, so it
# verifies the signed release (Ed25519) and then hands the privileged work here:
#
#   agent  <version> <artifact.tar.gz>   install -> --selfcheck probe -> atomic
#                                        symlink swap -> restart -> post-restart
#                                        health-check -> rollback on failure
#   system <security|full>               apt updates (security via unattended-upgrade)
#   reboot                               reboot the host
#
# The artifact is ALREADY signature-verified by the agent; this helper trusts the
# staged file (a compromised agent is already game over). Invoked as:
#   sudo -n /opt/experanto-edge/ota-helper.sh <cmd> [args...]
# =============================================================================
set -euo pipefail

APP_DIR="${EXPERANTO_EDGE_APP_DIR:-/opt/experanto-edge}"
RELEASES="$APP_DIR/releases"
CURRENT="$APP_DIR/current"
SERVICE="experanto-edge.service"
CFG="${EXPERANTO_EDGE_CONFIG:-/etc/experanto-edge/config.yaml}"
HEALTH="${EXPERANTO_EDGE_HEALTH:-/var/lib/experanto-edge/health}"
SVC_USER="experanto-edge"
KEEP_RELEASES=3
HEALTH_WAIT=45   # seconds to let the new agent prove itself after restart

log() { echo "[ota-helper] $*"; logger -t experanto-edge-ota "$*" 2>/dev/null || true; }

_swap_current() {  # $1 = target release dir; atomic symlink repoint
    ln -sfn "$1" "$CURRENT.tmp"
    mv -Tf "$CURRENT.tmp" "$CURRENT"
    chown -h "$SVC_USER":"$SVC_USER" "$CURRENT" 2>/dev/null || true
}

_prune_releases() {
    local cur; cur="$(readlink -f "$CURRENT" 2>/dev/null || true)"
    ls -1dt "$RELEASES"/*/ 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)) | while read -r d; do
        d="${d%/}"
        [[ "$d" == "$cur" ]] && continue
        rm -rf "$d"
    done
}

cmd_agent() {
    local version="${1:-}" artifact="${2:-}"
    [[ -n "$version" && -f "$artifact" ]] || { log "agent: versione/artifact mancanti"; exit 2; }
    local rel="$RELEASES/$version"

    log "installo release $version in $rel"
    rm -rf "$rel"; mkdir -p "$rel"
    python3 -m venv "$rel/venv"
    "$rel/venv/bin/pip" install --upgrade pip -q
    "$rel/venv/bin/pip" install -q "$artifact"
    rm -f "$artifact"   # consumato: il codice e' nel venv

    log "probe --selfcheck ($version)"
    if ! EXPERANTO_EDGE_CONFIG="$CFG" "$rel/venv/bin/experanto-edge" --selfcheck; then
        log "selfcheck FALLITO -> scarto $version (nessuno swap = rollback implicito)"
        rm -rf "$rel"; exit 3
    fi

    local prev; prev="$(readlink -f "$CURRENT" 2>/dev/null || true)"
    log "swap current -> $rel (precedente: ${prev:-nessuno})"
    _swap_current "$rel"
    systemctl restart "$SERVICE"

    # Post-restart health-check: il nuovo agente deve aggiornare l'health marker.
    local restart_ts healthy=0 mtime
    restart_ts="$(date +%s)"
    for _ in $(seq 1 "$HEALTH_WAIT"); do
        sleep 1
        if systemctl is-active --quiet "$SERVICE" && [[ -f "$HEALTH" ]]; then
            mtime="$(stat -c %Y "$HEALTH" 2>/dev/null || echo 0)"
            if (( mtime >= restart_ts )); then healthy=1; break; fi
        fi
    done

    if (( healthy == 1 )); then
        log "release $version SANA"
        _prune_releases
        exit 0
    fi

    log "release $version NON sana -> ROLLBACK a ${prev:-precedente}"
    if [[ -n "$prev" && -d "$prev" ]]; then
        _swap_current "$prev"
        systemctl restart "$SERVICE"
    fi
    rm -rf "$rel"
    exit 4
}

cmd_system() {
    local mode="${1:-security}"
    export DEBIAN_FRONTEND=noninteractive
    log "apt-get update"
    apt-get update -qq
    if [[ "$mode" == "full" ]]; then
        log "upgrade completo"
        apt-get -y -qq upgrade
    else
        if command -v unattended-upgrade >/dev/null 2>&1; then
            log "security upgrade (unattended-upgrade)"
            unattended-upgrade -v || true
        else
            log "unattended-upgrade non installato: nessun upgrade security applicato"
        fi
    fi
    log "aggiornamento sistema ($mode) completato"
    exit 0
}

cmd_reboot() { log "reboot host"; systemctl reboot; exit 0; }

case "${1:-}" in
    agent)  shift; cmd_agent "${1:-}" "${2:-}" ;;
    system) shift; cmd_system "${1:-security}" ;;
    reboot) cmd_reboot ;;
    *) echo "uso: $0 {agent <version> <artifact>|system <security|full>|reboot}" >&2; exit 64 ;;
esac
