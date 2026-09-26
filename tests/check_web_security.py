#!/usr/bin/env python3
"""Web-UI security regression guards for web/server.py and web/installer.py.

WHY THIS EXISTS. The setup web UI accepts an Apple ID password over plain HTTP and, on the
shipped stack, is bound to 0.0.0.0 on a host that is also reachable over WireGuard. Several
browser- and network-borne holes were live at once, each invisible in normal use:

  * CSRF: POST accepted a JSON body under ANY Content-Type, so a page on another origin could
    drive /api/install/start with a text/plain body (which skips the CORS preflight).
  * A double-start race: start() checked state under the lock but set RUNNING outside it, so
    concurrent POSTs spawned several AltServer children at once -- several real Apple sign-ins
    from one anisette identity, a fast path to a locked Apple ID.
  * An unbounded / negative Content-Length made rfile.read() block forever, leaking a thread per
    request (and buffering gigabytes if the body was actually sent -- an OOM on a 4 GB Pi).
  * No Host check, so a DNS-rebinding page could read /api/status (device identifiers) and POST.
  * An abandoned 2FA prompt never timed out: the child sat on std::cin and every later install was
    refused until the container was restarted, with no way to cancel.

These are not checked by parsing the code -- they are exercised against a live server on an
ephemeral port with a fake AltServer, because that is the only thing that proves the guard is
actually wired into the request path. Stdlib only, so it runs in CI with no dependencies:

    python3 tests/check_web_security.py

Point it at a copy of the web/ dir with ALTSERVER_WEB_DIR to test a patched tree in place.
"""

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.environ.get("ALTSERVER_WEB_DIR", os.path.join(ROOT, "web"))
sys.path.insert(0, WEB_DIR)

import installer  # noqa: E402
import server  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

FAILURES = []
PASSES = []


def check(name, ok, detail=""):
    (PASSES if ok else FAILURES).append(name)
    print(("ok    " if ok else "FAIL  ") + name + (("  -- " + detail) if detail else ""))
    return ok


# --------------------------------------------------------------------------------------------
# A fake AltServer: records every spawn, asks for a 2FA code, then blocks reading stdin (exactly
# where the real binary blocks on std::cin). Written at runtime so the test is self-contained.
# --------------------------------------------------------------------------------------------
FAKE_SRC = r'''#!/usr/bin/env python3
import os, sys, time
log = os.environ.get("FAKE_SPAWN_LOG")
if log:
    with open(log, "a") as f:
        f.write("spawn %d\n" % os.getpid())
print("Starting AltServer...", flush=True)
print("Enter two factor code:", flush=True)
line = sys.stdin.readline()          # blocks like std::cin >> _verificationCode
if not line:
    print("Error: stdin closed", flush=True); sys.exit(1)
print("Installation Succeeded", flush=True)
'''


def make_fake(tmp):
    binpath = os.path.join(tmp, "fake_altserver")
    with open(binpath, "w") as f:
        f.write(FAKE_SRC)
    os.chmod(binpath, 0o755)
    ipa = os.path.join(tmp, "AltStore.ipa")
    with open(ipa, "wb") as f:
        f.write(b"PK\x03\x04dummy")
    return binpath, ipa


def raw_request(port, data):
    """Send raw bytes, return (first_response_line_or_None, timed_out). Closes the socket so a
    server stuck in rfile.read() unblocks on EOF instead of leaking a thread for the whole run."""
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    try:
        s.sendall(data)
        s.settimeout(2.5)
        try:
            first = s.recv(200).split(b"\r\n", 1)[0].decode("latin-1")
            return first, False
        except socket.timeout:
            return None, True
    finally:
        s.close()


def status(port, host="127.0.0.1"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/api/install/status", headers={"Host": host})
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    return r.status, (json.loads(body) if r.status == 200 else body)


def drain(port):
    """Return the installer to a terminal state between stateful sub-tests."""
    try:
        installer.INSTALLER.cancel()
    except Exception:
        pass
    for _ in range(30):
        st = installer.INSTALLER.snapshot()["state"]
        if st not in ("running", "awaiting_2fa"):
            return
        time.sleep(0.1)


def main():
    tmp = tempfile.mkdtemp(prefix="altweb-sec-")
    binpath, ipa = make_fake(tmp)
    spawn_log = os.path.join(tmp, "spawns.log")
    os.environ["FAKE_SPAWN_LOG"] = spawn_log

    # Point the singleton the server uses at the fake binary, with a short 2FA timeout.
    installer.INSTALLER = installer.Installer(binary=binpath, ipa=ipa)
    try:
        installer.INSTALLER.two_fa_timeout = 1.0     # patched attribute; harmless if absent
    except Exception:
        pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("test server on 127.0.0.1:%d  (web dir: %s)\n" % (port, WEB_DIR))

    def reset_spawns():
        open(spawn_log, "w").close()

    def nspawns():
        try:
            return sum(1 for l in open(spawn_log) if l.startswith("spawn"))
        except FileNotFoundError:
            return 0

    try:
        # ---- 1. CSRF: a text/plain body must be refused (no CORS preflight otherwise) ----
        reset_spawns()
        body = json.dumps({"udid": "u", "apple_id": "a@b.c", "password": "pw"})
        line, to = raw_request(port,
            ("POST /api/install/start HTTP/1.1\r\nHost: 127.0.0.1\r\n"
             "Content-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
             % (len(body), body)).encode())
        time.sleep(0.3)
        check("CSRF: text/plain POST is rejected (415), no install spawned",
              (line is not None and "415" in line) and nspawns() == 0,
              "response=%r spawns=%d" % (line, nspawns()))
        drain(port)

        # ---- 2. Content-Type application/json is accepted (guard is not just a blanket block) --
        reset_spawns()
        line, to = raw_request(port,
            ("POST /api/install/start HTTP/1.1\r\nHost: 127.0.0.1\r\n"
             "Content-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
             % (len(body), body)).encode())
        time.sleep(0.3)
        check("application/json POST is accepted (200)",
              line is not None and "200" in line, "response=%r" % line)
        drain(port)

        # ---- 3. Content-Length: negative, huge, and non-numeric must not hang ----
        for label, cl in (("negative", "-1"), ("huge", "5000000000"), ("non-numeric", "abc")):
            line, to = raw_request(port,
                ("POST /api/install/start HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                 "Content-Type: application/json\r\nContent-Length: %s\r\nConnection: close\r\n\r\n{}"
                 % cl).encode())
            check("Content-Length %s is rejected without hanging" % label,
                  (not to) and line is not None and (" 4" in line),   # some 4xx, no timeout
                  "timed_out=%s response=%r" % (to, line))

        # ---- 4. Host allowlist ----
        for host, want_ok, why in (
            ("evil.example:8099", False, "rebinding name rejected"),
            ("attacker.com", False, "public name rejected"),
            ("127.0.0.1", True, "IP literal accepted"),
            ("localhost", True, "localhost accepted"),
            ("raspberrypi.local", True, "*.local accepted"),
            ("rasai", True, "single-label name accepted"),
            ("rasai.lan:8099", True, "router-assigned .lan name accepted"),
            ("pi.home.arpa", True, "RFC 8375 home.arpa name accepted"),
            ("[::1]:8099", True, "bracketed IPv6 literal accepted"),
            ("lan.attacker.com", False, "private-looking label under a public name rejected"),
            ("evil.local.attacker.com", False, ".local must be the suffix, not a label"),
        ):
            code, _ = status(port, host=host)
            check("Host %r -> %s" % (host, "allowed" if want_ok else "403"),
                  (code == 200) == want_ok, "%s (got HTTP %d)" % (why, code))

        # ---- 5. Double-start race: concurrent starts spawn exactly one child ----
        reset_spawns()
        drain(port)
        real_exists = os.path.exists

        def slow_exists(p):                # model a slow USB-SSD stat() to widen the window
            r = real_exists(p); time.sleep(0.05); return r
        installer.os.path.exists = slow_exists
        results = []

        def fire():
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", "/api/install/start",
                      body=body, headers={"Host": "127.0.0.1",
                                          "Content-Type": "application/json"})
            results.append(json.loads(c.getresponse().read().decode())["ok"])
            c.close()
        ts = [threading.Thread(target=fire) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        installer.os.path.exists = real_exists
        time.sleep(0.4)
        oks = sum(1 for x in results if x)
        check("double-start race spawns exactly one AltServer child",
              oks == 1 and nspawns() == 1,
              "ok=True x%d, children spawned=%d (want 1/1)" % (oks, nspawns()))

        # ---- 6. Abandoned 2FA times out to FAILED on its own ----
        drain(port)
        reset_spawns()
        installer.INSTALLER.two_fa_timeout = 1.0
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/install/start", body=body,
                  headers={"Host": "127.0.0.1", "Content-Type": "application/json"})
        c.getresponse().read()
        c.close()
        # wait for awaiting_2fa
        for _ in range(30):
            if installer.INSTALLER.snapshot()["state"] == "awaiting_2fa":
                break
            time.sleep(0.1)
        awaiting = installer.INSTALLER.snapshot()["state"] == "awaiting_2fa"
        deadline = time.time() + 5
        final = installer.INSTALLER.snapshot()
        while time.time() < deadline and final["state"] == "awaiting_2fa":
            time.sleep(0.2)
            final = installer.INSTALLER.snapshot()
        check("abandoned 2FA times out to FAILED (no permanent lock)",
              awaiting and final["state"] == "failed" and "imed out" in (final["error"] or ""),
              "awaiting_reached=%s final_state=%s error=%r"
              % (awaiting, final["state"], final.get("error")))

        # ---- 7. /api/install/cancel exists and works ----
        drain(port)
        reset_spawns()
        installer.INSTALLER.two_fa_timeout = 600.0
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/install/start", body=body,
                  headers={"Host": "127.0.0.1", "Content-Type": "application/json"})
        c.getresponse().read()
        c.close()
        for _ in range(30):
            if installer.INSTALLER.snapshot()["state"] in ("running", "awaiting_2fa"):
                break
            time.sleep(0.1)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/install/cancel", body="{}",
                  headers={"Host": "127.0.0.1", "Content-Type": "application/json"})
        r = c.getresponse()
        cbody = json.loads(r.read().decode())
        c.close()
        time.sleep(0.4)
        check("/api/install/cancel terminates the install and returns to FAILED",
              r.status == 200 and cbody.get("ok") is True
              and installer.INSTALLER.snapshot()["state"] == "failed",
              "http=%d body=%s final_state=%s"
              % (r.status, cbody, installer.INSTALLER.snapshot()["state"]))
        drain(port)

    finally:
        httpd.shutdown()
        try:
            installer.INSTALLER.cancel()
        except Exception:
            pass
        # reap any fake children still blocked on stdin
        import subprocess
        subprocess.run(["pkill", "-f", binpath], capture_output=True)

    print("\n%d passed, %d failed" % (len(PASSES), len(FAILURES)))
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        return 1
    print("All web-security guards hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
