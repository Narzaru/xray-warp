#!/usr/bin/env python3
"""Offline: resolve the supported Compose overlays without a Docker daemon."""
import itertools
import json
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Profiles(unittest.TestCase):
    def test_resolved_profiles(self):
        # Set COMPOSE_BINARY for a standalone Compose v2 executable.
        cmd = [os.environ['COMPOSE_BINARY']] if os.getenv('COMPOSE_BINARY') else ['docker', 'compose']
        for ipv6, warp, hysteria in itertools.product((False, True), ('off', 'microsocks', 'hev'), (False, True)):
            with self.subTest(ipv6=ipv6, warp=warp, hysteria=hysteria):
                files = ['docker-compose.yml']
                if ipv6:
                    files.append('docker-compose.ipv6.yml')
                if warp != 'off':
                    files.append('docker-compose.warp.yml')
                if warp == 'hev':
                    files.append('docker-compose.warp-udp.yml')
                if hysteria:
                    files.append('docker-compose.hysteria.yml')
                args = [flag for name in files for flag in ('-f', name)]
                config = json.loads(subprocess.check_output(
                    [*cmd, '--env-file', '.env.example', *args, 'config', '--format', 'json'],
                    cwd=ROOT, env={**os.environ, 'SSH_PORT': '2222'}))
                services = config['services']
                expected = {'sslh', '3x-ui', 'caddy', 'certbot'}
                if hysteria:
                    expected.add('hysteria')
                if warp != 'off':
                    expected.add('warp')
                self.assertEqual(set(services), expected)
                self.assertEqual(config['networks']['xray-net'].get('enable_ipv6', False), ipv6)
                sslh = config['configs']['sslh-cfg']['content']
                self.assertEqual('host: "::";' in sslh, ipv6)
                for token in ('port: "2222";', 'numeric: true;', 'resolve_on_forward: true;'):
                    self.assertIn(token, sslh)
                panel_ports = services['3x-ui']['ports']
                self.assertEqual([(p.get('host_ip'), p['target']) for p in panel_ports], [('127.0.0.1', 2053)])
                if warp != 'off':
                    self.assertFalse(services['warp'].get('ports'))
                    self.assertEqual('build' in services['warp'], warp == 'hev')
                    self.assertEqual(services['3x-ui']['depends_on']['warp']['condition'], 'service_healthy')
                    if warp == 'hev':
                        self.assertEqual(services['warp']['pull_policy'], 'never')
                if hysteria:
                    self.assertEqual([(p['target'], p['protocol']) for p in services['hysteria']['ports']], [(443, 'udp')])


if __name__ == '__main__':
    unittest.main()
