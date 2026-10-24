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
# Зависимости: docker, docker-compose, envsubst (пакет gettext-base)
#
# Использование:
#   cp .env.example .env && nano .env
#   ./setup-3xui.sh

set -euo pipefail
IFS=$'\n\t'

# ── Отвязка от терминала ────────────────────────────────────────────────────
# SSH-сессия через 443 идёт сквозь sslh, а скрипт сам его перезапускает: шаг 2
# рестартует dockerd, шаг 5 пересоздаёт контейнеры. Обрыв сессии убивал скрипт
# на середине — в худшем случае новый sslh оставался в состоянии created, и SSH
# через 443 пропадал совсем. Поэтому скрипт перезапускает себя в отдельной
# сессии, а в терминале только показывает лог: обрыв SSH убьёт tail, не установку.
if [ -z "${XUI_DETACHED:-}" ] && [ -t 1 ]; then
    _log="$(pwd)/setup.log"
    XUI_DETACHED=1 setsid nohup bash "$0" "$@" > "$_log" 2>&1 < /dev/null &
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
║                  Ubuntu 24.04 / 26.04                  ║
╚════════════════════════════════════════════════════════╝
BANNER
echo -e "${NC}"

# ── Загрузка .env ────────────────────────────────────────────────────────────

[ "$EUID" -eq 0 ] || die "Запустите скрипт от root: sudo ./setup-3xui.sh"

if [ ! -f .env ]; then
    [ -f .env.example ] || die "Запустите скрипт из корневой директории проекта (не найден .env.example)"
    cp .env.example .env
    echo -e "${YELLOW}Файл .env создан из шаблона. Заполните его и запустите скрипт повторно:${NC}"
    echo -e "  ${BLUE}nano .env${NC}"
    exit 0
fi

# .env, скопированный с Windows, приходит с CRLF: значения получают хвостовой \r
# (DOMAIN=example.com\r), и certbot, envsubst и compose ломаются невнятно.
sed -i 's/\r$//' .env

# shellcheck source=/dev/null
source .env

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

# Список compose-файлов собирается один раз и используется везде: при запуске,
# в cron и в итоговой справке. Иначе --remove-orphans снесёт контейнеры,
# объявленные в неподключённых override-файлах.
COMPOSE_FILES=(-f docker-compose.yml)
if [ "$PROXY_MODE" = "warp" ]; then
    COMPOSE_FILES+=(-f docker-compose.warp.yml)
fi
if [ "$HYSTERIA" = "on" ]; then
    COMPOSE_FILES+=(-f docker-compose.hysteria.yml)
fi

echo -e "  Домен:       ${GREEN}${DOMAIN}${NC}"
echo -e "  Email:       ${GREEN}${EMAIL}${NC}"
echo -e "  Timezone:    ${GREEN}${TIMEZONE}${NC}"
echo -e "  SSH порт:    ${GREEN}${SSH_PORT}${NC}"
echo -e "  Прокси:      ${GREEN}${PROXY_MODE}${NC}"
echo -e "  Hysteria2:   ${GREEN}${HYSTERIA}${NC}"

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 1 — Зависимости
# ════════════════════════════════════════════════════════════════════════════

step 1 "Проверка зависимостей"

command -v docker &>/dev/null || die "Docker не найден → https://docs.docker.com/engine/install/ubuntu/"
command -v envsubst &>/dev/null || die "envsubst не найден → sudo apt-get install -y gettext-base"
command -v python3 &>/dev/null || die "python3 не найден → sudo apt-get install -y python3"
command -v curl &>/dev/null || die "curl не найден → sudo apt-get install -y curl"
# `docker compose version` работает и без демона, поэтому проверяем его отдельно:
# иначе скрипт молча умирал на шаге 2 при чтении адреса docker0.
docker info &>/dev/null || die "Демон Docker не запущен → systemctl enable --now docker"

# Поддержка docker-compose v1 (docker-compose) и v2 (docker compose).
# v2 проверяется первым: v1.29.2 несовместим с Docker Engine 25+ (KeyError: ContainerConfig).
if docker compose version &>/dev/null; then
    DC=(docker compose)
    _dc_ver=$(docker compose version --short 2>/dev/null)
elif command -v docker-compose &>/dev/null; then
    DC=(docker-compose)
    _dc_ver=$(docker-compose version --short 2>/dev/null \
              || docker-compose --version | awk '{print $3}' | tr -d ',')
    # docker-compose v1 не работает с Docker 25+ — предупредить и прервать
    _docker_major=$(docker version --format '{{.Server.Version}}' 2>/dev/null | cut -d. -f1)
    if [ "${_docker_major:-0}" -ge 25 ]; then
        die "docker-compose v1 несовместим с Docker Engine ${_docker_major}+.
  Установите Compose v2: apt-get install -y docker-compose-v2
  Затем запустите скрипт повторно."
    fi
else
    die "docker-compose / docker compose не найден.
  Ubuntu 24 (рекомендуется): apt-get install -y docker-compose-v2
  Ubuntu 24 (альтернатива):  apt-get install -y docker-compose"
fi

ok "docker $(docker --version | awk '{print $3}' | tr -d ',')"
ok "$(IFS=' '; echo "${DC[*]}") ${_dc_ver}"
ok "envsubst $(envsubst --version 2>&1 | head -1)"

# UFW: прописываем правила независимо от того, включён UFW или нет.
# ufw allow работает и на выключенном UFW — правила сохраняются и применятся при включении.
# sslh форвардит SSH-трафик с :443 в host.docker.internal:SSH_PORT через Docker-мост;
# без правила 172.16.0.0/12 UFW блокирует это соединение.
if command -v ufw &>/dev/null; then
    ufw allow 80/tcp   >/dev/null
    ufw allow 443/tcp  >/dev/null
    ufw allow from 172.16.0.0/12 to any port "${SSH_PORT}" proto tcp >/dev/null

    if ufw status 2>/dev/null | grep -q "Status: active"; then
        ok "ufw: правила для 80/tcp, 443/tcp и Docker→SSH:${SSH_PORT} применены"
    else
        ok "ufw: правила добавлены (UFW выключен — применятся при включении)"
    fi
fi

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
# || true: без docker0 ip завершается с ошибкой, и pipefail + set -e молча
# оборвали бы скрипт. Подставлять дефолт 172.17.0.1 тоже нельзя — без docker0
# он заведомо неверен, и SSH через 443 сломается уже после установки.
_host_gw=$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)
[ -n "$_host_gw" ] || die "Не найден IPv4-адрес интерфейса docker0 (\"bridge\": \"none\" в ${_daemon_cfg}?)"

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
    [ -f "$_daemon_cfg" ] && cp "$_daemon_cfg" "${_daemon_cfg}.bak"
    mv "$_tmp" "$_daemon_cfg"
    # host-gateway-ip читается только при старте демона — reload его не подхватит.
    if ! systemctl restart docker || ! docker info &>/dev/null; then
        fail "Docker не поднялся с новым ${_daemon_cfg}"
        if [ -f "${_daemon_cfg}.bak" ]; then
            mv "${_daemon_cfg}.bak" "$_daemon_cfg"
            systemctl restart docker || true
            die "Возвращён прежний ${_daemon_cfg}. Причина: journalctl -u docker -n 30"
        fi
        die "Причина: journalctl -u docker -n 30"
    fi
    ok "Docker: log rotation + host-gateway-ip=${_host_gw} (прежний конфиг: ${_daemon_cfg}.bak)"
    ;;
  *)
    rm -f "$_tmp"
    die "Не удалось разобрать ${_daemon_cfg} — проверьте, что это валидный JSON"
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
# Без BBR в ядре стек не поднимется вовсе: sysctls: в docker-compose.yml у sslh,
# caddy и 3x-ui падают с invalid argument. Лучше остановиться здесь, до запуска.
modprobe tcp_bbr 2>/dev/null || true
grep -qw bbr /proc/sys/net/ipv4/tcp_available_congestion_control \
    || die "В ядре нет BBR (tcp_available_congestion_control: $(cat /proc/sys/net/ipv4/tcp_available_congestion_control)). На LXC/OpenVZ уберите sysctls из docker-compose.yml."

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
    _iface=$(ip route show default | awk '/default/ {print $5; exit}')
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

envsubst '${DOMAIN}' < config/caddy/Caddyfile > Caddyfile
ok "Caddyfile"

cp config/nginx/nginx-init.conf nginx-init.conf
ok "nginx-init.conf (временный, для certbot)"

if [ "$HYSTERIA" = "on" ]; then
    [ -f config/hysteria/config.yaml ] || die "Не найден config/hysteria/config.yaml"

    # Пароль генерируется один раз и дописывается в .env, чтобы переустановка
    # не сбрасывала доступ у уже настроенных клиентов.
    if [ -z "${HYSTERIA_PASSWORD:-}" ]; then
        HYSTERIA_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 28)
        printf 'HYSTERIA_PASSWORD=%s\n' "$HYSTERIA_PASSWORD" >> .env
        ok "Сгенерирован HYSTERIA_PASSWORD"
    fi
    export HYSTERIA_PASSWORD

    envsubst '${DOMAIN} ${HYSTERIA_PASSWORD}' \
        < config/hysteria/config.yaml > config/hysteria/config.generated.yaml
    chmod 600 config/hysteria/config.generated.yaml
    ok "config/hysteria/config.generated.yaml (пароль из .env)"
fi

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 4 — Получение SSL-сертификата
# ════════════════════════════════════════════════════════════════════════════

step 4 "SSL-сертификат"

# ── DNS: A и AAAA должны вести на этот сервер ───────────────────────────────
# Let's Encrypt при наличии AAAA проверяет домен сначала по IPv6 и на IPv4 не
# откатывается, если по IPv6 ответил чужой сервер. AAAA, оставшаяся от старого
# хоста, даёт невнятный отказ certbot, а после установки уводит IPv6-клиентов
# не туда. Записи берём из публичного DNS (DoH), а не через getent: тот читает
# /etc/hosts, где cloud-init нередко прописывает домен как 127.0.1.1, а для
# AAAA при её отсутствии возвращает v4-mapped адрес ::ffff:x.x.x.x.
dns_records() {   # $1 = A | AAAA
    curl -fsS -m 10 "https://dns.google/resolve?name=${DOMAIN}&type=$1" 2>/dev/null \
        | python3 -c "
import json, sys
t = {'A': 1, 'AAAA': 28}['$1']
for a in json.load(sys.stdin).get('Answer', []):
    if a.get('type') == t: print(a['data'])
" 2>/dev/null || true
}

SERVER_IP4=$(curl -4 -fsS -m 10 https://api.ipify.org 2>/dev/null || true)
_dns_a=$(dns_records A)
_dns_aaaa=$(dns_records AAAA)
_local_v6=$(ip -6 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)

_dns_err=""
if [ -z "$_dns_a" ]; then
    _dns_err="у ${DOMAIN} нет A-записи (или DoH недоступен)"
elif [ -n "$SERVER_IP4" ] && ! grep -qxF "$SERVER_IP4" <<< "$_dns_a"; then
    _dns_err="A-запись ${DOMAIN} → $(echo $_dns_a), а IPv4 сервера ${SERVER_IP4}"
fi
while read -r _aaaa; do
    [ -n "$_aaaa" ] || continue
    if ! grep -qxF "$_aaaa" <<< "$_local_v6"; then
        _dns_err="${_dns_err:+${_dns_err}; }AAAA-запись ${DOMAIN} → ${_aaaa}, но такого IPv6 на сервере нет. Удалите AAAA или пропишите адрес из: ip -6 addr show scope global"
    fi
done <<< "$_dns_aaaa"

[ -n "$_dns_a" ] && ok "A:    $(echo $_dns_a)"
[ -n "$_dns_aaaa" ] && ok "AAAA: $(echo $_dns_aaaa)"

CERT="certbot/conf/live/${DOMAIN}/fullchain.pem"

if [ -n "$_dns_err" ]; then
    # С готовым сертификатом это не блокер, но клиентов собьёт с толку.
    # SKIP_DNS_CHECK=1 — для DNS за прокси/GeoDNS, где проверка заведомо врёт.
    if [ -f "$CERT" ] || [ "${SKIP_DNS_CHECK:-0}" = "1" ]; then
        fail "DNS: ${_dns_err}"
    else
        die "DNS: ${_dns_err}. Проверку можно отключить: SKIP_DNS_CHECK=1 ./setup-3xui.sh"
    fi
fi

if [ -f "$CERT" ]; then
    ok "Сертификат уже существует, пропускаем"
else
    # Порт 80 для ACME-challenge. Если стек уже работает (повторный запуск со
    # сменой DOMAIN), его держит Caddy и сам отдаёт /.well-known/acme-challenge
    # из certbot/www — временный nginx тогда не нужен и не смог бы занять порт.
    if [ "$(docker inspect -f '{{.State.Running}}' caddy 2>/dev/null || echo false)" = "true" ]; then
        ok "Порт 80 обслуживает запущенный Caddy"
    else
        # Временный nginx только с HTTP (без SSL-блоков — сертификата ещё нет)
        echo "  Запуск временного nginx для webroot..."
        docker stop nginx-certbot-init 2>/dev/null || true
        if ! docker run -d --rm --name nginx-certbot-init \
            -p 80:80 \
            -v "$(pwd)/nginx-init.conf:/etc/nginx/conf.d/default.conf:ro" \
            -v "$(pwd)/certbot/www:/var/www/certbot" \
            nginx:alpine; then
            die "Не удалось запустить nginx на порту 80. Проверьте: ss -tlnp | grep :80"
        fi

        # Подождать, пока nginx полностью поднимется (bash /dev/tcp — без внешних зависимостей)
        _retries=10
        until bash -c 'echo >/dev/tcp/localhost/80' 2>/dev/null || [ $_retries -eq 0 ]; do
            sleep 1; _retries=$((_retries - 1))
        done
        [ $_retries -gt 0 ] || { docker stop nginx-certbot-init 2>/dev/null || true; die "nginx не ответил на порту 80 за 10 секунд. Проверьте: ss -tlnp | grep :80"; }
        ok "nginx слушает порт 80"
    fi

    echo "  Запрос сертификата для: ${DOMAIN}"

    if ! docker run --rm \
        -v "$(pwd)/certbot/conf:/etc/letsencrypt" \
        -v "$(pwd)/certbot/www:/var/www/certbot" \
        certbot/certbot certonly \
            --webroot \
            --webroot-path=/var/www/certbot \
            --non-interactive \
            --agree-tos \
            --no-eff-email \
            --email "${EMAIL}" \
            -d "${DOMAIN}"; then

        docker stop nginx-certbot-init 2>/dev/null || true
        fail "Не удалось получить сертификат"
        echo ""
        echo -e "${YELLOW}Проверьте:${NC}"
        echo "  • DNS: A (и AAAA, если есть) ${DOMAIN} → этот сервер"
        echo "  • Firewall хостера и ufw: открыт 80/tcp"
        echo "  • Порт 80 не занят другим процессом: ss -tlnp | grep :80"
        exit 1
    fi

    docker stop nginx-certbot-init 2>/dev/null || true

    # certbot сохраняет метод получения в renewal-конфиге.
    # Меняем authenticator на webroot — иначе renewal-контейнер попытается
    # использовать standalone и упрётся в запущенный nginx.
    RENEWAL="certbot/conf/renewal/${DOMAIN}.conf"
    if [ -f "$RENEWAL" ]; then
        sed -i 's/^authenticator = .*/authenticator = webroot/' "$RENEWAL"
        grep -q 'webroot_path' "$RENEWAL" \
            || echo "webroot_path = /var/www/certbot" >> "$RENEWAL"
        ok "Renewal-конфиг переключён на webroot"
    fi

    ok "Сертификат получен"
fi

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 5 — Запуск стека
# ════════════════════════════════════════════════════════════════════════════

step 5 "Запуск Docker-контейнеров"

# Убедиться, что порт 80 свободен
docker stop nginx-certbot-init 2>/dev/null || true

"${DC[@]}" "${COMPOSE_FILES[@]}" pull --quiet

# COMPOSE_FILES собран выше по PROXY_MODE и HYSTERIA. Передавать его обязательно:
# --remove-orphans удалит контейнеры из override-файлов, которые не подключены.
"${DC[@]}" "${COMPOSE_FILES[@]}" up -d --remove-orphans

ok "Контейнеры запущены"

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 6 — Проверка состояния
# ════════════════════════════════════════════════════════════════════════════

step 6 "Проверка состояния"

sleep 5
"${DC[@]}" ps

# Caddyfile смонтирован файлом, и up -d не пересоздаёт caddy, если изменился
# только он: при повторном запуске Caddy остался бы со старым конфигом.
# --force — потому что без него reload с неизменным конфигом ничего не делает,
# а сертификат для нового DOMAIN тоже нужно подхватить.
docker exec caddy caddy reload --force --config /etc/caddy/Caddyfile >/dev/null 2>&1 \
    && ok "Caddy перечитал Caddyfile" \
    || fail "caddy reload не прошёл: docker logs caddy --tail 20"

# Статус running сам по себе ничего не гарантирует: контейнер в цикле падений
# в момент проверки тоже running, а sslh может работать, но не пересылать SSH
# (так было с host.docker.internal на Docker 29). Поэтому смотрим, не растёт
# ли счётчик рестартов, и проверяем реальный путь через 443. Счётчик сравниваем
# во времени, а не с нулём: у давно работающего контейнера он бывает ненулевым.
_failed=0
_svcs=(sslh caddy 3x-ui)
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

# SSH через 443: клиент молчит, sslh по таймауту отдаёт соединение в ssh,
# и должен прийти баннер sshd.
_banner=$(timeout 15 bash -c 'exec 3<>/dev/tcp/127.0.0.1/443; head -c 4 <&3' 2>/dev/null || true)
if [ "$_banner" = "SSH-" ]; then
    ok "SSH через 443 отвечает"
else
    fail "SSH через 443 не отвечает — не закрывайте порт ${SSH_PORT}. Логи: docker logs sslh --tail 20"
    _failed=1
fi

# TLS через 443: sslh → Caddy, сертификат валиден для DOMAIN, сайт отдаёт 200.
_code=$(curl -sS -o /dev/null -w '%{http_code}' -m 15 \
        --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/" 2>/dev/null || true)
if [ "$_code" = "200" ]; then
    ok "HTTPS через 443: сайт отвечает, сертификат валиден"
else
    fail "HTTPS через 443: ответ '${_code:-нет}'. Проверьте: curl -v --resolve ${DOMAIN}:443:127.0.0.1 https://${DOMAIN}/"
    _failed=1
fi

[ $_failed -eq 0 ] || die "Стек поднят с ошибками. Проверьте логи выше."

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 7 — Cron: подхват обновлённого сертификата и обновление образов
# ════════════════════════════════════════════════════════════════════════════

step 7 "Cron: перечитывание сертификата и обновление образов"

CRON_FILE="/etc/cron.d/xray-cert-reload"
_PROJECT_DIR="$(pwd)"
_DC_UP="$(IFS=' '; echo "${DC[*]} ${COMPOSE_FILES[*]}")"

# Сертификат читает Caddy (он терминирует TLS), а не XRay — у XRay security: none.
# --force обязателен: Caddyfile после продления тот же, и без него Caddy пишет
# «config is unchanged» и продолжает отдавать старый сертификат до истечения.
# reload перечитывает сертификат без разрыва соединений.
_CERT_RELOAD='docker exec caddy caddy reload --force --config /etc/caddy/Caddyfile'
if [ "$HYSTERIA" = "on" ]; then
    # Hysteria2 читает сертификат при старте, поэтому её нужно перезапустить.
    _CERT_RELOAD="${_CERT_RELOAD} && docker restart hysteria"
fi

cat > "$CRON_FILE" << CRON
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
# compose из cron видит только .env, а TIMEZONE скрипт мог взять из системы:
# без этой строки недельное обновление пересоздавало бы 3x-ui с UTC.
TIMEZONE=${TIMEZONE}

# Certbot обновляет сертификат за 30 дней до истечения; ежесуточной перезагрузки достаточно.
# Запуск в 4:00 ночи — минимум активных соединений.
0 4 * * * root ${_CERT_RELOAD} >> /var/log/xray-cert-reload.log 2>&1

# Автообновление Docker-образов
# Запуск раз в неделю в воскресенье в 3:00 ночи.
0 3 * * 0 root cd "${_PROJECT_DIR}" && ${_DC_UP} pull --quiet && ${_DC_UP} up -d --remove-orphans >> /var/log/xray-image-update.log 2>&1
CRON
chmod 644 "$CRON_FILE"
ok "Создан $CRON_FILE"

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 8 — Исходящий прокси XRay
# ════════════════════════════════════════════════════════════════════════════

step 8 "Исходящий прокси (PROXY_MODE=${PROXY_MODE})"

# connIdle по умолчанию 300 с: XRay рвёт соединение, по которому 5 минут не
# шло ни байта. Стриминговые API (LLM, SSE) молчат дольше, пока генерируют
# большой ответ, и обрываются на середине. Нужно при любом PROXY_MODE.
echo ""
echo -e "${YELLOW}0. Настройки → XRay Configs → Advanced — замените блок policy на:${NC}"
echo ""
cat config/xray/policy.json
echo ""

case "${PROXY_MODE}" in

  off)
    ok "Прокси отключён — XRay выходит напрямую в интернет"
    ;;

  warp)
    ok "WARP-контейнер запущен (поднят на шаге 5)"

    echo ""
    echo -e "${YELLOW}1. Настройки → XRay Configs → Custom Config — вставьте:${NC}"
    echo ""
    cat config/xray/outbound-warp.json
    echo ""
    echo -e "${YELLOW}2. Настройки → XRay Configs → Routing Rules — добавьте правило:${NC}"
    echo ""
    cat config/xray/routing-warp.json
    echo ""
    echo -e "${YELLOW}3. Туда же — запасное правило, ПОД правилом warp:${NC}"
    echo ""
    cat config/xray/routing-direct.json
    echo ""
    echo -e "${YELLOW}Пока оно ниже, оно не срабатывает: warp перехватывает весь${NC}"
    echo -e "${YELLOW}tcp,udp раньше. Если WARP сломается — поднимите его выше${NC}"
    echo -e "${YELLOW}правила warp, и трафик пойдёт напрямую без правки конфигов.${NC}"
    echo ""
    echo -e "${YELLOW}После сохранения нажмите «Restart XRay» в панели.${NC}"
    echo -e "${YELLOW}Правила обязаны лежать плоско в routing.rules: если вставить${NC}"
    echo -e "${YELLOW}блок routing целиком в Custom Config, панель вложит его внутрь${NC}"
    echo -e "${YELLOW}rules, и XRay не стартует (exit status 23).${NC}"
    ;;

  vps)
    VPS_ADDRESS="${VPS_ADDRESS:?Переменная VPS_ADDRESS не задана в .env}"
    VPS_PORT="${VPS_PORT:-443}"
    VPS_UUID="${VPS_UUID:?Переменная VPS_UUID не задана в .env}"
    VPS_SNI="${VPS_SNI:-${VPS_ADDRESS}}"
    export VPS_ADDRESS VPS_PORT VPS_UUID VPS_SNI

    envsubst '${VPS_ADDRESS} ${VPS_PORT} ${VPS_UUID} ${VPS_SNI}' \
        < config/xray/outbound-vps.json > outbound-config.json
    ok "Конфиг сгенерирован → outbound-config.json"

    echo ""
    echo -e "${YELLOW}1. Настройки → XRay Configs → Custom Config — вставьте:${NC}"
    echo ""
    cat outbound-config.json
    echo ""
    echo -e "${YELLOW}2. Настройки → XRay Configs → Routing Rules — добавьте правило:${NC}"
    echo ""
    cat config/xray/routing-vps.json
    echo ""
    echo -e "${YELLOW}3. Туда же — запасное правило, ПОД правилом vps:${NC}"
    echo ""
    cat config/xray/routing-direct.json
    echo ""
    echo -e "${YELLOW}Если exit VPS отвалится — поднимите его выше, и трафик${NC}"
    echo -e "${YELLOW}пойдёт напрямую.${NC}"
    echo ""
    echo -e "${YELLOW}После сохранения нажмите «Restart XRay» в панели.${NC}"
    ;;

  *)
    fail "Неизвестный PROXY_MODE=${PROXY_MODE} (допустимые значения: off, warp, vps)"
    exit 1
    ;;
esac

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
echo -e "  Логин: ${YELLOW}admin${NC}   Пароль: ${YELLOW}admin${NC}  ${RED}← сразу смените!${NC}"

echo ""
# IPv4 сервера — для подсказки по dns.hosts в клиентском конфиге. Не getent:
# он вернул бы AAAA или 127.0.1.1 из /etc/hosts, а нужен именно внешний IPv4.
DOMAIN_IP="${SERVER_IP4:-$(head -1 <<< "$_dns_a")}"
[ -n "$DOMAIN_IP" ] || DOMAIN_IP="<ip сервера>"

echo -e "${GREEN}Настройка XRay Inbound в панели:${NC}"
echo "  Protocol:     VLESS"
echo "  Port:         10000"
echo -e "  Security:     ${YELLOW}None${NC}  — TLS снимает Caddy, не XRay."
echo "                Пути к сертификатам в панели указывать не нужно."
echo ""
echo -e "${GREEN}Вариант A — xHTTP (рекомендуется, обходит DPI):${NC}"
echo "  Transmission: xHTTP"
echo "  Path:         /game  (или любой другой)"
echo "  Host:         ${DOMAIN}"
echo "  Mode:         auto   — режим выбирает клиент"
echo ""
echo -e "  ${YELLOW}Ссылку из панели импортировать нельзя:${NC} она содержит внутренний"
echo -e "  ${YELLOW}порт 10000 и security=none. Приведите её к рабочему виду:${NC}"
echo "    python3 tools/xui-link.py '<ссылка из панели>'"
echo "  Ссылка уже содержит xmux (параметр extra). JSON нужен, только если"
echo "  хотите прописать IP в dns.hosts: --ip ${DOMAIN_IP}"
echo ""
# Docker-сеть стека без IPv6: XRay не может открыть соединение на IPv6-адрес.
# Клиент с IPv6 в TUN шлёт такие адреса в туннель, а приложение не откатывается
# на IPv4, потому что TUN уже принял соединение локально, — сайты висят.
echo -e "  ${YELLOW}В клиенте выключите IPv6 (v2rayNG: «Enable IPv6» — выкл.):${NC}"
echo -e "  ${YELLOW}выход в интернет у сервера только по IPv4.${NC}"
echo ""
echo -e "${GREEN}Вариант B — TCP/RAW (проще, но хуже обходит блокировки):${NC}"
echo "  Transmission: TCP (RAW)"
echo "  TLS:          включить,  uTLS: none,  ALPN: http/1.1"
echo "  SNI клиента:  ${DOMAIN}"
echo "  Fallback:     [{\"dest\":\"caddy:8080\",\"xVer\":0}]"

if [ "$PROXY_MODE" != "off" ]; then
    echo ""
    echo -e "${GREEN}Исходящий прокси:${NC}"
    case "$PROXY_MODE" in
      warp) echo "  Маршрут: XRay → WARP → интернет" ;;
      vps)  echo "  Маршрут: XRay → VPS (${VPS_ADDRESS}:${VPS_PORT}) → интернет" ;;
    esac
    echo -e "  ${YELLOW}Не забудьте вставить Custom Config и перезапустить XRay в панели!${NC}"
fi

echo ""

if [ "$HYSTERIA" = "on" ]; then
    echo ""
    echo -e "${GREEN}Hysteria2 (QUIC/UDP на :443/udp):${NC}"
    echo -e "  ${YELLOW}v2rayNG этот протокол не поддерживает — нужен Hiddify, NekoBox или sing-box.${NC}"
    echo ""
    echo "  Ссылка для импорта (общая на всех клиентов):"
    echo "    hy2://${HYSTERIA_PASSWORD}@${DOMAIN}:443/?sni=${DOMAIN}#${DOMAIN}-hy2"
    echo ""
    echo -e "  ${YELLOW}В клиенте задайте полосу (Upload/Download Mbps) чуть ниже реальной${NC}"
    echo -e "  ${YELLOW}скорости канала — иначе не включится Brutal и смысл транспорта теряется.${NC}"
    echo -e "  ${YELLOW}Выставьте в клиенте IPv6 Mode = Disable: выход у сервера только по IPv4.${NC}"
fi
_dc=$(IFS=" "; echo "${DC[*]} ${COMPOSE_FILES[*]}")
echo -e "${GREEN}Полезные команды:${NC}"
echo "  ${_dc} ps                       # состояние контейнеров"
echo "  ${_dc} logs -f 3x-ui            # логи панели / XRay"
echo "  ${_dc} logs -f caddy            # логи Caddy"
echo "  ${_dc} logs -f sslh             # логи мультиплексора"
echo "  ${_dc} restart caddy            # перезапуск Caddy"
echo "  ${_dc} pull && ${_dc} up -d     # обновить образы"
echo ""
