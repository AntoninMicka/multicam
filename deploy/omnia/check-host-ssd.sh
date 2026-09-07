#!/bin/sh
# Read-only check on the Turris host before creating the LXC container.
set -eu
ssd_mount="${1:-/srv}"
command -v findmnt >/dev/null || { echo 'Nainstalujte findmnt (util-linux).' >&2; exit 1; }
mountpoint -q "$ssd_mount" || { echo 'SSD není připojené jako samostatný mount.' >&2; exit 1; }
ssd_source=$(findmnt -n -o SOURCE --target "$ssd_mount")
ssd_source=${ssd_source%%\[*}
ssd_source=$(readlink -f "$ssd_source")
case "$ssd_source" in
  /dev/sd*|/dev/nvme*) ;;
  *) echo "Odmítám interní flash nebo neznámé zařízení: $ssd_source" >&2; exit 1 ;;
esac
[ -b "$ssd_source" ] || { echo 'Úložiště není blokové zařízení.' >&2; exit 1; }
ssd_name=${ssd_source##*/}
ssd_sys=$(readlink -f "/sys/class/block/$ssd_name")
if [ -f "$ssd_sys/partition" ]; then ssd_sys=${ssd_sys%/*}; fi
[ "$(cat "$ssd_sys/queue/rotational")" = 0 ] || { echo 'Úložiště není SSD.' >&2; exit 1; }
case ",$(findmnt -n -o OPTIONS --target "$ssd_mount")," in
  *,rw,*) ;;
  *) echo 'SSD není připojené pro zápis.' >&2; exit 1 ;;
esac
printf 'SSD ověřeno: %s na %s\n' "$ssd_source" "$ssd_mount"
