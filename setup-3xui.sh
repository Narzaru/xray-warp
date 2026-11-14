#!/bin/bash
# setup-3xui.sh — установка 3x-ui + XRay + Caddy + sslh в Docker
#
# Архитектура порта 443:
#   Интернет :443/tcp → [sslh]
#                         ├── SSH → host sshd (порт из .env)
#                         └── TLS → [caddy :8443]  ← TLS терминирует Caddy
#                                      ├── /game          → [XRay :10000] xhttp без TLS
#                                      └── всё остальное  → фейковый сайт
#
#   Интернет :443/udp → [hysteria]  (опционально, HYSTERIA=on в .env)
#
# Зависимости Linux-хоста перечислены в docs/install.md.
#
# Использование:
#   cp .env.example .env && nano .env
#   ./setup-3xui.sh

set -euo pipefail
IFS=$'\n\t'
# Resolve templates and relative volumes from the checkout, even with an absolute invocation.
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
source ./tools/setup-network.sh

# ── Отвязка от терминала ────────────────────────────────────────────────────
# SSH-сессия через 443 идёт сквозь sslh, а скрипт сам его перезапускает: шаг 2
# рестартует dockerd, шаг 5 пересоздаёт контейнеры. Обрыв сессии убивал скрипт
# на середине — в худшем случае новый sslh оставался в состоянии created, и SSH
# через 443 пропадал совсем. Поэтому скрипт перезапускает себя в отдельной
# сессии, а в терминале только показывает лог: обрыв SSH убьёт tail, не установку.
if [ -z "${XUI_DETACHED:-}" ] && [ -t 1 ]; then
    _log="$(pwd)/setup.log"
    (umask 077; touch "$_log") && chmod 600 "$_log"
    XUI_DETACHED=1 setsid nohup bash ./setup-3xui.sh "$@" > "$_log" 2>&1 < /dev/null &
    _pid=$!
    echo "Установка идёт в фоне (PID ${_pid}), лог: ${_log}"
    echo "Если SSH оборвётся — переподключитесь и смотрите: tail -f ${_log}"
    echo ""
    tail -n +1 -f --pid="$_pid" "$_log"
    wait "$_pid"   # код выхода установки; при set -e ошибка уходит наружу
    exit 0
fi

# ── Вывод ───────────────────────────────────────────────────────────────────

GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'
RED='\033[0;31m'; MAGENTA='\033[0;35m'; NC='\033[0m'

TOTAL=8
step() { echo -e "\n${BLUE}[$1/${TOTAL}]${NC} ${YELLOW}$2${NC}"; }
ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1" >&2; }
die()  { fail "$1"; exit 1; }

# ── Баннер ──────────────────────────────────────────────────────────────────

clear 2>/dev/null || true   # без TERM clear падает, а set -e завершил бы скрипт
echo -e "${MAGENTA}"
cat << "BANNER"
╔════════════════════════════════════════════════════════╗
║       3x-ui · XRay · Caddy · sslh — Docker Setup       ║
║                  Linux · systemd · Docker Engine       ║
╚════════════════════════════════════════════════════════╝
BANNER
echo -e "${NC}"

# ── Загрузка .env ────────────────────────────────────────────────────────────

[ "$EUID" -eq 0 ] || die "Запустите скрипт от root: sudo ./setup-3xui.sh"

if [ ! -f .env ]; then
    [ -f .env.example ] || die "Запустите скрипт из корневой директории проекта (не найден .env.example)"
    install -m 600 .env.example .env
    echo -e "${YELLOW}Файл .env создан из шаблона. Заполните его и запустите скрипт повторно:${NC}"
    echo -e "  ${BLUE}nano .env${NC}"
    exit 0
fi

# Compose принимает CRLF. Для Bash удаляем CR из входного потока, сохраняя исходный файл.
# shellcheck source=/dev/null
source <(sed 's/\r$//' .env)

DOMAIN="${DOMAIN:?Переменная DOMAIN не задана в .env}"
EMAIL="${EMAIL:?Переменная EMAIL не задана в .env}"

[ "$DOMAIN" = "example.com" ] && die "Замените DOMAIN=example.com в .env на реальный домен"
[ "$EMAIL" = "admin@example.com" ] && die "Замените EMAIL=admin@example.com в .env на реальный адрес"
_sys_tz=$(timedatectl show --property=Timezone --value 2>/dev/null \
          || cat /etc/timezone 2>/dev/null \
          || echo UTC)
TIMEZONE="${TIMEZONE:-${_sys_tz}}"
SSH_PORT="${SSH_PORT:-22}"
PROXY_MODE="${PROXY_MODE:-off}"
export DOMAIN TIMEZONE SSH_PORT PROXY_MODE

# Hysteria2 — опциональный транспорт QUIC/UDP (docker-compose.hysteria.yml).
HYSTERIA="${HYSTERIA:-off}"

# Проверить сразу: опечатка вроде PROXY_MODE=Warp иначе всплывала только на
# шаге 8 — после того как стек уже поднят без warp и записан cron.
case "$PROXY_MODE" in off|warp|vps) ;; *) die "Неизвестный PROXY_MODE=${PROXY_MODE} (допустимо: off, warp, vps)" ;; esac
case "$HYSTERIA"   in off|on) ;;       *) die "Неизвестный HYSTERIA=${HYSTERIA} (допустимо: off, on)" ;; esac

case "${WARP_SOCKS:-auto}" in auto|hev|microsocks) ;; *) die "WARP_SOCKS: auto, hev или microsocks" ;; esac
[[ "$SSH_PORT" =~ ^[1-9][0-9]{0,4}$ && "$SSH_PORT" -le 65535 ]] || die "Недопустимый SSH_PORT"
[[ "$SSH_PORT" != 443 && "$SSH_PORT" != 80 ]] || die "SSH_PORT должен отличаться от публичных TCP-портов 80 и 443, занятых Caddy и sslh"
if [[ $PROXY_MODE == vps ]]; then
    : "${VPS_ADDRESS:?Задайте VPS_ADDRESS}" "${VPS_UUID:?Задайте VPS_UUID}"
    VPS_PORT=${VPS_PORT:-443}
    [[ "$VPS_PORT" =~ ^[1-9][0-9]{0,4}$ && "$VPS_PORT" -le 65535 ]] || die "Недопустимый VPS_PORT"
fi
echo -e "  Домен: ${GREEN}${DOMAIN}${NC}; SSH: ${SSH_PORT}; прокси: ${PROXY_MODE}; Hysteria: ${HYSTERIA}"

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 1 — Зависимости
# ════════════════════════════════════════════════════════════════════════════

step 1 "Проверка зависимостей"

[[ $DOMAIN =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && $DOMAIN != *..* && $DOMAIN == *.* ]] || die "Нужен DNS-домен без протокола, порта и пути"
[[ $(uname -s) == Linux && -d /run/systemd/system ]] || die "Нужен Linux с systemd"
for tool in docker envsubst python3 curl ip sysctl flock systemctl; do
    command -v "$tool" >/dev/null || die "Не найдена команда $tool. Зависимости: docs/install.md"
done
if ! systemctl is-active --quiet cron && ! systemctl is-active --quiet crond; then
    die "Для перечитывания сертификатов и обновлений нужен работающий cron/crond с /etc/cron.d"
fi
[[ $(docker context inspect --format '{{.Endpoints.docker.Host}}' | head -1) == unix://* && ${DOCKER_HOST:-unix://} == unix://* ]] \
    || die "Установка требует локальный сокет Docker"
docker info >/dev/null || die "Нет доступа к локальному Docker Engine"
if docker info --format '{{json .SecurityOptions}}' | grep -qw rootless; then
    die "Требуется rootful Docker для настройки host-gateway и сетевых sysctl"
fi
DC=(docker compose)
"${DC[@]}" version >/dev/null || die "Установите Docker Compose с поддержкой configs.content (см. docs/install.md)"

# Захватываем тот же lock, что cron и средства обслуживания, до проверки БД/файлов.
exec 9>/run/lock/xui-container-update.lock
flock -n 9 || die "Уже выполняется обновление стека"
[[ ! -e pending-youtube-cache-exception.json && ! -e geodata/pending.json ]] ||
    die "Ожидает применения правило Xray или восстановления транзакция geodata. Подробности: docs/operations.md"

# Читаем маршрут из ядра; внешний IPv4 и приватный адрес docker0 независимы.
route4=no; route6=no
ip -4 route get 1.1.1.1 >/dev/null 2>&1 && route4=yes
ip -6 route get 2606:4700:4700::1111 >/dev/null 2>&1 && route6=yes
_base_config=$("${DC[@]}" -f docker-compose.yml config --format json) || die "Compose не поддерживает базовую конфигурацию"
_net=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["networks"]["xray-net"]["name"])' <<< "$_base_config")
_project=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])' <<< "$_base_config")
for _svc in 3x-ui caddy certbot sslh warp hysteria; do
    if _owner=$(docker inspect --type container -f '{{index .Config.Labels "com.docker.compose.project"}}' "$_svc" 2>/dev/null); then
        [[ $_owner == "$_project" ]] || die "Имя $_svc занято другим проектом"
    fi
done
if [[ $(docker inspect --format '{{range .Mounts}}{{println .Destination}}{{end}}' 3x-ui 2>/dev/null || true) == *'/var/lib/xray/geodata'* ]]; then
    python3 -c '
import json, pathlib, sys
s = json.load(sys.stdin)["services"]["3x-ui"]
if s.get("environment", {}).get("XRAY_LOCATION_ASSET") != "/var/lib/xray/geodata/current" or not any(
        v["target"] == "/var/lib/xray/geodata" and v.get("read_only") and
        pathlib.Path(v["source"]).resolve() == pathlib.Path("geodata").resolve() for v in s.get("volumes", [])):
    sys.exit("Восстановите конфигурацию активного geodata mount перед установкой.")
' <<< "$_base_config"
elif [[ -e geodata-bootstrap-plan.json ]]; then
    die "Уже подготовлен план подключения geodata, но mount ещё не активен. Дождитесь завершения запланированного обслуживания"
fi
_existing_ipv6=$(docker network inspect --format '{{.EnableIPv6}}' "$_net" 2>/dev/null || true)
HOST_NETWORK=$(select_host_network "${HOST_NETWORK:-auto}" "$route4" "$route6" "$_existing_ipv6") || exit 1
COMPOSE_FILES=(-f docker-compose.yml)
if [[ $HOST_NETWORK != ipv4 ]]; then
    _major=$(docker version --format '{{.Server.Version}}' | cut -d. -f1)
    [[ $_major =~ ^[0-9]+$ && $_major -ge 27 ]] || die "Для IPv6 bridge без заданной подсети нужен Docker Engine 27+"
    COMPOSE_FILES+=(-f docker-compose.ipv6.yml)
fi
if [[ $_existing_ipv6 == true && $HOST_NETWORK == ipv4 || $_existing_ipv6 == false && $HOST_NETWORK != ipv4 ]]; then
    die "Сеть $_net уже существует с EnableIPv6=$_existing_ipv6. Измените bridge в отдельное окно или сохраните действующий профиль"
fi
if [[ $PROXY_MODE == warp ]]; then
    [[ $route4 == yes && $HOST_NETWORK != ipv6 ]] || die "MicroWARP требует исходящий IPv4 для WireGuard. Используйте off или достижимый vps"
    COMPOSE_FILES+=(-f docker-compose.warp.yml)
    WARP_SOCKS=${WARP_SOCKS:-auto}
    if [[ $WARP_SOCKS == auto ]]; then
        _warp_image=$(docker inspect --format '{{.Config.Image}}' warp 2>/dev/null || true)
        case "$_warp_image" in
            ''|local/microwarp-hev:0.3.1-hev2.13.1) WARP_SOCKS=hev ;;
            ghcr.io/ccbkkb/microwarp:*|ghcr.io/ccbkkb/microwarp@sha256:*) WARP_SOCKS=microsocks ;;
            *) die "Неизвестный образ warp. Выберите WARP_SOCKS явно" ;;
        esac
    fi
    [[ $WARP_SOCKS != hev ]] || COMPOSE_FILES+=(-f docker-compose.warp-udp.yml)
fi
[[ $HYSTERIA != on ]] || COMPOSE_FILES+=(-f docker-compose.hysteria.yml)
"${DC[@]}" "${COMPOSE_FILES[@]}" config -q || die "Набор Compose-файлов несовместим с установленной версией"
_selected_services=$("${DC[@]}" "${COMPOSE_FILES[@]}" config --services)
_current_services=$(docker ps -a --filter "label=com.docker.compose.project=$_project" \
    --format '{{.Label "com.docker.compose.service"}}')
while IFS= read -r _svc; do
    [[ -z $_svc ]] || grep -Fxq -- "$_svc" <<< "$_selected_services" ||
        die "В проекте есть сервис $_svc вне выбранного профиля. Сверьте .env и существующие Compose-файлы"
done <<< "$_current_services"
ok "Сеть: $HOST_NETWORK. Compose: $(IFS=' '; echo "${COMPOSE_FILES[*]}")"

CRON_FILE=/etc/cron.d/xray-cert-reload
_PROJECT_DIR=$(pwd -P)
[[ $_PROJECT_DIR != *%* && $_PROJECT_DIR != *$'\n'* ]] || die "Каталог проекта не должен содержать % или перевод строки"
_DC_UP="$(IFS=' '; echo "${DC[*]} ${COMPOSE_FILES[*]}")"
_CERT_RELOAD='docker exec caddy caddy reload --force --config /etc/caddy/Caddyfile'
[[ $HYSTERIA != on ]] || _CERT_RELOAD+=' && docker restart hysteria'
printf -v _update_command 'cd %q && %s pull --quiet --ignore-buildable && %s up -d --no-build --pull never --remove-orphans' "$_PROJECT_DIR" "$_DC_UP" "$_DC_UP"
# Keep the normal command readable in /etc/cron.d. Quote the command if it contains a quote.
_cron_command="'${_update_command//\'/\'\\\'\'}'"

_cron_candidate=$(mktemp)
cat > "$_cron_candidate" << CRON
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
TIMEZONE=${TIMEZONE}
0 4 * * * root ${_CERT_RELOAD} >> /var/log/xray-cert-reload.log 2>&1
0 3 * * 0 root /usr/bin/flock -n /run/lock/xui-container-update.lock /bin/bash -c ${_cron_command} >> /var/log/xray-image-update.log 2>&1
CRON
if [[ -f $CRON_FILE ]]; then
    # A disabled weekly task stays disabled. An active job must carry the same
    # overrides, build policy and lock, or a later run could undo the installation.
    _weekly=$(awk '!/^[[:space:]]*#/ && /docker[ -]compose/ && / up -d/ {print}' "$CRON_FILE")
    _reload=$(awk '!/^[[:space:]]*#/ && /docker exec caddy caddy reload/ {print}' "$CRON_FILE")
    if [[ -n $_weekly && ( $_weekly != *"$_DC_UP pull"* || $_weekly != *"$_DC_UP up -d"* ||
           $_weekly != *'--ignore-buildable'* || $_weekly != *'--no-build'* ||
           $_weekly != *'--pull never'* || $_weekly != *'/run/lock/xui-container-update.lock'* ) ||
           -n $_reload && ( $_reload != *'--force'* || $HYSTERIA == on && $_reload != *'restart hysteria'* ||
           $HYSTERIA == off && $_reload == *'restart hysteria'* ) ]]; then
        install -m 644 "$_cron_candidate" "${CRON_FILE}.candidate"
        rm -f "$_cron_candidate"
        die "Действующий cron несовместим с профилем. Сверьте $CRON_FILE и ${CRON_FILE}.candidate перед повторным запуском."
    fi
    rm -f "$_cron_candidate"
    _cron_candidate=""
    ok "Существующий $CRON_FILE сохранён"
fi

# Сравниваем все A/AAAA из публичного DNS, включая узлы за NAT.
CERT="certbot/conf/live/${DOMAIN}/fullchain.pem"
_dns_a=""; _dns_aaaa=""
if [[ ${SKIP_DNS_CHECK:-0} != 1 ]]; then
    _dns_err=""
    _dns_a=$(dns_records A) || _dns_err="Не удалось проверить DNS A"
    _dns_aaaa=$(dns_records AAAA) || _dns_err="Не удалось проверить DNS AAAA"
    _local_v4=$(ip -4 -o addr show scope global | awk '{split($4,a,"/"); print a[1]}')
    _local_v6=$(ip -6 -o addr show scope global | awk '{split($4,a,"/"); print a[1]}')
    if [[ -n $_dns_a && $route4 == yes ]]; then
        _public_v4=$(curl -4 -fsS -m 8 https://api.ipify.org 2>/dev/null || true)
        _local_v4+=$'\n'"${_public_v4}"
    fi
    if [[ -z $_dns_err ]]; then
        if ! _dns_err=$(check_dns_addresses "$_dns_a" "$_dns_aaaa" "$_local_v4" "$_local_v6" 2>&1); then
            _dns_err=${_dns_err:-Не удалось проверить адреса DNS}
        fi
    fi
    if [[ -n $_dns_err ]]; then
        [[ -f $CERT ]] || die "DNS: $_dns_err. Для проверенного вручную DNS: SKIP_DNS_CHECK=1 ./setup-3xui.sh"
        fail "DNS: $_dns_err. Используется существующий сертификат"
    fi
fi

modprobe tcp_bbr 2>/dev/null || true
grep -qw bbr /proc/sys/net/ipv4/tcp_available_congestion_control \
    || die "В ядре нет BBR, обязательного для sysctls Compose. Нужен хост с поддержкой BBR."
if command -v ufw >/dev/null; then
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
    [[ $HYSTERIA != on ]] || ufw allow 443/udp >/dev/null
fi
chmod 600 .env

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 2 — Директории
# ════════════════════════════════════════════════════════════════════════════

step 2 "Создание директорий"

mkdir -p certbot/{conf,www} \
         logs/{3x-ui,certbot}

# Настройки демона Docker:
#   * ограничение логов глобально (покрывает временные контейнеры certbot/nginx-init)
#   * host-gateway-ip -- адрес хоста для extra_hosts: host.docker.internal.
#     На Docker 29 автоопределение отдаёт "invalid IP", и sslh не может
#     резолвить host.docker.internal -- форвардинг SSH с 443 ломается.
_daemon_cfg=/etc/docker/daemon.json
# На хосте с внешним IPv6 sslh всё равно может обращаться к sshd через приватный IPv4.
_host_gw=$(docker network inspect -f '{{range .IPAM.Config}}{{println .Gateway}}{{end}}' bridge 2>/dev/null |
    awk '/^[0-9]+\./ && !seen++ {print}' || true)

# Python отвечает 0, если менять нечего, 10 — если нужен рестарт демона.
# host-gateway-ips (Docker 28+) равнозначен host-gateway-ip: второй не добавляем.
# log-opts max-size понимают только json-file и local — с journald/syslog
# dockerd с такой опцией не стартует.
_tmp=$(mktemp)
_daemon_rc=0
python3 - "$_daemon_cfg" "$_host_gw" > "$_tmp" << 'PY' || _daemon_rc=$?
import json, os, sys
path, gw = sys.argv[1], sys.argv[2]
d = {}
if os.path.exists(path) and os.path.getsize(path) > 0:
    d = json.load(open(path))
before = json.dumps(d, sort_keys=True)
d.setdefault('log-driver', 'json-file')
if d['log-driver'] in ('json-file', 'local'):
    opts = d.setdefault('log-opts', {})
    opts.setdefault('max-size', '10m')
    opts.setdefault('max-file', '3')
if 'host-gateway-ip' not in d and 'host-gateway-ips' not in d:
    if not gw:
        sys.exit('Нет IPv4-шлюза Docker bridge. Настройте host-gateway-ips для доступа sslh к sshd.')
    d['host-gateway-ip'] = gw
print(json.dumps(d, indent=2))
sys.exit(10 if json.dumps(d, sort_keys=True) != before else 0)
PY

case "$_daemon_rc" in
  0)
    rm -f "$_tmp"
    ok "Docker daemon.json уже настроен"
    ;;
  10)
    _had_config=false
    if [ -f "$_daemon_cfg" ]; then
        cp -p "$_daemon_cfg" "${_daemon_cfg}.bak"
        _had_config=true
    fi
    mkdir -p /etc/docker
    mv "$_tmp" "$_daemon_cfg"
    # host-gateway-ip читается только при старте демона — reload его не подхватит.
    if ! systemctl restart docker || ! docker info &>/dev/null; then
        fail "Docker не поднялся с новым ${_daemon_cfg}"
        if [ "$_had_config" = true ]; then
            mv "${_daemon_cfg}.bak" "$_daemon_cfg"
            systemctl restart docker || true
            die "Возвращён прежний ${_daemon_cfg}. Причина: journalctl -u docker -n 30"
        fi
        rm -f "$_daemon_cfg"
        systemctl restart docker || true
        die "Восстановлено отсутствие daemon.json. Причина: journalctl -u docker -n 30"
    fi
    ok "Docker: log rotation + host-gateway-ip=${_host_gw} (прежний конфиг: ${_daemon_cfg}.bak)"
    ;;
  *)
    rm -f "$_tmp"
    die "Не удалось подготовить ${_daemon_cfg}; см. ошибку выше"
    ;;
esac

chmod 700 certbot/conf
chmod 755 certbot/www logs logs/3x-ui logs/certbot

ok "Директории готовы"

# ── Сетевой тюнинг хоста ────────────────────────────────────────────────────
# BBR вместо cubic. Cubic трактует любой скачок задержки как перегрузку и режет
# окно вдвое — на мобильных сетях с джиттером в сотни миллисекунд это роняет
# скорость в разы. BBR ориентируется на измеренную пропускную способность.
# Буферы: при RTT 400 мс для 10 Мбит/с нужно ~500 КБ «в полёте», дефолтных
# 208 КБ не хватает; 8 МБ заодно покрывают запрос quic-go для HTTP/3 и Hysteria.
#
# Важно: sysctl хоста НЕ распространяется на контейнеры — у каждого свой сетевой
# namespace. Congestion control контейнеров задан в docker-compose.yml (sysctls:),
# и это критично для sslh, который терминирует внешнее TCP-соединение клиента.
_sysctl_cfg=/etc/sysctl.d/99-vpn-tuning.conf
cat > "$_sysctl_cfg" << 'SYSCTL'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.core.rmem_max = 8388608
net.core.wmem_max = 8388608
net.ipv4.tcp_rmem = 4096 131072 8388608
net.ipv4.tcp_wmem = 4096 16384 8388608
net.ipv4.tcp_mtu_probing = 1
SYSCTL

if sysctl -p "$_sysctl_cfg" >/dev/null 2>&1; then
    # default_qdisc применяется только к новым интерфейсам — существующим меняем вручную
    _iface=$({ ip -4 route show default; ip -6 route show default; } |
        awk '/^default/ && !found {for (i=1; i<NF; i++) if ($i=="dev") {print $(i+1); found=1; break}}')
    [ -n "$_iface" ] && tc qdisc replace dev "$_iface" root fq 2>/dev/null || true
    ok "Сетевой тюнинг: BBR + fq, буферы 8 МБ"
else
    fail "Не удалось применить сетевой тюнинг (нужен BBR в ядре: sysctl net.ipv4.tcp_available_congestion_control)"
fi

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 3 — Генерация конфигов из шаблонов
# ════════════════════════════════════════════════════════════════════════════

step 3 "Генерация конфигов"

[ -f config/caddy/Caddyfile ]       || die "Не найден config/caddy/Caddyfile"
[ -f config/nginx/nginx-init.conf ] || die "Не найден config/nginx/nginx-init.conf"

# shellcheck disable=SC2016  # envsubst принимает список имён переменных.
envsubst '${DOMAIN}' < config/caddy/Caddyfile > Caddyfile
ok "Caddyfile"

if [[ $route6 != yes ]]; then
    cp config/nginx/nginx-init.conf nginx-init.conf
else
    sed '/^    listen 80;/a\    listen [::]:80;' config/nginx/nginx-init.conf > nginx-init.conf
fi
ok "nginx-init.conf (временный, для certbot)"

if [ "$HYSTERIA" = "on" ]; then
    [ -f config/hysteria/config.yaml ] || die "Не найден config/hysteria/config.yaml"

    # Пароль генерируется один раз и дописывается в .env, чтобы переустановка
    # не сбрасывала доступ у уже настроенных клиентов.
    if [ -z "${HYSTERIA_PASSWORD:-}" ]; then
        HYSTERIA_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_hex(20))')
        printf '\nHYSTERIA_PASSWORD=%s\n' "$HYSTERIA_PASSWORD" >> .env
        ok "Сгенерирован HYSTERIA_PASSWORD"
    fi
    export HYSTERIA_PASSWORD

    # Quote the password as a YAML string even when it resembles a number or bool.
    (umask 077
     HYSTERIA_PASSWORD=$(python3 -c 'import json,os; print(json.dumps(os.environ["HYSTERIA_PASSWORD"]))')
     # shellcheck disable=SC2016
     envsubst '${DOMAIN} ${HYSTERIA_PASSWORD}' \
        < config/hysteria/config.yaml > config/hysteria/config.generated.yaml)
    chmod 600 config/hysteria/config.generated.yaml
    ok "config/hysteria/config.generated.yaml (пароль из .env)"
fi

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 4 — Получение SSL-сертификата
# ════════════════════════════════════════════════════════════════════════════

step 4 "SSL-сертификат"

if [[ -f $CERT ]]; then
    [[ -s certbot/conf/live/${DOMAIN}/privkey.pem ]] || die "Цепочка сертификата существует, но нет закрытого ключа"
    ok "Сертификат уже существует"
else
    _init_name=""
    cleanup_init() {
        if [[ -n $_init_name ]]; then docker rm -f "$_init_name" >/dev/null 2>&1 || true; fi
    }
    trap cleanup_init EXIT
    if [[ $(docker inspect -f '{{.State.Running}}' caddy 2>/dev/null || true) == true ]]; then
        ok "Порт 80 обслуживает запущенный Caddy"
    else
        # Host networking gives the temporary ACME listener both public families
        # independently of the daemon's default bridge or its userland proxy.
        _init_name=$(docker run -d --rm --network host \
            -v "$(pwd)/nginx-init.conf:/etc/nginx/conf.d/default.conf:ro" \
            -v "$(pwd)/certbot/www:/var/www/certbot:ro" nginx:alpine)
        _ready=false
        for _attempt in {1..10}; do
            if [[ $(docker inspect -f '{{.State.Running}}' "$_init_name" 2>/dev/null || true) != true ]]; then
                die "Временный nginx не запущен. Проверьте занятость порта 80"
            fi
            if curl --noproxy '*' -fsSm 2 http://127.0.0.1:80/ >/dev/null; then _ready=true; break; fi
            sleep 1
        done
        [[ $_ready == true ]] || die "Временный nginx не ответил на порту 80"
    fi

    docker run --rm --network host \
        -v "$(pwd)/certbot/conf:/etc/letsencrypt" \
        -v "$(pwd)/certbot/www:/var/www/certbot" \
        certbot/certbot certonly --webroot --webroot-path=/var/www/certbot \
        --non-interactive --agree-tos --email "$EMAIL" -d "$DOMAIN" \
        || die "Не удалось получить сертификат. Проверьте все A/AAAA и входящий 80/tcp"
    cleanup_init
    trap - EXIT
    ok "Сертификат получен"
fi

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 5 — Запуск стека
# ════════════════════════════════════════════════════════════════════════════

step 5 "Запуск Docker-контейнеров"

"${DC[@]}" "${COMPOSE_FILES[@]}" pull --quiet --ignore-buildable
if [[ $PROXY_MODE == warp && $WARP_SOCKS == hev ]]; then
    # The derivative is local. Preserve the running tested build on a repeat install.
    _hev_image=$("${DC[@]}" "${COMPOSE_FILES[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["warp"]["image"])')
    docker image inspect "$_hev_image" >/dev/null 2>&1 || "${DC[@]}" "${COMPOSE_FILES[@]}" build warp
fi

# COMPOSE_FILES собран выше по PROXY_MODE и HYSTERIA. Передавать его обязательно:
# --remove-orphans удалит контейнеры из override-файлов, которые не подключены.
"${DC[@]}" "${COMPOSE_FILES[@]}" up -d --no-build --pull never --remove-orphans

if command -v ufw >/dev/null; then
    # Only the actual project bridge should reach host sshd. Docker might use
    # a custom IPv4 pool outside 172.16/12 and a different bridge name.
    _id=$(docker network inspect -f '{{.Id}}' "$_net")
    _if=$(docker network inspect -f '{{index .Options "com.docker.network.bridge.name"}}' "$_net")
    [[ -n $_if && $_if != '<no value>' ]] || _if="br-${_id:0:12}"
    ufw allow in on "$_if" to any port "$SSH_PORT" proto tcp >/dev/null
fi
ok "Контейнеры запущены"

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 6 — Проверка состояния
# ════════════════════════════════════════════════════════════════════════════

step 6 "Проверка состояния"

sleep 5
"${DC[@]}" "${COMPOSE_FILES[@]}" ps

# Caddyfile смонтирован файлом, и up -d не пересоздаёт caddy, если изменился
# только он: при повторном запуске Caddy остался бы со старым конфигом.
# --force — потому что без него reload с неизменным конфигом ничего не делает,
# а сертификат для нового DOMAIN тоже нужно подхватить.
docker exec caddy caddy reload --force --config /etc/caddy/Caddyfile >/dev/null \
    || die "caddy reload не прошёл: docker logs caddy --tail 20"
ok "Caddy перечитал Caddyfile"

# Статус running сам по себе ничего не гарантирует: контейнер в цикле падений
# в момент проверки тоже running, а sslh может работать, но не пересылать SSH
# (так было с host.docker.internal на Docker 29). Поэтому смотрим, не растёт
# ли счётчик рестартов, и проверяем реальный путь через 443. Счётчик сравниваем
# во времени, а не с нулём: у давно работающего контейнера он бывает ненулевым.
_failed=0
mapfile -t _svcs <<< "$_selected_services"
declare -A _restarts
for _svc in "${_svcs[@]}"; do
    _restarts[$_svc]=$(docker inspect --format='{{.RestartCount}}' "$_svc" 2>/dev/null || echo 0)
done
sleep 10
for _svc in "${_svcs[@]}"; do
    _state=$(docker inspect --format='{{.State.Status}}' "$_svc" 2>/dev/null || echo "missing")
    _now=$(docker inspect --format='{{.RestartCount}}' "$_svc" 2>/dev/null || echo 0)
    if [ "$_state" = "running" ] && [ "$_now" = "${_restarts[$_svc]}" ]; then
        ok "$_svc — running"
    else
        fail "$_svc — ${_state}, перезапусков за 10 с: $((_now - _restarts[$_svc]))"
        docker logs --tail 15 "$_svc" 2>&1 | sed 's/^/    /' >&2
        _failed=1
    fi
done

# Probe both listeners on a dual-stack bridge. Force the requested destination
# and send SSH identification so this also checks the selected protocol backend.
_probes=(127.0.0.1)
[[ $HOST_NETWORK == ipv4 && -z $_dns_aaaa ]] || _probes+=(::1)
for _addr in "${_probes[@]}"; do
    if python3 - "$_addr" <<'PY'
import socket, sys
with socket.create_connection((sys.argv[1], 443), timeout=12) as sock:
    sock.sendall(b"SSH-2.0-XuiSetup\r\n")
    if sock.makefile("rb").read(4) != b"SSH-":
        sys.exit("Missing SSH banner")
PY
    then ok "SSH $_addr:443"; else fail "SSH $_addr:443 недоступен; сохраните прямой SSH"; _failed=1; fi
    _resolve=$_addr; [[ $_addr != *:* ]] || _resolve="[$_addr]"
    _code=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' -m 15 \
            --resolve "${DOMAIN}:443:${_resolve}" "https://${DOMAIN}/" || true)
    if [[ $_code == 200 ]]; then ok "HTTPS $_addr:443"; else fail "HTTPS $_addr:443: $_code"; _failed=1; fi
done

[ $_failed -eq 0 ] || die "Стек поднят с ошибками. Проверьте логи выше."

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 7 — Cron: подхват обновлённого сертификата и обновление образов
# ════════════════════════════════════════════════════════════════════════════

step 7 "Cron: перечитывание сертификата и обновление образов"

if [[ -n $_cron_candidate ]]; then
    install -m 644 "$_cron_candidate" "$CRON_FILE"
    rm -f "$_cron_candidate"
    ok "Создан $CRON_FILE"
fi

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 8 — Исходящий прокси XRay
# ════════════════════════════════════════════════════════════════════════════

step 8 "Исходящий прокси (PROXY_MODE=${PROXY_MODE})"

echo "Настройте inbound, policy и маршруты по docs/panel.md и docs/outbound.md."
echo "Служебные правила API и блокировки должны находиться выше общего маршрута."
if [[ $PROXY_MODE == vps ]]; then
    export VPS_ADDRESS VPS_PORT VPS_UUID VPS_SNI="${VPS_SNI:-$VPS_ADDRESS}"
    (umask 077
     # shellcheck disable=SC2016
     envsubst '${VPS_ADDRESS} ${VPS_PORT} ${VPS_UUID} ${VPS_SNI}' < config/xray/outbound-vps.json > outbound-config.json)
    chmod 600 outbound-config.json
    ok "Конфигурация выхода VPS: outbound-config.json"
elif [[ $PROXY_MODE == warp ]]; then
    ok "WARP: $WARP_SOCKS. Настройте outbound warp-out и правила панели по docs/outbound.md."
fi

# ════════════════════════════════════════════════════════════════════════════
# Итог
# ════════════════════════════════════════════════════════════════════════════

echo ""
echo -e "${MAGENTA}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${MAGENTA}║             ✓  УСТАНОВКА ЗАВЕРШЕНА УСПЕШНО             ║${NC}"
echo -e "${MAGENTA}╚════════════════════════════════════════════════════════╝${NC}"

echo ""
echo -e "${GREEN}Адреса сервисов:${NC}"
echo -e "  Сайт           https://${DOMAIN}"
echo -e "  SSH через 443  ssh user@${DOMAIN} -p 443"

echo ""
echo -e "${GREEN}Первый вход в панель (только через SSH-туннель):${NC}"
echo -e "  ${YELLOW}ssh -L 2053:localhost:2053 -p 443 user@${DOMAIN}${NC}"
echo -e "  Затем открыть: ${BLUE}http://localhost:2053${NC}"
echo "  Доступ к панели и конфигурация xHTTP /game: docs/panel.md"
echo "  Параметры клиента: python3 tools/xui-link.py '<ссылка из панели>'"
if [[ $HOST_NETWORK == ipv4 || $PROXY_MODE == warp ]]; then
    echo "  Для IPv4-выхода выбирайте IPv4 в клиенте. Публичный адрес входа задаётся DNS домена."
fi

if [ "$HYSTERIA" = "on" ]; then
    echo ""
    echo -e "${GREEN}Hysteria2 (QUIC/UDP на :443/udp):${NC}"
    echo "  Пароль находится в .env (HYSTERIA_PASSWORD). Настройка клиента: docs/hysteria.md"
fi
_dc=$(IFS=" "; echo "${DC[*]} ${COMPOSE_FILES[*]}")
echo -e "${GREEN}Полезные команды:${NC}"
echo "  ${_dc} ps                       # состояние контейнеров"
echo "  ${_dc} logs -f 3x-ui            # логи панели / XRay"
echo "  ${_dc} logs -f caddy            # логи Caddy"
echo "  ${_dc} logs -f sslh             # логи мультиплексора"
echo "  ${_dc} restart caddy            # перезапуск Caddy"
echo "  ${_dc} pull --ignore-buildable && ${_dc} up -d --no-build --pull never  # обновить образы"
echo ""
