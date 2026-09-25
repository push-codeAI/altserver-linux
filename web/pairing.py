"""Pairing diagnosis for the setup wizard.

Pairing is where someone without background knowledge gets stuck, and its failure modes are
unusually opaque:

  * Wireless pairing is impossible in this build (HAVE_WIRELESS_PAIRING is undefined, and
    upstream libimobiledevice restricts it to Apple TV), so a USB cable is mandatory exactly
    once -- which is surprising for a project whose whole point is wireless refresh.
  * `idevicepair validate` fails with "a passcode is set" unless the device is UNLOCKED at that
    moment, which reads like a permissions error rather than "press the button".
  * On a VM the device may never appear at all, and the cause is hypervisor USB passthrough
    rather than anything on the Linux side.
  * A genuine device fault is later DISPLAYED by AltStore as "AltServer could not be found",
    because it remaps deviceNotFound/lostConnection to serverNotFound for any wireless server
    that is not isPreferred -- and AltServer-Linux hardcodes serverID "1234567" where
    Mac/Windows use a UUID, so isPreferred is permanently false here. That sends people to debug
    mDNS when mDNS is fine.

So this module's job is not to run commands, it is to tell the three "nothing is showing up"
cases apart and say which one you are in.
"""

import os
import re
import shutil
import stat
import subprocess

STEP_OK, STEP_TODO, STEP_BLOCKED = "ok", "todo", "blocked"


# netmuxd's socket, matching deploy/altserver-stack.yml. Same variable status_checks.py uses.
NETMUXD_SOCKET = os.environ.get("ALTSERVER_NETMUXD_SOCKET", "/run/muxd/usbmuxd")
_WIRELESS_ENV = {"USBMUXD_SOCKET_ADDRESS": "UNIX:" + NETMUXD_SOCKET}
# The host usbmuxd's socket (libusbmuxd's compiled-in default). Overridable for tests.
USBMUXD_SOCKET = os.environ.get("ALTSERVER_USBMUXD_SOCKET", "/var/run/usbmuxd")


def _path_kind(path):
    """'socket', 'missing', 'directory' or 'other'. os.path.exists() cannot tell a live socket from
    the empty DIRECTORY Docker creates when a bind-mounted host path is missing at container start."""
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "other"
    if stat.S_ISSOCK(mode):
        return "socket"
    return "directory" if stat.S_ISDIR(mode) else "other"


def _run(cmd, timeout=15, env=None):
    if shutil.which(cmd[0]) is None:
        return None, "%s is not installed" % cmd[0]
    try:
        run_env = dict(os.environ, **env) if env else None
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=run_env)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return None, "%s timed out" % cmd[0]
    except Exception as exc:  # pragma: no cover
        return None, "%s could not be run: %s" % (cmd[0], exc)


def _in_container():
    return os.path.exists("/.dockerenv")


def _find_pairing_backup():
    """Find a lockdown backup that contains BOTH required plists. Returns (path, summary)."""
    import glob
    import tarfile

    candidates = []
    for pattern in ("~/lockdown-backup*.tgz", "~/lockdown-backup*.tar.gz",
                    "/root/lockdown-backup*.tgz", "~/*lockdown*.tgz"):
        candidates.extend(glob.glob(os.path.expanduser(pattern)))

    for path in sorted(set(candidates), key=os.path.getmtime, reverse=True):
        try:
            with tarfile.open(path) as tf:
                names = tf.getnames()
        except Exception:
            continue
        has_system = any(n.endswith("SystemConfiguration.plist") for n in names)
        has_device = any(re.search(r"/[0-9A-Fa-f-]{8,}\.plist$", n) for n in names)
        if has_system and has_device:
            return path, "contains both plists"
    return None, ""


def diagnose():
    """Return an ordered wizard state: which step you are on, and what to do about it."""
    steps = []
    udids = []

    # --- 1. tooling -------------------------------------------------------------------------
    missing = [t for t in ("idevice_id", "idevicepair") if shutil.which(t) is None]
    if missing:
        steps.append({
            "title": "Install the device tools",
            "state": STEP_BLOCKED,
            "detail": "Missing: %s" % ", ".join(missing),
            "action": "sudo apt install -y usbmuxd libimobiledevice-utils",
            "note": "Do NOT `systemctl enable usbmuxd` on Ubuntu -- it is udev-activated and has "
                    "no [Install] section, so enabling it just prints a confusing message.",
        })
        return {"steps": steps, "udids": [], "paired": False, "next": steps[-1]["title"]}
    steps.append({"title": "Device tools installed", "state": STEP_OK,
                  "detail": "idevice_id and idevicepair are available", "action": "", "note": ""})

    # --- 2. is usbmuxd actually listening? ---------------------------------------------------
    sock = USBMUXD_SOCKET
    kind = _path_kind(sock)
    if kind == "socket":
        steps.append({"title": "usbmuxd socket present", "state": STEP_OK,
                      "detail": sock, "action": "", "note": ""})
    elif kind == "directory":
        # Docker made this: a bind mount of a host path that did not exist when the container
        # started (usbmuxd is udev-activated, so after a boot with no cable it never does) is
        # created as an empty DIRECTORY. The host usbmuxd then dies on every start with
        # "unlink(/var/run/usbmuxd) failed: Is a directory", and a container created earlier
        # against the socket no longer starts ("not a directory" mount error).
        steps.append({
            "title": "%s is a directory, not a socket" % sock,
            "state": STEP_BLOCKED,
            "detail": "Docker created it for a bind mount while usbmuxd was not running. The "
                      "host's usbmuxd cannot start until it is removed, so a USB cable does "
                      "nothing.",
            "action": "On the host: delete the /var/run/usbmuxd lines from the stack, run "
                      "`docker compose up -d --force-recreate`, then `sudo rmdir /run/usbmuxd` "
                      "and replug the iPhone",
            "note": "Wi-Fi refresh does not use this socket (it goes through netmuxd), so an "
                    "already-paired phone keeps working. Only a USB pairing needs it.",
        })
        return {"steps": steps, "udids": [], "paired": False, "next": steps[-1]["title"]}
    elif kind == "other":
        steps.append({
            "title": "%s is not a socket" % sock,
            "state": STEP_BLOCKED,
            "detail": "Something other than usbmuxd created this path.",
            "action": "On the HOST: sudo rm %s, then replug the iPhone" % sock,
            "note": "",
        })
        return {"steps": steps, "udids": [], "paired": False, "next": steps[-1]["title"]}
    elif _path_kind(NETMUXD_SOCKET) == "socket":
        # No cable mux, but netmuxd is up: normal for a paired phone on Wi-Fi. Carry on to the
        # device check instead of stopping here -- that stop reported "usbmuxd is not running"
        # for a setup whose wireless path was fine.
        steps.append({"title": "No USB mux (normal without a cable)", "state": STEP_OK,
                      "detail": "%s is absent; checking Wi-Fi via netmuxd (%s)"
                                % (sock, NETMUXD_SOCKET),
                      "action": "",
                      "note": "A first-time pairing needs the cable: plug the iPhone into the "
                              "HOST and run `idevicepair pair` there."})
    elif _in_container():
        steps.append({
            "title": "No device mux reachable",
            "state": STEP_BLOCKED,
            "detail": "Neither %s (host usbmuxd, cable) nor %s (netmuxd, Wi-Fi) exists here."
                      % (sock, NETMUXD_SOCKET),
            "action": "docker compose ps netmuxd; docker logs --tail 50 netmuxd",
            "note": "netmuxd serves the Wi-Fi path through the shared muxd-socket volume. A "
                    "first-time pairing is done with the cable on the HOST (`idevicepair pair`).",
        })
        return {"steps": steps, "udids": [], "paired": False, "next": steps[-1]["title"]}
    else:
        steps.append({
            "title": "usbmuxd is not running",
            "state": STEP_BLOCKED,
            "detail": "%s does not exist." % sock,
            "action": "sudo systemctl start usbmuxd    # or plug the phone in, which starts it",
            "note": "Older guidance says to stop usbmuxd because netmuxd takes this same "
                    "socket. That is NOT true of this stack: netmuxd is given its own "
                    "--socket-path in a shared volume, so the host usbmuxd keeps the cable "
                    "and nothing contends. Leave usbmuxd alone.",
        })
        return {"steps": steps, "udids": [], "paired": False, "next": steps[-1]["title"]}

    # --- 3. is a device visible? -------------------------------------------------------------
    # BOTH transports. `idevice_id -l` includes USB only (tools/idevice_id.c: case 'l' sets
    # include_usb), and `-n` includes network only. On a cable-free server the phone is reachable
    # solely over netmuxd, so checking USB alone reported "no device" for a perfectly paired phone
    # and made /install demand a cable it does not need. Same flag trap as the reachability check.
    def _udids(args, env=None):
        _rc, o = _run(["idevice_id"] + args, env=env)
        return [l.strip() for l in (o or "").splitlines()
                if re.match(r"^[0-9A-Fa-f-]{8,}$", l.strip())]

    usb_udids = _udids(["-l"])
    net_udids = [u for u in _udids(["-n"], env=_WIRELESS_ENV) if u not in usb_udids]
    udids = usb_udids + net_udids
    wireless_only = bool(net_udids) and not usb_udids

    if not udids:
        steps.append({
            "title": "Plug the iPhone in with a USB cable",
            "state": STEP_TODO,
            "detail": "No device detected yet.",
            "action": "",
            "note": "A cable is required for this step and cannot be avoided: wireless pairing is "
                    "not supported by this build. Once paired, refreshing works over Wi-Fi and the "
                    "cable is never needed again.\n\n"
                    "If it is plugged in and still not showing: on a Proxmox/VMware guest the USB "
                    "device must be passed through to the VM in the hypervisor. Also try a "
                    "different cable -- charge-only cables carry no data.",
        })
        return {"steps": steps, "udids": [], "paired": False, "next": steps[-1]["title"]}

    steps.append({
        "title": "iPhone detected over Wi-Fi" if wireless_only else "iPhone detected",
        "state": STEP_OK,
        "detail": "UDID %s%s" % (udids[0], " (via netmuxd, no cable attached)" if wireless_only else ""),
        "action": "",
        "note": ("Already paired and reachable wirelessly -- no cable is needed. The USB step "
                 "above is only for a phone that has never been paired with this server."
                 if wireless_only else ""),
    })

    # --- 4. pairing ---------------------------------------------------------------------------
    # -n for a network device: idevicepair.c:372 selects IDEVICE_LOOKUP_USBMUX unless it is
    # passed, so validating a wireless device without it checks a USB device that is not there.
    rc, out = (_run(["idevicepair", "-n", "validate"], env=_WIRELESS_ENV) if wireless_only
               else _run(["idevicepair", "validate"]))
    low = (out or "").lower()

    if rc == 0:
        steps.append({"title": "Pairing valid", "state": STEP_OK,
                      "detail": out, "action": "", "note": ""})
        # Look for an existing backup rather than nagging about one already taken. Only counts
        # it if the archive actually contains BOTH files -- an archive with one of them is worse
        # than none, because it restores a mismatched HostID/SystemBUID that iOS rejects.
        backup, why = _find_pairing_backup()
        steps.append({
            "title": "Back up the pairing record",
            "state": STEP_OK if backup else STEP_TODO,
            "detail": ("Found %s (%s)" % (backup, why)) if backup
                      else "Losing it means fetching the cable again.",
            "action": "" if backup else "sudo tar czf ~/lockdown-backup.tgz /var/lib/lockdown/",
            "note": "Back up BOTH <UDID>.plist and SystemConfiguration.plist together. They are "
                    "not independent -- restoring only one produces a mismatched HostID/SystemBUID "
                    "that iOS rejects, reported as the same generic error as every other "
                    "lockdownd fault. Half a pairing is indistinguishable from none.",
        })
        return {"steps": steps, "udids": udids, "paired": True,
                "next": "Nothing -- setup complete" if backup else "Back up the pairing record"}

    if "passcode" in low:
        steps.append({
            "title": "Unlock the iPhone",
            "state": STEP_TODO,
            "detail": out,
            "action": "",
            "note": "The screen must be unlocked at the moment this runs. Unlock it and re-check "
                    "-- this is not a permissions problem, despite how it reads.",
        })
        return {"steps": steps, "udids": udids, "paired": False, "next": "Unlock the iPhone"}

    steps.append({
        "title": "Trust this computer on the iPhone",
        "state": STEP_TODO,
        "detail": out or "The device is visible but not paired.",
        "action": "idevicepair pair",
        "note": "Unlock the phone, run the pair command, then tap TRUST on the prompt that "
                "appears on the device and enter its passcode. If no prompt appears, unplug and "
                "replug the cable with the phone unlocked.",
    })
    return {"steps": steps, "udids": udids, "paired": False, "next": "Trust this computer"}


if __name__ == "__main__":
    import json
    print(json.dumps(diagnose(), indent=2))
