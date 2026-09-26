# Raspberry Pi 4 as a 24/7 AltServer host

Notes for the host this fork targets: a Pi 4 (4 GB) booting Raspberry Pi OS Lite 64-bit from a
USB-attached SSD, on wired Ethernet, refreshing an iPhone over home Wi-Fi. Nothing here is needed
to get the stack running; it is about the ways a box like this degrades over months.

Start with the read-only check. It changes nothing and prints no secrets:

```bash
sudo bash deploy/pi/pi-health-check.sh
```

Its first lines say how AltServer actually runs on this host (container or bare binary, native
arm64 or emulated amd64). Every `WARN` maps to a section below. Apply a section only when the
check, or your own hardware, says it applies.

## 1. Power and the USB SSD

- **`get_throttled` must read `0x0`.** Bits `0x1`/`0x10000` are undervoltage now/since boot. An
  SSD on the Pi's USB 3 ports draws from the same 5 V rail; brown-outs show up as USB resets and
  I/O errors long before anything crashes. Use the official 5.1 V / 3 A supply, or a powered hub
  or a self-powered enclosure for the SSD.
- **UAS errors** (`uas_eh_abort_handler`, `reset SuperSpeed USB device`, I/O errors on the root
  disk): some USB-SATA bridges misbehave under UAS. Fall back to usb-storage for that bridge only,
  by appending to the single line of `/boot/firmware/cmdline.txt` (usb-storage is built in, so
  `modprobe.d` cannot do this):

  ```
  usb-storage.quirks=VVVV:PPPP:u
  ```

  `VVVV:PPPP` is the bridge's ID from `lsusb` (the check prints it as `bridge=`).
- **TRIM.** `fstrim.timer` is enabled by default, but it silently skips a disk that reports no
  discard support (`discard_max_bytes` 0). Many bridges support UNMAP without advertising it.
  Only after `sudo sg_vpd -p lbpv /dev/sda` shows `Unmap command supported (LBPU): 1` and a manual
  `echo unmap | sudo tee /sys/block/sda/device/scsi_disk/*/provisioning_mode && sudo fstrim -v /`
  succeeds, make it permanent with `/etc/udev/rules.d/60-usb-ssd-trim.rules`:

  ```
  ACTION=="add|change", SUBSYSTEM=="scsi_disk", ATTRS{idVendor}=="VVVV", ATTRS{idProduct}=="PPPP", ATTR{provisioning_mode}="unmap"
  ```

## 2. Recover from hangs without a person

- **Hardware watchdog.** The Pi 4's watchdog device is on by default, but systemd only uses it if
  told to. Raspberry Pi OS Trixie already ships this; on Bookworm add
  `/etc/systemd/system.conf.d/10-watchdog.conf`:

  ```ini
  [Manager]
  RuntimeWatchdogSec=1m
  RebootWatchdogSec=2m
  ```

- **Reboot on kernel panic** instead of hanging forever (the default, `kernel.panic=0`):
  `/etc/sysctl.d/90-appliance.conf` with `kernel.panic = 10`.
- **avahi-daemon restarts itself.** Its unit has no `Restart=`; if it dies, nothing advertises the
  server. `sudo systemctl edit avahi-daemon`:

  ```ini
  [Service]
  Restart=on-failure
  RestartSec=5
  ```

  AltServer's mDNS helper waits for avahi and re-registers after it comes back; it no longer
  needs restarting itself.

## 3. Logs you can read after a crash

Raspberry Pi OS keeps the journal in RAM (`Storage=volatile`), so the logs of the boot you most
need to explain -- the one that hung or lost power -- are gone. On an SSD root keep them, bounded,
in `/etc/systemd/journald.conf.d/90-persistent.conf`:

```ini
[Journal]
Storage=persistent
SystemMaxUse=200M
```

Docker's own logs are already bounded per service by the stack (`max-size`, `max-file`). For any
other container, `/etc/docker/daemon.json`:

```json
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
```

## 4. Network

- **One interface for mDNS.** If the Pi's Wi-Fi is also on the home LAN, avahi publishes on both
  interfaces -- and on Docker's bridges -- and a host seen twice on one LAN is a common cause of
  avahi renaming itself (`rasai-2.local`). In `/etc/avahi/avahi-daemon.conf`, `[server]` section:

  ```ini
  allow-interfaces=eth0
  use-ipv6=no
  ```

  `use-ipv6=no` because AltServer listens on IPv4 only; an IPv6 address in the advert is one the
  phone can try and fail. If the Pi's Wi-Fi is not used for anything, `dtoverlay=disable-wifi`
  in `/boot/firmware/config.txt` does the same more bluntly.
- **Link flaps.** If the check reports `Link is Down` events on `eth0`, try disabling Energy-
  Efficient Ethernet, which some switch/PHY combinations handle badly: `dtparam=eee=off` in
  `/boot/firmware/config.txt`.
- **Give the phone a fixed address** (a DHCP reservation on the router) and put it, with the
  phone's UDID, in the stack's environment as `ALTSERVER_PHONE_ADDRESSES` / `ALTSERVER_UDID`.
  netmuxd drops the phone when it sleeps or roams and does not always notice it coming back; the
  netmuxd healthcheck re-adds it (see the main README, *Wireless refresh*).
- **WireGuard cannot carry discovery.** AltStore finds the server by Bonjour only, and WireGuard
  interfaces have no multicast. AltStore's own refresh works on the home network; away from home
  only a server-initiated install can reach the phone, through its tunnel address.

## 5. Memory limits are off unless you turn them on

The Pi 4 device tree passes `cgroup_disable=memory`, so Docker's `mem_limit` and systemd's
`MemoryMax=` are silently ignored (`docker info` warns "No memory limit support"). Nothing in the
stack needs a limit to run -- AltServer, netmuxd and anisette together use a few hundred MB of the
4 GB -- but if you want a leak to end in a restart rather than in swap, append
`cgroup_enable=memory` to `/boot/firmware/cmdline.txt` and reboot.

## 6. Time

The Pi has no real-time clock: after a power cut it boots with the last saved time
(`fake-hwclock`) until NTP syncs. Apple rejects sign-ins whose anisette timestamp is far off, with
an opaque error. The status page's clock row compares this host with an outside reference; keep
`systemd-timesyncd` (the default) enabled and let the network come up before a refresh is tried.

## 7. Running the bare binary instead of Docker

Lighter by the ~100 MB dockerd and containerd take, at the cost of the stack's healthchecks and
web UI. [`altserver.service`](altserver.service) is a systemd unit for the static binary, with its
install steps at the top: restart on any exit, a stable working directory for `AltServerData`,
the same credential filter the container pipes its log through, and a descriptor limit below
`FD_SETSIZE`. It assumes netmuxd and anisette already run on the host.
