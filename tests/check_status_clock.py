#!/usr/bin/env python3
"""Hold the status page's clock and anisette checks to the failures they exist to catch.

WHY THIS EXISTS. The clock check used to compare the anisette server's X-Apple-I-Client-Time with
time.time() in this container. On the intended deployment both run on ONE host, and containers
have no CLOCK_REALTIME of their own, so that comparison is ~0 no matter how wrong the host clock
is. A Raspberry Pi has no RTC: after a power cut it runs on the last saved time until NTP syncs,
and the page said "Anisette clock within 0s of ours" throughout.

The anisette check also fetched "/" on every 30 s poll. On current anisette-v3-server images "/"
provisions against Apple whenever the machine is unprovisioned, so an unprovisioned server was
hit with a provisioning attempt per poll. Liveness now uses the static /v3/client_info and "/" is
fetched at most every ALTSERVER_ANISETTE_CHECK_INTERVAL seconds.

Everything runs against local mock servers; nothing leaves the machine.
"""

import json
import os
import sys
import tempfile
import threading
import time as real_time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "web"))
import status_checks as sc  # noqa: E402

HITS = {}
STATE = {"device_id": "EXAMPLE1-0000-4000-8000-EXAMPLE00000", "anisette_utc_offset": 0}


class HostClock:
    """What status_checks (and the same-host anisette mock) see as time.time()."""

    def __init__(self):
        self.error = 0.0  # seconds the host clock is AHEAD of real time (negative = behind)

    def time(self):
        return real_time.time() + self.error

    def __getattr__(self, name):
        return getattr(real_time, name)


HOST = HostClock()
sc.time = HOST  # only the module under test sees the wrong clock; the reference sees real time


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, code, body=b"", date=None):
        self.send_response_only(code)
        self.send_header("Date", date or formatdate(real_time.time(), usegmt=True))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        HITS[self.path] = HITS.get(self.path, 0) + 1
        if self.path == "/nodate":
            self.send_response_only(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:  # the reference: real time, like any NTP-synced web server
            self._reply(404 if self.path == "/missing" else 200)

    def do_GET(self):
        HITS[self.path] = HITS.get(self.path, 0) + 1
        if self.path == "/v3/client_info":
            self._reply(200, b'{"client_info":"x","user_agent":"akd/1.0"}')
            return
        # anisette-v3-server: LOCAL wall time of ITS host, fraction cut, literal "Z" appended.
        stamp = real_time.strftime("%Y-%m-%dT%H:%M:%S", real_time.gmtime(
            HOST.time() + STATE["anisette_utc_offset"])) + "Z"
        body = {k: "EXAMPLE" for k in sc.ANISETTE_REQUIRED_KEYS}
        body.update({"X-Apple-I-Client-Time": stamp, "X-Apple-I-MD-RINFO": "17106176",
                     "X-Mme-Device-Id": STATE["device_id"]})
        self._reply(200, json.dumps(body).encode())


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    tmp = tempfile.mkdtemp()
    sc.ANISETTE_IDENTITY_FILE = os.path.join(tmp, "identity")
    sc.CLOCK_REFERENCE_URL = base + "/ref"
    failures = []

    def expect(label, result, state, needle=""):
        text = "%s | %s" % (result["summary"], result["detail"])
        if result["state"] != state or needle not in text:
            failures.append("%s: got %s (%s), want %s containing %r"
                            % (label, result["state"], text, state, needle))
        else:
            print("ok   %-44s %s: %s" % (label, result["state"], result["summary"]))

    def fresh():
        sc._reference.clear()
        sc._anisette_cache.clear()

    real_kernel = sc._kernel_ntp_synced
    sc._kernel_ntp_synced = lambda: True  # deterministic; the real call is exercised in step 5

    # 1. The blind spot: host 3 h slow, anisette on the same host -> anisette agrees with us.
    fresh(); HOST.error = -3 * 3600
    a = sc.check_anisette(base)
    expect("same-host anisette agrees (skew ~0)", sc.check_clock(None, a.get("anisette_skew")),
           "fail", "behind real time")
    if abs(a.get("anisette_skew", 99)) > 2:
        failures.append("anisette skew should be ~0 on one host, got %r" % a.get("anisette_skew"))

    # 2. NTP steps the clock after the reference was cached: must clear at the next poll.
    HOST.error = 0
    sc._anisette_cache.clear()
    a = sc.check_anisette(base)
    expect("clock stepped after probe (cached ref)", sc.check_clock(None, a.get("anisette_skew")),
           "ok", "Within 30s")
    if HITS.get("/ref") != 1:
        failures.append("reference must be probed once per interval, was %r" % HITS.get("/ref"))

    # 3. Anisette container with TZ=Asia/Tokyo: local time + literal Z = 9 h in the future.
    fresh(); STATE["anisette_utc_offset"] = 9 * 3600
    a = sc.check_anisette(base)
    expect("anisette TZ != UTC", sc.check_clock(None, a.get("anisette_skew")), "fail",
           "Anisette clock is")
    STATE["anisette_utc_offset"] = 0

    # 4. No reference reachable: fall back to the kernel NTP state (injected for determinism).
    sc.CLOCK_REFERENCE_URL = "http://127.0.0.1:9/"  # discard port: connection refused
    for synced, state, needle in ((False, "warn", "NOT NTP"), (True, "ok", "no external"),
                                  (None, "unknown", "No independent")):
        fresh(); sc._kernel_ntp_synced = lambda s=synced: s
        expect("no reference, kernel synced=%s" % synced, sc.check_clock(), state, needle)
    sc._kernel_ntp_synced = real_kernel
    for path, label in (("/nodate", "reference without Date"), ("/missing", "reference 404 w/ Date")):
        fresh(); sc.CLOCK_REFERENCE_URL = base + path; sc._kernel_ntp_synced = lambda: True
        expect(label, sc.check_clock(), "ok", "")
    sc._kernel_ntp_synced = real_kernel
    sc.CLOCK_REFERENCE_URL = base + "/ref"

    # 5. The real adjtimex() call must not raise, whatever the environment allows.
    v = real_kernel()
    print("ok   %-44s %r" % ("real adjtimex() -> synced", v)) if v in (True, False, None) \
        else failures.append("adjtimex returned %r" % v)

    # 6. "/" at most once per interval; liveness hits /v3/client_info every time.
    fresh(); HITS.clear()
    for _ in range(5):
        sc.check_anisette(base)
    if HITS.get("/") != 1 or HITS.get("/v3/client_info") != 5:
        failures.append("expected 1x '/' and 5x '/v3/client_info', got %r" % HITS)
    else:
        print("ok   %-44s %r" % ("5 polls -> hits", HITS))

    # 7. Identity change (lost/replaced anisette volume) must be loud, and persist across restarts.
    fresh(); STATE["device_id"] = "EXAMPLE2-0000-4000-8000-EXAMPLE00000"
    expect("anisette identity changed", sc.check_anisette(base), "fail", "CHANGED")

    srv.shutdown()
    if failures:
        print("\n".join("FAIL: " + f for f in failures))
        return 1
    print("clock and anisette checks behave as intended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
