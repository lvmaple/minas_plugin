#!/bin/sh
# This NAS runs Docker's default bridge without an outbound masquerade rule.
# Give only the Motrix container's current address outbound access.
set -eu

DOCKER=/data/docker/docker
CONTAINER=motrix-server
CHAIN=MOTRIX_EGRESS

attempt=0
while ! "$DOCKER" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo 'Motrix container did not start; outbound rule not installed' >&2
    exit 1
  fi
  sleep 2
done

mode="$("$DOCKER" inspect -f '{{.HostConfig.NetworkMode}}' "$CONTAINER")"
if [ "$mode" != default ] && [ "$mode" != bridge ]; then
  echo "Unexpected Motrix network mode: $mode" >&2
  exit 1
fi
ip="$("$DOCKER" inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$CONTAINER")"
python3 - "$ip" <<'PY'
import ipaddress, sys
ip = ipaddress.ip_address(sys.argv[1])
if (not isinstance(ip, ipaddress.IPv4Address) or not ip.is_private
        or ip.is_unspecified or ip.is_loopback or ip.is_link_local):
    raise SystemExit('Motrix bridge address is not a private IPv4 address')
PY

iptables -t nat -N "$CHAIN" 2>/dev/null || true
iptables -t nat -F "$CHAIN"
iptables -t nat -A "$CHAIN" -s "$ip/32" ! -o docker0 -j MASQUERADE
iptables -t nat -C POSTROUTING -j "$CHAIN" 2>/dev/null || iptables -t nat -A POSTROUTING -j "$CHAIN"
echo "Motrix outbound network ready for $ip"
