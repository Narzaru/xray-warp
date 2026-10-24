# Эксплуатация

Команды выполняются из директории проекта (`/opt/x-ui`). При `PROXY_MODE=warp` или `HYSTERIA=on` добавляйте соответствующие `-f docker-compose.*.yml` — иначе `up -d --remove-orphans` снесёт их контейнеры.

```bash
# Состояние
docker compose ps

# Логи
docker compose logs -f 3x-ui        # панель и XRay
docker compose logs -f caddy        # TLS-фронт
docker compose logs -f sslh         # мультиплексор

# Перезапуск
docker compose restart 3x-ui
docker exec caddy caddy reload --force --config /etc/caddy/Caddyfile   # Caddy без разрыва соединений

# Обновить образы вручную
docker compose pull && docker compose up -d

# То же с WARP
docker compose -f docker-compose.yml -f docker-compose.warp.yml pull
docker compose -f docker-compose.yml -f docker-compose.warp.yml up -d

# Сгенерированный XRay-конфиг
docker exec 3x-ui cat /app/bin/config.json
```

## Изменения Caddyfile

Правится шаблон `config/caddy/Caddyfile`, затем:

```bash
export $(grep ^DOMAIN= .env)
envsubst '${DOMAIN}' < config/caddy/Caddyfile > Caddyfile
docker exec caddy caddy validate --config /etc/caddy/Caddyfile
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

## Автоматика

`/etc/cron.d/xray-cert-reload`:

| Когда | Что |
|---|---|
| ежедневно 4:00 | `caddy reload --force` — подхватить обновлённый сертификат (+ рестарт hysteria) |
| вс 3:00 | `pull` + `up -d` — обновление всех образов |

Логи: `/var/log/xray-cert-reload.log`, `/var/log/xray-image-update.log`.

> Образы тянутся по плавающим тегам (`latest`, `caddy:2-alpine`), поэтому вместе с обновлением приезжают и смены дефолтов. Так пришёл 60-секундный таймаут Caddy 2.11 ([docs/network.md](network.md#таймауты-простоя)). Если что-то сломалось в понедельник — смотрите логи контейнеров и `docker image ls`.

## Логи и место

Скрипт настраивает ротацию глобально в `/etc/docker/daemon.json` (10 МБ × 3) и `logging:` для каждого сервиса. 3x-ui и certbot дополнительно пишут в `./logs/`.

```bash
du -sh /var/lib/docker/containers/*/
du -sh logs/*
```
