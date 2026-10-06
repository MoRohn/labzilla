#!/bin/sh
# Keeps labzilla.local published via avahi and republishes it when it stops resolving.
# avahi-publish can hang without ever establishing the record when it starts during boot-time
# interface churn (k3s/docker veths), and it doesn't notice DHCP address changes; a still-running
# publisher is not proof the name resolves, so this checks the name itself.
# Run by labzilla-mdns.service; see that unit for install steps.
set -u
NAME=${LABZILLA_MDNS_NAME:-labzilla.local}
IFACE=${LABZILLA_MDNS_IFACE:?set LABZILLA_MDNS_IFACE}
CHECK_SEC=${LABZILLA_MDNS_CHECK_SEC:-30}

addr() { ip -4 -o addr show dev "$IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1; }
nap() { sleep "$1" & wait $!; }  # interruptible, so SIGTERM is handled promptly
healthy() {
  kill -0 "$pid" 2>/dev/null || return 1
  [ "$(addr)" = "$ip" ] || return 1
  timeout 10 avahi-resolve -4 -n "$NAME" 2>/dev/null | grep -qw "$ip"
}

pid=
stop() { [ -n "$pid" ] && kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; pid=; }
trap 'stop; exit 0' TERM INT

while :; do
  ip=$(addr)
  if [ -z "$ip" ]; then
    echo "no IPv4 on $IFACE yet; waiting"
    nap 5
    continue
  fi
  /usr/bin/avahi-publish -a -R "$NAME" "$ip" &
  pid=$!
  nap 10
  fails=0
  while :; do
    if healthy; then
      fails=0
    else
      fails=$((fails + 1))
      if [ "$fails" -ge 2 ]; then
        echo "$NAME not resolving to $ip (iface now: $(addr)); republishing"
        break
      fi
    fi
    nap "$CHECK_SEC"
  done
  stop
done
