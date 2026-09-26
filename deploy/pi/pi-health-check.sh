#!/bin/bash
# Read-only health check for a Raspberry Pi running this stack. Changes nothing, prints no secrets.
#   sudo bash deploy/pi/pi-health-check.sh
# The first section answers "how is AltServer actually running here?" (container or bare binary,
# native arm64 or emulated amd64); the rest covers the Pi-specific ways a 24/7 box degrades.
ok(){ printf 'OK    %s\n' "$*"; }
warn(){ printf 'WARN  %s\n' "$*"; }
info(){ printf 'INFO  %s\n' "$*"; }
have(){ command -v "$1" >/dev/null 2>&1; }
info "$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-?}") | kernel $(uname -r) | $( { tr -d '\0' </proc/device-tree/model; } 2>/dev/null) | rev $(awk '/^Revision/{print $3}' /proc/cpuinfo)"
# --- how AltServer runs here: container vs bare binary, native vs emulated ------------------
# Command lines are shown with any -p/--password value and ALTSERVER_APPLE_PASSWORD masked.
mask(){ sed -E 's/((^| )(-p|--password)[ =])[^ ]+/\1****/g; s/(ALTSERVER_APPLE_PASSWORD=)[^ ]+/\1****/g'; }
# Snapshot first, so the grep/sed below never list themselves.
procs=$(ps -eo pid=,user=,etime=,args= 2>/dev/null)
printf '%s\n' "$procs" | grep -iE 'AltServer|netmuxd|anisette|muxproxy' | grep -v 'pi-health-check' | mask | cut -c1-160 | sed 's/^ */INFO  process /'
have systemctl && systemctl list-units --all --no-legend --plain 2>/dev/null | grep -iE 'altserver|netmuxd|anisette|muxproxy' | awk '{print "INFO  unit " $1 " " $3 "/" $4}'
qemu=""; for f in /proc/sys/fs/binfmt_misc/qemu-*; do [ -e "$f" ] && qemu="$qemu ${f##*/}"; done
[ -n "$qemu" ] && warn "QEMU binfmt registered:$qemu -- foreign-arch binaries can run EMULATED" || ok "no QEMU binfmt: every binary here runs natively"
if have docker; then
  for c in $(docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'altserver|netmuxd|anisette|muxproxy'); do
    img=$(docker inspect -f '{{.Config.Image}}' "$c" 2>/dev/null)
    arch=$(docker image inspect -f '{{.Architecture}}' "$img" 2>/dev/null)
    elf=$(docker exec "$c" sh -c 'od -An -tx1 -j18 -N2 /usr/local/bin/AltServer 2>/dev/null' 2>/dev/null | tr -d ' \n')
    case "$elf" in b700) m="AltServer aarch64 (native)";; 3e00) m="AltServer x86-64 (EMULATED on a Pi)";; *) m="";; esac
    info "container $c image=$img arch=${arch:-?} ${m}"
  done
fi
for b in $(printf '%s\n' "$procs" | awk '{print $4}' | grep -E '/AltServer[^/ ]*$' | sort -u); do
  [ -r "$b" ] && info "binary $b ELF machine $(od -An -tx1 -j18 -N2 "$b" | tr -d ' \n') (b700=aarch64, 3e00=x86-64)"
done
have ip && ip -o link show type wireguard 2>/dev/null | awk -F': ' '{print "INFO  wireguard interface " $2}'
have ss && ss -ltnp 2>/dev/null | grep -i altserver | awk '{print "INFO  AltServer listening on " $4}'
# --- power / thermal (bits per RPi docs: 0x1 UV now, 0x2 capped, 0x4 throttled, 0x8 soft-temp; <<16 = has occurred)
if have vcgencmd; then
  t=$(vcgencmd get_throttled | cut -d= -f2); [ "$t" = "0x0" ] && ok "get_throttled=$t" || warn "get_throttled=$t (0x1/0x10000 = undervoltage now/since boot: PSU, cable or SSD current draw)"
  info "$(vcgencmd measure_temp)"
fi
n=$(dmesg 2>/dev/null | grep -c 'Undervoltage detected'); [ "${n:-0}" -eq 0 ] && ok "no undervoltage in dmesg" || warn "$n undervoltage events this boot"
# --- ethernet / EEE
IF=$(ip -o route show default 2>/dev/null | awk '{print $5; exit}'); IF=${IF:-eth0}
have ethtool && info "EEE on $IF: $(ethtool --show-eee "$IF" 2>/dev/null | awk -F': ' '/EEE status/{print $2}') (want: disabled / not supported)"
f=$(journalctl -k -b 2>/dev/null | grep -c "$IF: Link is Down"); [ "${f:-0}" -eq 0 ] && ok "no link flaps on $IF this boot" || warn "$f link-down events on $IF this boot"
# --- root storage: bridge, driver, UAS errors, TRIM
ROOTDEV=$(findmnt -no SOURCE / 2>/dev/null); DISK=$(lsblk -no PKNAME "$ROOTDEV" 2>/dev/null | head -1)
if [ -n "$DISK" ] && [ -e "/sys/block/$DISK" ]; then
  u=$(readlink -f "/sys/block/$DISK/device"); vid=""; while [ "$u" != "/" ] && [ -z "$vid" ]; do [ -r "$u/idVendor" ] && vid=$(cat "$u/idVendor"):$(cat "$u/idProduct"); u=$(dirname "$u"); done
  drv=$(basename "$(readlink -f "/sys/block/$DISK/device/../../../driver" 2>/dev/null)" 2>/dev/null)
  info "root=$ROOTDEV on /dev/$DISK bridge=${vid:-?} driver=${drv:-?} (lsusb -t shows Driver=uas or usb-storage)"
  e=$(dmesg 2>/dev/null | grep -cE 'uas_eh_|reset SuperSpeed|I/O error, dev '"$DISK"); [ "${e:-0}" -eq 0 ] && ok "no UAS/I-O errors this boot" || warn "$e UAS/I-O error lines: consider usb-storage.quirks=${vid:-VID:PID}:u"
  d=$(cat "/sys/block/$DISK/queue/discard_max_bytes" 2>/dev/null); [ "${d:-0}" != 0 ] && ok "discard supported (fstrim works)" || info "no discard on /dev/$DISK: weekly fstrim is silently skipped"
fi
have systemctl && info "fstrim.timer: $(systemctl is-enabled fstrim.timer 2>&1)"
# --- watchdog / panic / journal
w=$(systemctl show -p RuntimeWatchdogUSec --value 2>/dev/null); [ -n "$w" ] && [ "$w" != "0" ] && [ "$w" != "infinity" ] && ok "systemd RuntimeWatchdog=$w" || warn "hardware watchdog not used by systemd (RuntimeWatchdogSec off)"
p=$(sysctl -n kernel.panic 2>/dev/null); [ "${p:-0}" -gt 0 ] && ok "kernel.panic=$p" || warn "kernel.panic=${p:-?} (panic = hang forever)"
s=$(systemd-analyze cat-config systemd/journald.conf 2>/dev/null | grep -E '^Storage=' | tail -1); [ "$s" = "Storage=volatile" ] && warn "journal is $s: logs of a crashed boot are lost" || ok "journal ${s:-Storage=auto}"
# --- cgroups / LSM
grep -qw memory /sys/fs/cgroup/cgroup.controllers 2>/dev/null && ok "memory cgroup controller available" || warn "memory controller OFF (DT bootargs cgroup_disable=memory): mem_limit is silently discarded"
a=$(cat /sys/module/apparmor/parameters/enabled 2>/dev/null); info "AppArmor enabled=${a:-absent} (N/absent => security_opt apparmor=* is ignored by Docker)"
# --- time
have timedatectl && { [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] && ok "NTP synchronised" || warn "clock NOT NTP-synchronised (anisette stamps Apple requests with it)"; }
# --- packages / swap / eeprom
dpkg -s unattended-upgrades >/dev/null 2>&1 && info "unattended-upgrades installed (avahi restarts can drop the AltServer advert)" || info "unattended-upgrades not installed"
info "swap: $(swapon --show=NAME,SIZE --noheadings 2>/dev/null | tr '\n' ' ')"
have rpi-eeprom-update && info "eeprom: $(rpi-eeprom-update 2>/dev/null | grep -E 'BOOTLOADER|VL805' | tr -s ' ' | tr '\n' ';')"
# --- avahi / advert
grep -qE '^[[:space:]]*allow-interfaces=' /etc/avahi/avahi-daemon.conf 2>/dev/null && ok "avahi $(grep -E '^[[:space:]]*allow-interfaces=' /etc/avahi/avahi-daemon.conf)" || info "avahi allow-interfaces unset (publishes on every multicast interface)"
have avahi-browse && { avahi-browse -rpt _altserver._tcp 2>/dev/null | grep -q '^=' && ok "_altserver._tcp is being advertised" || warn "_altserver._tcp NOT advertised (see the 'mDNS:' lines in AltServer's log, and deploy/pi/README.md)"; }
if have docker; then
  docker info 2>&1 | grep -q 'No memory limit support' && warn "docker: No memory limit support"
  docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | sed 's/^/INFO  container /'
fi
