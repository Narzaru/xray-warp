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
# С локальной машины — из директории проекта
scp -r . user@YOUR_SERVER_IP:/opt/x-ui
ssh user@YOUR_SERVER_IP
cd /opt/x-ui
```

> **Важно**: скрипт должен запускаться из директории проекта, где лежат `config/`, `docker-compose.yml` и т.д. Если запустить из другой директории — конфиги не найдутся и монтирование в Docker сломается.

> **Windows**: если файлы скопированы с Windows-машины, а не через `git clone`, скрипт может содержать CRLF-переносы строк. Исправить: `sed -i 's/\r//' setup-3xui.sh`

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
# После полной настройки — через порт 443 (sslh мультиплексирует SSH и TLS)
ssh -L 2053:localhost:2053 -p 443 user@yourdomain.com

# При первичной настройке (если sslh ещё не работает) — через стандартный SSH-порт
ssh -L 2053:localhost:2053 user@yourdomain.com
```

Открыть в браузере: `http://localhost:2053`  
Логин: `admin` / Пароль: `admin` — **сразу смените в настройках панели**.

### Добавить VLESS Inbound

`Inbounds → Add Inbound`. Общие параметры для обоих вариантов:

| Поле | Значение |
|---|---|
| Protocol | VLESS |
| Port | 10000 |
| Security | TLS |
| Certificate File | `/etc/letsencrypt/live/yourdomain.com/fullchain.pem` |
| Key File | `/etc/letsencrypt/live/yourdomain.com/privkey.pem` |

#### Вариант A — xHTTP (рекомендуется, лучше обходит DPI)

| Поле | Значение |
|---|---|
| Transmission | xHTTP |
| Path | `/game` (или любой другой) |
| Host | `yourdomain.com` |
| uTLS | `firefox` (или `chrome`) |
| ALPN | `h2`, `http/1.1` |
| SNI клиента | `yourdomain.com` |

#### Вариант B — TCP/RAW (проще)

| Поле | Значение |
|---|---|
| Transmission | TCP (RAW) |
| uTLS | none |
| ALPN | `http/1.1` (убрать h2) |
| SNI клиента | `yourdomain.com` |

В секции **Fallbacks** добавить (только для TCP/RAW — при xHTTP fallback не нужен):

| Поле | Значение |
|---|---|
| Dest | `nginx:8080` |
| xVer | `0` |

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

# Сколько места занимают логи Docker
du -sh /var/lib/docker/containers/*/
```

> **Ротация логов**: скрипт автоматически настраивает `/etc/docker/daemon.json` (глобально, 10 МБ × 3 файла) и `logging:` в docker-compose.yml для каждого сервиса. Если место всё равно заканчивается — проверьте логи в `./logs/` (nginx, 3x-ui, certbot монтируют их туда).

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

### Certbot: `Connection refused` при получении сертификата

Let's Encrypt не может скачать ACME-challenge с вашего сервера. Три причины:

**1. Фаервол облачного провайдера** (самая частая причина)

Большинство провайдеров (Hetzner, DigitalOcean, Serverspace, Timeweb и др.) имеют **отдельный фаервол на уровне панели управления**, независимый от UFW. Он может блокировать порт 80 даже если UFW разрешает его.

→ Войдите в панель управления вашего VPS и откройте входящие TCP-порты **80** и **443**.

**2. Порт 80 занят другим процессом**

```bash
ss -tlnp | grep :80
```
Если что-то уже слушает — остановите перед запуском скрипта.

**3. DNS ещё не обновился**

```bash
dig +short yourdomain.com   # должен вернуть IP этого сервера
```

---

### nginx-контейнер падает сразу после запуска

Симптом: `docker run` возвращает ID контейнера, но `docker ps` пустой.

Запустите без `--rm` чтобы увидеть логи:
```bash
docker run -d --name nginx-debug \
    -p 80:80 \
    -v "$(pwd)/nginx-init.conf:/etc/nginx/conf.d/default.conf:ro" \
    nginx:alpine
docker logs nginx-debug
docker rm nginx-debug
```

**Причина A: IPv6 не поддерживается ядром**

Ошибка: `socket() [::]:80 failed (97: Address family not supported by protocol)`

В файлах конфига уже убраны IPv6-директивы. Если проблема осталась — убедитесь что на сервере лежит актуальная версия файлов (не старая копия с `listen [::]:80`).

**Причина B: `nginx-init.conf` — директория вместо файла**

Ошибка: `mount ... not a directory: Are you trying to mount a directory onto a file?`

Docker создал папку вместо монтирования файла — значит файл не существовал в момент запуска. Скрипт не был запущен из директории проекта:
```bash
rm -rf nginx-init.conf   # удалить ошибочно созданную директорию
# убедиться что вы в директории проекта:
ls config/nginx/nginx-init.conf
```

---

### Скрипт: `$'\r': command not found` / `invalid option name`

Windows-переносы строк (CRLF) в shell-скрипте. Исправить:
```bash
sed -i 's/\r//' setup-3xui.sh
```
В репозитории уже добавлен `.gitattributes`, который автоматически выдаёт LF при `git clone`. Проблема возникает только при копировании файлов через `scp` с Windows без git.

---

### WARP-контейнер: ошибка sysctl IPv6

Ошибка: `open sysctl net.ipv6.conf.all.disable_ipv6 file: unsafe procfs detected: no such file or directory`

Ядро сервера собрано без поддержки IPv6 — соответствующий путь в `/proc/sys/net/ipv6` отсутствует. В `docker-compose.warp.yml` директива уже убрана. Если проблема осталась — убедитесь что на сервере актуальный файл.

---

### XRay не запускается: `neither outboundTag nor balancerTag is specified`

Ошибка в routing-конфиге XRay. Чаще всего возникает при переносе конфига со старого сервера.

Проверьте конфиг:
```bash
docker exec 3x-ui cat /usr/local/x-ui/bin/config.json
```

Два частых виновника:

**1. Нестандартное поле `finalRules` в outbound `direct`**

Удалите `finalRules` из настроек freedom-outbound, оставив только:
```json
{
  "tag": "direct",
  "protocol": "freedom",
  "settings": { "domainStrategy": "AsIs" }
}
```

**2. Пустое catch-all правило роутинга**

Правило `{"type": "field", "outboundTag": "direct"}` без единого условия отвергается новыми версиями XRay. Удалите его в панели: **Settings → Xray Configs → Routing**.

---

### Клиент не подключается

- Порт в клиенте должен быть **443**, а не 10000 (10000 — внутренний, снаружи слушает sslh)
- SNI клиента должен совпадать с доменом в сертификате (`yourdomain.com`, не другой)
- Проверьте что XRay запущен: `docker compose logs 3x-ui --tail 20`
- Проверьте sslh: `docker compose logs sslh --tail 10`

---

### Фейковый сайт не открывается

`https://yourdomain.com` должен открываться при TCP/RAW transport — XRay не распознаёт обычный HTTPS и делает fallback в nginx:8080.

При xHTTP-transport фейковый сайт через браузер не откроется (XRay ожидает конкретный path и заголовки). Это нормально.

```bash
docker compose logs nginx --tail 20
```

---

### WARP не работает

```bash
docker logs warp --tail 30
docker exec 3x-ui curl -x socks5://warp:1080 https://ifconfig.me
# Должен вернуть IP Cloudflare, не IP вашего VPS
```

---

### Не могу подключиться по SSH после `ufw enable`

UFW был выключен во время установки — правила не применились. Добавьте вручную (замените `22` на ваш `SSH_PORT` из `.env`):
```bash
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow from 172.16.0.0/12 to any port 22 proto tcp
ufw reload
```