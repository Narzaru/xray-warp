#!/usr/bin/env python3
"""
Превращает ссылку, скопированную из панели 3x-ui, в рабочую.

Панель не знает, что перед XRay стоит Caddy, и генерирует ссылку по данным
inbound: порт 10000 (внутренний) и security=none (TLS снимает Caddy, а не
XRay). Импортировать её как есть нельзя — клиент пойдёт не туда и не по TLS.

Скрипт правит эти поля и на выходе даёт два варианта:
  1. Исправленную vless://-ссылку с xmux в параметре extra — основной вариант,
     импортируется в любой клиент (v2rayNG, iOS-клиенты на Xray-core).
     Клиенты, не знающие extra, просто игнорируют его.
  2. Полный JSON-конфиг — когда нужен ещё и IP в dns.hosts (--ip), чтобы
     убрать резолв домена из критического пути.

Использование:
    python3 tools/xui-link.py '<ссылка из панели>'
    python3 tools/xui-link.py '<ссылка>' --mode packet-up --connections 2-4
    python3 tools/xui-link.py '<ссылка>' --ip 203.0.113.10
    python3 tools/xui-link.py '<ссылка>' --link-only
"""

import argparse
import json
import sys
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

PUBLIC_PORT = 443  # снаружи слушает sslh, а не XRay


def build_extra(connections):
    """Объект extra транспорта xhttp — общий для ссылки и JSON."""
    return {
        "xPaddingBytes": "100-1000",
        "xmux": {"maxConnections": connections, "maxConcurrency": 0},
    }


def parse_link(link):
    link = link.strip()
    if not link.startswith("vless://"):
        sys.exit("Ожидается ссылка vless://…  (Variant A — xHTTP)")

    u = urlparse(link)
    if not u.username:
        sys.exit("В ссылке нет UUID клиента")

    params = {k: v[0] for k, v in parse_qs(u.query).items()}
    return {
        "uuid": u.username,
        "address": u.hostname,
        "port": u.port or PUBLIC_PORT,
        "params": params,
        "tag": unquote(u.fragment) if u.fragment else "",
    }


def fix_params(src, mode, connections):
    """Приводит параметры к тому, что реально ждёт клиент."""
    p = dict(src["params"])
    domain = src["address"]

    p["encryption"] = "none"          # обязателен в VLESS-URI
    p["security"] = "tls"             # TLS у клиента с Caddy, не с XRay
    p["type"] = p.get("type", "xhttp")
    p["path"] = p.get("path", "/game")
    p["host"] = p.get("host") or domain
    p["sni"] = p.get("sni") or domain
    p["alpn"] = "h2"                  # stream-one требует HTTP/2
    p["fp"] = p.get("fp") or "firefox"
    p["mode"] = mode
    p["extra"] = json.dumps(build_extra(connections), separators=(",", ":"))

    # Панель кладёт сюда пути к сертификатам и прочее, клиенту не нужное.
    for junk in ("flow", "headerType", "serviceName", "seed", "spx", "pbk", "sid"):
        p.pop(junk, None)
    return {k: v for k, v in p.items() if v not in ("", None)}


def build_link(src, params, mode):
    tag = src["tag"] or f"{src['address']}-{mode}"
    # path кодируем целиком (%2Fgame) — часть клиентов иначе теряет слеш
    query = urlencode(params, safe="", quote_via=quote)
    return f"vless://{src['uuid']}@{src['address']}:{PUBLIC_PORT}?{query}#{quote(tag)}"


def build_config(src, params, mode, connections):
    domain = src["address"]
    xhttp = {
        "extra": build_extra(connections),
        "host": params["host"],
        "mode": mode,
        "path": params["path"],
    }

    return {
        "dns": {"hosts": {domain: ""}, "servers": ["1.1.1.1"], "tag": "dns-module"},
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
                        "alpn": ["h2", "http/1.1"],
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


def main():
    # Консоль Windows по умолчанию не в UTF-8 — иначе падает на рамках вывода.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Чинит ссылку из панели 3x-ui")
    ap.add_argument("link", nargs="?", help="ссылка vless:// из панели (или stdin)")
    ap.add_argument("--mode", default="stream-one",
                    choices=["stream-one", "stream-up", "packet-up", "auto"],
                    help="режим XHTTP (по умолчанию stream-one)")
    # Больше 3 одновременных рукопожатий к одному хосту ТСПУ замораживает
    # на ~120 с (2026), поэтому по умолчанию держимся ниже порога.
    ap.add_argument("--connections", default="2-3",
                    help="xmux maxConnections, например 2-3 (по умолчанию 2-3)")
    ap.add_argument("--ip", default="",
                    help="IP сервера — пропишется в dns.hosts, чтобы убрать "
                         "резолв из критического пути")
    ap.add_argument("--link-only", action="store_true", help="только ссылка, без JSON")
    args = ap.parse_args()

    raw = args.link or sys.stdin.read()
    src = parse_link(raw)
    params = fix_params(src, args.mode, args.connections)

    print("── Исправленная ссылка (с xmux) ─────────────────────────────────")
    print(build_link(src, params, args.mode))
    print()

    if args.link_only:
        return

    cfg = build_config(src, params, args.mode, args.connections)
    if args.ip:
        cfg["dns"]["hosts"][src["address"]] = args.ip
    else:
        cfg["dns"].pop("hosts")

    print("── JSON-конфиг (v2rayNG → Custom Config) ────────────────────────")
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    print()


if __name__ == "__main__":
    main()
