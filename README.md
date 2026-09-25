# AltServer-Linux

**Keep sideloaded iOS apps working, from a Linux box, with no Mac or PC involved.**

## What this is

Apps installed outside the App Store — through [AltStore](https://altstore.io) — are signed with
a free Apple developer certificate that **expires after 7 days**. Something has to re-sign them
before then, or they stop opening. Normally that something is AltServer on a Mac or Windows PC,
which has to be awake and on the same Wi-Fi when the deadline comes round.

This runs that job on a Linux machine instead — a home server, a NAS, a Raspberry Pi, a VM. Set it
up once and your sideloaded apps keep refreshing by themselves, over Wi-Fi, with nothing else
switched on.

### What you need

| | |
|---|---|
| A Linux host with Docker | Written against Ubuntu 24.04; nothing is Ubuntu-specific beyond the `apt` lines |
| An iPhone or iPad | On the same network as the host |
| A USB cable | **Once**, for the first pairing. Never needed again after that |
| An Apple ID | Ideally a secondary one — see the warning in [Setup](#setup) |
| ~30 minutes | Most of it waiting on downloads |

A free Apple ID caps you at **3 sideloaded apps**, 10 new app IDs per week, and the 7-day
certificate. A paid developer account raises those limits but is not required.

### What it will not do

- **AltJIT on iOS 17+.** Needs a personalised DDI, TSS signing and a RemoteXPC tunnel. Use
  [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) instead.
- **Pair without a cable.** Wireless pairing is an Apple-TV-only feature and is not available here.
- **Refresh while your phone is off the network.** It needs to reach the device.

### Where to go next

| You want to | Go to |
|---|---|
| Install it | **[Setup](#setup)** — seven steps, start to finish |
| Understand the design | [How the build works](#how-the-build-works) and [docs/REVIVAL.md](docs/REVIVAL.md) |
| Run the binary without Docker | [Reference](#reference) |
| Deploy with Portainer / compose | [deploy/](deploy/) |

---

## Fork status

> **This is a fork** of [NyaMisty/AltServer-Linux](https://github.com/NyaMisty/AltServer-Linux),
> whose last real code commit predates 2025 and whose CI had been failing on every run. The table
> below is what this fork changed — useful if you arrived from upstream or an issue thread, and
> safe to skip if you just want it running.

| | |
|---|---|
| CI | **Fixed.** Was dying at "Set up job" on every run; no binaries since 2025-03 |
| Apple sign-in | **Working**, including 2FA, team lookup, device registration and certificate issuance |
| Apple's 2026 GSA client-info block | **Fixed** and confirmed in both directions against live Apple infrastructure |
| GrandSlam `429` on connection reuse | **Fixed**; proven with a zero-credential probe |
| corecrypto build (#111) | **Fixed.** The buildenv image is rebuildable from source again |
| iOS 26 launch crash (#131) | Fixed in code; AltStore **installs and launches** |
| Wireless refresh | **Working.** The sockaddr-layout bug that broke every Wi-Fi connection is fixed and confirmed on a real device: profiles refresh over Wi-Fi with no cable |

---

## Setup

> **Use a secondary Apple ID if you have one.** Issue #88 documents Apple IDs being *locked* after
> anisette trouble. An app-specific password will **not** work — sideloading needs the real
> password plus a 2FA code. Free accounts cap at 3 sideloaded apps, 10 app IDs/week, 7-day certs.

> **Never run a second signing agent against the same Apple ID** — a Mac or Windows AltServer,
> Sideloadly, or Xcode. Each one revokes the other's certificate, and your apps stop opening.

### 1. Host prerequisites

The container image bundles everything AltServer itself needs. Three things must exist on the
**host**, because the stack reaches out to them:

```bash
sudo apt install -y avahi-daemon avahi-utils usbmuxd libimobiledevice-utils
```

| Host package | Why the stack needs it |
|---|---|
| `avahi-daemon` **running** | It does the actual mDNS publishing; the containers reach it over the bind-mounted system D-Bus socket |
| `usbmuxd` | Owns the USB cable for step 2's one-time pairing, on the host. The stack does **not** mount `/var/run/usbmuxd`: usbmuxd is udev-activated, so after a boot with no cable that path is missing and Docker would create an empty directory there, which stops the host usbmuxd from ever starting again |
| `libimobiledevice-utils` | `idevice_id` / `idevicepair`, used to confirm the pairing worked |

Do **not** `systemctl enable usbmuxd` on Ubuntu — it is udev-activated and has no `[Install]`
section, so `enable` prints a confusing "unit files have no installation config" message. It starts
on its own when an iOS device is plugged in, and exits when the last one is unplugged. That is
normal and is exactly why the stack ships netmuxd for the wireless path.

```bash
systemctl is-active avahi-daemon          # expect: active
```

### 2. Pair the iPhone (USB cable, once)

Do this before deploying anything. Wireless pairing is not possible in this build —
`HAVE_WIRELESS_PAIRING` is undefined, and Apple restricts it to Apple TV.

Plug the iPhone into the host, unlock it, and tap **Trust**. If the host is a VM, pass the USB
device through to it first.

```bash
idevice_id -l                    # prints the UDID -- save it
idevicepair validate             # expect: SUCCESS
```

The pairing record lands in `/var/lib/lockdown/` on the host, which the stack mounts. Back up
**both** files together — they are not independent, and half a pairing is indistinguishable from
none:

```bash
sudo tar czf ~/lockdown-backup.tgz /var/lib/lockdown/
```

> That tarball contains a record named after your phone's UDID. `.gitignore` already refuses
> `lockdown-backup*.tgz`, but keep it out of anything you publish.

### 3. Deploy the stack

Everything else runs as one stack: AltServer, an anisette server, netmuxd for the wireless
transport, and a web UI that drives the install.

**Portainer → Stacks → Add stack → Repository**, pointing at this repo with compose path
`deploy/altserver-stack.yml`. Or with plain compose:

```bash
docker compose -f deploy/altserver-stack.yml up -d
```

**No host preparation beyond step 1.** It uses named volumes, the image is public, and the
AltStore IPA is fetched automatically on start, resolved from AltStore's own catalogue so it is
always current.

**Raspberry Pi / arm64:** the default image is amd64-only and fails to pull with "no matching
manifest for linux/arm64/v8". Set `ALTSERVER_IMAGE` first — see [Download](#download).

Then open **`http://<your-host>:8099`**.

#### Two settings that are load-bearing

- **`security_opt: apparmor=unconfined`** on the `altserver` and `altserver-web` services. Docker's
  default AppArmor profile contains no `dbus` rules at all, and AppArmor denies a mediated class it
  does not mention — so the very first call a Bonjour client makes is refused, mDNS silently fails,
  and the server is invisible to your phone. [`deploy/apparmor/`](deploy/apparmor/) ships a
  narrower profile you can install on the host instead; see its README comments.
- **`network_mode: host`** on netmuxd and the web UI. netmuxd has to see mDNS on UDP 5353.

#### The anisette trap, if you deploy anisette separately

Every copy of the upstream docs says to mount `/home/Alcoholic/.config/anisette-v3/lib/`. That is
**wrong**. `lib/` holds only the two Apple `.so` files downloaded at first run; the machine identity
— `device.json` and the ADI provisioning blob — lives one level **up**. Mount the parent, or you
re-provision on every redeploy and collect 2FA prompts forever.

[`deploy/altserver-stack.yml`](deploy/altserver-stack.yml) already gets this right with a named
volume. The standalone [`deploy/anisette-stack.yml`](deploy/anisette-stack.yml) exists for running
anisette on its own.

#### Verify before going further

```bash
curl -fsS http://127.0.0.1:6969 | jq 'keys'     # ten X-Apple-* keys, all strings
```

The status page at `/` checks this and more. **Survive a redeploy, not just a restart** — `docker
restart` keeps the container's filesystem, so it proves nothing about persistence. Recreate the
container and confirm the identity is still there.

### 4. Install AltStore

Open **`http://<your-host>:8099/install`**, enter your Apple ID and password, and submit. The 2FA
code is prompted for **in the browser**.

That page exists because AltServer reads the 2FA code from `std::cin`, and a detached container has
no terminal. The web UI supervises the process and delivers the code to a read with no tty.

> **The install page takes an Apple ID password over plain HTTP.** On a trusted LAN that is a
> considered trade-off. Anywhere else, change the `altserver-web` command to
> `["--host", "127.0.0.1", "--port", "8099"]` and reach it over an SSH tunnel:
> `ssh -L 8099:127.0.0.1:8099 you@your-host`. Never put it behind a reverse proxy or a tunnel —
> this is LAN-only by design.

Credentials go to AltServer through the **environment**, never a command line, and the install log
shown in the browser is filtered before it is rendered — enforced by
[`tests/check_redaction.py`](tests/check_redaction.py). `docker logs` is **not** filtered and does
contain the full account record and bearer tokens.

#### Reading the output

| What you see | What it means |
|---|---|
| `No anisette server is configured` | `ALTSERVER_ANISETTE_SERVER` unset |
| `ALTSERVER_ANISETTE_SERVER is not a usable URL` | Missing `http://` scheme |
| `Could not reach the anisette server at …` | Container not running, or wrong port |
| `… returned HTTP 502/404. Response body: …` | Anisette up but unhealthy — the body is quoted for you |
| `… did not return a JSON object` | Wrong endpoint; you are getting HTML |
| `… no "X-Apple-I-MD-M" field` | Protocol mismatch — re-check step 3 |
| `-36607` / "Unable to sign you in" | Anisette identity or clock. Check NTP **on the anisette host** — its timestamp is forwarded to Apple verbatim |
| `AltServer could not find the device` | Pairing or usbmuxd, **not** mDNS at this stage |
| `Finished!` | **Not proof of success** — it prints even on failure. Read the lines above it |

### 5. Verify it actually worked

1. AltStore appears on the home screen.
2. **Settings → General → VPN & Device Management** → trust the developer certificate.
3. **Open AltStore.** This is the real test — an app that installs but will not launch is the
   failure mode issue #131 described, and that is fixed in this fork.

### 6. Wireless refresh

**Already running.** netmuxd is part of the stack; there is nothing to install and nothing to
switch over. Confirmed working on iOS 26: profiles refresh over Wi-Fi with no cable attached.

This deliberately does **not** follow the advice in issue #77, and you should not apply that advice
here. netmuxd runs on its **own socket path** in a shared volume:

```yaml
command: ["--socket-path", "/run/muxd/usbmuxd", "--disable-usb", ...]
```

so the host's usbmuxd is left completely alone to handle the cable, and nothing contends for
anything. **Do not stop usbmuxd** — older guidance says to, because netmuxd binds `/var/run/usbmuxd`
by default, but this stack overrides that. AltServer is pointed at netmuxd with
`USBMUXD_SOCKET_ADDRESS` (note the spelling — the widely-copied `USBMUXD_SOCKET_ADRESS`, one D, is
silently ignored).

Requirements on the phone side, both normally already true after step 2:

| Needs to be true | How to check from the server |
|---|---|
| Phone advertises itself | `avahi-browse -rt _apple-mobdev2._tcp` shows it |
| `lockdownd` accepts network connections | port **62078** open on the phone's IP |

Port 62078 is the one that matters. The port in the mDNS TXT record is a different service and
refuses connections — which looks alarming and is not.

AltStore refreshes itself: it sets an hourly background-fetch interval and runs a
`BackgroundRefreshAppsOperation`. iOS grants that at its own discretion, so if an app ever expires
unexpectedly, that is why — not the server.

### 7. Don't lose it

```bash
sudo tar czf ~/lockdown-backup.tgz /var/lib/lockdown/
docker run --rm -v <stack>_anisette-config:/data -v ~:/backup \
  alpine tar czf /backup/anisette-state.tgz /data
```

`adi.pb` is rewritten on every request, so stop the container first if you want a clean copy.
Restoring that tarball is the **only** disaster-recovery path for the anisette identity; without it
a loss means re-provisioning and a fresh 2FA prompt.

Both tarballs contain identifying material and are already in `.gitignore`.

---

## The web UI

Reachable at `http://<your-host>:8099` once the stack is up. Everything in Setup can be driven
from here; the command lines above are for when you want to see what it is doing.

| Page | What it does |
|---|---|
| `/` | Health: anisette contract, clock, pairing, mDNS publication, process |
| `/pairing` | Guided pairing, distinguishing the "nothing shows up" cases |
| `/install` | Apple ID sign-in with 2FA entry in the browser |

The status page also carries a **live log view**. Start it, trigger a refresh from AltStore,
and watch AltServer's own output -- which is the only thing that can actually confirm a
refresh worked, since `idevice_id` and the wireless status row both use Debian's
libimobiledevice rather than the vendored copy AltServer links.

It reads a file on the volume the two containers share, written by the same redaction
filter that protects `docker logs` -- so the web UI needs neither the Docker socket (which
would be root-on-host for a service that takes an Apple ID password over plain HTTP) nor a
host bind mount, and what it shows is already redacted. Polling runs only while watching is
on, and stops itself after five minutes so a forgotten tab does not poll forever.

The status page exists because **this software cannot report its own health.** avahi can report a
successful registration while publishing nothing; AltStore suppresses the one error it would raise
during a background refresh; and nearly everything is logged to stdout at info level, so
`journalctl -p err` stays empty no matter what breaks. Without an external check, the first symptom
of a dead deployment is an app that will not open, a week later.

---

## Before trusting it unattended

- **Watch it from outside.** Nothing inside AltServer reports its own health: no liveness signal,
  no re-registration if avahi restarts, and `journalctl -p err` stays empty no matter what breaks.
  A background refresh that finds no server notifies nobody on either end. The status page at `/`
  exists for this; have something poll it.
- **Two checks that cannot tell you anything.** `docker exec altserver idevice_id -l` and the
  wireless row on the status page both use **Debian's** libimobiledevice from the image's apt
  packages, not the vendored copy AltServer links. They were green throughout a bug that broke
  every wireless refresh. The only evidence that refresh works is AltServer's own log.
- **A misleading error.** Real device faults are *displayed* as "AltServer could not be found",
  because AltStore remaps them for any server that is not `isPreferred` -- and this port hardcodes
  serverID `"1234567"` where Mac and Windows use a UUID, so only an AltStore that THIS server
  installed (it writes that ID into AltStore's Info.plist) treats it as preferred. Otherwise it
  will send you to debug mDNS when mDNS is fine. AltServer's log now names the device call that
  failed (`[device] com.apple.misagent failed: lockdownd -27 (Invalid service)` and the like).
- **`-d` makes things worse.** `libusbmuxd_set_debug_level(debugLogLevel - 2)` underflows, and a
  single `-d` silences the two messages that actually diagnose a netmuxd mismatch.
- **Changing the Apple ID password kills unattended refresh.** A background refresh has no way to
  present a login view, so it fails permanently until someone opens AltStore **on the phone** and
  re-enters the credentials. The phone's keychain and the stack's `ALTSERVER_APPLE_PASSWORD` are
  separate copies — updating Portainer alone is not enough.
- **Do not casually re-run the one-shot install.** The revoke-confirmation prompt is compiled out
  on Linux, so a revoke proceeds unattended and invalidates the certificate your installed apps
  depend on. It happens whenever the install cannot find the cached key in
  `./AltServerData/Certificates/` -- so always run installs from the same working directory. Scripted
  installs should set `ALTSERVER_NONINTERACTIVE=1`, which refuses the revoke instead (exit 6).
- **iOS 18 and later: an install keeps the other apps' profiles.** The 2022 code removed every
  other free provisioning profile around each install with a free Apple ID and put them back
  afterwards; upstream AltServer 1.7.2 stopped doing that on iOS 18+ because it leaves apps
  "unverified", and so does this port now. The log says which path ran:
  `Device iOS 27; keeping other free provisioning profiles during install`.
- **AltStore's refresh removes profiles it does not know about.** On a free Apple ID, AltStore
  sends the list of apps in its own library, and the server removes every other free provisioning
  profile. An app installed with the CLI (not through AltStore) stops launching after AltStore's
  next refresh, until it is installed again.

---

## Reference

### Running it directly

```
Usage:  AltServer-Linux options [ ipa-file ]
  -h  --help             Display this usage information.
  -u  --udid UDID        Device's UDID, only needed when installing IPA.
  -a  --appleID AppleID  Apple ID to sign the ipa, only needed when installing IPA.
  -p  --password passwd  Password of Apple ID, only needed when installing IPA.
  -d  --debug            Print debug output, can be used several times to increase debug level.
```

No IPA argument starts the daemon. With one, it performs a one-time install — which needs a real
terminal, because the 2FA code is read from stdin.

An install's exit status says how it ended, so a script can tell a retry from a problem that needs
a person: `0` installed, `1` other failure, `2` Apple needs a human (2FA code, password), `3`
anisette server, `4` device unreachable, `5` free-account limit (3 apps, 10 App IDs per 7 days),
`6` refused to revoke the certificate, `7` another install is running (one install at a time per
`./AltServerData`, enforced with a lock). Retry 3, 4 and 7 later; stop and look at 2, 5 and 6 --
retrying a sign-in in a loop is how Apple IDs get locked.

### Environment

| Variable | Purpose |
|---|---|
| `ALTSERVER_ANISETTE_SERVER` | **Required.** Full URL including scheme, e.g. `http://127.0.0.1:6969`. There is no default |
| `ALTSERVER_UDID` / `ALTSERVER_APPLE_ID` / `ALTSERVER_APPLE_PASSWORD` | Alternatives to `-u` / `-a` / `-p`. Prefer these: a password passed as `-p` is visible in `ps` to every user on the host |
| `ALTSERVER_NO_CLIENTINFO_SANITIZE` | Set to `1` to stop rewriting `com.apple.dt.Xcode` in `X-MMe-Client-Info`. Diagnostic only — leave unset |
| `ALTSTORE_SKIP_FETCH` | Set to `1` to stop the container refreshing `AltStore.ipa` on start |
| `ALTSERVER_NONINTERACTIVE` | Set to `1` for scripted installs (cron, systemd, a re-sign pipeline). Never waits on stdin, fails at once if Apple asks for a 2FA code, and refuses to revoke the signing certificate |
| `ALTSERVER_ALLOW_REVOKE` | With `ALTSERVER_NONINTERACTIVE=1`, set to `1` to allow that revoke -- deliberately, once |
| `ALTSERVER_2FA_TIMEOUT` | Web UI: seconds an unanswered 2FA prompt waits before the install is cancelled (default 600) |
| `ALTSERVER_GSA_USER_AGENT` | Replaces the User-Agent of the Apple sign-in and 2FA requests (default: the 2019-era values that are proven here). Leave unset unless sign-in starts failing as an outdated client; upstream AltSign moved to `AuthKit/1 (Macintosh; OS X 26.5.2) (com.apple.dt.Xcode/26.0)` in September 2026 |
| `ALTSERVER_WEB_ALLOWED_HOSTS` | Web UI: extra host names it may be reached by. IP addresses, single-label names and `.local` / `.lan` / `.home.arpa` / `.internal` names always work |

There is deliberately **no default anisette server**. The one that used to be hardcoded has
returned HTTP 502 since 2026-09, and pointing every user at a single shared anisette identity can
get Apple IDs locked.

### Runtime requirements

Bundled in the container image. Needed on the host if you run the binary directly:

| Requirement | Why | If missing |
|---|---|---|
| `python3` | The binary is `-static` and cannot dlopen Bonjour, so it shells out to python3 | Advertisement fails |
| `libavahi-compat-libdnssd-dev` | Provides the **unversioned** `libdns_sd.so` the code dlopens | Advertisement fails |
| `avahi-daemon` running | Performs the actual mDNS publishing | Advertisement fails |
| `usbmuxd` for cabled pairing; **`netmuxd` for Wi-Fi** | Device access | No device found |
| An anisette server | Apple machine identity | Sign-in fails |
| Accurate clock **on the anisette host** | Its timestamp is forwarded to Apple verbatim | Opaque `-36607` |

Note the **`-dev`** package, not `libavahi-compat-libdnssd1`: the runtime package ships only
`libdns_sd.so.1`, while the code dlopens the unversioned name. This is the single most common way
to end up with a server that runs, reports nothing wrong, and is invisible to your phone.

---

## Download

- Container image: `ghcr.io/<owner>/altserver-linux:latest`, built by
  [`build_image.yml`](.github/workflows/build_image.yml) for **`linux/amd64` and `linux/arm64`**.
  The stack's default, `ghcr.io/ben-diehlci/altserver-linux:latest`, is **amd64 only** — on a
  Raspberry Pi set `ALTSERVER_IMAGE` to your own account's image, or build locally (the Dockerfile
  picks the toolchain for the host's architecture) and point `ALTSERVER_IMAGE` at the tag:
  `docker build -f docker/Dockerfile -t altserver-linux:local .`
- Static binaries: GitHub Actions artifacts. Branch pushes build **aarch64** only; tags build all
  four architectures. **`chmod +x` after downloading** — artifact upload does not preserve the
  executable bit. Artifacts expire after 90 days; keep a copy, or push a tag for a release

---

## Advanced: building from source

- Preparation: `git clone --recursive <this repo>`

- Easiest, using the same prebuilt toolchain CI uses (it already has corecrypto, cpprestsdk, boost
  and libzip):
  ```
  docker run --rm -v "$PWD":/workdir -w /workdir \
    ghcr.io/ben-diehlci/altserver_builder_alpine_amd64 \
    bash -c 'mkdir -p build; cd build; make -f ../Makefile -j"$(nproc)"'
  ```
  Or build the container image directly: `docker build -f docker/Dockerfile -t altserver-linux:local .`

- By hand (note the `cd build` — the Makefile builds into the *current* directory):
  ```
  cd AltServer-Linux
  mkdir build
  cd build
  make -f ../Makefile -j3
  ls AltServer-*
  ```

### How the build works

This project never forked AltServer-Windows. `upstream_repo/` is a submodule of it, and the build
**rewrites those sources at compile time** — `makefiles/rewrite_altserver_source.py` and friends
convert `L"…"` to `U("…")`, `std::wstring` to `std::string`, `boost::filesystem` to
`std::filesystem`, strip the Win32 GUI and splice in a console implementation. Win32 gaps are
filled by `-include shims/windows_shim.h`.

Patches to vendored code live in those rewriters rather than in the submodule, because the
libraries are submodules: an edit in place cannot be committed here — only the submodule pointer
would move, to a commit that does not exist upstream, breaking every fresh clone.

**Every rewriter fails the build if its patterns stop matching**, rather than emitting a binary
that is quietly missing a transformation:

| Rewriter | How it fails loudly |
|---|---|
| `makefiles/rewrite_altserver_source.py` | Match counts on the AltServerApp.cpp substitutions, plus post-conditions on the output |
| `makefiles/AltSign-build/rewrite_altsign_source.py` | Match assertions |
| `makefiles/AltSign-build/rewrite_ldid_source.py` | Match assertions |
| `makefiles/libimobiledevice-build/rewrite_idevice_source.py` | Match assertions |

The first one needs both kinds because its substitutions fail differently. The AltServerApp.cpp
block runs for one file and every substitution in it is mandatory, so each asserts a count. The
global ones run over all 35 files in the directory — including binaries like `MenuBarIcon.ico` —
and legitimately match zero times in most, so a count would be meaningless; they are checked as
post-conditions on the output instead (no `L"…"` literal, no `boost::filesystem`, no bare
`std::wstring` may survive). That is stronger than counting, because it also catches an occurrence
arriving in a form the pattern was never written to handle.

### Building the buildenv image

`buildenv/Dockerfile` builds the toolchain. Apple's current corecrypto distribution needs three
fixes, all applied there:

1. The archive extracts to `corecrypto-2024/`, not `corecrypto/`. Docker's `WORKDIR` silently
   *creates* the missing directory, so the error surfaces one step later as a confusing
   "does not appear to contain CMakeLists.txt"
2. `CMakeLists.txt` includes `scripts/code-coverage.cmake`, which Apple does not ship
3. `CoreCryptoSources.cmake` still points at `corecrypto_static/ccrng_static.c`, which moved to the
   tree root. The visible error is "No SOURCES given to target"; the real one is the
   "Cannot find source file" line above it

The old note about removing `-mno-default` for ARM is **stale** — the Makefile already guards that
flag to i386/i686, so ARM builds work unmodified.

---

## Credits

Original Linux port by [NyaMisty](https://github.com/NyaMisty/AltServer-Linux). AltStore, AltServer
and AltSign by [Riley Testut](https://github.com/rileytestut). This fork only revives and extends
that work. Licensed AGPL-3.0, as upstream.
