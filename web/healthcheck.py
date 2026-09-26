#!/usr/bin/env python3
"""Container healthchecks for deploy/altserver-stack.yml. Exit 0 = healthy, 1 = not.

Deliberately cheap -- this runs every few minutes for months: /proc reads, one socket round trip,
at most one avahi-browse. Stdlib only.

  altserver  AltServer holds a LISTENING TCP socket, and _altserver._tcp is advertised on that
             port. It never connects to AltServer: every connection costs a worker task and log
             lines there, and builds before the WirelessConnection fix spun forever on a peer that
             connects and closes.
  netmuxd    one ListDevices round trip on netmuxd's socket. An empty list is healthy -- the phone
             being away is not a netmuxd fault -- but with ALTSERVER_UDID + ALTSERVER_PHONE_ADDRESSES
             a missing phone is re-added with AddDevice, and a listed-but-dead entry is a failure.
  web        GET /api/install/status: proves the HTTP server answers, runs nothing. Not /healthz
             or /api/status -- those run the full check suite (avahi-browse, idevice_id, and a
             lockdownd pairing validation that wakes the phone's radio).

--restart-after N: after N consecutive failures, SIGTERM PID 1 so the container exits and
restart: unless-stopped starts it again (a Docker healthcheck alone only flags "unhealthy").
One failed probe never restarts anything -- an avahi-daemon restart, say, leaves a few seconds
with no advert -- and PID 1 must be Docker's init (init: true): with pid: host it would be the
HOST's systemd, so the script refuses.
"""
import glob
import os
import plistlib
import signal
import socket
import struct
import subprocess
import sys
import urllib.request

# Consecutive-failure counters. /dev/shm (tmpfs) rather than /tmp: no SSD write every 5 minutes.
STATE_DIR = "/dev/shm" if os.access("/dev/shm", os.W_OK) else "/tmp"
DOCKER_INITS = ("docker-init", "tini", "catatonit")


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def altserver():
    pids = [p.split("/")[2] for p in glob.glob("/proc/[0-9]*/comm")
            if _read(p).startswith("AltServer")]
    if not pids:
        return "no AltServer process"
    inodes = set()
    for pid in pids:
        for fd in glob.glob("/proc/%s/fd/*" % pid):
            try:
                link = os.readlink(fd)
            except OSError:
                continue
            if link.startswith("socket:["):
                inodes.add(link[8:-1])
    ports = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for row in _read(table).splitlines()[1:]:
            col = row.split()
            if len(col) > 9 and col[3] == "0A" and col[9] in inodes:     # 0A = LISTEN
                ports.add(int(col[1].rsplit(":", 1)[1], 16))
    if not ports:
        return "AltServer is running but has no listening socket"
    if os.environ.get("ALTSERVER_HEALTHCHECK_MDNS", "1") == "0":
        return None
    try:
        p = subprocess.run(["avahi-browse", "-rpt", "_altserver._tcp"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None     # cannot judge the advert; never restart a listening server over that
    if "Daemon not running" in p.stderr or "Failed to create client" in p.stderr:
        return None     # avahi itself is down: restarting AltServer cannot fix it
    adverts = set()
    for line in p.stdout.splitlines():
        f = line.split(";")
        if f[0] == "=" and len(f) > 8 and f[8].isdigit():
            adverts.add(int(f[8]))
    if not ports & adverts:
        return ("listening on %s but _altserver._tcp is advertised on %s"
                % (sorted(ports), sorted(adverts) or "nothing"))
    return None


def _mux(path, message, timeout=10):
    body = plistlib.dumps(dict(message, ProgName="healthcheck", ClientVersionString="healthcheck"))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, 1) + body)
        hdr = s.recv(16, socket.MSG_WAITALL)
        if len(hdr) < 16:
            return {}           # netmuxd closes without a reply when it ignores a request
        length = struct.unpack("<I", hdr[:4])[0]
        return plistlib.loads(s.recv(length - 16, socket.MSG_WAITALL))


def netmuxd(path=os.environ.get("ALTSERVER_NETMUXD_SOCKET", "/run/muxd/usbmuxd")):
    try:
        reply = _mux(path, {"MessageType": "ListDevices"})
    except Exception as exc:
        return "%s: %s" % (path, exc)
    if not isinstance(reply.get("DeviceList"), list):
        return "%s: unexpected reply %r" % (path, sorted(reply))
    return _phone(path, reply["DeviceList"])


def _phone(path, devices):
    """netmuxd (v0.4.3) adds a network device only when mdns-sd reports a NEW or CHANGED record,
    and drops it on any heartbeat failure (SleepyTime, a Wi-Fi blip over ~15 s, a DHCP address
    change). The phone's unchanged advert then never re-adds it: it stays missing until netmuxd
    restarts. With ALTSERVER_UDID and ALTSERVER_PHONE_ADDRESSES (its reserved LAN IP and/or
    WireGuard IP) set, ask netmuxd to add it back. netmuxd verifies the address itself (lockdown
    TLS with the pairing record, then a heartbeat) and answers Result 0 for a wrong or unreachable
    one. Sent only when the UDID is absent: for a listed device netmuxd drops AddDevice and logs
    an ERROR, and two adds in flight at once create duplicate entries. A listed address whose
    lockdownd port is dead is a stale duplicate netmuxd will never drop: report it, so
    --restart-after replaces netmuxd."""
    udid = os.environ.get("ALTSERVER_UDID", "")
    addrs = os.environ.get("ALTSERVER_PHONE_ADDRESSES", "").replace(",", " ").split()
    if not udid:
        return None
    listed = [d.get("Properties", {}) for d in devices
              if d.get("Properties", {}).get("SerialNumber") == udid]
    for props in listed:
        raw = props.get("NetworkAddress") or b""
        if len(raw) < 8 or raw[0] != 2:                     # not a Linux sockaddr_in: cannot judge
            return None
        try:
            socket.create_connection((socket.inet_ntoa(raw[4:8]), 62078), timeout=4).close()
            return None
        except OSError:
            pass
    if listed:
        return "netmuxd lists %s but none of its addresses answers on 62078" % udid
    for ip in addrs:
        try:
            ok = _mux(path, {"MessageType": "AddDevice", "ConnectionType": "Network",
                             "ServiceName": "_apple-mobdev2._tcp.local", "IPAddress": ip,
                             "DeviceID": udid}, timeout=6).get("Result") == 1
        except Exception:
            ok = False
        print("netmuxd did not list %s; AddDevice %s: %s" % (udid, ip, "added" if ok else "no"))
        if ok:
            break
    return None                 # the phone being away is not a netmuxd fault


def web(port=os.environ.get("ALTSERVER_WEB_PORT", "8099")):
    try:
        urllib.request.urlopen("http://127.0.0.1:%s/api/install/status" % port, timeout=10).read()
    except Exception as exc:
        return str(exc)
    return None


def _count_failure(what, failed):
    """Consecutive failures of `what`, including this one (0 after a success)."""
    path = os.path.join(STATE_DIR, "altserver-healthcheck-%s.failures" % what)
    if not failed:
        try:
            os.unlink(path)
        except OSError:
            pass
        return 0
    try:
        n = int(_read(path) or 0) + 1
    except ValueError:
        n = 1
    try:
        with open(path, "w") as f:
            f.write(str(n))
    except OSError:
        pass
    return n


def _restart_container():
    init = _read("/proc/1/comm").strip()
    if init not in DOCKER_INITS:
        print("not restarting: PID 1 is %r, not Docker's init (needs init: true, no pid: host)"
              % init)
        return
    print("restarting the container (SIGTERM to %s)" % init)
    os.kill(1, signal.SIGTERM)


def main(argv):
    what = argv[1] if len(argv) > 1 else ""
    check = {"altserver": altserver, "netmuxd": netmuxd, "web": web}.get(what)
    restart_after = 0
    if len(argv) == 4 and argv[2] == "--restart-after" and argv[3].isdigit():
        restart_after = int(argv[3])
    elif len(argv) != 2:
        check = None
    if check is None:
        sys.exit("usage: healthcheck.py altserver|netmuxd|web [--restart-after N]")
    problem = check()
    failures = _count_failure(what, bool(problem))
    if not problem:
        print("healthy")
        return 0
    print("UNHEALTHY (%d in a row): %s" % (failures, problem))
    if restart_after and failures >= restart_after:
        _count_failure(what, False)     # the fresh container starts counting from zero
        _restart_container()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
