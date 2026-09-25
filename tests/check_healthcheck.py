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
being away is not netmuxd's fault), a mux that accepts and hangs up is not. Stdlib only:

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

    if failures:
        print("\n".join("FAIL: " + f for f in failures))
        return 1
    print("healthchecks restart only after repeated failures, and only via Docker's init.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
