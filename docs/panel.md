# Настройка XRay в панели 3x-ui

## Доступ к панели

Порт 2053 слушает только `127.0.0.1` сервера — панель доступна через SSH-туннель:

```bash
# Обычно — через 443 (sslh пропускает SSH)
ssh -L 2053:localhost:2053 -p 443 root@yourdomain.com

# Если sslh ещё не поднят — через обычный SSH-порт
ssh -L 2053:localhost:2053 root@yourdomain.com
```

Браузер: `http://localhost:2053`, логин `admin` / пароль `admin`.

## Inbound

`Inbounds → Add Inbound`:

| Поле | Значение |
|---|---|
| Protocol | VLESS |
| Port | 10000 |
| Transmission | xHTTP |
| Path | `/game` |
| Host | `yourdomain.com` |
| Mode | `auto` (режим выбирает клиент) |
| Security | **None** |

> ⚠️ **Security именно `None`.** TLS снимает Caddy ([почему](architecture.md#почему-tls-снимает-caddy-а-не-xray)). Если включить TLS и здесь, Caddy будет слать h2c в TLS-порт, и туннель не поднимется. Пути к сертификатам не нужны.

Затем **Settings → Xray Configs → Advanced**: заменить блок `policy` на `config/xray/policy.json` и **Restart XRay** — иначе XRay рвёт соединения, молчащие 5 минут ([подробно](network.md#xray-connidle)).

При `PROXY_MODE=warp` или `vps` — ещё outbound и правила маршрутизации: [docs/outbound.md](outbound.md).

### TCP/RAW (не поддерживается текущей схемой)

Caddy понимает HTTP, а не сырой VLESS, поэтому TCP/RAW через него не пройдёт. Чтобы его использовать, TLS придётся вернуть в XRay:

1. В `docker-compose.yml`, в конфиге `sslh-cfg`, направить протоколы `tls` и `anyprot` на `3x-ui:10000` вместо `caddy:8443`.
2. В inbound: Transmission TCP (RAW), Security TLS, Certificate `/etc/letsencrypt/live/yourdomain.com/fullchain.pem`, Key `/etc/letsencrypt/live/yourdomain.com/privkey.pem`, ALPN `http/1.1`.
3. В Fallbacks: Dest `caddy:8080`, xVer `0` — фейковый сайт будет отдаваться через fallback.

## Ссылка для клиента

**Ссылку, которую генерирует 3x-ui, импортировать нельзя.** Панель не знает, что перед XRay стоит Caddy:

| Поле | Панель отдаёт | Должно быть | Почему |
|---|---|---|---|
| порт | `10000` | `443` | 10000 — внутренний порт XRay, снаружи слушает sslh |
| `security` | `none` | `tls` | клиент устанавливает TLS с Caddy |
| `encryption` | иногда отсутствует | `none` | обязателен в VLESS-URI |
| `alpn` | пусто | `h2` | `stream-one` работает только поверх HTTP/2 |
| `sni` | пусто | домен | иначе рукопожатие уйдёт не на тот виртуальный хост |
| `mode` | `auto` | явный режим | [режимы xHTTP](network.md#режимы-xhttp) |

Чинится одной командой:

```bash
python3 tools/xui-link.py '<ссылка из панели>'
```

Результат:

1. **Ссылка с xmux** — основной вариант. Настройки xmux едут в параметре `extra`; v2rayNG и клиенты на свежем Xray-core его понимают, остальные игнорируют.
2. **JSON-конфиг** — когда нужен ещё и IP в `dns.hosts` (`--ip`): ссылкой это не передать. В v2rayNG — **Настройки → Custom Config**, в Hiddify и NekoBox — custom outbound.

```bash
python3 tools/xui-link.py '<ссылка>' --mode packet-up --connections 2-4  # другой режим и число соединений
python3 tools/xui-link.py '<ссылка>' --ip 203.0.113.10                   # IP в dns.hosts
python3 tools/xui-link.py '<ссылка>' --link-only                         # только ссылка
```

## Ручная настройка клиента

| Поле | Значение |
|---|---|
| Address | `yourdomain.com` |
| Port | **443** |
| Protocol | VLESS, encryption `none` |
| Transport | xHTTP, path `/game`, host `yourdomain.com`, mode `stream-one` |
| Security | TLS, SNI `yourdomain.com`, ALPN `h2`, fingerprint `firefox` |
| Mux | **выключен** ([почему](network.md#xmux)) |
| IPv6 | **выключен** (v2rayNG: «Enable IPv6») — [почему](network.md#ipv6) |
