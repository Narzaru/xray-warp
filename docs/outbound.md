# Исходящий прокси (смена выходного IP)

По умолчанию XRay выходит в интернет напрямую с IP сервера. Сменить выходной IP — `PROXY_MODE` в `.env` до запуска скрипта.

| `PROXY_MODE` | Выходной IP | Цена |
|---|---|---|
| `off` | IP сервера | — |
| `warp` | IP Cloudflare | бесплатно |
| `vps` | IP второго VPS | $3–5/мес, быстрее WARP |

## Как применяются шаблоны

Файлы `config/xray/` — шаблоны: скрипт не монтирует их в контейнер, а печатает в терминал на шаге 8. Вставить их в панель нужно вручную, двумя кусками:

- `outbound-*.json` → **Settings → Xray Configs → Custom Config**;
- `routing-*.json` → **Routing Rules**, отдельным правилом.

Без этого XRay выходит напрямую, что бы ни стояло в `PROXY_MODE`.

> **Почему правило отдельно.** На 3x-ui 26.9.x панель кладёт вставленный блок `routing` целиком внутрь `routing.rules` — как одно правило без `outboundTag`, и XRay уходит в цикл перезапуска:
>
> ```
> Failure in running xray-core: exit status 23
> failed to build routing configuration > invalid field rule >
> neither outboundTag nor balancerTag is specified in routing rule
> ```
>
> Правило должно лежать в `routing.rules` плоско.

## PROXY_MODE=warp

Скрипт поднимает контейнер `warp` (MicroWARP — WireGuard-туннель Cloudflare средствами ядра, SOCKS5 на `warp:1080`). В панели:

1. **Custom Config** — outbounds из `config/xray/outbound-warp.json`:

   ```json
   {
     "outbounds": [
       { "tag": "warp-out", "protocol": "socks",
         "settings": { "servers": [{ "address": "warp", "port": 1080 }] } },
       { "tag": "direct", "protocol": "freedom" },
       { "tag": "blocked", "protocol": "blackhole", "settings": { "response": { "type": "http" } } }
     ]
   }
   ```

2. **Routing Rules** — правило из `config/xray/routing-warp.json`:

   ```json
   { "type": "field", "outboundTag": "warp-out", "network": "tcp,udp" }
   ```

3. Туда же, **ниже** warp, — запасное правило из `config/xray/routing-direct.json`:

   ```json
   { "type": "field", "outboundTag": "direct", "network": "tcp,udp" }
   ```

4. **Restart XRay**.

> **Зачем запасное правило.** Пока оно ниже warp, оно не срабатывает. Но если WARP сломается — протухнет ключ, ляжет Cloudflare, — весь трафик встанет. Тогда правило поднимается выше warp прямо в панели, и трафик идёт напрямую с IP сервера: два клика, без SSH. Обратно — так же.
>
> Порядок outbounds тоже важен: `warp-out` первым, потому что первый outbound — маршрут по умолчанию.

Проверка:

```bash
docker exec 3x-ui curl -s -x socks5://warp:1080 https://ifconfig.me   # IP Cloudflare, не IP сервера
```

Обновление стека с WARP — всегда с обоими compose-файлами:

```bash
docker compose -f docker-compose.yml -f docker-compose.warp.yml up -d
```

## PROXY_MODE=vps

Трафик уходит через второй сервер по VLESS/TCP/TLS:

```env
PROXY_MODE=vps
VPS_ADDRESS=1.2.3.4
VPS_PORT=443
VPS_UUID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
VPS_SNI=yourexit.domain.com
```

Скрипт сгенерирует `outbound-config.json` и выведет его — вставить в **Custom Config**, правило из `config/xray/routing-vps.json` (и при желании запасное `routing-direct.json` ниже) — в **Routing Rules**. Затем **Restart XRay**.
