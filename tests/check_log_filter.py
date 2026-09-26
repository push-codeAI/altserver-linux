#!/usr/bin/env python3
"""Hold docker/redact-log.py's log-volume features to what they promise.

It folds per-chunk progress counters, timestamps the tee copy and records the last successful
refresh. Each of those sits in the daemon's only output path, so the test is mostly about what
must NOT happen: a non-progress line dropped or reordered, a failed send() folded away, a line
lost when the filter crashes, or a folded counter held back while the daemon is quiet.
"""

import json
import os
import re
import select
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILTER = os.path.join(ROOT, "docker", "redact-log.py")
PROGRESS = re.compile(r"Checking socket: \d+|Received bytes: \d+\(of \d+\)|"
                      r"Sent Bytes Count: \d+ \(\d+\)|Sent Data: \d+ Bytes|Represented Value: \d+")
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ")
SECRET = "EXAMPLEmachineIDnotARealAnisetteIdentity0000000000000000000000000000000000000000"


def source(web=os.path.join(ROOT, "web"), inject=""):
    src = open(FILTER).read().replace("/opt/altserver-web", web)
    return src.replace('if __name__ == "__main__":', inject + '\nif __name__ == "__main__":')


def upload(n_chunks, size_step=4096):
    total = n_chunks * size_step
    out = []
    for i in range(1, n_chunks + 1):
        out += ["Checking socket: 8", "Received bytes: %d(of %d)" % (i * size_step, total)]
    return out


FEED = (["Not supplying ipa, running in server mode!",
         "MachineID : " + SECRET,
         "Receiving app (40960000 bytes)..."]
        + upload(10000)
        + ["Error!"]
        + upload(3)
        # A failed send() in the middle of a run: -1 is its only trace, so it must survive.
        + ["Represented Value: 156", "Sent Bytes Count: 4 (4)", "Sent Data: 4 Bytes",
           "Sent Bytes Count: -1 (-1)", "Sent Data: -1 Bytes"]
        + ["Removed profile: com.EXAMPLE.AltStore (PROFILE0-1111-4111-8111-PROFILE11111)",
           "Installed profile: com.EXAMPLE.AltStore (PROFILE0-3333-4333-8333-PROFILE33333)",
           "Represented Value: 70", "Sent Bytes Count: 4 (4)", "Sent Data: 4 Bytes",
           "Sent Bytes Count: 70 (70)", "Sent Data: 70 Bytes",
           "Finished handling request!",
           "Failed to handle request:There was an error connecting to the device."])


def run(src, feed, args=(), env=None):
    return subprocess.run([sys.executable, "-c", src] + list(args), input="\n".join(feed) + "\n",
                          capture_output=True, text=True, env=env)


def is_subsequence(needles, hay):
    it = iter(hay)
    return all(any(h == n for h in it) for n in needles)


def main():
    problems = []
    tmp = tempfile.mkdtemp(prefix="check_log_filter.")
    tee = os.path.join(tmp, "altserver.log")
    env = dict(os.environ)
    env.pop("ALTSERVER_LAST_REFRESH", None)
    env.pop("ALTSERVER_LOG_COLLAPSE", None)

    r = run(source(), FEED, ["--tee", tee], env)
    out = r.stdout.splitlines()
    if r.returncode != 0:
        problems.append("filter exited %d: %s" % (r.returncode, r.stderr[-300:]))

    sys.path.insert(0, os.path.join(ROOT, "web"))
    from installer import _redact
    kept = [_redact(l) for l in FEED if not PROGRESS.fullmatch(l)]
    kept = [l for l in kept if l is not None]
    if not is_subsequence(kept, out):
        problems.append("a non-progress line was dropped, altered or reordered")
    if "Sent Bytes Count: -1 (-1)" not in out or "Sent Data: -1 Bytes" not in out:
        problems.append("a failed send() line was folded away")
    if not any(l.startswith("Received bytes: 40960000(of 40960000)") for l in out):
        problems.append("the last counter of a transfer was not shown")
    if len(out) > len(kept) + 12:
        problems.append("progress was not folded: %d lines out for %d kept" % (len(out), len(kept)))
    if SECRET in r.stdout or SECRET in open(tee).read():
        problems.append("redaction no longer applies")
    if any(STAMP.match(l) for l in out):
        problems.append("stdout lines carry a timestamp (docker already adds one)")
    tee_lines = open(tee).read().splitlines()
    if not tee_lines or not all(STAMP.match(l) for l in tee_lines):
        problems.append("a tee line has no ISO-8601 UTC timestamp")
    if [STAMP.sub("", l) for l in tee_lines] != out:
        problems.append("the tee copy differs from stdout")

    marker = os.path.join(tmp, "last-refresh.json")
    try:
        data = json.load(open(marker))
        if data["last_success"]["line"] != [l for l in FEED if l.startswith("Installed")][-1]:
            problems.append("last-refresh.json names the wrong line: %r" % data["last_success"])
        if "error connecting" not in data["last_failure"]["line"]:
            problems.append("last-refresh.json did not record the failure")
    except Exception as exc:
        problems.append("last-refresh.json missing or invalid: %s" % exc)
    if [f for f in os.listdir(tmp) if f.endswith(".tmp")]:
        problems.append("a temporary marker file was left behind")

    # A failed request must not be recorded as a refresh.
    tmp2 = tempfile.mkdtemp(prefix="check_log_filter.")
    run(source(), ["Installed profile: a (U)", "Failed to install provisioning profile: b (V). "
                   "Error code: -402620383", "Failed to handle request:x",
                   "Finished handling request!"], ["--tee", os.path.join(tmp2, "l")], env)
    if "last_success" in json.load(open(os.path.join(tmp2, "last-refresh.json"))):
        problems.append("a refresh that failed was recorded as successful")

    # An anisette request also ends in "Finished handling request!"; it is not a refresh. An app
    # install (daemon) and a CLI install are.
    for name, feed, want in (
            ("anisette", ["Received response status code: 200", "Finished handling request!"], None),
            ("app install", ["Unzipping .ipa...", "Writing to device...",
                             "Finished writing to device.", "Finished handling request!"],
             "Finished writing to device."),
            ("cli install", ["Installing app...", "Notify: Installation Succeeded",
                             "    AltStore was successfully installed on iPhone.", "Finished!"],
             "Notify: Installation Succeeded")):
        d = tempfile.mkdtemp(prefix="check_log_filter.")
        run(source(), feed, ["--tee", os.path.join(d, "l")], env)
        m = os.path.join(d, "last-refresh.json")
        got = json.load(open(m))["last_success"]["line"] if os.path.exists(m) else None
        if got != want:
            problems.append("%s: last-refresh.json recorded %r, expected %r" % (name, got, want))

    # Fail open: without installer.py every line still comes through.
    r = run(source(web=os.path.join(tmp, "nowhere")), FEED, [], env)
    if not is_subsequence([l for l in FEED if not PROGRESS.fullmatch(l)], r.stdout.splitlines()):
        problems.append("without installer.py, lines were lost")

    # A crash mid-stream must not lose what was already read.
    boom = ("_h = Filter.handle\n"
            "def _boom(self, line, _n=[0]):\n"
            "    _n[0] += 1\n"
            "    if _n[0] == 3:\n"
            "        raise RuntimeError('injected')\n"
            "    return _h(self, line)\n"
            "Filter.handle = _boom\n")
    feed = ["line %d" % i for i in range(1, 11)]
    r = run(source(inject=boom), feed, [], env)
    if r.stdout.splitlines() != feed:
        problems.append("after a crash, output was %r" % r.stdout.splitlines())

    # A folded counter must be shown within ~1s even if the daemon then goes quiet.
    p = subprocess.Popen([sys.executable, "-c", source()], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, env=env)
    p.stdin.write(("\n".join(upload(50)) + "\n").encode())
    p.stdin.flush()
    got, deadline = b"", time.time() + 3
    while time.time() < deadline and b"204800(of 204800)" not in got:
        if select.select([p.stdout], [], [], 0.2)[0]:
            got += os.read(p.stdout.fileno(), 65536)
    p.stdin.close()
    p.wait(timeout=10)
    if b"204800(of 204800)" not in got:
        problems.append("the newest folded counter was held back while the input was idle")

    if problems:
        print("\n".join("FAIL: " + p for p in problems))
        return 1
    print("docker/redact-log.py: %d lines in, %d out; order, errors, marker and timestamps hold."
          % (len(FEED), len(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
