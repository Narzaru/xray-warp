> Написано с помощью [Claude Code](https://claude.ai/code)

# 3x-ui · XRay · Caddy · sslh — Docker Setup

XRay-прокси (VLESS + xHTTP) на порту 443 за настоящим HTTPS-сайтом. Один домен, одна A-запись, SSH на том же порту. Снаружи сервер выглядит как обычный сайт с сертификатом Let's Encrypt.

```
:443/tcp ─► sslh ─┬─ SSH ─► host sshd
                  └─ TLS ─► Caddy ─┬─ /game ─► XRay (3x-ui) ─► direct / WARP / exit VPS
                                   └─ всё остальное ─► фейковый сайт
:443/udp ─► Hysteria2 (опционально)
```

## Быстрый старт

Нужны Ubuntu 24.04/26.04, публичный IP и A-запись домена на него.

```bash
apt-get install -y docker.io docker-compose-v2 gettext-base
git clone https://github.com/Narzaru/xray-warp.git /opt/x-ui && cd /opt/x-ui
cp .env.example .env && nano .env      # DOMAIN, EMAIL, SSH_PORT, PROXY_MODE
sudo ./setup-3xui.sh
```

Дальше:

1. Открыть панель через SSH-туннель: `ssh -L 2053:localhost:2053 -p 443 root@yourdomain.com` → `http://localhost:2053`.
2. Создать inbound VLESS / xHTTP / порт 10000 / **Security: None** — [подробно](docs/panel.md#inbound).
3. Применить `config/xray/policy.json` (Settings → Xray Configs) — [зачем](docs/network.md#таймауты-простоя).
4. Прогнать ссылку клиента через `python3 tools/xui-link.py '<ссылка>'` — ссылка из панели как есть не работает, [почему](docs/panel.md#ссылка-для-клиента).

## Документация

| Файл | О чём |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Как устроен стек, зачем Caddy и почему TLS снимает он, структура файлов |
| [docs/install.md](docs/install.md) | Требования, `.env`, шаги скрипта, фаервол, сертификат |
| [docs/panel.md](docs/panel.md) | Inbound в 3x-ui, параметры клиента, `xui-link.py` |
| [docs/network.md](docs/network.md) | Мобильные сети: режимы xHTTP, xmux, BBR, таймауты простоя, диагностика TCP |
| [docs/outbound.md](docs/outbound.md) | Смена выходного IP: `PROXY_MODE=warp` / `vps` |
| [docs/hysteria.md](docs/hysteria.md) | Hysteria2 (QUIC/UDP) для каналов с потерями |
| [docs/operations.md](docs/operations.md) | Повседневные команды, автообновление, логи |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Ошибки по симптомам |
