#!/bin/sh
# Adapter for MicroWARP's unchanged launch_microsocks() entrypoint.
set -eu
umask 077

die() { printf '%s\n' "MicroWARP/Hev: $*" >&2; exit 1; }
yaml_string() { printf '%s' "$1" | sed "s/'/''/g"; }

listen=0.0.0.0
port=1080
user=
password=
while [ "$#" -gt 0 ]; do
    [ "$#" -ge 2 ] || die 'option value missing'
    case "$1" in
        -i) listen=$2 ;;
        -p) port=$2 ;;
        -u) user=$2 ;;
        -P) password=$2 ;;
        *) die 'unsupported MicroSOCKS option' ;;
    esac
    shift 2
done
case "$port" in ''|*[!0-9]*) die 'invalid listen port' ;; esac
[ "$port" -ge 1 ] && [ "$port" -le 65535 ] || die 'invalid listen port'

if [ -n "$user" ] || [ -n "$password" ]; then
    [ -n "$user" ] && [ -n "$password" ] || die 'both credentials are required'
    [ "${#user}" -le 255 ] && [ "${#password}" -le 255 ] || die 'credentials too long'
else
    case "${ALLOW_NO_AUTH:-0}" in
        1|true|TRUE|yes|YES|on|ON) ;;
        *) die 'anonymous mode requires ALLOW_NO_AUTH=1' ;;
    esac
fi
# YAML strings are quoted; reject line breaks rather than emitting multiline YAML.
cr=$(printf '\r')
for value in "$listen" "$user" "$password"; do
    case "$value" in *'
'*|*"$cr"*) die 'line breaks are not supported in listen address or credentials' ;; esac
done

ip link show dev wg0 >/dev/null 2>&1 || die 'WireGuard interface wg0 is missing'
wg_ipv4=$(ip -4 -o addr show dev wg0 | awk 'NR == 1 {split($4,a,"/"); print a[1]}')
[ -n "$wg_ipv4" ] || die 'WireGuard IPv4 address is missing'
case "${ENABLE_IPV6:-1}" in
    0|false|FALSE|no|NO|off|OFF) family=ipv4 ;;
    *) family=unspec ;;
esac

config=/run/microwarp-hev.yml
cat > "$config" <<EOF
main:
  workers: 1
  port: $port
  listen-address: '$(yaml_string "$listen")'
  udp-port: 0
  domain-address-type: $family
  bind-interface: 'wg0'
  bind-address-v4: '$wg_ipv4'
misc:
  connect-timeout: 10000
  tcp-read-write-timeout: 1800000
  udp-read-write-timeout: 60000
  log-file: stderr
  log-level: warn
EOF
if [ -n "$user" ]; then
    cat >> "$config" <<EOF
auth:
  username: '$(yaml_string "$user")'
  password: '$(yaml_string "$password")'
EOF
fi

printf '%s\n' "MicroWARP/Hev: TCP and UDP SOCKS5 on $listen:$port; outbound bound to wg0" >&2
exec /usr/local/bin/hev-socks5-server "$config"
