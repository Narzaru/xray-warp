# Установка

## Требования

- Ubuntu 24.04 или 26.04
- Публичный IP-адрес
- DNS: одна A-запись `yourdomain.com → IP сервера`
- Открытые порты `80/tcp`, `443/tcp` (и `443/udp` при `HYSTERIA=on`) — в том числе в фаерволе панели хостера
- Пакеты: `docker`, `docker-compose-v2` (или `docker-compose`), `gettext-base`

```bash
apt-get update
apt-get install -y docker.io docker-compose-v2 gettext-base
```

## 1. Файлы на сервер

```bash
git clone https://github.com/Narzaru/xray-warp.git /opt/x-ui
cd /opt/x-ui
```

Или копированием с локальной машины: `scp -r . user@SERVER:/opt/x-ui`.

> Скрипт запускается **из директории проекта** — иначе не найдутся `config/` и сломается монтирование в Docker.

> При копировании с Windows без git скрипт может получить CRLF-переносы: `sed -i 's/\r//' setup-3xui.sh`.

## 2. Переменные

```bash
cp .env.example .env
nano .env
```

| Переменная | Значение |
|---|---|
| `DOMAIN` | домен, A-запись уже указывает на сервер |
| `EMAIL` | email для уведомлений Let's Encrypt |
| `SSH_PORT` | порт sshd на хосте — sslh пересылает SSH с 443 сюда |
| `PROXY_MODE` | `off` \| `warp` \| `vps` — [docs/outbound.md](outbound.md) |
| `HYSTERIA` | `off` \| `on` — [docs/hysteria.md](hysteria.md) |
| `TIMEZONE` | необязательно, по умолчанию берётся системный |

## 3. Запуск

```bash
chmod +x setup-3xui.sh
sudo ./setup-3xui.sh
```

Скрипт сам уходит в фон и в терминале только показывает лог `setup.log`. Установка перезапускает dockerd и sslh, и SSH-сессия через 443 может оборваться — на установку это не влияет: переподключитесь и смотрите `tail -f setup.log`.

Шаги скрипта:

1. Проверка зависимостей и демона Docker, правила UFW.
2. Директории (`certbot/`, `logs/`), `daemon.json` (ротация логов, `host-gateway-ip`; прежний сохраняется в `.bak`), сетевой тюнинг хоста (BBR обязателен).
3. Генерация конфигов (Caddyfile, Hysteria2).
4. Проверка DNS и SSL-сертификат через Let's Encrypt (webroot; временный nginx на `:80` или уже запущенный Caddy).
5. Запуск контейнеров.
6. Проверка: Caddy перечитывает конфиг, контейнеры не падают в цикле, через 443 отвечают SSH и HTTPS.
7. Cron: перечитывание сертификата (ежедневно 4:00) и обновление образов (вс 3:00).
8. Вывод JSON для `policy` и исходящего прокси в панели.

### Проверка DNS

Перед выпуском сертификата скрипт берёт A и AAAA домена из публичного DNS и сравнивает с адресами сервера. Установка остановится, если:

- A-записи нет или она ведёт не на внешний IPv4 сервера;
- есть AAAA, но такого IPv6 на сервере нет — например, запись осталась от старого хоста. Let's Encrypt проверяет домен сначала по IPv6 и при ответе чужого сервера на IPv4 не откатывается.

Если сертификат уже есть, расхождение только выводится предупреждением. Отключить проверку (GeoDNS, домен за прокси): `SKIP_DNS_CHECK=1 ./setup-3xui.sh`.

После скрипта — настройка XRay в панели: [docs/panel.md](panel.md).

## Фаервол

Скрипт добавляет правила UFW (работает и на выключенном UFW — применятся при включении):

```bash
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow from 172.16.0.0/12 to any port <SSH_PORT> proto tcp  # Docker (sslh) → host SSH
```

Когда XRay работает и SSH через 443 проверен, можно включить UFW и закрыть 22:

```bash
ufw enable
ufw delete allow 22/tcp   # только если SSH через :443 уже работает
ufw status
```

## SSL-сертификат

Выпускается на шаге 4. Контейнер certbot проверяет обновление каждые 12 часов. Сертификат читает **Caddy**, поэтому cron ежедневно перечитывает его конфиг:

```
# /etc/cron.d/xray-cert-reload
0 4 * * * root docker exec caddy caddy reload --force --config /etc/caddy/Caddyfile
```

- `--force` обязателен. Caddyfile после продления не меняется, и без флага Caddy отвечает `config is unchanged`, продолжая отдавать старый сертификат, пока тот не истечёт. Файлы из директивы `tls <cert> <key>` сам Caddy с диска не перечитывает. На серверах, поставленных до этого исправления, поправьте строку в `/etc/cron.d/xray-cert-reload` вручную.
- `caddy reload` не рвёт соединения — для него в Caddyfile включён admin API на loopback внутри контейнера.
- При `HYSTERIA=on` добавляется `&& docker restart hysteria`: Hysteria2 читает сертификат только при старте.
- Перезапускать `3x-ui` не нужно: у XRay `security: none`, сертификатов он не читает. Если вы вернули TLS в XRay (вариант TCP/RAW в [docs/panel.md](panel.md)), верните и `docker restart 3x-ui` в cron.
