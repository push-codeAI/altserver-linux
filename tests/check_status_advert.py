#!/usr/bin/env python3
"""Guards for the status page's view of the phone's own Bonjour advert (web/status_checks.py).

WHY THIS EXISTS. netmuxd finds the phone by its _apple-mobdev2._tcp advert, and iOS 26.4 changed
what that advert carries: the UDID is now matched through TXT keys (identifier + authTag, HMAC'd
with the pairing record's HostID) instead of a MAC in the instance name. If a later iOS changes it
again, netmuxd simply never lists the phone -- and logs that only at debug level -- while the
status page said "Discoverable over mDNS" and pointed at the network. So:

  * the advert check shows the TXT keys, and WARNs when neither known form is present;
  * when the phone advertises but netmuxd lists nothing, the reachability check says that this is
    the netmuxd <-> iOS mismatch, not a Wi-Fi problem.

avahi-browse output is canned here; nothing touches the network. Stdlib only:

    python3 tests/check_status_advert.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "web"))
import status_checks as sc  # noqa: E402

failures = []

# Parsable avahi-browse rows: = ; iface ; proto ; name ; type ; domain ; host ; address ; port ; txt
NEW = ('=;eth0;IPv4;7A1B2C3D4E5F;_apple-mobdev2._tcp;local;iPhone.local;192.168.1.50;32498;'
       '"identifier=q0FmWm9Q" "authTag=Zm9vYmFy" "rpBA=00:11:22"\n')
LEGACY = ('=;eth0;IPv4;a4:c3:37:00:11:22@fe80::1;_apple-mobdev2._tcp;local;iPhone.local;'
          '192.168.1.50;32498;\n')
UNKNOWN_FORM = ('=;eth0;IPv4;7A1B2C3D4E5F;_apple-mobdev2._tcp;local;iPhone.local;192.168.1.50;'
                '32498;"deviceKey=AAAA" "sig=BBBB"\n')


CHECK_PHONE_ADVERTISEMENT = sc.check_phone_advertisement


def advert(output):
    real = sc._run
    sc._run = lambda cmd, timeout=10, env=None: (0, output)
    try:
        return CHECK_PHONE_ADVERTISEMENT()
    finally:
        sc._run = real


def expect(what, cond, got):
    if cond:
        print("ok   %-48s %s" % (what, got))
    else:
        failures.append("%s: %r" % (what, got))


def main():
    r = advert(NEW)
    expect("iOS 26.4+ TXT form -> OK, keys shown",
           r["state"] == sc.OK and "authTag, identifier" in r["detail"], r["detail"])
    r = advert(LEGACY)
    expect("MAC@ instance name (older iOS) -> OK", r["state"] == sc.OK, r["summary"])
    r = advert(UNKNOWN_FORM)
    expect("neither form -> WARN", r["state"] == sc.WARN and "deviceKey" in r["detail"], r["summary"])
    r = advert("")
    expect("no advert -> FAIL", r["state"] == sc.FAIL, r["summary"])

    # run_all: the drift hint lands on the reachability check, found by name, not by position.
    saved = {n: getattr(sc, n) for n in ("check_anisette", "check_clock", "check_device",
                                          "check_phone_advertisement", "check_advertisement",
                                          "check_altserver_running", "check_last_refresh")}
    ok = lambda name: (lambda *a, **k: sc._result(name, sc.OK, "fine"))  # noqa: E731
    try:
        sc.check_anisette = lambda url=None: sc._result("Anisette", sc.OK, "fine")
        for n in ("check_clock", "check_advertisement", "check_altserver_running",
                  "check_last_refresh"):
            setattr(sc, n, ok(n))
        sc.check_phone_advertisement = lambda: advert(NEW)
        sc.check_device = lambda deep=False: sc._result(
            "iPhone reachability", sc.FAIL, "No device on either transport", "netmuxd: no device")
        dev = [c for c in sc.run_all()["checks"] if c["name"] == "iPhone reachability"][0]
        expect("advertising + no device -> netmuxd hint", "netmuxd lists nothing" in dev["detail"],
               dev["detail"][:60] + "...")

        sc.check_device = lambda deep=False: sc._result(
            "iPhone reachability", sc.FAIL, "Found over Wi-Fi, but pairing did not validate", "x")
        dev = [c for c in sc.run_all()["checks"] if c["name"] == "iPhone reachability"][0]
        expect("found over Wi-Fi -> no netmuxd hint", "netmuxd lists nothing" not in dev["detail"],
               dev["detail"])
    finally:
        for n, f in saved.items():
            setattr(sc, n, f)

    if failures:
        print("\n".join("FAIL: " + f for f in failures))
        return 1
    print("the advert check shows the TXT form, and a netmuxd/iOS mismatch is named as such.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
