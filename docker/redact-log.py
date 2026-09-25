#!/usr/bin/env python3
"""Filter AltServer's stdout so credentials never reach `docker logs`.

WHY. A successful sign-in prints Apple's full account record and every bearer token -- including
com.apple.gs.icloud.auth with a 31536000-second lifetime -- and every refresh prints the anisette
machine identity: MachineID, the one-time password, the local user ID. All of it goes to stdout,
and Docker's json-file driver writes it to disk on the host, where it accumulates for the life of
the deployment. web/installer.py has always redacted what reaches the BROWSER; nothing redacted
what reaches the log, which is the copy that persists.

ONE IMPLEMENTATION, NOT TWO. The redaction comes from installer.py rather than being reimplemented
here. Two copies of a security filter drift, and the one nobody is looking at is the one that
rots. tests/check_redaction.py covers that function, so it covers this too.

FAIL OPEN, ALWAYS. This sits in the daemon's output path. A filter that crashes and takes the logs
with it is worse than no filter -- you would lose the only diagnostic this project has, and the
"silent failure" trap the rest of the codebase exists to avoid. Every failure mode here degrades
to passing the line through unchanged:

  * installer.py missing or unimportable -> passthrough for every line
  * a line that makes _redact raise       -> that line passes through
  * anything unexpected at the top level  -> passthrough for the rest of the stream

The cost of failing open is a credential in a log you already had. The cost of failing closed is a
server you cannot debug.

PROGRESS COUNTERS ARE COLLAPSED. WirelessConnection prints "Checking socket: N" and "Received
bytes: X(of Y)" for every <=4 KB it receives: one 100 MB app upload measured 52,871 lines / 1.5 MB,
5.2 MB once the json-file driver has wrapped each line, and it pushed everything else out of the
1 MB tee file. The peer-close busy loop prints the same pair ~64,000 times a second. A run of
_PROGRESS lines is reduced to its first line, then at most one line per second (one per minute
while the counter is not moving), then its last line, each annotated with how many were folded
in. Only lines matching _PROGRESS exactly are ever folded -- digits only, so an error cannot match,
and a negative "Sent Bytes Count" (the only trace of a failed send()) always passes verbatim.
ALTSERVER_LOG_COLLAPSE=0 turns this off.

THE TEE COPY IS TIMESTAMPED (UTC). stdout is not: docker already stamps every line
(`docker logs -t`), and so does journald for a bare-metal unit.

A SUCCESSFUL REFRESH IS RECORDED in last-refresh.json next to the tee file (override with
ALTSERVER_LAST_REFRESH). The profiles a free Apple ID installs expire after 7 days, and "when did
the last refresh actually land" is the one fact the rolling log cannot keep. The status page
reads it.
"""

import json
import os
import re
import select
import sys
import tempfile
import time

sys.path.insert(0, "/opt/altserver-web")

# Optional second destination, for the web UI's live log view. Passed as --tee <path>; the file
# lives on the volume altserver and altserver-web share, so the UI needs neither the Docker socket
# nor a host bind mount to read it. Everything written here has already been through _redact.
TEE_PATH = None
if "--tee" in sys.argv:
    i = sys.argv.index("--tee")
    if i + 1 < len(sys.argv):
        TEE_PATH = sys.argv[i + 1]

# Bound it. This file is written for the life of the deployment and nothing rotates it; without a
# cap it would do exactly what the unbounded docker logs did before.
TEE_MAX = 1_000_000
TEE_KEEP = 500_000

MARKER_PATH = os.environ.get("ALTSERVER_LAST_REFRESH") or (
    os.path.join(os.path.dirname(os.path.abspath(TEE_PATH)), "last-refresh.json")
    if TEE_PATH else None)

# The per-chunk counters from WirelessConnection::SendData/ReceiveData and
# ClientConnection::SendResponse, and nothing else.
_PROGRESS = re.compile(r"Checking socket: \d+|Received bytes: \d+\(of \d+\)|"
                       r"Sent Bytes Count: \d+ \(\d+\)|Sent Data: \d+ Bytes|Represented Value: \d+")
COLLAPSE = os.environ.get("ALTSERVER_LOG_COLLAPSE", "1") != "0"
COLLAPSE_EVERY = 1.0
COLLAPSE_STALLED = 60.0

# The request identifier is never logged, so a refresh is recognised by what it prints. Evidence:
# "Installed profile:" (DeviceManager.cpp, only after misagent accepted it -- an AltStore refresh)
# or "Finished writing to device." (an app install, whose embedded profile iOS installs). It
# counts once the request finishes cleanly; a CLI install's own success line counts by itself.
_EVIDENCE = ("Installed profile: ", "Finished writing to device.")
_DONE_OK = "Finished handling request!"
_CLI_OK = "Notify: Installation Succeeded"
_DONE_FAILED = ("Failed to handle request:", "Failed to install provisioning profile:", "Alert: ")

try:
    from installer import _redact
except Exception as exc:  # noqa: BLE001 -- any import failure must degrade, not abort
    sys.stderr.write("redact-log: installer.py unavailable (%s); logging unfiltered\n" % exc)
    _redact = None


def _iso(now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _open_tee():
    if not TEE_PATH:
        return None
    try:
        return open(TEE_PATH, "ab")
    except Exception as exc:
        sys.stderr.write("redact-log: cannot write %s (%s); stdout only\n" % (TEE_PATH, exc))
        return None


def _trim(tee):
    """Keep the tail when the file grows past the cap. Returns a fresh handle, or the old one."""
    try:
        if tee.tell() < TEE_MAX:
            return tee
        tee.close()
        with open(TEE_PATH, "rb") as f:
            f.seek(max(0, os.path.getsize(TEE_PATH) - TEE_KEEP))
            f.readline()          # drop the partial line the seek landed in
            tail = f.read()
        with open(TEE_PATH, "wb") as f:
            f.write(tail)
        return open(TEE_PATH, "ab")
    except Exception:
        try:
            return open(TEE_PATH, "ab")
        except Exception:
            return None           # tee is best-effort; stdout must keep working


class Collapser:
    """Folds runs of _PROGRESS lines. Holds at most one line; never reorders anything."""

    def __init__(self, show):
        self.show = show          # show(text, now): write one line to every destination
        self.in_run = False
        self.held = None          # newest folded line not yet shown
        self.best = None          # newest folded line that is not a bare "Checking socket"
        self.folded = 0
        self.shown_at = 0.0
        self.last = None          # the progress line last shown, without annotation
        self.same_since = 0.0

    def deadline(self):
        if self.held is None:
            return None
        rep = self.best or self.held
        return self.shown_at + (COLLAPSE_STALLED if rep == self.last else COLLAPSE_EVERY)

    def feed(self, line, now):
        """Consume a progress line and return True, or flush and return False for any other."""
        if not _PROGRESS.fullmatch(line):
            self.flush(now)
            self.in_run = False
            return False
        if not self.in_run:
            self.in_run = True
            self._show(line, now, 0)
            return True
        self.held = line
        if not line.startswith("Checking socket"):
            self.best = line
        self.folded += 1
        if now >= self.deadline():
            self.flush(now)
        return True

    def flush(self, now):
        if self.held is None:
            return
        rep = self.best or self.held
        n = self.folded - 1
        self.held = self.best = None
        self.folded = 0
        self._show(rep, now, n)

    def _show(self, line, now, n):
        if line != self.last:
            self.same_since = now
        text = line
        if n > 0:
            text += "  [+%d progress lines folded" % n
            if line == self.last:
                text += "; unchanged for %ds" % (now - self.same_since)
            text += "]"
        self.shown_at, self.last = now, line
        self.show(text, now)


class RefreshMarker:
    """Records the last refresh that landed on the phone, atomically, as JSON."""

    def __init__(self, path):
        self.path = path
        self.pending = []
        self.failure_written = 0.0

    def feed(self, line, now):
        if not self.path:
            return
        if line.startswith(_EVIDENCE):
            self.pending = (self.pending + [line])[-8:]
        elif (line.startswith(_DONE_OK) and self.pending) or line.startswith(_CLI_OK):
            evidence = self.pending or [line]
            self._save("last_success", {"time": _iso(now), "epoch": int(now),
                                        "line": evidence[-1], "evidence": evidence,
                                        "confirmed_by": line})
            self.pending = []
        elif line.startswith(_DONE_FAILED):
            self.pending = []
            if now - self.failure_written >= 60:   # a failure storm must not become a write storm
                self.failure_written = now
                self._save("last_failure", {"time": _iso(now), "epoch": int(now), "line": line})

    def _save(self, key, value):
        tmp = None
        try:
            # Re-read rather than cache: a CLI install piped through this filter writes it too.
            try:
                with open(self.path, encoding="utf-8") as f:
                    state = json.load(f)
                if not isinstance(state, dict):
                    state = {}
            except Exception:
                state = {}
            state[key] = value
            fd, tmp = tempfile.mkstemp(prefix=".last-refresh.", suffix=".tmp",
                                       dir=os.path.dirname(self.path) or ".")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=1, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
            tmp = None
        except Exception as exc:
            sys.stderr.write("redact-log: could not record %s in %s (%s)\n" % (key, self.path, exc))
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


class Filter:
    def __init__(self):
        self.out = sys.stdout.buffer
        self.tee = _open_tee()
        self.n = 0
        self.buf = b""            # read but not yet split into lines
        self.todo = []            # split lines; todo[pos:] not yet handled
        self.pos = 0
        self.collapser = Collapser(self.show)
        self.marker = RefreshMarker(MARKER_PATH)

    def show(self, text, now):
        data = (text + "\n").encode("utf-8", "surrogateescape")
        self.out.write(data)
        self.out.flush()  # unbuffered: `docker logs -f` must stay live

        if self.tee is not None:
            try:
                self.tee.write(_iso(now).encode("ascii") + b" " + data)
                self.tee.flush()
                self.n += 1
                if self.n % 200 == 0:
                    self.tee = _trim(self.tee)
            except Exception:
                self.tee = None  # never let the tee break the primary output path

    def handle(self, line):
        now = time.time()
        if COLLAPSE:
            try:
                if self.collapser.feed(line, now):
                    return
            except Exception:
                pass  # folding is an optimisation; show the line instead
        if _redact is None:
            shown = line
        else:
            try:
                shown = _redact(line)
            except Exception:
                shown = line  # never drop a line because the filter tripped over it
            if shown is None:
                return  # _redact drops pure noise, e.g. the SRP "Byte:-42" spew
        self.show(shown, now)
        try:
            self.marker.feed(shown, now)
        except Exception:
            pass

    def run(self):
        fd = sys.stdin.fileno()
        while True:
            deadline = self.collapser.deadline()
            if deadline is not None:
                # A folded line is waiting: show it on time even if the daemon goes quiet, so a
                # stalled transfer's last counter is visible while it is stalled.
                ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.time()))
                if not ready:
                    self.collapser.flush(time.time())
                    continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            self.buf += chunk
            if b"\n" not in chunk:
                continue
            *self.todo, self.buf = self.buf.split(b"\n")
            self._drain()
        if self.buf:
            self.todo, self.buf = [self.buf], b""
            self._drain()
        self.collapser.flush(time.time())

    def _drain(self):
        self.pos = 0
        while self.pos < len(self.todo):
            self.handle(self.todo[self.pos].decode("utf-8", "surrogateescape"))
            self.pos += 1
        self.todo = []

    def rescue(self):
        """After a crash: write whatever was read but not shown, then copy the rest raw."""
        held = self.collapser.best or self.collapser.held
        pending = [held.encode("utf-8", "surrogateescape")] if held else []
        pending += self.todo[self.pos:]
        data = b"".join(p + b"\n" for p in pending) + self.buf
        fd = sys.stdin.fileno()
        while True:
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            data = os.read(fd, 65536)
            if not data:
                return


if __name__ == "__main__":
    filt = None
    try:
        filt = Filter()
        filt.run()
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("redact-log: filter died (%s); remaining output is unfiltered\n" % exc)
        if filt is not None:
            filt.rescue()
        else:
            for line in sys.stdin:
                sys.stdout.write(line)
                sys.stdout.flush()
