#!/usr/bin/env bash
# Run on the VPS. No production container, Compose file or routing is changed.
set -Eeuo pipefail
umask 077

mode=${1:---plan}
name=xui-warp-probe
image=ghcr.io/ccbkkb/microwarp:latest
state=/opt/x-ui/backups/warp-probe-identity
network=x-ui_xray-net
[[ $mode == --plan || $mode == --run ]] || { echo 'Usage: probe-warp.sh [--plan|--run]' >&2; exit 2; }
for cmd in docker curl flock timeout; do command -v "$cmd" >/dev/null; done
docker info >/dev/null
docker network inspect "$network" >/dev/null
if docker container inspect "$name" >/dev/null 2>&1; then
    echo "Refusing to touch existing container: $name" >&2
    exit 1
fi
cat <<EOF
Plan:
  Pull $image and resolve its immutable image ID.
  Start ONE temporary container: $name, $network, NO published ports.
  NET_ADMIN applies only inside its own network namespace; no host networking.
  Keep WARP registration in $state (root only).
  Compare fixed-IP HTTPS using the SAME host curl directly and through SOCKS5.
  Remove only the temporary container on exit; preserve its identity for reuse.
  Do not restart or reconfigure sslh, Caddy, Xray, certbot, or host routes.
EOF
[[ $mode == --run ]] || exit 0
[[ $EUID == 0 ]] || { echo 'Root required' >&2; exit 1; }
exec 9>/run/lock/xui-warp-probe.lock
flock -n 9
created=0
cleanup() {
    local rc=$?
    trap - EXIT
    if (( created )); then docker rm -f "$name" >/dev/null || rc=1; fi
    echo "WARP probe finished, exit=$rc; production containers untouched."
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
timeout 240 docker pull --quiet "$image"
image_id=$(docker image inspect "$image" --format '{{.Id}}')
echo "Using $image_id"
install -d -m 700 "$state"
docker create --name "$name" --network "$network" --cap-add NET_ADMIN \
    --sysctl net.ipv4.conf.all.src_valid_mark=1 \
    --memory 160m --pids-limit 64 \
    --log-opt max-size=2m --log-opt max-file=1 \
    -e ALLOW_NO_AUTH=1 -e ENABLE_IPV6=0 -e TUNNEL_PROTOCOL=wireguard \
    --mount "type=bind,src=$state,dst=/etc/wireguard" "$image_id" >/dev/null
created=1
docker start "$name" >/dev/null
proxy_ip=$(docker inspect "$name" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
ready=0
for ((i=0;i<24;i++)); do
    if trace=$(curl -fsS --connect-timeout 3 --max-time 5 \
        --socks5 "$proxy_ip:1080" https://1.1.1.1/cdn-cgi/trace 2>/dev/null) \
        && [[ $trace == *warp=on* || $trace == *warp=plus* ]]; then
        printf '%s\n' "$trace" | sed -n '/^warp=/p; /^loc=/p; /^ip=/p'
        ready=1
        break
    fi
    [[ $(docker inspect "$name" --format '{{.State.Running}}') == true ]] || break
    sleep 2
done
if (( ! ready )); then
    echo 'WARP did not become ready; production routing is unchanged.' >&2
    # Logs can contain registration secrets, so do not print them to the console.
    docker logs "$name" >"$state/startup.log" 2>&1
    exit 1
fi
while read -r ip host; do
    for route in direct warp; do
        args=()
        [[ $route == direct ]] || args=(--socks5 "$proxy_ip:1080")
        printf '%s %s ' "$route" "$ip"
        # socks5 (NOT socks5-hostname) makes --resolve pin the target IP.
        curl --noproxy '' "${args[@]}" --silent --show-error \
            --connect-timeout 5 --max-time 8 --resolve "$host:443:$ip" \
            -o /dev/null -w 'remote=%{remote_ip} tcp=%{time_connect} tls=%{time_appconnect} http=%{http_code}\n' \
            "https://$host/" || true
    done
done <<'TARGETS'
188.43.61.147 rr8---sn-ug5onuxaxjvh-n8v6.googlevideo.com
188.43.61.141 rr2---sn-ug5onuxaxjvh-n8v6.googlevideo.com
80.64.175.142 rr3---sn-npcnxu-v8cs.googlevideo.com
89.113.122.236 r1---sn-8ph2xajvh-vhns.googlevideo.com
74.125.100.39 rr2---sn-5hnekn7s.googlevideo.com
TARGETS
