#!/usr/bin/env bash
#
# Run commission_sensor.py on a Doovit over SSH (LAN / mDNS).
#
# The sensor's serial line (/dev/ttyAMA0) lives on the Doovit host, so this
# copies the commissioning tool + driver to the device and runs it there.
#
# Usage:
#   scripts/commission_sensor_remote.sh <host> [commission args...]
#
# <host> is the Doovit's mDNS name or IP. If you don't give a user it
# defaults to the standard doovit login (doovit / doovit):
#   doovit-5051af.local              # → doovit@doovit-5051af.local
#   doovit@doovit-5051af.local       # explicit user
#   192.168.1.50                     # by IP
#
# Everything after the host is forwarded verbatim to commission_sensor.py:
#   scripts/commission_sensor_remote.sh doovit-5051af.local \
#       --new-id 10 --new-baud 19200 --verify
#
# Password: defaults to "doovit" via sshpass so you're not prompted. Override
# with DOOVIT_PASSWORD=... in the environment. If sshpass isn't installed
# (brew install hudochenkov/sshpass/sshpass) it falls back to normal SSH and
# you'll be prompted once (connections are multiplexed).
#
# NOTE: if a Doover Modbus app on the device is actively polling the bus it
# will contend for /dev/ttyAMA0. Stop/pause that app (or commission before
# it starts) so the two aren't talking over each other.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,34p'
  exit 1
fi

TARGET="$1"; shift
[[ "$TARGET" == *@* ]] || TARGET="doovit@$TARGET"   # default to the doovit login

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRIVER="$HERE/../src/prosense_sensors/prosense_driver.py"
SCRIPT="$HERE/commission_sensor.py"
for f in "$DRIVER" "$SCRIPT"; do
  [[ -f "$f" ]] || { echo "missing $f" >&2; exit 1; }
done

REMOTE_DIR="/tmp/prosense-commission"
PASSWORD="${DOOVIT_PASSWORD:-doovit}"

# Multiplex SSH so even the no-sshpass fallback only authenticates once.
CTL="$(mktemp -u "${TMPDIR:-/tmp}/prosense-ssh-XXXXXX")"
COMMON_OPTS=(-o StrictHostKeyChecking=accept-new
             -o ControlMaster=auto -o "ControlPath=$CTL" -o ControlPersist=30)
cleanup() { ssh "${COMMON_OPTS[@]}" -O exit "$TARGET" 2>/dev/null || true; }
trap cleanup EXIT

# Use sshpass when available; otherwise plain ssh/scp (will prompt once).
if command -v sshpass >/dev/null 2>&1; then
  AUTH=(sshpass -p "$PASSWORD")
else
  echo ">> sshpass not found — you'll be prompted for the password" \
       "(it's '$PASSWORD'). Install sshpass to skip this." >&2
  AUTH=()
fi
SSH() { "${AUTH[@]}" ssh "${COMMON_OPTS[@]}" "$@"; }
SCP() { "${AUTH[@]}" scp "${COMMON_OPTS[@]}" "$@"; }

echo ">> staging tool on $TARGET"
SSH "$TARGET" "mkdir -p '$REMOTE_DIR'"
# Copy both flat — commission_sensor.py falls back to importing a sibling
# prosense_driver.py when the package isn't on the path.
SCP -q "$DRIVER" "$SCRIPT" "$TARGET:$REMOTE_DIR/"

# Forward args with quoting intact through the remote shell.
ARGS=""
for a in "$@"; do ARGS+=" $(printf '%q' "$a")"; done

echo ">> ensuring pyserial, then running commissioning tool"
# shellcheck disable=SC2029  — $ARGS / $REMOTE_DIR must expand locally.
SSH -t "$TARGET" "
  set -e
  if ! python3 -c 'import serial' 2>/dev/null; then
    echo '   installing pyserial ...'
    pip3 install --user pyserial 2>/dev/null \
      || pip3 install --user --break-system-packages pyserial 2>/dev/null \
      || { echo '   could not install pyserial automatically' >&2; exit 3; }
  fi
  cd '$REMOTE_DIR' && python3 commission_sensor.py$ARGS
"
