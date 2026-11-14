#!/usr/bin/env python3
"""Offline regression checks for reference routing, SQLite writes and client links."""
import copy
import ipaddress
import json
import runpy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

TOOLS = Path(__file__).resolve().parent
CONFIG = TOOLS.parent / 'config/xray'
block = runpy.run_path(str(TOOLS / 'block-ru.py'))
warp = runpy.run_path(str(TOOLS / 'enable-warp.py'))
direct = runpy.run_path(str(TOOLS / 'xray-direct.py'))
link = runpy.run_path(str(TOOLS / 'xui-link.py'))


def snippet(name):
    return json.loads((CONFIG / name).read_text())


class PolicyTests(unittest.TestCase):
    def test_migrations_reproduce_runtime_reference(self):
        routing = snippet('routing-warp-ru.json')
        outbounds = snippet('outbound-warp.json')['outbounds']
        rules = routing['rules']
        original = {'routing': {'domainStrategy': 'AsIs', 'rules': [rules[0], *rules[3:5]]},
                    'outbounds': outbounds[1:]}
        blocked = block['routing_config'](original)
        result = warp['transform'](blocked)
        self.assertEqual(result, {'routing': routing, 'outbounds': outbounds})
        self.assertEqual(direct['direct_config'](result), blocked)
        self.assertEqual(rules[-1], snippet('routing-warp.json'))
        pending = copy.deepcopy(result)
        pending['routing']['rules'].insert(1, snippet('youtube-cache-exception.json'))
        with self.assertRaisesRegex(RuntimeError, 'Unexpected WARP routing'):
            direct['direct_config'](pending)

    def test_sqlite_conflicts_preserve_operator_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'x-ui.db'
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)')
                conn.execute('CREATE TABLE inbounds (id INTEGER PRIMARY KEY, sniffing TEXT)')
                conn.execute("INSERT INTO settings VALUES ('xrayTemplateConfig', 'old')")
                conn.execute("INSERT INTO inbounds VALUES (1, 'newer edit')")
            state = dict(db=str(db), inbound_id=1, template_old='old', template_new='candidate',
                         sniff_old='old sniffing', sniff_new='candidate sniffing')
            with self.assertRaisesRegex(RuntimeError, 'Inbound changed'):
                block['write_state'](state)
            self.assertEqual(direct['read_template'](db), 'old')
            with self.assertRaisesRegex(RuntimeError, 'Template changed'):
                direct['set_template'](db, 'old', 'candidate')
            self.assertEqual(direct['read_template'](db), 'old')

    def test_ipv6_link_preserves_the_endpoint_host_and_tls_name(self):
        src = link['parse_link']('vless://example@[2001:db8::1]:10000?host=proxy.example&path=%2Fgame')
        params = link['fix_params'](src, 'stream-up', '2-3')
        uri = urlsplit(link['build_link'](src, params, 'stream-up'))
        self.assertEqual((uri.hostname, uri.port), ('2001:db8::1', 443))
        self.assertEqual(parse_qs(uri.query)['sni'], ['proxy.example'])
        config = link['build_config'](src, params)
        stream = config['outbounds'][0]['streamSettings']
        self.assertEqual(stream['tlsSettings']['serverName'], 'proxy.example')
        self.assertEqual(stream['xhttpSettings']['extra'], json.loads(parse_qs(uri.query)['extra'][0]))

    def test_domain_endpoint_bootstraps_without_the_tunnel(self):
        src = link['parse_link']('vless://example@proxy.example:10000?path=%2Fgame')
        params = link['fix_params'](src, 'stream-up', '2-3')
        self.assertEqual(link['build_config'](src, params)['dns'], {'servers': ['localhost']})
        for pin in ['192.0.2.10', '2001:db8::1']:
            dns = link['build_config'](src, params, ipaddress.ip_address(pin))['dns']
            self.assertEqual(dns['hosts'], {'proxy.example': pin})


if __name__ == '__main__':
    unittest.main()
