#!/usr/bin/env bash
# Network decisions shared by setup and its offline matrix test.

select_host_network() {
    local requested=$1 route4=$2 route6=$3 existing_ipv6=$4
    if [[ $requested == auto ]]; then
        if [[ $existing_ipv6 == false && $route4 == yes ]]; then
            requested=ipv4
        elif [[ $route4 == yes && $route6 == yes ]]; then
            requested=dual
        elif [[ $route6 == yes ]]; then
            requested=ipv6
        else
            requested=ipv4
        fi
    fi
    case "$requested:$route4:$route6" in
        ipv4:yes:*|ipv6:*:yes|dual:yes:yes) printf '%s\n' "$requested" ;;
        *) echo "HOST_NETWORK=$requested: нет требуемого маршрута (IPv4=$route4 IPv6=$route6)" >&2; return 1 ;;
    esac
}

dns_records() {
    # Distinguish a valid empty answer from DNS or network failure.
    curl -fsS -m 10 "https://dns.google/resolve?name=${DOMAIN}&type=$1" |
        python3 -c '
import ipaddress, json, sys
answer = json.load(sys.stdin)
if answer["Status"] != 0:
    sys.exit("DNS status " + str(answer["Status"]))
rrtype = {"A": 1, "AAAA": 28}[sys.argv[1]]
for record in answer.get("Answer", []):
    if record["type"] == rrtype:
        print(ipaddress.ip_address(record["data"]))
' "$1"
}

check_dns_addresses() {
    # Validate *every* RR, including hosts behind IPv4 NAT, using numeric IP equality.
    python3 - "$@" <<'PY'
import ipaddress, sys
a, aaaa, local4, local6 = sys.argv[1:]
def ips(raw):
    return {ipaddress.ip_address(value) for value in raw.split()}
v4, v6 = ips(a), ips(aaaa)
if not (v4 or v6):
    sys.exit('Нет ни A, ни AAAA')
for addr in v4 | v6:
    if not addr.is_global or addr.is_multicast:
        sys.exit('В публичном DNS указан непубличный адрес: ' + str(addr))
unexpected = (v4 - ips(local4)) | (v6 - ips(local6))
if unexpected:
    sys.exit('Адреса DNS не подтверждены на хосте: ' + ', '.join(sorted(map(str, unexpected))))
PY
}
