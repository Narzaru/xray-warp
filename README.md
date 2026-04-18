> Написано с помощью [Claude Code](https://claude.ai/code)

# 3x-ui · XRay · Nginx · sslh — Docker Setup

Готовый стек для запуска XRay-прокси (VLESS) на порту 443 с фейковым сайтом-прикрытием, автоматическими SSL-сертификатами и SSH на том же порту. Одна DNS-запись, один домен — снаружи выглядит как обычный HTTPS-сайт.

## Архитектура

```
Интернет :443
    │
    ▼
 [sslh]  — мультиплексор по первым байтам пакета
    ├── SSH  ──────────────────────► host sshd (порт из .env)
    └── TLS  ──────────────────────► [3x-ui / XRay :10000]
                                          │
                                ┌─────────┴───────────┐
                                │ VLESS-трафик        │ Всё остальное
                                ▼                     ▼
                           proxy out           [nginx :8080]
                        (warp / vps / direct)   фейковый сайт
```

- **sslh** — слушает `:443`, определяет протокол по первым байтам и маршрутизирует SSH на хост, TLS — в XRay
- **XRay** (внутри 3x-ui) — терминирует TLS, обрабатывает VLESS-клиентов, некорректный трафик отдаёт nginx через fallback
- **nginx** — отдаёт фейковый сайт по plain HTTP на внутреннем порту 8080 (снаружи недоступен), а также обслуживает порт 80 для ACME-challenge certbot
- **certbot** — автоматически обновляет Let's Encrypt сертификат каждые 12 часов
- **cron** — автоматически обновляет Docker-образы раз в неделю (воскресенье, 3:00)

## Требования

- Ubuntu 24.04 LTS
- Публичный IP-адрес
- DNS: одна A-запись `yourdomain.com → IP сервера`
- Открытые порты: `80/tcp`, `443/tcp`
- Установленные пакеты: `docker`, `docker-compose-v2` (или `docker-compose`), `gettext-base`

> **UFW**: На Ubuntu 24 фаервол активен по умолчанию. Скрипт автоматически добавляет три правила (работает и на выключенном UFW — правила применятся при включении):
> ```bash
> ufw allow 80/tcp
> ufw allow 443/tcp
> ufw allow from 172.16.0.0/12 to any port <SSH_PORT> proto tcp  # Docker → host SSH
> ```
> После проверки SSH через 443 порт 22 можно закрыть: `ufw delete allow 22/tcp`

```bash
# Установка зависимостей
apt-get update
# Docker + Compose v2 (рекомендуется для Ubuntu 24)
apt-get install -y docker.io docker-compose-v2 gettext-base
# Или Compose v1 (устарел, но поддерживается скриптом):
# apt-get install -y docker.io docker-compose gettext-base
```

## Структура файлов

```
.
├── setup-3xui.sh              # скрипт установки
├── docker-compose.yml         # основной стек
├── docker-compose.warp.yml    # override для PROXY_MODE=warp
├── .env.example               # шаблон переменных
├── config/
│   ├── nginx/
│   │   ├── nginx.conf         # шаблон конфига nginx
│   │   └── nginx-init.conf    # временный конфиг для первичного certbot
│   └── xray/
│       ├── outbound-warp.json # шаблон outbound через Cloudflare WARP
│       └── outbound-vps.json  # шаблон outbound через exit VPS (VLESS)
└── html/
    └── index.html             # фейковый корпоративный сайт
```

После запуска скрипта в корне появятся:
```
├── nginx.conf                 # сгенерированный конфиг nginx
├── nginx-init.conf            # временный конфиг (можно удалить после установки)
├── certbot/                   # SSL-сертификаты
└── logs/                      # логи nginx, 3x-ui, certbot
```

## Установка

### 1. Скопировать файлы на сервер

```bash
scp -r ./x-ui user@YOUR_SERVER_IP:/opt/x-ui
ssh user@YOUR_SERVER_IP
cd /opt/x-ui
```

### 2. Настроить переменные

```bash
cp .env.example .env
nano .env
```

Обязательные поля:

```env
DOMAIN=yourdomain.com        # ваш домен (A-запись уже должна вести на этот сервер)
EMAIL=you@example.com        # email для Let's Encrypt уведомлений
SSH_PORT=22                  # порт SSH на хосте (sslh будет слушать :443 и форвардить сюда)
PROXY_MODE=off               # off | warp | vps
```

### 3. Запустить установку

```bash
chmod +x setup-3xui.sh
sudo ./setup-3xui.sh
```

Скрипт выполнит 8 шагов:
1. Проверка зависимостей
2. Создание директорий (`certbot/`, `logs/`)
3. Генерация конфигов nginx
4. Получение SSL-сертификата через Let's Encrypt (webroot)
5. Запуск Docker-контейнеров
6. Проверка состояния
7. Настройка cron: ежедневный рестарт XRay (4:00) + еженедельное обновление образов (вс 3:00)
8. Настройка исходящего прокси (если `PROXY_MODE ≠ off`)

## Настройка XRay через панель

После установки панель доступна **только через SSH-туннель** (порт 2053 не пробрасывается в интернет):

```bash
ssh -L 2053:localhost:2053 -p 443 user@yourdomain.com
```

Открыть в браузере: `http://localhost:2053`  
Логин: `admin` / Пароль: `admin` — **сразу смените в настройках панели**.

### Добавить VLESS Inbound

`Inbounds → Add Inbound`:

| Поле | Значение |
|---|---|
| Protocol | VLESS |
| Port | 10000 |
| Transmission | TCP (RAW) |
| Security | TLS |
| SNI | yourdomain.com |
| uTLS | none |
| ALPN | `http/1.1` (убрать h2, оставить только это) |
| Certificate File | `/etc/letsencrypt/live/yourdomain.com/fullchain.pem` |
| Key File | `/etc/letsencrypt/live/yourdomain.com/privkey.pem` |

В секции **Fallbacks** добавить:

| Поле | Значение |
|---|---|
| Dest | `nginx:8080` |
| xVer | `0` |

Или вставить JSON напрямую: `[{"dest":"nginx:8080","xVer":0}]`

Сохранить → **Restart XRay**.

### Подключение клиента

Параметры в клиентском приложении (v2rayN, Nekoray, Hiddify и др.):

| Поле | Значение |
|---|---|
| Address | `yourdomain.com` |
| **Port** | **443** (не 10000 — снаружи слушает sslh) |
| Protocol | VLESS |
| Security | TLS |
| SNI | `yourdomain.com` |

> ⚠️ Порт 10000 — внутренний. Клиент подключается на 443, sslh перенаправляет TLS в XRay.

## Исходящий прокси (смена выходного IP)

По умолчанию XRay выходит в интернет напрямую с IP вашего VPS. Для смены выходного IP настройте `PROXY_MODE` в `.env` до запуска скрипта.

> **Как работают файлы `config/xray/`**: `outbound-warp.json` и `outbound-vps.json` — это шаблоны. Скрипт не монтирует их в контейнер автоматически. При запуске он выводит готовый JSON в терминал — его нужно скопировать и вставить в панели вручную: **Settings → Xray Configs → Custom Config → Restart XRay**. Без этого шага XRay выходит в интернет напрямую независимо от `PROXY_MODE`.

### PROXY_MODE=warp — через Cloudflare WARP

Выходной IP становится одним из IP Cloudflare. Бесплатно.

```env
PROXY_MODE=warp
```

Скрипт запустит отдельный `warp`-контейнер и выведет JSON-конфиг. После установки в панели нужно:

**Settings → Xray Configs → Custom Config** — вставить весь JSON из вывода скрипта (или из `config/xray/outbound-warp.json`):

```json
{
  "outbounds": [
    {
      "tag": "warp-out",
      "protocol": "socks",
      "settings": {
        "servers": [{"address": "warp", "port": 1080}]
      }
    }
  ],
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [
      {
        "type": "field",
        "outboundTag": "warp-out",
        "network": "tcp,udp"
      }
    ]
  }
}
```

Сохранить → **Restart XRay**.

Проверить что трафик идёт через WARP:
```bash
docker exec warp curl -s https://ifconfig.me
# Должен вернуть IP Cloudflare, а не IP вашего VPS
```

### PROXY_MODE=vps — через exit VPS (VLESS)

Выходной IP — IP второго VPS. Быстрее WARP, стоит $3–5/мес.

```env
PROXY_MODE=vps
VPS_ADDRESS=1.2.3.4
VPS_PORT=443
VPS_UUID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
VPS_SNI=yourexit.domain.com
```

Скрипт сгенерирует файл `outbound-config.json` и выведет его содержимое — вставить в **Settings → Xray Configs → Custom Config** и нажать Restart XRay.

## Завершение

После проверки что XRay работает и SSH через 443 доступен — включите UFW и закройте порт 22:

```bash
ufw enable
ufw delete allow 22/tcp   # только если SSH через :443 уже работает
ufw status
```

## SSL-сертификат

Сертификат выпускается автоматически на шаге 4. Certbot-контейнер проверяет обновление каждые 12 часов. После обновления cron-задача перезапускает контейнер 3x-ui (XRay читает сертификат при старте):

```
# /etc/cron.d/xray-cert-reload
0 4 * * * root docker restart 3x-ui
```

> **Даунтайм**: перезапуск занимает ~30 секунд. Выбрано 4:00 ночи — минимум активных соединений. Certbot обновляет сертификат за 30 дней до истечения, ежесуточного рестарта достаточно.

## Полезные команды

```bash
# Состояние контейнеров
docker compose ps

# Логи
docker compose logs -f 3x-ui        # панель и XRay
docker compose logs -f nginx        # nginx
docker compose logs -f sslh         # мультиплексор

# Перезапуск
docker compose restart 3x-ui
docker compose restart nginx

# Обновить образы вручную (автоматически — каждое воскресенье в 3:00 через cron)
docker compose pull && docker compose up -d

# С WARP
docker compose -f docker-compose.yml -f docker-compose.warp.yml pull
docker compose -f docker-compose.yml -f docker-compose.warp.yml up -d
```

## Диагностика логов XRay

### "connection forcibly closed by remote host" / "connection aborted"

Эти ошибки в логах XRay **нормальны** и не требуют исправления. Удалённый сервер (Telegram, Microsoft CDN и др.) закрывает неактивное TCP-соединение, отправляя RST. XRay логирует это как ошибку, но это штатное поведение.

### YouTube / Telegram / Instagram не грузятся несколько секунд

**Симптом**: сайты периодически не открываются, в логах клиентского XRay видны ошибки `outbound/direct` для зарубежных IP.

**Причина**: клиентский роутинг отправляет часть трафика напрямую (`direct`), минуя прокси. ISP блокирует эти IP.

**Решение** — в настройках клиента (v2rayN, Hiddify, Nekoray и др.) проверить режим роутинга:
- Убедитесь что для Telegram, Instagram, YouTube выбран outbound `proxy`, а не `direct`
- Или переключитесь на режим **«весь трафик через proxy»** и добавьте исключение только для локальных адресов

Пример правил для клиента (добавить в proxy-список):
```
geosite:youtube
geosite:instagram
geosite:telegram
geoip:telegram
```

### "open connection ... using outbound/vless[proxy]: connection attempt failed"

VPS-прокси временно недоступен или перегружен. Если происходит часто — проверьте стабильность exit VPS.

## Troubleshooting

**Certbot не может получить сертификат**
- Проверьте DNS: `dig +short yourdomain.com` должен вернуть IP сервера
- Проверьте firewall: `ufw allow 80/tcp && ufw allow 443/tcp`
- Порт 80 не занят: `ss -tlnp | grep :80`

**Клиент не подключается**
- Убедитесь что в клиенте указан порт `443`, а не `10000`
- Проверьте что XRay запущен: `docker compose logs 3x-ui | tail -20`
- Проверьте что inbound добавлен в панели с Fallback `nginx:8080`

**Фейковый сайт не открывается**
- Должен открываться `https://yourdomain.com` — XRay принимает соединение, не распознаёт VLESS и отдаёт через fallback в nginx
- Проверьте что nginx запущен: `docker compose logs nginx`

**WARP не работает**
```bash
docker logs warp --tail 30
docker exec 3x-ui curl -x socks5://warp:1080 https://ifconfig.me
```

**Не могу подключиться по SSH после `ufw enable`**

UFW был выключен во время установки — правила не применились. Добавьте вручную (замените `22` на ваш `SSH_PORT` из `.env`):
```bash
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow from 172.16.0.0/12 to any port 22 proto tcp
ufw reload
```