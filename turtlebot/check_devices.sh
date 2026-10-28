#!/bin/bash
# Shows what is plugged into the robot and where.
# Run:  ./check_devices.sh

hdr() { echo; echo "===== $* ====="; }

hdr "LiDAR (Hokuyo)"
ls -l /dev/serial/by-id/ 2>/dev/null | grep -i -E "hokuyo|urg" || echo "no Hokuyo in /dev/serial/by-id"
ls /dev/ttyACM* 2>/dev/null || echo "no /dev/ttyACM*"

hdr "Robot base (Kobuki)"
ls -l /dev/kobuki 2>/dev/null || echo "no /dev/kobuki"
ls /dev/ttyUSB* 2>/dev/null

hdr "Camera (Orbbec)"
lsusb | grep -i -E "2bc5|orbbec" || echo "Orbbec camera NOT found on USB"

hdr "USB devices"
lsusb

hdr "Microphones (capture cards)"
arecord -l
echo
echo "card  id            USB port   name"
for c in /sys/class/sound/card*; do
    n=${c##*card}
    id=$(cat "$c/id" 2>/dev/null)
    dev=$(readlink -f "$c/device")
    port=$(basename "$(dirname "$dev")")
    case "$dev" in *usb*) ;; *) port="(not USB)";; esac
    name=$(awk -v n="$n" '$1==n {sub(/.*\]: [^ ]+ - /,""); print; exit}' /proc/asound/cards)
    printf "%-5s %-13s %-10s %s\n" "$n" "$id" "$port" "$name"
done

hdr "Saved mic setup"
cat ~/mic_config.json 2>/dev/null || echo "none yet - run: python3 setup_mics.py"
