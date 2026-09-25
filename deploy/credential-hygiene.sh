#!/usr/bin/env bash
#
# Audit -- and optionally clear -- the places this deployment stores Apple credentials and device
# identity. Run it ON THE SERVER.
#
#   bash deploy/credential-hygiene.sh                 # report only, changes nothing
#   bash deploy/credential-hygiene.sh --clean-logs    # truncate container logs
#   bash deploy/credential-hygiene.sh --clean-strays  # remove leftover copies nothing needs
#
# WHY THIS EXISTS. A successful sign-in prints the full Apple account record and every bearer
# token to stdout -- including a com.apple.gs.icloud.auth token with a 31536000-second (one year)
# lifetime -- and every refresh prints the anisette machine identity. All of it lands in
# `docker logs`, which the json-file driver persists to disk on the host. web/installer.py redacts
# what reaches the BROWSER; nothing redacts what reaches docker logs.
#
# WHAT THIS DELIBERATELY WILL NOT TOUCH. Four things are working state, not leaks. Deleting them
# breaks unattended refresh, so no flag here removes them:
#
#   the Apple ID password in the container environment and Portainer's stack definition
#   the signing certificate and private key (<team-id>.p12) in the altserver-data volume
#   the Apple machine identity in the anisette-config volume
#   the iPhone pairing record in /var/lib/lockdown   <-- removing this forces a USB re-pair
#
# Remove those only when decommissioning, or if the Apple ID is believed compromised, and read
# docs/REVIVAL.md ("Credential sweep") first for what each costs you.
#
# This script never prints a secret value. It reports locations and sizes only.

set -uo pipefail

CLEAN_LOGS=0
CLEAN_STRAYS=0
for arg in "$@"; do
    case "$arg" in
        --clean-logs)   CLEAN_LOGS=1 ;;
        --clean-strays) CLEAN_STRAYS=1 ;;
        -h|--help)      sed -n '2,28p' "$0" | sed 's/^#//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

have() { command -v "$1" >/dev/null 2>&1; }
hr()   { printf '%s\n' "------------------------------------------------------------------------"; }

if ! have docker; then
    echo "docker not found. Run this on the host where the stack is deployed." >&2
    exit 1
fi

findings=0
note() { printf '  %s\n' "$*"; }
flag() { findings=$((findings+1)); printf '  [!] %s\n' "$*"; }

hr; echo "CONTAINER LOGS -- hold the account record and long-lived tokens"; hr
for c in altserver altserver-web anisette netmuxd; do
    if ! docker inspect "$c" >/dev/null 2>&1; then
        note "$c: not present, skipping"
        continue
    fi
    logpath=$(docker inspect -f '{{.LogPath}}' "$c" 2>/dev/null)
    # `test -e` must run as root: /var/lib/docker/containers is mode 0710 root:root, so an
    # unprivileged check returns false for a file that is plainly there -- which reported
    # "no json-file log" for every container on a host whose logs were full of credentials.
    if [ -z "$logpath" ] || ! sudo test -e "$logpath"; then
        note "$c: no json-file log on disk (driver may not be json-file)"
        continue
    fi
    size=$(sudo du -h "$logpath" 2>/dev/null | cut -f1)
    # Count, never print. A hit means credential material is sitting in this file.
    # Count lines that carry a marker but are NOT already redacted. Since docker/redact-log.py
    # masks the VALUE and keeps the LABEL, a successfully filtered line still contains the word
    # "MachineID" -- so a naive marker count reports a leak for a log that is doing its job.
    hits=$(sudo grep -aiE 'GsIdmsToken|com\.apple\.gs\.|adsid|DsPrsId|MachineID|X-Apple-I-MD' "$logpath" 2>/dev/null \
           | grep -avc '\[withheld\]')
    hits=${hits:-0}
    redacted=$(sudo grep -ac '\[withheld\]' "$logpath" 2>/dev/null)
    redacted=${redacted:-0}
    if [ "${hits:-0}" -gt 0 ]; then
        flag "$c: ${size:-?} log, $hits line(s) carrying credentials or machine identity"
    elif [ "${redacted:-0}" -gt 0 ]; then
        note "$c: ${size:-?} log, clean -- $redacted line(s) redacted by the filter"
    else
        note "$c: ${size:-?} log, no credential markers found"
    fi
    if [ "$CLEAN_LOGS" = "1" ]; then
        sudo truncate -s 0 "$logpath" && note "    truncated (container keeps running; only history is lost)"
    fi
done

hr; echo "STRAY COPIES -- nothing in the running stack needs these"; hr

# ~/AltServerData is only a stray when the daemon runs in the container. A bare-metal deployment
# (the static binary, e.g. under systemd) keeps its signing certificate in ./AltServerData relative
# to its working directory -- often $HOME -- and deleting that is not cleanup: the next install
# finds no cached .p12, REVOKES the certificate, and every sideloaded app, AltStore included,
# stops launching. So it counts as live when any AltServer process has $HOME as its working
# directory, or when there is no altserver container at all.
altserverdata_live() {
    local pid cwd
    for pid in $(pgrep -f AltServer 2>/dev/null); do
        cwd=$(sudo readlink "/proc/$pid/cwd" 2>/dev/null) || continue
        [ "$cwd" = "$HOME" ] && return 0
    done
    ! docker inspect altserver >/dev/null 2>&1
}

strays=()
if [ -d "$HOME/AltServerData" ]; then
    if altserverdata_live; then
        note "$HOME/AltServerData: LIVE working state of a bare-metal AltServer -- never removed here"
    else
        strays+=("$HOME/AltServerData")
    fi
fi

# The tarballs are the backups README.md step 7 tells you to take. They hold identifying material,
# so they are reported -- but never deleted by a flag: they are the only disaster-recovery path for
# the anisette identity and the pairing record, and losing the latter means fetching the cable.
backups=()
for g in "$HOME"/anisette-state*.tgz "$HOME"/anisette-identity*.tgz "$HOME"/lockdown-backup*.tgz; do
    [ -e "$g" ] && backups+=("$g")
done

if [ ${#strays[@]} -eq 0 ] && [ ${#backups[@]} -eq 0 ]; then
    note "none found"
fi
for s in "${strays[@]}"; do
    flag "$(du -sh "$s" 2>/dev/null | cut -f1)  $s  (holds a .p12 -- the PRIVATE KEY for your signing certificate)"
done
for b in "${backups[@]}"; do
    flag "$(du -sh "$b" 2>/dev/null | cut -f1)  $b  (backup: move it off this host; not removed by any flag)"
done
if [ "$CLEAN_STRAYS" = "1" ]; then
    for s in "${strays[@]}"; do
        rm -rf -- "$s" && note "    removed $s"
    done
fi

hr; echo "INTERRUPTED INSTALLS -- signed bundles left in container /tmp"; hr
tmp_checked=0
for c in altserver altserver-web; do
    docker inspect "$c" >/dev/null 2>&1 || { note "$c: not present, skipping"; continue; }
    tmp_checked=1
    leftovers=$(docker exec "$c" sh -c 'ls -d /tmp/*-*-*-*-* 2>/dev/null | head -20' 2>/dev/null)
    if [ -n "$leftovers" ]; then
        flag "$c: UUID-named directories in /tmp (may contain ALTCertificate.p12)"
        echo "$leftovers" | sed 's/^/        /'
        if [ "$CLEAN_STRAYS" = "1" ]; then
            docker exec "$c" sh -c 'rm -rf /tmp/*-*-*-*-*' 2>/dev/null && note "    cleared"
        fi
    else
        note "$c: clean"
    fi
done

hr; echo "EXPOSURE -- who can reach the web UI"; hr
if docker inspect altserver-web >/dev/null 2>&1; then
    cmd=$(docker inspect -f '{{range .Config.Cmd}}{{.}} {{end}}' altserver-web 2>/dev/null)
    if printf '%s' "$cmd" | grep -q '0\.0\.0\.0'; then
        flag "altserver-web is bound to 0.0.0.0 -- the status page shows the device UDID and"
        note "    anisette Device-Id to anyone on the LAN, and /install takes an Apple ID"
        note "    password over plain HTTP. To restrict it, set in deploy/altserver-stack.yml:"
        note "        command: [\"--host\", \"127.0.0.1\", \"--port\", \"8099\"]"
        note "    then reach it with:  ssh -L 8099:127.0.0.1:8099 you@this-host"
    else
        note "altserver-web is not bound to 0.0.0.0 (command: ${cmd:-unknown})"
    fi
else
    note "altserver-web not present, skipping"
fi

hr; echo "SHELL HISTORY -- from any bare-metal install that passed -p"; hr
histhits=0
for h in "$HOME/.bash_history" "$HOME/.zsh_history"; do
    [ -f "$h" ] || continue
    n=$(grep -acE 'AltServer.* -p |ALTSERVER_APPLE_PASSWORD=' "$h" 2>/dev/null)
    n=${n:-0}
    if [ "${n:-0}" -gt 0 ]; then
        flag "$h: $n line(s) that may contain an Apple ID password"
        histhits=1
    fi
done
[ "$histhits" = "0" ] && note "no matching lines"

hr
if [ "$findings" -eq 0 ]; then
    echo "Nothing flagged."
else
    echo "$findings item(s) flagged."
    [ "$CLEAN_LOGS" = "0" ] && echo "Re-run with --clean-logs to truncate the logs above."
    [ "$CLEAN_STRAYS" = "0" ] && echo "Re-run with --clean-strays to remove the stray copies above."
fi
echo
echo "One thing this script cannot reach: a password your browser saved for this host."
echo "Check your browser's password manager for an entry on http://<this-host>:8099."
