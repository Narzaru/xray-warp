#!/bin/bash
# setup-3xui.sh — установка 3x-ui + XRay + Nginx + sslh в Docker
#
# Архитектура порта 443:
#   Интернет :443 → [sslh]
#                     ├── SSH  → host sshd (порт из .env)
#                     └── TLS  → [3x-ui XRay :10000]
#                                  ├── VLESS             → proxy out
#                                  └── fallback (HTTP)   → nginx :8080 (фейковый сайт)
#
# Зависимости: docker, docker-compose, envsubst (пакет gettext-base)
#
# Использование:
#   cp .env.example .env && nano .env
#   ./setup-3xui.sh

set -euo pipefail
IFS=$'\n\t'

# ── Вывод ───────────────────────────────────────────────────────────────────

GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'
RED='\033[0;31m'; MAGENTA='\033[0;35m'; NC='\033[0m'

TOTAL=8
step() { echo -e "\n${BLUE}[$1/${TOTAL}]${NC} ${YELLOW}$2${NC}"; }
ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1" >&2; }
die()  { fail "$1"; exit 1; }

# ── Баннер ──────────────────────────────────────────────────────────────────

clear
echo -e "${MAGENTA}"
cat << "BANNER"
╔════════════════════════════════════════════════════════╗
║       3x-ui · XRay · Nginx · sslh — Docker Setup      ║
║                   Ubuntu 24 Edition                    ║
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
export TIMEZONE SSH_PORT PROXY_MODE

echo -e "  Домен:       ${GREEN}${DOMAIN}${NC}"
echo -e "  Email:       ${GREEN}${EMAIL}${NC}"
echo -e "  Timezone:    ${GREEN}${TIMEZONE}${NC}"
echo -e "  SSH порт:    ${GREEN}${SSH_PORT}${NC}"
echo -e "  Прокси:      ${GREEN}${PROXY_MODE}${NC}"

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 1 — Зависимости
# ════════════════════════════════════════════════════════════════════════════

step 1 "Проверка зависимостей"

command -v docker &>/dev/null || die "Docker не найден → https://docs.docker.com/engine/install/ubuntu/"
command -v envsubst &>/dev/null || die "envsubst не найден → sudo apt-get install -y gettext-base"

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
         logs/{nginx,3x-ui,certbot}

# Ограничение логов Docker глобально (покрывает временные контейнеры certbot/nginx-init)
_daemon_cfg=/etc/docker/daemon.json
if [ ! -f "$_daemon_cfg" ] || ! grep -q '"max-size"' "$_daemon_cfg"; then
    _tmp=$(mktemp)
    if [ -f "$_daemon_cfg" ] && [ -s "$_daemon_cfg" ]; then
        # Влить в существующий JSON
        python3 -c "
import json, sys
d = json.load(open('$_daemon_cfg'))
d.setdefault('log-driver','json-file')
d.setdefault('log-opts',{}).update({'max-size':'10m','max-file':'3'})
print(json.dumps(d, indent=2))
" > "$_tmp"
    else
        echo '{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}' > "$_tmp"
    fi
    mv "$_tmp" "$_daemon_cfg"
    systemctl reload docker 2>/dev/null || true
    ok "Docker log rotation настроен (10m × 3 файла)"
else
    ok "Docker log rotation уже настроен"
fi

chmod 700 certbot/conf
chmod 755 certbot/www logs logs/nginx logs/3x-ui logs/certbot

ok "Директории готовы"

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 3 — Генерация конфигов из шаблонов
# ════════════════════════════════════════════════════════════════════════════

step 3 "Генерация конфигов"

[ -f config/nginx/nginx.conf ]      || die "Не найден config/nginx/nginx.conf"
[ -f config/nginx/nginx-init.conf ] || die "Не найден config/nginx/nginx-init.conf"

cp config/nginx/nginx.conf nginx.conf
ok "nginx.conf"

cp config/nginx/nginx-init.conf nginx-init.conf
ok "nginx-init.conf (временный, для certbot)"

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 4 — Получение SSL-сертификата
# ════════════════════════════════════════════════════════════════════════════

step 4 "SSL-сертификат"

CERT="certbot/conf/live/${DOMAIN}/fullchain.pem"

if [ -f "$CERT" ]; then
    ok "Сертификат уже существует, пропускаем"
else
    # Запустить временный nginx только с HTTP (без SSL-блоков — сертификата ещё нет)
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
        echo "  • DNS: ${DOMAIN} → IP этого сервера"
        echo "  • Firewall: ufw allow 80/tcp && ufw allow 443/tcp"
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

"${DC[@]}" pull --quiet

# Если включён WARP — подключить override-файл с warp-контейнером
if [ "$PROXY_MODE" = "warp" ]; then
    "${DC[@]}" -f docker-compose.yml -f docker-compose.warp.yml up -d --remove-orphans
else
    "${DC[@]}" up -d --remove-orphans
fi

ok "Контейнеры запущены"

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 6 — Проверка состояния
# ════════════════════════════════════════════════════════════════════════════

step 6 "Проверка состояния"

sleep 5
"${DC[@]}" ps

_failed=0
for _svc in sslh nginx 3x-ui; do
    _state=$(docker inspect --format='{{.State.Status}}' "$_svc" 2>/dev/null || echo "missing")
    if [ "$_state" = "running" ]; then
        ok "$_svc — running"
    else
        fail "$_svc — ${_state}"
        docker logs --tail 15 "$_svc" 2>&1 | sed 's/^/    /' >&2
        _failed=1
    fi
done
[ $_failed -eq 0 ] || die "Один или несколько контейнеров не запущены. Проверьте логи выше."

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 7 — Cron: перезапуск XRay после обновления сертификатов
# ════════════════════════════════════════════════════════════════════════════

step 7 "Настройка перезапуска XRay после renewal"

CRON_FILE="/etc/cron.d/xray-cert-reload"
_PROJECT_DIR="$(pwd)"
_DC_STR=$(IFS=' '; echo "${DC[*]}")
# При PROXY_MODE=warp в cron нужно передавать оба compose-файла,
# иначе --remove-orphans удалит warp-контейнер как "чужой".
if [ "$PROXY_MODE" = "warp" ]; then
    _DC_UP="${_DC_STR} -f docker-compose.yml -f docker-compose.warp.yml"
else
    _DC_UP="${_DC_STR}"
fi
cat > "$CRON_FILE" << CRON
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

# XRay читает сертификат при старте — перезапускаем контейнер после renewal.
# Certbot обновляет сертификат за 30 дней до истечения; ежесуточного рестарта достаточно.
# Запуск в 4:00 ночи — минимум активных соединений, ~30 сек даунтайма.
0 4 * * * root docker restart 3x-ui >> /var/log/xray-cert-reload.log 2>&1

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

case "${PROXY_MODE}" in

  off)
    ok "Прокси отключён — XRay выходит напрямую в интернет"
    ;;

  warp)
    ok "WARP-контейнер запущен (поднят на шаге 5)"

    echo ""
    echo -e "${YELLOW}Вставьте в панели: Настройки → XRay Configs → Custom Config:${NC}"
    echo ""
    cat config/xray/outbound-warp.json
    echo ""
    echo -e "${YELLOW}После сохранения нажмите «Restart XRay» в панели.${NC}"
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
    echo -e "${YELLOW}Вставьте в панели: Настройки → XRay Configs → Custom Config:${NC}"
    echo ""
    cat outbound-config.json
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
echo -e "${GREEN}Настройка XRay Inbound в панели:${NC}"
echo "  Protocol:     VLESS"
echo "  Port:         10000"
echo "  Certificate:  /etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
echo "  Key:          /etc/letsencrypt/live/${DOMAIN}/privkey.pem"
echo ""
echo -e "${GREEN}Вариант A — xHTTP (рекомендуется, обходит DPI):${NC}"
echo "  Transmission: xHTTP"
echo "  Path:         /game  (или любой другой)"
echo "  Host:         ${DOMAIN}"
echo "  TLS:          включить,  uTLS: firefox,  ALPN: h2, http/1.1"
echo "  SNI клиента:  ${DOMAIN}"
echo ""
echo -e "${GREEN}Вариант B — TCP/RAW (проще, но хуже обходит блокировки):${NC}"
echo "  Transmission: TCP (RAW)"
echo "  TLS:          включить,  uTLS: none,  ALPN: http/1.1"
echo "  SNI клиента:  ${DOMAIN}"
echo "  Fallback:     [{\"dest\":\"nginx:8080\",\"xVer\":0}]"

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
_dc=$(IFS=' '; echo "${DC[*]}")
echo -e "${GREEN}Полезные команды:${NC}"
echo "  ${_dc} ps                       # состояние контейнеров"
echo "  ${_dc} logs -f 3x-ui            # логи панели / XRay"
echo "  ${_dc} logs -f nginx            # логи nginx"
echo "  ${_dc} logs -f sslh             # логи мультиплексора"
echo "  ${_dc} restart nginx            # перезапуск nginx"
echo "  ${_dc} pull && ${_dc} up -d     # обновить образы"
echo ""
