#!/bin/sh
# Read-only; redirect stdout to a file on your workstation over SSH.
# Review before sharing: includes device serials, kernel logs and application logs.
set -u
section() { printf '\n### %s\n' "$*"; }
section identity
date -u
cat /proc/sys/kernel/random/boot_id /proc/uptime /etc/os-release
uname -a
section services
rc-service sweep status
ps -ef
section application
cat /run/ddt/status.json
tail -n 150 /var/log/ddt.log
section hardware
vcgencmd get_throttled
lsusb
ls -l /dev/i2c* /dev/gpiochip*
section storage
mount
df -h
section packages
apk info -v
section kernel
dmesg
