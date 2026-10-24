# Troubleshooting

- [Обрывы долгих соединений, видео встаёт](#обрывы-долгих-соединений-видео-встаёт)
- [Клиент не подключается](#клиент-не-подключается)
- [Сайты периодически не открываются](#сайты-периодически-не-открываются)
- [Google/YouTube висят, остальное работает](#googleyoutube-висят-остальное-работает)
- [Нормальные ошибки в логах XRay](#нормальные-ошибки-в-логах-xray)
- [XRay не запускается: neither outboundTag nor balancerTag](#xray-не-запускается-neither-outboundtag-nor-balancertag)
- [WARP](#warp)
- [Certbot: Connection refused](#certbot-connection-refused)
- [Временный nginx для certbot падает](#временный-nginx-для-certbot-падает)
- [Скрипт: `$'\r': command not found`](#скрипт-r-command-not-found)
- [Нет SSH после `ufw enable`](#нет-ssh-после-ufw-enable)

## Обрывы долгих соединений, видео встаёт

`Connection lost mid-response` у LLM-клиентов, видео не догружается до перезагрузки страницы, большие загрузки срываются.

| Обрыв через | Виновник | Решение |
|---|---|---|
| ~60 с тишины | Caddy `read_body_idle` / `write_idle` | [docs/network.md](network.md#caddy-read_body_idle--write_idle) |
| ~300 с тишины | XRay `connIdle` (сервер или клиент) | [docs/network.md](network.md#xray-connidle) |

Признак первого: `docker logs caddy 2>&1 | grep -c "i/o timeout"` — сотни и тысячи.

## Клиент не подключается

- Ссылка взята из панели как есть — её нужно прогнать через `tools/xui-link.py` ([docs/panel.md](panel.md#ссылка-для-клиента)).
- Порт **443**, а не 10000.
- Security клиента — **TLS**, SNI — домен из сертификата.
- В inbound Security — **None**.
- XRay жив: `docker compose logs 3x-ui --tail 20`; sslh: `docker compose logs sslh --tail 10`.
- Caddy видит запросы к `/game`: `docker compose logs caddy --tail 20`. `dial tcp ...:10000: connection refused` — XRay не запущен или inbound не на 10000.

Фейковый сайт `https://yourdomain.com` открывается в браузере всегда — его отдаёт Caddy. Если не открывается он, проблема до XRay: DNS, фаервол, sslh или Caddy.

## Сайты периодически не открываются

В логах клиента ошибки `outbound/direct` для зарубежных IP: клиентский роутинг отправляет часть трафика мимо прокси, и провайдер её режет. В клиенте:

- для Telegram, Instagram, YouTube выбрать outbound `proxy`, а не `direct`;
- или режим «весь трафик через proxy» с исключением локальных адресов.

```
geosite:youtube
geosite:instagram
geosite:telegram
geoip:telegram
```

## Google/YouTube висят, остальное работает

Скорее всего, в клиенте включён IPv6: сервер выходит в интернет только по IPv4 и IPv6-адреса назначения открыть не может. Выключите IPv6 в клиенте — [docs/network.md](network.md#ipv6).

## Нормальные ошибки в логах XRay

- `connection forcibly closed by remote host` / `connection aborted` — удалённый сервер закрыл неактивное соединение RST. Штатно.
- `open connection ... using outbound/vless[proxy]: connection attempt failed` — недоступен exit VPS (`PROXY_MODE=vps`). Если часто — проверьте его стабильность.

## XRay не запускается: neither outboundTag nor balancerTag

```
Failure in running xray-core: exit status 23
... neither outboundTag nor balancerTag is specified in routing rule
```

Почти всегда — блок `routing` вставлен в **Custom Config** вместе с outbounds. 3x-ui 26.9.x вкладывает его целиком в `routing.rules` как одно правило без `outboundTag`. Убрать `routing` из Custom Config и добавить правила отдельно в **Routing Rules** ([docs/outbound.md](outbound.md#как-применяются-шаблоны)).

Ещё одна причина — правило вообще без условий (`{"type": "field", "outboundTag": "direct"}`): новые версии XRay его отвергают. Нужно хотя бы `"network": "tcp,udp"`.

Посмотреть итоговый конфиг:

```bash
docker exec 3x-ui cat /app/bin/config.json
```

## WARP

```bash
docker logs warp --tail 30
docker exec 3x-ui curl -s -x socks5://warp:1080 https://ifconfig.me   # должен быть IP Cloudflare
```

- `Operation not permitted` при инициализации WireGuard — нужен `cap_add: SYS_MODULE` (уже в `docker-compose.warp.yml`). Если wireguard встроен в ядро, хватит `NET_ADMIN`. Проверить: `lsmod | grep wireguard`, `modinfo wireguard`.
- Отказ стартовать без `SOCKS_USER`/`SOCKS_PASS` — в compose уже задан `ALLOW_NO_AUTH=1`; проверьте, что используется актуальный `docker-compose.warp.yml`.
- `unsafe procfs detected ... disable_ipv6` — ошибка старого образа `caomingjun/warp` на ядрах без IPv6. Перейдите на MicroWARP из текущего compose.
- WARP лёг, а клиентам нужен доступ прямо сейчас — поднимите правило `direct` выше `warp-out` в Routing Rules ([docs/outbound.md](outbound.md#proxy_modewarp)).

## Certbot: Connection refused

Если установка остановилась на `DNS: ...` раньше certbot — исправьте A/AAAA-записи, как написано в сообщении ([проверка DNS](install.md#проверка-dns)).

Let's Encrypt не может скачать ACME-challenge:

1. **Фаервол хостера** (самое частое). У Hetzner, DigitalOcean, Timeweb и др. он отдельный от UFW — откройте входящие TCP **80** и **443** в панели провайдера.
2. **Порт 80 занят**: `ss -tlnp | grep :80` — остановите процесс перед запуском скрипта.
3. **DNS не обновился**: `dig +short yourdomain.com` должен вернуть IP сервера.

## Временный nginx для certbot падает

`docker run` вернул ID, но `docker ps` пуст. Этот nginx поднимается только на шаге 4 ради ACME-challenge. Посмотреть ошибку:

```bash
docker run -d --name nginx-debug -p 80:80 \
    -v "$(pwd)/nginx-init.conf:/etc/nginx/conf.d/default.conf:ro" nginx:alpine
docker logs nginx-debug; docker rm -f nginx-debug
```

- `socket() [::]:80 failed (97: Address family not supported)` — ядро без IPv6, а на сервере старая копия конфига с `listen [::]:80`. Обновите файлы.
- `not a directory: Are you trying to mount a directory onto a file?` — скрипт запущен не из директории проекта, и Docker создал папку вместо файла:

  ```bash
  rm -rf nginx-init.conf
  ls config/nginx/nginx-init.conf   # убедиться, что вы в директории проекта
  ```

## Скрипт: `$'\r': command not found`

CRLF-переносы после копирования с Windows без git: `sed -i 's/\r//' setup-3xui.sh`. При `git clone` `.gitattributes` отдаёт LF сам.

## Нет SSH после `ufw enable`

UFW был выключен во время установки, и правила не применились. Через консоль хостера (замените `22` на `SSH_PORT`):

```bash
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow from 172.16.0.0/12 to any port 22 proto tcp
ufw reload
```
