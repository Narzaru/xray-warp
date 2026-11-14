#!/usr/bin/env python3
"""
Превращает ссылку, скопированную из панели 3x-ui, в рабочую.

Панель не знает, что перед XRay стоит Caddy, и генерирует ссылку по данным
inbound: порт 10000 (внутренний) и security=none (TLS снимает Caddy, а не
XRay). Импортировать её как есть нельзя — клиент пойдёт не туда и не по TLS.

Скрипт правит эти поля и на выходе даёт два варианта:
  1. Исправленную vless://-ссылку с xmux в параметре extra — основной вариант,
     импортируется в клиенты на Xray-core с поддержкой extra.
     Клиенты, не знающие extra, просто игнорируют его.
  2. Полный JSON-конфиг — когда нужен ещё и IP в dns.hosts (--ip), чтобы
     убрать резолв домена из критического пути.

Использование:
    python3 tools/xui-link.py '<ссылка из панели>'
    python3 tools/xui-link.py '<ссылка>' --mode packet-up --connections 2-4
    python3 tools/xui-link.py '<ссылка>' --ip 203.0.113.10
    python3 tools/xui-link.py '<ссылка>' --link-only
    python3 tools/xui-link.py '<ссылка>' --output-dir client-configs/example
    python3 tools/xui-link.py '<ссылка>' --reusable-secs 900-1800 --keepalive 0
"""

import argparse
import ipaddress
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

PUBLIC_PORT = 443  # снаружи слушает sslh, а не XRay
DEFAULT_MODE = "stream-up"
DEFAULT_CONNECTIONS = "2-3"
DEFAULT_REUSABLE_SECS = "1800-3000"
DEFAULT_KEEPALIVE = 0


def build_extra(connections, reusable_secs=DEFAULT_REUSABLE_SECS,
                keepalive=DEFAULT_KEEPALIVE):
    """Объект extra транспорта xhttp — общий для ссылки и JSON."""
    return {
        "xPaddingBytes": "100-1000",
        # Встроенные умолчания xmux Xray подставляет, только если объект
        # пустой. Поэтому лимиты переиспользования задаём явно.
        # После hMaxReusableSecs старое соединение не получает новых
        # запросов, но текущие продолжают работать до завершения.
        # hKeepAlivePeriod=0 выбирает стандартный интервал (H2: 45 с),
        # а не выключает keepalive; отрицательное значение выключает его.
        "xmux": {
            "maxConnections": connections,
            "maxConcurrency": 0,
            "hMaxRequestTimes": "600-900",
            "hMaxReusableSecs": reusable_secs,
            "hKeepAlivePeriod": keepalive,
        },
    }


def parse_link(link):
    link = link.strip()
    if not link.startswith("vless://"):
        sys.exit("Ожидается ссылка vless://…  (Variant A — xHTTP)")

    u = urlparse(link)
    if not u.username:
        sys.exit("В ссылке нет UUID клиента")
    if not u.hostname:
        sys.exit("В ссылке нет адреса сервера")

    params = {k: v[0] for k, v in parse_qs(u.query).items()}
    return {
        "uuid": u.username,
        "address": u.hostname,
        "port": u.port or PUBLIC_PORT,
        "params": params,
        "tag": unquote(u.fragment) if u.fragment else "",
    }


def fix_params(src, mode, connections, reusable_secs=DEFAULT_REUSABLE_SECS,
               keepalive=DEFAULT_KEEPALIVE):
    """Приводит параметры к тому, что реально ждёт клиент."""
    p = dict(src["params"])
    domain = src["address"]

    p["encryption"] = "none"          # обязателен в VLESS-URI
    p["security"] = "tls"             # TLS у клиента с Caddy, не с XRay
    p["type"] = "xhttp"
    p["path"] = p.get("path", "/game")
    p["host"] = p.get("host") or p.get("sni") or domain
    p["sni"] = p.get("sni") or p["host"]
    p["alpn"] = "h2"                  # внешний транспорт до Caddy — HTTP/2
    p["fp"] = p.get("fp") or "firefox"
    p["mode"] = mode
    p["extra"] = json.dumps(
        build_extra(connections, reusable_secs, keepalive), separators=(",", ":")
    )

    # Панель кладёт сюда пути к сертификатам и прочее, клиенту не нужное.
    for junk in ("flow", "headerType", "serviceName", "seed", "spx", "pbk", "sid"):
        p.pop(junk, None)
    return {k: v for k, v in p.items() if v not in ("", None)}


def build_link(src, params, mode):
    tag = src["tag"] or f"{src['address']}-{mode}"
    # path кодируем целиком (%2Fgame) — часть клиентов иначе теряет слеш
    query = urlencode(params, safe="", quote_via=quote)
    address = src["address"]
    authority = f"[{address}]" if ":" in address else address
    return f"vless://{src['uuid']}@{authority}:{PUBLIC_PORT}?{query}#{quote(tag)}"


def build_config(src, params, server_ip=None):
    domain = src["address"]
    mode = params["mode"]
    # Resolve the tunnel endpoint without sending DNS through the unopened
    # tunnel. A pinned IPv4/IPv6, when supplied, takes precedence.
    dns = {"servers": ["localhost"]}
    if server_ip:
        dns["hosts"] = {domain: str(server_ip)}
    xhttp = {
        # Ссылка и JSON должны содержать одни и те же параметры extra.
        "extra": json.loads(params["extra"]),
        "host": params["host"],
        "mode": mode,
        "path": params["path"],
    }

    return {
        "dns": dns,
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": 10808,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True, "userLevel": 8},
                "sniffing": {
                    "destOverride": ["http", "tls", "quic"],
                    "enabled": True,
                    "routeOnly": False,
                },
                "tag": "socks",
            }
        ],
        "log": {"loglevel": "warning"},
        "outbounds": [
            {
                # mux.cool выключен намеренно: он сложил бы потоки обратно
                # в одно соединение и отменил эффект от xmux.
                "mux": {"concurrency": -1, "enabled": False},
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": domain,
                            "port": PUBLIC_PORT,
                            "users": [
                                {
                                    "encryption": "none",
                                    "flow": "",
                                    "id": src["uuid"],
                                    "level": 8,
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": {
                    "network": "xhttp",
                    "security": "tls",
                    "sockopt": {"domainStrategy": "UseIP"},
                    "tlsSettings": {
                        "allowInsecure": False,
                        "alpn": params["alpn"].split(","),
                        "fingerprint": params["fp"],
                        "serverName": params["sni"],
                    },
                    "xhttpSettings": xhttp,
                },
                "tag": "proxy",
            },
            {"protocol": "freedom", "tag": "direct"},
            {
                "protocol": "blackhole",
                "settings": {"response": {"type": "http"}},
                "tag": "block",
            },
        ],
        "remarks": src["tag"] or f"{domain}-{mode}",
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [{"outboundTag": "proxy", "port": "0-65535", "type": "field"}],
        },
    }


def positive_range(value):
    """Положительное число или диапазон Xray Int32Range."""
    parts = value.split("-")
    if len(parts) not in (1, 2) or not all(p.isascii() and p.isdigit() for p in parts):
        raise argparse.ArgumentTypeError("ожидается число или диапазон, например 2-3")
    bounds = [int(p) for p in parts]
    if not all(1 <= n <= 2147483647 for n in bounds) or bounds[0] > bounds[-1]:
        raise argparse.ArgumentTypeError("нужен положительный диапазон от меньшего к большему")
    return value


def reusable_range(value):
    # Ноль выключает ограничение по времени, а не возвращает умолчание.
    return value if value == "0" else positive_range(value)


def main():
    # Консоль Windows по умолчанию не в UTF-8 — иначе падает на рамках вывода.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Чинит ссылку из панели 3x-ui")
    ap.add_argument("link", nargs="?", help="ссылка vless:// из панели (или stdin)")
    ap.add_argument("--mode", default=DEFAULT_MODE,
                    choices=["stream-one", "stream-up", "packet-up", "auto"],
                    help="режим XHTTP (по умолчанию stream-up; auto зависит от версии ядра)")
    ap.add_argument("--connections", default=DEFAULT_CONNECTIONS, type=positive_range,
                    help="xmux maxConnections, например 2-3 (по умолчанию 2-3)")
    ap.add_argument("--reusable-secs", default=DEFAULT_REUSABLE_SECS, type=reusable_range,
                    help="xmux hMaxReusableSecs (по умолчанию 1800-3000; 0 — без лимита)")
    ap.add_argument("--keepalive", default=DEFAULT_KEEPALIVE, type=int,
                    help="xmux hKeepAlivePeriod в секундах (0 — стандартный интервал, "
                         "отрицательное значение — выключить)")
    ap.add_argument("--ip", type=ipaddress.ip_address,
                    help="IP сервера — пропишется в dns.hosts, чтобы убрать "
                         "резолв из критического пути")
    ap.add_argument("--link-only", action="store_true", help="только ссылка, без JSON")
    ap.add_argument("--output-dir", type=Path,
                    help="сохранить link.txt и config.json в указанную папку")
    args = ap.parse_args()

    raw = args.link or sys.stdin.read()
    src = parse_link(raw)
    params = fix_params(src, args.mode, args.connections, args.reusable_secs, args.keepalive)
    link = build_link(src, params, args.mode)
    cfg = build_config(src, params, args.ip)

    config_text = json.dumps(cfg, ensure_ascii=False, indent=2)
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "link.txt").write_text(link + "\n", encoding="utf-8")
        if not args.link_only:
            (args.output_dir / "config.json").write_text(config_text + "\n", encoding="utf-8")
        print(f"Сохранено в {args.output_dir.resolve()}")
    elif args.link_only:
        print(link)
    else:
        print("── Исправленная ссылка (с xmux) ─────────────────────────────────")
        print(link)
        print()
        print("── JSON-конфиг (v2rayNG → Custom Config) ────────────────────────")
        print(config_text)
        print()


if __name__ == "__main__":
    main()
