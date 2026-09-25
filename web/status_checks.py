"""Health checks for an AltServer-Linux deployment.

Stdlib only, deliberately: python3 is already a hard runtime requirement of this project (the
-static binary cannot dlopen Bonjour, so it shells out to python3 to do it), which means adding
these checks costs no new dependency.

Every check here corresponds to a failure this project can produce SILENTLY. That is the whole
point -- AltServer cannot report its own health:

  * DNSServiceRegister returned success unconditionally until we fixed it, and even now avahi can
    report success while publishing nothing, so the only trustworthy test is an external browse.
  * A background refresh that finds no server is suppressed by AltStore itself
    (BackgroundRefreshAppsOperation sets ignoresServerNotFoundError = true), so the phone stays
    quiet too.
  * `journalctl -p err` is empty no matter what breaks, because essentially everything is written
    to stdout at info level.

So the first symptom of a broken deployment is an app that will not open, a week later. These
checks exist to turn that into something visible.
"""

import calendar
import concurrent.futures
import ctypes
import email.utils
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

# The exact contract src/AnisetteDataManager.cpp enforces. Casing is inconsistent upstream and
# matched case-sensitively: X-MMe-Client-Info has a capital MM, X-Mme-Device-Id a lowercase m.
ANISETTE_REQUIRED_KEYS = [
    "X-Apple-I-MD-M",
    "X-Apple-I-MD",
    "X-Apple-I-MD-LU",
    "X-Apple-I-MD-RINFO",
    "X-Mme-Device-Id",
    "X-Apple-I-SRL-NO",
    "X-MMe-Client-Info",
    "X-Apple-I-Client-Time",
    "X-Apple-Locale",
    "X-Apple-I-TimeZone",
]

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"


def _in_container():
    return os.path.exists("/.dockerenv")


def _result(name, state, summary, detail=None, fix=None):
    return {"name": name, "state": state, "summary": summary, "detail": detail or "", "fix": fix or ""}


def _run(cmd, timeout=10, env=None):
    """Run a command, returning (rc, stdout+stderr). Never raises."""
    if shutil.which(cmd[0]) is None:
        return None, "%s is not installed" % cmd[0]
    try:
        merged = dict(os.environ, **env) if env else None
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=merged)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return None, "%s timed out after %ss" % (cmd[0], timeout)
    except Exception as exc:  # pragma: no cover - defensive
        return None, "%s could not be run: %s" % (cmd[0], exc)


# How often this page may call the anisette server's "/". That is the one endpoint that runs the
# ADI machinery, logs the full identity at INFO (anisette-v3-server app.d:225), and -- in images
# built from upstream b76cd01 (2026-04-04) on, i.e. the current :latest -- PROVISIONS AGAINST
# APPLE whenever the machine is unprovisioned (app.d:199-203). This page polls every 30 s and a
# watchdog polling /api/status multiplies that; none of it needs a fresh one-time password.
# Liveness is checked on every poll via /v3/client_info instead, which is a static reply
# (app.d:228-240): no ADI, no Apple, no file I/O.
ANISETTE_CONTRACT_INTERVAL = int(os.environ.get("ALTSERVER_ANISETTE_CHECK_INTERVAL", "600"))
_anisette_lock = threading.Lock()
_anisette_cache = {}  # url -> (monotonic expiry, result)

# The machine identity Apple sees is X-Mme-Device-Id (device.json) plus X-Apple-I-MD-M (the ADI
# machine ID in adi.pb). If either changes, the next sign-in is a NEW machine and needs 2FA, which
# an unattended refresh can never answer. Only a fingerprint is stored, and only when first seen.
ANISETTE_IDENTITY_FILE = os.environ.get("ALTSERVER_ANISETTE_IDENTITY_FILE",
                                        "/data/.anisette-identity")


def _identity_note(data):
    """'' if unchanged or not trackable, else a description of the change."""
    fingerprint = hashlib.sha256(("%s|%s" % (data.get("X-Mme-Device-Id"),
                                             data.get("X-Apple-I-MD-M"))).encode()).hexdigest()[:16]
    path = ANISETTE_IDENTITY_FILE
    try:
        with open(path) as fh:
            known = fh.read().split()
    except FileNotFoundError:
        try:
            if path and os.access(os.path.dirname(path) or ".", os.W_OK):
                with open(path, "w") as fh:
                    fh.write("%s %s\n" % (fingerprint,
                                          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        except OSError:
            pass  # untracked is better than a status page that throws
        return ""
    except OSError:
        return ""
    if known and known[0] != fingerprint:
        return ("identity fingerprint %s differs from %s recorded %s"
                % (fingerprint, known[0], known[1] if len(known) > 1 else "earlier"))
    return ""


def check_anisette(url=None):
    """Liveness on every call; the full contract at most every ANISETTE_CONTRACT_INTERVAL s."""
    url = url or os.environ.get("ALTSERVER_ANISETTE_SERVER", "")

    if not url:
        return _result("Anisette server", FAIL, "ALTSERVER_ANISETTE_SERVER is not set",
                       "There is no default; the server that used to be hardcoded is dead.",
                       "Set it to a full URL including the scheme, e.g. http://127.0.0.1:6969")

    if not url.startswith(("http://", "https://")):
        return _result("Anisette server", FAIL, "URL has no http:// or https:// scheme",
                       "Configured as %r." % url,
                       "AltServer's HTTP client constructor rejects a scheme-less URL before "
                       "sending anything. Use e.g. http://127.0.0.1:6969")

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/v3/client_info", timeout=10) as resp:
            resp.read(4096)
    except urllib.error.HTTPError:
        pass  # alive, just no v3 API (a v1-only server); the contract check below still runs
    except Exception as exc:
        return _result("Anisette server", FAIL, "Cannot reach %s" % url, str(exc),
                       "Is the anisette container running? Check `docker ps`.")

    with _anisette_lock:
        cached = _anisette_cache.get(url)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        result = _check_anisette_contract(url)
        result["detail"] = (result["detail"] + " | " if result["detail"] else "") + \
            "\"/\" fetched %s, next fetch in %ds" % (
                time.strftime("%H:%M:%SZ", time.gmtime()), ANISETTE_CONTRACT_INTERVAL)
        _anisette_cache[url] = (time.monotonic() + ANISETTE_CONTRACT_INTERVAL, result)
        return result


def _check_anisette_contract(url):
    """Fetch anisette data and validate it against the client's actual contract."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Xcode"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body = resp.read(65536).decode("utf-8", "replace")
            fetched_at = time.time()
    except urllib.error.HTTPError as exc:
        return _result("Anisette server", FAIL, "HTTP %s from %s" % (exc.code, url),
                       "Server is reachable but unhealthy.",
                       "Check the anisette container's logs.")
    except Exception as exc:
        return _result("Anisette server", FAIL, "Cannot reach %s" % url, str(exc),
                       "Is the anisette container running? Check `docker ps`.")

    if status != 200:
        return _result("Anisette server", FAIL, "HTTP %s" % status, body[:200],
                       "AltServer requires exactly 200.")

    try:
        data = json.loads(body)
    except ValueError:
        return _result("Anisette server", FAIL, "Response is not JSON", body[:200],
                       "Wrong endpoint? Some servers serve the v1 payload only at /.")

    if not isinstance(data, dict):
        return _result("Anisette server", FAIL, "Response is not a JSON object", body[:200])

    missing = [k for k in ANISETTE_REQUIRED_KEYS if k not in data]
    if missing:
        return _result("Anisette server", FAIL, "Missing %d required field(s)" % len(missing),
                       ", ".join(missing),
                       "This server does not speak the legacy v1 flat-JSON contract.")

    # A numeric value here is the one failure the client swallows: X-Apple-I-MD-RINFO is parsed
    # with std::atoi, which returns 0 for a non-string without erroring, and the consequence
    # surfaces much later as an opaque Apple -36607.
    not_strings = [k for k in ANISETTE_REQUIRED_KEYS if not isinstance(data[k], str)]
    if not_strings:
        return _result("Anisette server", FAIL, "Field(s) not sent as JSON strings",
                       ", ".join(not_strings),
                       "AltServer requires every value to be a string. X-Apple-I-MD-RINFO as a "
                       "number is the common variant, and it fails silently.")

    client_info = data.get("X-MMe-Client-Info", "")
    detail = "Device-Id %s" % data.get("X-Mme-Device-Id", "?")
    if "com.apple.dt.Xcode" in client_info:
        # Verified against live Apple infrastructure: with this substring present the first GSA
        # request returns 503; rewritten to com.apple.akd it returns 200.
        detail += " | client-info contains com.apple.dt.Xcode, so the built-in sanitizer is " \
                  "load-bearing (leave ALTSERVER_NO_CLIENTINFO_SANITIZE unset)"

    changed = _identity_note(data)
    if changed:
        return _result("Anisette server", FAIL, "The anisette machine identity CHANGED",
                       detail + " | " + changed,
                       "Apple now sees a new machine: the next sign-in needs 2FA, which an "
                       "unattended refresh cannot answer. Restore the anisette-config backup. If "
                       "the change was intended, delete %s to accept it." % ANISETTE_IDENTITY_FILE)

    ok = _result("Anisette server", OK, "All 10 fields present, all strings, HTTP 200", detail)
    ok["anisette_time"] = data.get("X-Apple-I-Client-Time")
    # Skew as measured WHEN FETCHED: this result is cached, so comparing its timestamp with the
    # clock at some later poll would report the cache age as skew.
    try:
        ok["anisette_skew"] = calendar.timegm(
            time.strptime(ok["anisette_time"][:19], "%Y-%m-%dT%H:%M:%S")) - fetched_at
    except (TypeError, ValueError):
        pass
    return ok


CLOCK_TOLERANCE = 30

# An INDEPENDENT time reference. The anisette timestamp cannot be one: anisette and this page run
# on the same host, and containers have no CLOCK_REALTIME of their own, so the two always agree --
# including when the host clock is hours off, which is the normal state of a Pi 4 (no RTC) after a
# power cut until NTP succeeds. Default: the Debian mirror the host already uses for apt, so no new
# party learns anything. Plain HTTP on purpose: an HTTPS probe would depend on the very thing it
# measures (certificate validity is judged by the local clock). "off" disables it.
CLOCK_REFERENCE_URL = os.environ.get("ALTSERVER_CLOCK_REFERENCE_URL",
                                     "http://deb.debian.org/debian/")
CLOCK_REFERENCE_INTERVAL = 15 * 60  # at most one HEAD per 15 min, however often the page polls
CLOCK_REFERENCE_RETRY = 5 * 60
_reference_lock = threading.Lock()
_reference = {}  # url -> (monotonic expiry, reference wall time, its monotonic anchor, error)


def _reference_offset(url=None):
    """(seconds our clock is BEHIND the reference, None) or (None, why not). Cached.

    The reference instant is anchored to CLOCK_MONOTONIC rather than stored as an offset from our
    wall clock, so an NTP step after the probe shows up at the next poll, not 15 minutes later.
    """
    url = CLOCK_REFERENCE_URL if url is None else url
    if not url or url.lower() == "off":
        return None, "external reference disabled"
    with _reference_lock:
        entry = _reference.get(url)
        if not entry or time.monotonic() >= entry[0]:
            ref_wall = anchor = None
            error = ""
            try:
                req = urllib.request.Request(url, method="HEAD",
                                             headers={"User-Agent": "altserver-status"})
                t0 = time.monotonic()
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        date = resp.headers.get("Date")
                except urllib.error.HTTPError as exc:  # an error status still carries a Date
                    date = exc.headers.get("Date")
                t1 = time.monotonic()
                if not date:
                    raise ValueError("no Date header")
                # Date has 1 s resolution and is truncated: the server's instant was in [D, D+1).
                ref_wall = email.utils.parsedate_to_datetime(date).timestamp() + 0.5
                anchor = (t0 + t1) / 2
            except Exception as exc:
                error = "%s: %s" % (url, exc)
            entry = (time.monotonic() + (CLOCK_REFERENCE_INTERVAL if error == ""
                                         else CLOCK_REFERENCE_RETRY), ref_wall, anchor, error)
            _reference[url] = entry
        _, ref_wall, anchor, error = entry
    if ref_wall is None:
        return None, error
    return ref_wall + (time.monotonic() - anchor) - time.time(), None


class _Timex(ctypes.Structure):
    # Leading fields of struct timex (<sys/timex.h>); ctypes applies the C alignment. The kernel
    # struct is 208 bytes on 64-bit, so the padding is generous.
    _fields_ = [("modes", ctypes.c_uint), ("offset", ctypes.c_long), ("freq", ctypes.c_long),
                ("maxerror", ctypes.c_long), ("esterror", ctypes.c_long),
                ("status", ctypes.c_int), ("_rest", ctypes.c_byte * 512)]


def _kernel_ntp_synced():
    """True/False exactly as `timedatectl show -p NTPSynchronized` decides it, or None.

    systemd-timedated's ntp_synced() is adjtimex() with modes=0 and maxerror < 16 s
    (src/timedate/timedated.c). That reads the HOST kernel's clock-discipline state, needs no
    capability, and Docker's default seccomp profile allows it -- so unlike timedatectl it works in
    this container with no bind mount, no D-Bus and no systemd.
    """
    try:
        tx = _Timex()
        if ctypes.CDLL(None, use_errno=True).adjtimex(ctypes.byref(tx)) < 0:
            return None
        return tx.maxerror < 16000000
    except Exception:
        return None


def check_clock(anisette_time=None, anisette_skew=None):
    """Is the clock that stamps X-Apple-I-Client-Time right?

    Linux forwards the anisette server's X-Apple-I-Client-Time to Apple verbatim (macOS stamps
    Date() locally instead). Three signals, strongest first:

      1. Our clock vs an external reference: the only one that sees a host clock that is simply
         wrong (no RTC + power cut + NTP not yet, or never, synced).
      2. The kernel's NTP state: whether anything is disciplining the clock at all.
      3. Anisette timestamp vs our clock: catches a TZ other than UTC in the anisette container (it
         stamps LOCAL time and appends a literal "Z") or an anisette server on another host. On
         one host it reads ~0 for a merely wrong clock, so it can never clear the host clock.
    """
    if anisette_skew is None and anisette_time:
        try:
            anisette_skew = calendar.timegm(
                time.strptime(anisette_time[:19], "%Y-%m-%dT%H:%M:%S")) - time.time()
        except (TypeError, ValueError):
            pass
    said = "Anisette said %s" % anisette_time if anisette_time else "No anisette timestamp"
    offset, why = _reference_offset()
    synced = _kernel_ntp_synced()
    ntp = {True: "kernel reports NTP-synchronised", False: "kernel reports NOT NTP-synchronised",
           None: "kernel NTP state unavailable"}[synced]
    ref = ("reference %s says our clock is %+.0fs off" % (CLOCK_REFERENCE_URL, offset)
           if offset is not None else "no external reference (%s)" % why)
    detail = " | ".join([said, ref, ntp])

    if offset is not None and abs(offset) > CLOCK_TOLERANCE:
        return _result("Clock agreement", FAIL,
                       "This host's clock is %ds %s real time" % (round(abs(offset)),
                                                                   "behind" if offset > 0
                                                                   else "ahead of"),
                       detail,
                       "Apple sees this clock via the anisette timestamp; skew surfaces as an "
                       "opaque -36607. Check `timedatectl timesync-status` on the host: a Pi has "
                       "no RTC, so after a power cut it runs on the last saved time until NTP "
                       "(UDP 123) succeeds.")
    if anisette_skew is not None and abs(anisette_skew) > CLOCK_TOLERANCE:
        return _result("Clock agreement", FAIL,
                       "Anisette clock is %ds away from ours" % round(abs(anisette_skew)), detail,
                       "The anisette container must run with TZ=UTC (it stamps local time with a "
                       "literal Z); if it runs on another host, fix NTP there too.")
    if synced is False:
        return _result("Clock agreement", WARN, "Clock is NOT NTP-synchronised", detail,
                       "Nothing is disciplining the clock (e.g. just booted after a power cut, or "
                       "NTP is blocked), so it drifts or keeps a stale restored time.")
    if offset is None and synced is None:
        return _result("Clock agreement", UNKNOWN, "No independent clock reference", detail,
                       "Same-host anisette time cannot reveal a wrong host clock.")
    return _result("Clock agreement", OK,
                   "Within %ds of real time" % CLOCK_TOLERANCE if offset is not None
                   else "NTP-synchronised (no external reference)", detail)


# netmuxd's socket. Set by the stack; the default matches deploy/altserver-stack.yml.
NETMUXD_SOCKET = os.environ.get("ALTSERVER_NETMUXD_SOCKET", "/run/muxd/usbmuxd")

# libusbmuxd's env var. Note the spelling -- USBMUXD_SOCKET_ADRESS, with one D, is a widely-copied
# typo that is silently ignored. Verified at upstream_repo/libusbmuxd/src/libusbmuxd.c:158.
_WIRELESS_ENV = {"USBMUXD_SOCKET_ADDRESS": "UNIX:" + NETMUXD_SOCKET}
# Empty string makes libusbmuxd fall back to its compiled-in default, /var/run/usbmuxd.
_USB_ENV = {"USBMUXD_SOCKET_ADDRESS": ""}


def _devices_via(env, flag):
    """(udids, note) over one transport. Distinguishes 'no devices' from 'no mux listening'.

    `flag` is NOT optional and must match the transport. idevice_id's -l and -n are not
    verbosity switches, they select which transports are enumerated:

        -l  include_usb = 1        (tools/idevice_id.c: case 'l')
        -n  include_network = 1    (case 'n')
        neither, with no other args: both

    netmuxd only ever presents the phone as ConnectionType: Network, so `idevice_id -l` against
    netmuxd's socket returns an empty list NO MATTER WHAT -- the device is there, it is simply not
    a USB device. This check used -l for the wireless probe and so could never report OK for a
    wireless-only setup, which is the exact configuration it exists to verify. It read "No device
    on either transport" while refresh was demonstrably working.
    """
    rc, out = _run(["idevice_id", flag], env=env)
    if rc is None:
        return None, out
    # This exact string means libusbmuxd could not reach the socket AT ALL -- a dead or absent
    # mux. An empty device list is a silent success with no output, which is a completely
    # different condition and must not be conflated with it.
    if "Unable to retrieve device list" in out:
        return None, "no mux is listening on that socket"
    return [l.strip() for l in out.splitlines() if l.strip()], ""


def check_device():
    """Is the phone reachable, and -- the part that decides unattended refresh -- over WHICH path?

    Wireless is not a nicety here. Stock usbmuxd enumerates USB only, and on Ubuntu its unit is
    udev-activated: it exits when the last cable is unplugged. So a server with no cable has no mux
    at all unless netmuxd is running, and every refresh fails with what looks like a device fault.
    """
    # -n for netmuxd (network transport), -l for the host's usbmuxd (USB transport).
    wireless, wnote = _devices_via(_WIRELESS_ENV, "-n")
    usb, unote = _devices_via(_USB_ENV, "-l")

    if wireless:
        # -n is required here for the same reason as above: idevicepair.c:372 selects
        # IDEVICE_LOOKUP_USBMUX unless it is passed, so without it this validates a USB device
        # that does not exist on a cable-free server and reports a stale pairing record.
        rc, out = _run(["idevicepair", "-n", "validate"], env=_WIRELESS_ENV)
        if rc == 0:
            return _result("iPhone reachability", OK, "Reachable over Wi-Fi, pairing valid",
                           "UDID %s via netmuxd%s" % (wireless[0], ", also on USB" if usb else ""))
        if "passcode" in out.lower():
            return _result("iPhone reachability", WARN, "Found over Wi-Fi, but the device is locked",
                           out.strip(), "Unlock the phone and re-check.")
        return _result("iPhone reachability", FAIL, "Found over Wi-Fi, but pairing did not validate",
                       out.strip(),
                       "The pairing record in /var/lib/lockdown is stale or half-written. Re-pair "
                       "over USB and tap Trust; back up BOTH files together.")

    if usb:
        return _result(
            "iPhone reachability", WARN, "Reachable over USB ONLY -- wireless refresh will not work",
            "UDID %s. netmuxd: %s" % (usb[0], wnote or "running, but reports no device"),
            "Unattended refresh needs the phone reachable with no cable. Check the netmuxd "
            "container is up, that the phone is on this LAN, and that it advertises itself "
            "(see the next check).")

    # Tools absent entirely is not the same as "no device" -- saying FAIL there would be a
    # confident false negative on a host that simply lacks libimobiledevice.
    if "not installed" in (wnote or "") and "not installed" in (unote or ""):
        return _result("iPhone reachability", UNKNOWN, "idevice_id is not available here", wnote,
                       "Install libimobiledevice-utils. The container image ships it.")

    return _result(
        "iPhone reachability", FAIL, "No device on either transport",
        "netmuxd: %s | usbmuxd: %s" % (wnote or "no device", unote or "no device"),
        "If both say no mux is listening, nothing is serving device access at all. The host's "
        "usbmuxd is udev-activated and exits with the cable removed, which is normal -- that is "
        "what the netmuxd container is for.")


def check_phone_advertisement():
    """Is the phone itself discoverable? netmuxd finds it by mDNS, so this is its precondition."""
    rc, out = _run(["avahi-browse", "-rpt", "_apple-mobdev2._tcp"], timeout=15)
    if rc is None:
        return _result("iPhone is advertising", UNKNOWN, "avahi-browse not available", out)

    rows = [l.split(";") for l in out.splitlines() if l.startswith("=")]
    rows = [r for r in rows if len(r) > 8]
    if not rows:
        return _result(
            "iPhone is advertising", FAIL, "The phone is not advertising _apple-mobdev2._tcp",
            "netmuxd discovers the device this way, so it cannot find it.",
            "The phone must be awake, on this Wi-Fi, and have been paired over USB at least once. "
            "This advert is how a device offers itself for wireless access.")

    seen = sorted({"%s:%s" % (r[7], r[8]) for r in rows})
    return _result("iPhone is advertising", OK, "Discoverable over mDNS", ", ".join(seen))


def check_advertisement(service="_altserver._tcp"):
    """The only trustworthy advertisement test: browse for it, do not trust the server."""
    rc, out = _run(["avahi-browse", "-rpt", service], timeout=15)
    if rc is None:
        return _result("mDNS advertisement", UNKNOWN, "avahi-browse not available", out,
                       "Install avahi-utils. This is the ONLY reliable check: AltServer cannot "
                       "detect its own advertisement failing, and avahi can report success "
                       "while publishing nothing.")
    if any(line.startswith("=") for line in out.splitlines()):
        hosts = [l.split(";")[6] for l in out.splitlines()
                 if l.startswith("=") and len(l.split(";")) > 6]
        return _result("mDNS advertisement", OK, "%s is published" % service,
                       "Seen on: %s" % ", ".join(sorted(set(hosts))) if hosts else "")
    return _result("mDNS advertisement", FAIL, "%s is NOT published" % service,
                   "Nothing is advertising it, so AltStore cannot discover this server.",
                   "Check python3 and libavahi-compat-libdnssd-DEV (not -libdnssd1: the code "
                   "dlopens the unversioned libdns_sd.so) and that avahi-daemon is running.")


def check_altserver_running():
    """Is the daemon up? Only meaningful if we can actually see its process.

    Each container has its own PID namespace, so from a sidecar this sees nothing no matter how
    healthy the daemon is. Rather than report a confident false negative, say so -- and point at
    the mDNS check, which is the trustworthy signal either way.
    """
    rc, out = _run(["pgrep", "-af", "AltServer"])
    if rc is None:
        return _result("AltServer process", UNKNOWN, "Could not check", out)

    lines = [l for l in out.splitlines() if "AltServer" in l and "pgrep" not in l
             and "server.py" not in l]
    if lines:
        return _result("AltServer process", OK, "Running", lines[0][:160])

    if _in_container() and not os.path.exists("/proc/1/root/usr/local/bin/AltServer"):
        return _result(
            "AltServer process", UNKNOWN,
            "Cannot see other containers' processes from here",
            "Containers have separate PID namespaces, so this check is blind unless the service "
            "runs with pid: host.",
            "Judge by the mDNS check above -- if _altserver._tcp is published, something is "
            "advertising it and the daemon is alive.")

    return _result("AltServer process", FAIL, "Not running",
                   "Nothing to discover, and no refreshes will happen.",
                   "AltServer has no liveness signal: Listen() can fail early and the process "
                   "stays alive with no listener, so 'running' is necessary but not sufficient. "
                   "Trust the mDNS check above over this one.")


# Written by docker/redact-log.py when a refresh installs profiles and finishes without error.
LAST_REFRESH = os.environ.get("ALTSERVER_LAST_REFRESH", "/data/last-refresh.json")
REFRESH_WARN_DAYS = 4
REFRESH_FAIL_DAYS = 6


def _ago(seconds):
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    if seconds < 2 * 86400:
        return "%.1f hours" % (seconds / 3600)
    return "%.1f days" % (seconds / 86400)


def check_last_refresh(path=None, now=None):
    """The one health fact a 7-day certificate actually depends on: when did a refresh last land?

    Every other check here says whether a refresh COULD work. None says whether one DID, and the
    log cannot either: one app upload pushes everything else out of it. A free Apple ID's profiles
    expire 7 days after they were installed, so WARN after 4 days (a 3-day cycle has been missed)
    and FAIL after 6 (one day left).

    Only refreshes that pass through the daemon's log filter are recorded: AltStore-initiated
    refreshes and installs. A CLI install whose output is not piped through redact-log is not.
    """
    path = path or LAST_REFRESH
    now = time.time() if now is None else now
    name = "Last successful refresh"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        last = data.get("last_success") or {}
        failure = data.get("last_failure") or {}
    except FileNotFoundError:
        return _result(name, UNKNOWN, "No refresh recorded yet", "Nothing at %s." % path,
                       "It is written the first time AltServer installs profiles on the phone "
                       "(refresh from AltStore once). Needs an image whose redact-log records it.")
    except Exception as exc:
        return _result(name, UNKNOWN, "Could not read %s" % path, str(exc))

    epoch = last.get("epoch") if isinstance(last.get("epoch"), (int, float)) else None
    detail = ""
    if isinstance(failure.get("epoch"), (int, float)) and failure["epoch"] > (epoch or 0):
        detail = "Newer failure %s ago: %s" % (_ago(max(0, now - failure["epoch"])),
                                                failure.get("line", ""))
    if epoch is None:
        return _result(name, UNKNOWN, "No successful refresh recorded yet", detail)

    age = now - epoch
    detail = ("%s (%s)" % (last.get("line", ""), last.get("time", ""))
              + (" | " + detail if detail else ""))
    if age < -300:
        return _result(name, WARN, "Recorded refresh time is in the future", detail,
                       "This host's clock has jumped backwards. Check NTP.")
    summary = "Last successful refresh: %s ago" % _ago(max(0, age))
    if age > REFRESH_FAIL_DAYS * 86400:
        return _result(name, FAIL, summary, detail,
                       "Profiles from a free Apple ID expire 7 days after install. Refresh from "
                       "AltStore now (on the home Wi-Fi or the VPN) and check the log for why "
                       "the scheduled refreshes did not land.")
    if age > REFRESH_WARN_DAYS * 86400:
        return _result(name, WARN, summary, detail,
                       "At least one refresh cycle has been missed. Refresh from AltStore before "
                       "day 7, when the apps stop opening.")
    return _result(name, OK, summary, detail)


def run_all(anisette_url=None):
    """Run every check, in parallel apart from the one real dependency.

    These were serial, which made the page as slow as the SUM of its checks. Two of them shell out
    to avahi-browse with a 15s timeout, so when an AppArmor rule started denying avahi's D-Bus
    signals the dashboard took over half a minute to render anything -- the checks were reporting
    a problem correctly and the page was unusable while they did it.

    They are subprocess and HTTP calls, so threads are the right tool: the page is now as slow as
    its SLOWEST check, not their total. check_clock is the one genuine dependency, needing the
    timestamp check_anisette collected, so anisette runs first and the rest run together.

    A check that raises must not take the dashboard with it -- that is what the whole page exists
    to avoid -- so each result is collected defensively.
    """
    anisette = check_anisette(anisette_url)

    def _clock():
        return check_clock(anisette.get("anisette_time"), anisette.get("anisette_skew"))

    rest = [_clock, check_device, check_phone_advertisement,
            check_advertisement, check_altserver_running, check_last_refresh]

    results = [None] * len(rest)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(rest)) as pool:
        futures = {pool.submit(fn): i for i, fn in enumerate(rest)}
        for fut in concurrent.futures.as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as exc:  # a broken check must not blank the page
                results[i] = _result("Check #%d" % (i + 1), UNKNOWN,
                                     "This check raised an exception", str(exc))

    checks = [anisette] + results
    states = [c["state"] for c in checks]
    if FAIL in states:
        overall = FAIL
    elif WARN in states or UNKNOWN in states:
        overall = WARN
    else:
        overall = OK
    return {"overall": overall, "checks": checks, "host": socket.gethostname()}


if __name__ == "__main__":
    print(json.dumps(run_all(), indent=2))
