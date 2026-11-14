#!/usr/bin/env bash
# Offline: only route flags, local JSON fixtures and DNS answers are used.
set -eu -o pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
source tools/setup-network.sh

expect_mode() {
    local expected=$1; shift
    [[ $(select_host_network "$@") == "$expected" ]] || { echo "Failed: $*" >&2; exit 1; }
}
reject() {
    if "$@" >/dev/null 2>&1; then echo "Unexpected success: $*" >&2; exit 1; fi
}
expect_mode ipv4 auto yes no ''
expect_mode ipv6 auto no yes ''
expect_mode dual auto yes yes ''
expect_mode ipv4 auto yes yes false     # preserve existing bridge
expect_mode dual auto yes yes true
expect_mode ipv4 ipv4 yes yes true
reject select_host_network dual no yes ''
reject select_host_network dual yes no ''
reject select_host_network auto no no ''
reject select_host_network typo yes yes ''

a=1.1.1.1
aaaa=2606:4700:4700::1111
check_dns_addresses "$a" '' "$a" ''
check_dns_addresses '' "$aaaa" '' '2606:4700:4700:0:0:0:0:1111'
check_dns_addresses "$a" "$aaaa" "10.0.0.2 $a" "$aaaa"
reject check_dns_addresses '' '' "$a" "$aaaa"
reject check_dns_addresses "$a 8.8.8.8" '' "$a" ''
reject check_dns_addresses "$a" '2606:4700:4700::1001' "$a" "$aaaa"
reject check_dns_addresses '10.0.0.2' '' '10.0.0.2' ''

DOMAIN=example.test
curl() { printf '%s\n' "$fixture"; }
fixture='{"Status":0,"Answer":[{"type":5,"data":"alias."},{"type":28,"data":"2606:4700:4700:0:0:0:0:1111"}]}'
[[ $(dns_records AAAA) == "$aaaa" && -z $(dns_records A) ]]
fixture='{"Status":2}'
reject dns_records A
fixture='<html>network error</html>'
reject dns_records AAAA
curl() { return 1; }
reject dns_records A
echo "Installer network and DNS matrix passed."
