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

Для рабочего VPS есть несколько разных случаев:

- Если назначение — IPv6-адрес, проверьте IPv6 в TUN: текущий выход Hev работает по IPv4. [Разница между IPv6 входа и выхода](network.md#ipv6).
- Если зависает только определённый `googlevideo.com`, сверьте имя кэша, порт и порядок RU-правил. На 14 ноября один российский кэш разрешён **только в подготовленном плане**; обновление официальных баз само по себе не убирает `geoip:ru`. [Состояние заявки](server-state.md#что-ещё-не-применено).
- Если сбой связан с перемоткой, запишите в DevTools протокол запроса к видео: h3 (QUIC) и h2 идут разными путями внутри туннеля. Один ответ 429 или отдельный красный запрос не устанавливают причину зависания. Сравните задержки DNS и TLS для одного и того же медиазапроса.

## Нормальные ошибки в логах XRay

- `connection forcibly closed by remote host` / `connection aborted` — удалённый сервер закрыл неактивное соединение RST. Штатно.
- `open connection ... using outbound/vless[proxy]: connection attempt failed` — недоступен exit VPS (`PROXY_MODE=vps`). Если часто — проверьте его стабильность.

## XRay не запускается: neither outboundTag nor balancerTag

```
Failure in running xray-core: exit status 23
... neither outboundTag nor balancerTag is specified in routing rule
```

Почти всегда — блок `routing` вставлен в **Custom Config** вместе с outbounds. Редактор отдельного правила может вложить его целиком в `routing.rules` как одно правило без `outboundTag`. Убрать `routing` из Custom Config и добавить правила отдельно в **Routing Rules** ([docs/outbound.md](outbound.md#как-применяются-шаблоны)).

Ещё одна причина — правило вообще без условий (`{"type": "field", "outboundTag": "direct"}`): новые версии XRay его отвергают. Нужно хотя бы `"network": "tcp,udp"`.

Посмотреть итоговый конфиг:

```bash
docker exec 3x-ui cat /app/bin/config.json
```

## WARP

```bash
docker logs warp --tail 30
docker inspect --format '{{.State.Health.Status}}' warp
```

- `Operation not permitted` при инициализации WireGuard — нужен `cap_add: SYS_MODULE` (уже в `docker-compose.warp.yml`). Если wireguard встроен в ядро, хватит `NET_ADMIN`. Проверить: `lsmod | grep wireguard`, `modinfo wireguard`.
- Отказ стартовать без `SOCKS_USER`/`SOCKS_PASS` — в compose уже задан `ALLOW_NO_AUTH=1`; проверьте, что используется актуальный `docker-compose.warp.yml`.
- `unsafe procfs detected ... disable_ipv6` — ошибка старого образа `caomingjun/warp` на ядрах без IPv6. Перейдите на MicroWARP из текущего compose.
- Если TCP работает, а UDP нет, сверьте **образ**: upstream MicroSOCKS не поддерживает UDP ASSOCIATE. В рабочем профиле нужен локальный Hev. При отказе WARP рабочая политика прекращает передачу; переключение на direct меняет политику и раскрывает IP VPS.

## Certbot: Connection refused

Если установка остановилась на `DNS: ...` раньше certbot — исправьте A/AAAA-записи, как написано в сообщении ([проверка DNS](install.md#проверка-dns)).

Let's Encrypt не может скачать ACME-challenge:

1. **Фаервол хостера** (самое частое). У Hetzner, DigitalOcean, Timeweb и др. он отдельный от UFW — откройте входящие TCP **80** и **443** в панели провайдера.
2. **Порт 80 занят**: посмотрите слушателя в `ss -tlnp`. Рабочий Caddy может обслуживать challenge сам; если порт занят другим сайтом, согласуйте webroot или отдельное окно обслуживания.
3. **DNS не обновился**: `dig +short yourdomain.com` должен вернуть IP сервера.

## Временный nginx для certbot падает

При первой выдаче сертификата установщик использует временный nginx в host network. Проверьте занятость 80/tcp на хосте, выбранные семейства DNS и сгенерированный `nginx-init.conf`: это должен быть файл. Ошибка `[::]:80 failed (97: Address family not supported)` на хосте с отключённым IPv6 означает, что в конфиг ошибочно попал IPv6 listener. Не останавливайте рабочий Caddy ради диагностики: при его наличии установщик использует его webroot.

## Скрипт: `$'\r': command not found`

CRLF-переносы после копирования с Windows без git: `sed -i 's/\r//' setup-3xui.sh`. При `git clone` `.gitattributes` отдаёт LF сам.

## Нет SSH после `ufw enable`

Различайте внешний SSH на 443/tcp и соединение из контейнера sslh к порту sshd на хосте. Убедитесь через консоль хостера, что sshd слушает нужный адрес и `SSH_PORT`, 443/tcp открыт в панели провайдера, а UFW разрешает **интерфейс фактического bridge проекта** → порт sshd. Произвольное правило для `172.16.0.0/12` не покрывает пользовательский пул Docker. До проверки второго входа держите рабочую SSH-сессию и консоль восстановления доступными.
