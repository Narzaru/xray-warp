> Написано с помощью [Claude Code](https://claude.ai/code)

# 3x-ui · XRay · Caddy · sslh — Docker Setup

Готовый стек для запуска XRay-прокси (VLESS) на порту 443 с фейковым сайтом-прикрытием, автоматическими SSL-сертификатами и SSH на том же порту. Одна DNS-запись, один домен — снаружи выглядит как обычный HTTPS-сайт.

## Архитектура

```
Интернет :443/tcp                          Интернет :443/udp
    │                                             │
    ▼                                             ▼
 [sslh]  — мультиплексор по первым байтам    [hysteria]  (опционально)
    ├── SSH ─────────────► host sshd            QUIC/UDP, свой пароль
    └── TLS ─────────────► [caddy :8443]        см. docker-compose.hysteria.yml
                               │
                    ┌──────────┴──────────┐
                    │ /game               │ всё остальное
                    ▼                     ▼
            [3x-ui / XRay :10000]   фейковый сайт
              xhttp БЕЗ TLS         (реальный 200 OK)
                    │
                    ▼
                proxy out
           (warp / vps / direct)
```

- **sslh** — слушает `:443/tcp`, определяет протокол по первым байтам: SSH уходит на хост, TLS — в Caddy
- **Caddy** — **терминирует TLS** сертификатом Let's Encrypt. Путь `/game` проксирует в XRay по **h2c**, всё остальное отдаёт фейковым сайтом. Также обслуживает порт 80 для ACME-challenge
- **XRay** (внутри 3x-ui) — принимает xhttp **без TLS** (`security: none`), TLS уже снят Caddy
- **hysteria** — опциональный транспорт QUIC/UDP на `:443/udp`. Нужен на каналах с потерями пакетов, где TCP обрушивает окно перегрузки
- **certbot** — обновляет сертификат каждые 12 часов
- **cron** — обновляет Docker-образы раз в неделю (воскресенье, 3:00)

> **Почему TLS терминирует прокси, а не XRay.** При xhttp у XRay нет fallback: всё, что не совпало с путём `/game`, он просто отбрасывает. Домен с валидным сертификатом, отдающий 404 на любой запрос, — заметный признак при активном пробинге. С прокси впереди посторонний запрос получает настоящий сайт с настоящими заголовками.
>
> Для клиентов схема прозрачна: тот же домен, порт, путь и SNI. Точка снятия TLS переезжает внутри сервера и снаружи неотличима — конфиги менять не нужно.

> **Почему Caddy, а не nginx.** nginx проксирует по HTTP/1.1 последовательно и не держит дуплекс внутри одного запроса: он не начнёт отдавать ответ, пока не дочитает тело запроса. Для XHTTP это фатально — из трёх режимов выживает только `packet-up`, где каждый обмен представляет собой законченный запрос-ответ. А `packet-up` открывает поток коротких соединений, и на мобильных сетях за CGNAT это выжигает квоту NAT-трансляций: оператор перестаёт пропускать SYN-ACK, и установление соединения начинает занимать секунды.
>
> Caddy проксирует в XRay по h2c, где дуплекс штатный, поэтому доступны `stream-up` и `stream-one` — одно долгоживущее соединение вместо сотен коротких. Побочный эффект: соединений к XRay становится 2 вместо ~20 новых в секунду.

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
├── setup-3xui.sh                # скрипт установки
├── docker-compose.yml           # основной стек
├── docker-compose.warp.yml      # override для PROXY_MODE=warp
├── docker-compose.hysteria.yml  # override для транспорта Hysteria2 (QUIC/UDP)
├── .env.example                 # шаблон переменных
├── tools/
│   └── xui-link.py              # чинит ссылку из панели → рабочая ссылка + JSON
├── config/
│   ├── caddy/
│   │   └── Caddyfile            # шаблон конфига Caddy (TLS-фронт + фейковый сайт)
│   ├── nginx/
│   │   └── nginx-init.conf      # временный конфиг nginx для первичного certbot
│   ├── hysteria/
│   │   └── config.yaml          # шаблон конфига Hysteria2 (пароль из .env)
│   └── xray/
│       ├── outbound-warp.json   # шаблон outbound через Cloudflare WARP
│       └── outbound-vps.json    # шаблон outbound через exit VPS (VLESS)
└── html/
    └── index.html               # фейковый корпоративный сайт
```

После запуска скрипта в корне появятся:
```
├── Caddyfile                    # сгенерированный конфиг Caddy
├── nginx-init.conf              # временный конфиг (можно удалить после установки)
├── certbot/                     # SSL-сертификаты
└── logs/                        # логи 3x-ui, certbot (Caddy пишет в stdout)
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
2. Создание директорий (`certbot/`, `logs/`) и сетевой тюнинг хоста (BBR, буферы)
3. Генерация конфига Caddy
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
| Security | **None** |

> ⚠️ **Security именно `None`, а не TLS.** Сертификат подключает Caddy, который стоит впереди
> (см. раздел «Архитектура»). Если включить TLS ещё и здесь, Caddy будет слать plain HTTP
> в TLS-порт и туннель не поднимется. Пути к сертификатам в панели указывать не нужно.
>
> Из этого следует неочевидное: **ссылка, сгенерированная панелью, работать не будет** — панель
> видит `security: none` и проставляет его в ссылку. В клиентском конфиге нужен `security: tls`,
> потому что клиент устанавливает TLS с Caddy, а не с XRay.

#### Вариант A — xHTTP (рекомендуется, лучше обходит DPI)

| Поле | Значение |
|---|---|
| Transmission | xHTTP |
| Path | `/game` (или любой другой) |
| Host | `yourdomain.com` |
| uTLS | `firefox` (или `chrome`) |
| ALPN | `h2`, `http/1.1` |
| SNI клиента | `yourdomain.com` |

#### Вариант B — TCP/RAW

> ⚠️ **Несовместим с текущей архитектурой.** Caddy впереди понимает HTTP, а не сырой VLESS,
> поэтому TCP/RAW через него не пройдёт. Чтобы использовать этот вариант, нужно вернуть
> терминирование TLS в XRay: в `docker-compose.yml` заменить `--tls=caddy:8443` на
> `--tls=3x-ui:10000`, а в панели включить Security = TLS с путями к сертификатам.
> Тогда фейковый сайт снова отдаётся через Fallbacks, а не через прокси-фронт.

Параметры (при возврате TLS в XRay):

| Поле | Значение |
|---|---|
| Transmission | TCP (RAW) |
| Security | TLS |
| Certificate File | `/etc/letsencrypt/live/yourdomain.com/fullchain.pem` |
| Key File | `/etc/letsencrypt/live/yourdomain.com/privkey.pem` |
| uTLS | none |
| ALPN | `http/1.1` (убрать h2) |
| SNI клиента | `yourdomain.com` |

В секции **Fallbacks** добавить:

| Поле | Значение |
|---|---|
| Dest | `caddy:8080` |
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

### Как получить рабочую ссылку из панели

**Ссылку, которую генерирует 3x-ui, импортировать нельзя.** Панель строит её по данным inbound и не знает, что перед XRay стоит Caddy, поэтому подставляет неверные значения:

| Поле | Панель отдаёт | Должно быть | Почему |
|---|---|---|---|
| порт | `10000` | `443` | 10000 — внутренний порт XRay, снаружи слушает sslh |
| `security` | `none` | `tls` | у inbound TLS выключен, но клиент устанавливает TLS с Caddy |
| `encryption` | иногда отсутствует | `none` | обязателен в VLESS-URI, без него профиль отбраковывается |
| `alpn` | пусто | `h2` | `stream-one` работает только поверх HTTP/2 |
| `sni` | пусто | домен | иначе TLS-рукопожатие уйдёт не на тот виртуальный хост |
| `mode` | `auto` | явный режим | см. таблицу режимов ниже |

Чинится одной командой:

```bash
python3 tools/xui-link.py '<ссылка из панели>'
```

Скрипт печатает два результата:

1. **Исправленную ссылку** — для быстрого импорта в любой клиент.
2. **Готовый JSON-конфиг с xmux** — для мобильных сетей. Через ссылку xmux передать нельзя: v2rayNG не разбирает параметр `extra` в URI, поэтому там нужен импорт конфига целиком.

Полезные ключи:

```bash
# другой режим и число соединений
python3 tools/xui-link.py '<ссылка>' --mode packet-up --connections 2-4

# прописать IP в dns.hosts — убирает резолв домена из критического пути
python3 tools/xui-link.py '<ссылка>' --ip 203.0.113.10

# только ссылка, без JSON
python3 tools/xui-link.py '<ссылка>' --link-only
```

JSON вставляется в v2rayNG через **Настройки → Custom Config** (либо импортом файла), в Hiddify и NekoBox — как custom outbound.

## Транспорт на мобильных сетях

Раздел про то, почему на Wi-Fi всё летает, а на мобильном интернете тот же профиль еле шевелится.

### Режимы XHTTP

| Режим | Соединений | Когда выбирать |
|---|---|---|
| `packet-up` | много коротких | канал без потерь; единственный режим, переживающий nginx впереди |
| `stream-up` | download и upload раздельно | компромисс |
| `stream-one` | одно долгоживущее | дорогое установление соединения (CGNAT, DPI) |

Режим задаёт **клиент**; на сервере достаточно `mode: auto` — он принимает любой.

### xmux

`enableXmux: true` в панели только **разрешает** мультиплексирование, параметры задаёт клиент. На дефолтах весь трафик едет по одной трубе, и одна потеря блокирует всю очередь за собой — head-of-line blocking сначала на уровне TCP, затем на уровне HTTP/2. На канале с потерями это роняет скорость в разы.

Лечится явным числом соединений в клиентском конфиге:

```json
"xhttpSettings": {
  "extra": {
    "xPaddingBytes": "100-1000",
    "xmux": { "maxConnections": "4-8", "maxConcurrency": 0 }
  },
  "host": "yourdomain.com",
  "mode": "stream-one",
  "path": "/game"
}
```

`maxConnections` и `maxConcurrency` взаимоисключающие: задавая первое, второе обнуляют. Настройки xhttp живут **внутри объекта `extra`** — не отдельным полем.

Глобальный **Mux** (mux.cool) в клиенте при этом должен быть **выключен**: он складывает потоки обратно в одно соединение и отменяет весь смысл упражнения.

### Congestion control

Хостовой sysctl **не действует на контейнеры** — у каждого свой сетевой namespace. Поэтому BBR задан дважды: в `/etc/sysctl.d/99-vpn-tuning.conf` для хоста и в `sysctls:` каждого сервиса в `docker-compose.yml`. Критичен он именно для **sslh**, который терминирует внешнее TCP-соединение клиента.

Проверить, что применилось:

```bash
for c in sslh caddy 3x-ui; do
    printf "%-8s " "$c"
    docker exec $c cat /proc/sys/net/ipv4/tcp_congestion_control
done
```

Замена cubic на BBR на канале с джиттером в сотни миллисекунд даёт кратный прирост: cubic принимает скачок задержки за перегрузку и режет окно вдвое.

### Диагностика реального соединения

TCP-метрики живого клиентского соединения видны в сетевом namespace sslh:

```bash
PID=$(docker inspect -f '{{.State.Pid}}' sslh)
nsenter -t "$PID" -n ss -tin state established "( sport = :443 )"
```

Что смотреть:

- `bytes_retrans` против `bytes_sent` — доля ретрансмиссий; 15–20% означает, что канал теряет пакеты под нагрузкой, и никакая настройка сервера этого не исправит
- `app_limited` — TCP простаивает не из-за сети, а потому что приложение не подаёт данные: узкое место выше по цепочке
- `rtt` против `minrtt` — расхождение в разы указывает на буфербloat у оператора
- `bbr:(bw:...)` и `pacing_rate` — во что упёрся алгоритм

### Чего не ждать

Тест packet loss на speed.cloudflare.com через этот стек **не показывает ничего осмысленного**: он меряет потери через WebRTC поверх UDP, а UDP инкапсулирован в TCP-туннель. Он будет показывать то 0%, то 100% в зависимости от того, договорился ли ICE. Ориентируйтесь на скорость, latency и `ss`.

Потолок TCP-транспорта на канале с реальными потерями пробить нельзя — распараллеливание сглаживает, но не устраняет. Радикально задачу решает QUIC, где потеря в одном потоке не блокирует остальные: см. `HYSTERIA=on`.

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

## Hysteria2 — транспорт для каналов с потерями

xhttp работает поверх TCP. На канале, где теряется заметная доля пакетов (мобильные сети,
особенно при активной фильтрации трафика), TCP обрушивает окно перегрузки и скорость падает
в разы — это свойство алгоритма, а не настроек. Ни `cubic`, ни `BBR` тут не спасают.

Hysteria2 использует QUIC поверх UDP с алгоритмом Brutal: он удерживает заданную полосу
при потерях вместо отступления. На маршруте с 15–25% потерь разница получается кратной.

Транспорт полностью изолирован: свой контейнер, свой протокол на `:443/udp`, свой пароль.
3x-ui, XRay, sslh и конфиги существующих клиентов не затрагиваются. `443/tcp` и `443/udp` —
независимые порты, конфликта нет.

### Запуск

Ставится тем же скриптом, рядом с основным стеком — в `.env`:

```bash
HYSTERIA=on
```

и `./setup-3xui.sh`. Скрипт сам сгенерирует пароль, допишет его в `.env`,
соберёт конфиг из шаблона и поднимет контейнер. Порт `443/udp` при этом ничего
не переназначает: `443/tcp` остаётся за sslh, это независимые порты.

Пароль генерируется один раз. При повторном запуске скрипт видит уже заданное
значение и не трогает его, поэтому переустановка не сбрасывает доступ у настроенных
клиентов. Файл `config/hysteria/config.generated.yaml` содержит пароль и в git
не попадает (см. `.gitignore`).

### Клиенты

Пароль один и общий для всех — его же и раздаём. Ссылку скрипт печатает в конце установки:

```
hy2://ПАРОЛЬ@yourdomain.com:443/?sni=yourdomain.com#имя-профиля
```

Показать её повторно, не переустанавливая:

```bash
source .env
echo "hy2://${HYSTERIA_PASSWORD}@${DOMAIN}:443/?sni=${DOMAIN}#${DOMAIN}-hy2"
```

Сменить пароль (например, если ссылка утекла) — стереть `HYSTERIA_PASSWORD` из `.env`
и перезапустить скрипт: он сгенерирует новый. Ссылка при этом меняется **у всех сразу** —
отозвать доступ одному человеку в этой схеме нельзя.

> Если понадобится раздельный доступ, Hysteria2 умеет режим `userpass` — в шаблоне
> `config/hysteria/config.yaml` вместо `type: password` задаётся список логинов
> и паролей, а ссылка принимает вид `hy2://ЛОГИН:ПАРОЛЬ@...`.

**v2rayNG Hysteria2 не поддерживает** — нужен Hiddify, NekoBox или sing-box.

После импорта задайте полосу (**Upload / Download Mbps**) чуть ниже реальной скорости
канала — без этого не включится Brutal и смысл транспорта теряется. Завышать нельзя:
клиент начнёт заливать канал сверх ёмкости и сам создаст потери.

> **IPv6.** Если на сервере нет IPv6, в клиенте нужно выставить **IPv6 Mode = Disable**.
> Иначе клиент присылает IPv6-адреса назначения, сервер не может их набрать, а туннель
> при этом соединение принимает — из-за чего браузер не переключается на IPv4 и сайты
> с IPv6 (в первую очередь Google) висят до таймаута.

### Удаление

В `.env` поставить `HYSTERIA=off` и перезапустить скрипт, либо вручную:

```bash
docker compose -f docker-compose.yml -f docker-compose.hysteria.yml rm -sf hysteria
rm -f config/hysteria/config.generated.yaml
```

## Завершение

После проверки что XRay работает и SSH через 443 доступен — включите UFW и закройте порт 22:

```bash
ufw enable
ufw delete allow 22/tcp   # только если SSH через :443 уже работает
ufw status
```

## SSL-сертификат

Сертификат выпускается автоматически на шаге 4. Certbot-контейнер проверяет обновление каждые 12 часов. Сертификат читает **Caddy** (он терминирует TLS), поэтому cron-задача перечитывает его конфиг:

```
# /etc/cron.d/xray-cert-reload
0 4 * * * root docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

При `HYSTERIA=on` в ту же задачу добавляется `&& docker restart hysteria` — Hysteria2 читает сертификат при старте и требует перезапуска.

> **Даунтайм**: `caddy reload` перечитывает конфиг без разрыва соединений — даунтайма нет. Для этого в `Caddyfile` включён admin API (`admin localhost:2019`); он слушает только loopback внутри контейнера и наружу не публикуется. Перезапуск Hysteria2 занимает пару секунд. Выбрано 4:00 ночи — минимум активных соединений.
>
> ⚠️ Перезапускать `3x-ui` после обновления сертификата **не нужно**: у XRay `security: none`, сертификатов он не читает. Если вы вернули терминирование TLS в XRay (вариант TCP/RAW), верните и `docker restart 3x-ui` в cron.

## Полезные команды

```bash
# Состояние контейнеров
docker compose ps

# Логи
docker compose logs -f 3x-ui        # панель и XRay
docker compose logs -f caddy        # Caddy
docker compose logs -f sslh         # мультиплексор

# Перезапуск
docker compose restart 3x-ui
docker compose restart caddy

# Обновить образы вручную (автоматически — каждое воскресенье в 3:00 через cron)
docker compose pull && docker compose up -d

# С WARP
docker compose -f docker-compose.yml -f docker-compose.warp.yml pull
docker compose -f docker-compose.yml -f docker-compose.warp.yml up -d

# Сколько места занимают логи Docker
du -sh /var/lib/docker/containers/*/
```

> **Ротация логов**: скрипт автоматически настраивает `/etc/docker/daemon.json` (глобально, 10 МБ × 3 файла) и `logging:` в docker-compose.yml для каждого сервиса. Если место всё равно заканчивается — проверьте логи в `./logs/` (3x-ui и certbot монтируют их туда; Caddy пишет в stdout, его логи забирает Docker).

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

### Временный nginx для certbot падает сразу после запуска

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

Этот контейнер поднимается только на шаге 4, чтобы отдать ACME-challenge до появления сертификата — постоянный фронт работает на Caddy.

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

### WARP-контейнер: ошибка sysctl IPv6 (старый образ caomingjun/warp)

Ошибка: `open sysctl net.ipv6.conf.all.disable_ipv6 file: unsafe procfs detected: no such file or directory`

Возникала с прежним образом `caomingjun/warp`, если ядро сервера собрано без поддержки IPv6 (путь `/proc/sys/net/ipv6` отсутствует). С переходом на MicroWARP (`ghcr.io/ccbkkb/microwarp`) эта проблема не актуальна — соответствующая директива в `docker-compose.warp.yml` отсутствует.

---

### WARP-контейнер: не хватает прав на загрузку модуля ядра

Ошибка вида `Operation not permitted` при инициализации WireGuard внутри контейнера `warp`.

MicroWARP запускает WireGuard через модуль ядра и требует `cap_add: SYS_MODULE` (уже добавлено в `docker-compose.warp.yml`). Если ядро сервера собрано с `wireguard` как встроенным модулем (не загружаемым), хватит и одного `NET_ADMIN` — `SYS_MODULE` можно убрать. Проверить: `lsmod | grep wireguard` и `modinfo wireguard`.

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

`https://yourdomain.com` должен открываться при TCP/RAW transport — XRay не распознаёт обычный HTTPS и делает fallback в caddy:8080.

При xHTTP-transport фейковый сайт через браузер не откроется (XRay ожидает конкретный path и заголовки). Это нормально.

```bash
docker compose logs caddy --tail 20
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