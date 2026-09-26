#!/usr/bin/env python3
"""Guards for web/healthcheck.py, the container healthchecks in deploy/altserver-stack.yml.

WHY THIS EXISTS. A Docker healthcheck on its own only marks a container "unhealthy"; nothing acts
on it. So the stack's checks restart the service themselves by SIGTERMing PID 1 (Docker's init,
init: true), and two ways of getting that wrong are expensive on an unattended box:

  * restarting on ONE failed probe. An avahi-daemon restart leaves a few seconds with no advert;
    a restart then can land in the middle of a refresh. Only N failures IN A ROW may restart.
  * signalling the wrong PID 1. altserver-web runs with pid: host, where PID 1 is the HOST's
    systemd -- SIGTERM makes it re-execute. The script must refuse anything but Docker's init.

It also runs the netmuxd probe against a fake mux: an empty device list is healthy (the phone
being away is not netmuxd's fault), a mux that accepts and hangs up is not. And netmuxd v0.4.3
drops a phone on any heartbeat failure but re-adds it only when its Bonjour record changes, so
with ALTSERVER_UDID set the probe re-adds a missing phone -- only when it is absent (an add for a
listed UDID is dropped with an ERROR, two in flight make duplicates) -- and reports a listed entry
whose lockdownd port is dead. Stdlib only:

    python3 tests/check_healthcheck.py
"""

import os
import plistlib
import signal
import socket
import struct
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "web"))
import healthcheck as hc  # noqa: E402

failures = []


def expect(what, got, want):
    if got == want:
        print("ok   %-52s %r" % (what, got))
    else:
        failures.append("%s: got %r, want %r" % (what, got, want))


def fake_mux(path, reply):
    """One-shot usbmuxd-protocol server: answers the first request with `reply` (None = hang up)."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            hdr = conn.recv(16, socket.MSG_WAITALL)
            length, _version, _type, tag = struct.unpack("<IIII", hdr)
            conn.recv(length - 16, socket.MSG_WAITALL)
            if reply is not None:
                body = plistlib.dumps(reply)
                conn.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, tag) + body)
        srv.close()

    threading.Thread(target=serve, daemon=True).start()


class FakeNetmuxd:
    """A usbmuxd-protocol server for many requests: ListDevices returns `devices`; AddDevice
    answers Result 1 for the addresses in `accept`, Result 0 otherwise. Records every request."""

    def __init__(self, path, devices, accept=()):
        self.devices, self.accept, self.seen = devices, set(accept), []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                length, _v, _t, tag = struct.unpack("<IIII", conn.recv(16, socket.MSG_WAITALL))
                req = plistlib.loads(conn.recv(length - 16, socket.MSG_WAITALL))
                self.seen.append((req["MessageType"], req.get("IPAddress")))
                if req["MessageType"] == "ListDevices":
                    reply = {"DeviceList": self.devices}
                else:
                    reply = {"MessageType": "Result",
                             "Result": 1 if req.get("IPAddress") in self.accept else 0}
                body = plistlib.dumps(reply)
                conn.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, tag) + body)

    def close(self):
        self.srv.close()


def listed(udid, ip):
    # netmuxd's NetworkAddress: a Linux sockaddr_in (family 2, port 0, the IPv4 address).
    raw = bytes([2, 0, 0, 0]) + socket.inet_aton(ip) + bytes(8)
    return {"DeviceID": 1, "Properties": {"SerialNumber": udid, "ConnectionType": "Network",
                                          "NetworkAddress": raw}}


def phone_checks(tmp):
    udid = "00008150-000A0B0C0D0E0F10"
    os.environ["ALTSERVER_UDID"] = udid
    os.environ["ALTSERVER_PHONE_ADDRESSES"] = "10.8.0.2, 127.0.0.1"

    # Missing phone: AddDevice each address in turn, stop at the first that netmuxd accepts; the
    # phone being away is never a netmuxd failure.
    path = os.path.join(tmp, "nm-missing")
    mux = FakeNetmuxd(path, [], accept={"127.0.0.1"})
    expect("phone missing -> re-added, healthy", hc.netmuxd(path), None)
    expect("  AddDevice order, stops at success", mux.seen,
           [("ListDevices", None), ("AddDevice", "10.8.0.2"), ("AddDevice", "127.0.0.1")])
    mux.close()

    # Listed and its lockdownd port answers: healthy, and no AddDevice (netmuxd drops an add for
    # a listed UDID and logs an ERROR; two adds in flight create duplicates).
    lockdownd = socket.socket()
    lockdownd.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        lockdownd.bind(("127.0.0.1", 62078))
    except OSError as e:
        print("skip listed-phone cases: cannot bind 127.0.0.1:62078 (%s)" % e)
        lockdownd = None
    if lockdownd:
        lockdownd.listen(4)
        path = os.path.join(tmp, "nm-listed")
        mux = FakeNetmuxd(path, [listed(udid, "127.0.0.1")])
        expect("listed, lockdownd answers -> healthy", hc.netmuxd(path), None)
        expect("  no AddDevice for a listed phone", mux.seen, [("ListDevices", None)])
        mux.close()
        lockdownd.close()
        # Same entry with nothing listening: the stale duplicate netmuxd never drops.
        path = os.path.join(tmp, "nm-stale")
        mux = FakeNetmuxd(path, [listed(udid, "127.0.0.1")])
        expect("listed, lockdownd dead -> unhealthy", hc.netmuxd(path) is not None, True)
        mux.close()

    # Without ALTSERVER_UDID nothing is added or probed: an empty list is healthy.
    del os.environ["ALTSERVER_UDID"]
    path = os.path.join(tmp, "nm-nounid")
    mux = FakeNetmuxd(path, [], accept={"127.0.0.1"})
    expect("no ALTSERVER_UDID -> list only", (hc.netmuxd(path), mux.seen), (None, [("ListDevices", None)]))
    mux.close()
    del os.environ["ALTSERVER_PHONE_ADDRESSES"]


def run(problem, pid1):
    """One healthcheck run of a check that reports `problem`, with /proc/1/comm = pid1.
    Returns (exit status, signals sent)."""
    sent = []
    hc.altserver = lambda: problem
    real_read = hc._read
    hc._read = lambda p: (pid1 + "\n") if p == "/proc/1/comm" else real_read(p)
    real_kill = hc.os.kill
    hc.os.kill = lambda pid, sig: sent.append((pid, sig))
    try:
        rc = hc.main(["healthcheck.py", "altserver", "--restart-after", "3"])
    finally:
        hc._read, hc.os.kill = real_read, real_kill
    return rc, sent


def main():
    tmp = tempfile.mkdtemp()
    hc.STATE_DIR = tmp

    # 1. Two failures do nothing; the third restarts, via SIGTERM to Docker's init only.
    expect("1st failure", run("no advert", "docker-init"), (1, []))
    expect("2nd failure", run("no advert", "docker-init"), (1, []))
    expect("3rd failure restarts", run("no advert", "docker-init"), (1, [(1, signal.SIGTERM)]))
    # ...and the counter starts again from zero afterwards.
    expect("1st failure after the restart", run("no advert", "docker-init"), (1, []))

    # 2. A success in between resets the streak.
    expect("success", run(None, "docker-init"), (0, []))
    for n in (1, 2):
        expect("failure %d after a success" % n, run("no advert", "docker-init"), (1, []))
    expect("success again", run(None, "docker-init"), (0, []))

    # 3. PID 1 that is not Docker's init (pid: host => the host's systemd) is never signalled.
    for _ in range(2):
        run("no advert", "systemd")
    expect("3rd failure under pid: host", run("no advert", "systemd"), (1, []))

    # 4. Without --restart-after it only reports, however many failures.
    hc.altserver = lambda: "no advert"
    real_kill, sent = hc.os.kill, []
    hc.os.kill = lambda pid, sig: sent.append((pid, sig))
    try:
        codes = [hc.main(["healthcheck.py", "altserver"]) for _ in range(5)]
    finally:
        hc.os.kill = real_kill
    expect("report-only: 5 failures", (codes, sent), ([1] * 5, []))

    # 5. Bad arguments are a usage error, not a silent pass.
    try:
        hc.main(["healthcheck.py", "altserver", "--restart-after", "x"])
        failures.append("bad --restart-after accepted")
    except SystemExit as e:
        expect("bad --restart-after", bool(e.code), True)

    # 6. netmuxd probe: empty device list = healthy; hang-up and no socket = unhealthy.
    sock = os.path.join(tmp, "mux-ok")
    fake_mux(sock, {"DeviceList": []})
    expect("netmuxd, empty device list", hc.netmuxd(sock), None)
    sock = os.path.join(tmp, "mux-drop")
    fake_mux(sock, None)
    expect("netmuxd, accepts and hangs up", hc.netmuxd(sock) is not None, True)
    expect("netmuxd, no socket", hc.netmuxd(os.path.join(tmp, "absent")) is not None, True)

    # 7. Re-adding a phone netmuxd dropped (ALTSERVER_UDID + ALTSERVER_PHONE_ADDRESSES).
    phone_checks(tmp)

    if failures:
        print("\n".join("FAIL: " + f for f in failures))
        return 1
    print("healthchecks restart only after repeated failures, and only via Docker's init.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
