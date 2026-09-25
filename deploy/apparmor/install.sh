#!/usr/bin/env bash
#
# Installs the altserver-mdns AppArmor profile on the HOST.
#
# This cannot be folded into the image or the compose file. AppArmor profiles are loaded into the
# host kernel by root, and Docker's `security_opt: apparmor=<name>` only SELECTS a profile that is
# already loaded. That is a property of AppArmor, not a gap in this project -- so this one step
# stays manual no matter how self-contained the rest of the deployment is.
#
#   sudo bash deploy/apparmor/install.sh
#
# Then switch the stack from `apparmor=unconfined` to `apparmor=altserver-mdns` on the altserver
# and altserver-web services, and redeploy.

set -euo pipefail

PROFILE_NAME="altserver-mdns"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${PROFILE_NAME}"
DEST="/etc/apparmor.d/${PROFILE_NAME}"

if [ "$(id -u)" -ne 0 ]; then
    echo "This must run as root: sudo bash $0" >&2
    exit 1
fi

if [ ! -f "$SRC" ]; then
    echo "Profile not found at $SRC" >&2
    exit 1
fi

if [ ! -d /sys/kernel/security/apparmor ]; then
    echo "AppArmor is not enabled on this kernel. Nothing to do -- and nothing to work around:" >&2
    echo "without AppArmor the containers are not being denied D-Bus in the first place." >&2
    echo "(Raspberry Pi OS kernels: CONFIG_LSM=\"\", so AppArmor is off unless cmdline.txt adds security=apparmor.)" >&2
    exit 1
fi

if ! command -v apparmor_parser >/dev/null 2>&1; then
    echo "apparmor_parser is not installed. On Debian/Ubuntu: apt install apparmor-utils" >&2
    exit 1
fi

echo "Installing ${PROFILE_NAME} -> ${DEST}"
install -m 0644 "$SRC" "$DEST"

echo "Loading it into the kernel"
apparmor_parser -r -W "$DEST"

# Confirm rather than assume: a profile can parse, install, and still not be loaded.
#
# NOT `aa-status --profiled`. That flag prints the NUMBER of loaded profiles, not their names, so
# grepping it for a profile name can never match -- it reported failure for a profile that had
# loaded perfectly well. The kernel's own list is the authority; aa-status output is the fallback
# for a host where securityfs is mounted somewhere unusual.
profile_loaded() {
    if [ -r /sys/kernel/security/apparmor/profiles ]; then
        awk '{print $1}' /sys/kernel/security/apparmor/profiles | grep -qx "$PROFILE_NAME" && return 0
    fi
    aa-status 2>/dev/null | grep -qE "^[[:space:]]*${PROFILE_NAME}\$" && return 0
    return 1
}

if profile_loaded; then
    echo "OK: ${PROFILE_NAME} is loaded."
else
    echo "WARNING: ${PROFILE_NAME} is not in the kernel's profile list." >&2
    echo "Check with: sudo aa-status | grep ${PROFILE_NAME}" >&2
    echo "Do NOT set ALTSERVER_APPARMOR until this succeeds -- a container that asks for an" >&2
    echo "unloaded profile fails to start." >&2
    exit 1
fi

cat <<EOF

Done. Now edit deploy/altserver-stack.yml and change BOTH occurrences of:

    security_opt:
      - apparmor=unconfined

to:

    security_opt:
      - apparmor=${PROFILE_NAME}

then redeploy the stack.

Verify afterwards that the container is actually running under it:

    docker exec altserver cat /proc/self/attr/current     # expect: ${PROFILE_NAME} (enforce)

and that mDNS works -- from ANOTHER machine on the LAN, not this host:

    avahi-browse -rt _altserver._tcp
EOF
