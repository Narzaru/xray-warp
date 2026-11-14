#!/usr/bin/env python3
"""Offline configuration and rollback checks; run on the Linux VPS."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
import time
from types import SimpleNamespace
import unittest


SCRIPT = Path(__file__).with_name('sslh-numeric.py')


class NumericTests(unittest.TestCase):
    def test_single_insertion(self):
        enable = runpy.run_path(str(SCRIPT))['enable_numeric']
        for ending in ('\n', '\r\n'):
            old = f'content: |{ending}      foreground: true;{ending}      protocols: ();{ending}'
            new = enable(old)
            self.assertEqual(new.replace('      numeric: true;' + ending, ''), old)
        for invalid in ('numeric: false;\nforeground: true;\n',
                        'foreground: true;\nforeground: true;\n', 'foreground: false;\n'):
            with self.assertRaises(RuntimeError):
                enable(invalid)

    def test_rollout_scenarios(self):
        for scenario in ('success', 'file_drift', 'tampered_candidate', 'persisted_config_failure',
                         'partial_recreate_failure', 'ssh_failure', 'tls_failure', 'xhttp_failure'):
            with self.subTest(scenario=scenario):
                self.run_scenario(scenario)

    def run_scenario(self, scenario):
        module = runpy.run_path(str(SCRIPT))
        scope = module['apply'].__globals__
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'backups' / 'prepared'
            stage.mkdir(parents=True)
            original = b'foreground: true;\nprotocols: ();\n'
            candidate = module['enable_numeric'](original.decode()).encode()
            source = root / 'docker-compose.yml'
            source.write_bytes(original)
            for name, data in [('compose.before.yml', original), ('compose.after.yml', candidate),
                               ('sslh.after.cfg', candidate), ('PREPARED', b'ok')]:
                (stage / name).write_bytes(data)
            services = ('sslh', 'caddy', '3x-ui', 'warp', 'certbot')
            current = {name: {'Id': 'old-' + name, 'Image': 'image-' + name,
                              'State': {'Running': True}, 'RestartCount': 0} for name in services}
            hashes = {'config': 'before'}
            manifest = {'prepared_at': time.time(), 'file_hashes': hashes,
                        'container_ids': {s: c['Id'] for s, c in current.items()},
                        'image': 'image-sslh', 'before_sha256': module['digest'](original),
                        'after_sha256': module['digest'](candidate), 'source_mode': 0o640,
                        'helper_sha256': module['digest'](SCRIPT.with_name('update-containers.py').read_bytes())}
            (stage / 'manifest.json').write_text(json.dumps(manifest))
            if scenario == 'tampered_candidate':
                (stage / 'compose.after.yml').write_bytes(candidate + b'# tampered\n')
            commands, recreates, probes = [], [], {'ssh': 0, 'tls': 0, 'xhttp': 0}

            def run(*args, **kwargs):
                commands.append(args)
                if args[:4] == ('docker', 'exec', 'sslh', 'cat'):
                    return source.read_text()
                content = 'wrong' if scenario == 'persisted_config_failure' else candidate.decode()
                return json.dumps({'configs': {'sslh-cfg': {'content': content}}})

            def compose_up(directory):
                recreates.append(1)
                current['sslh']['Id'] = 'new-sslh'
                if scenario == 'partial_recreate_failure' and len(recreates) == 1:
                    raise RuntimeError('injected partial recreation failure')

            def probe(kind):
                probes[kind] += 1
                failing_call = 2 if kind == 'xhttp' else 1
                if scenario == kind + '_failure' and probes[kind] == failing_call:
                    raise RuntimeError('injected probe failure')
                return {'warp': 'on'} if kind == 'xhttp' else .01

            helper = SimpleNamespace(
                SERVICES=services, COMPOSE=['compose'], run=run,
                file_hashes=lambda: {'config': 'changed'} if scenario == 'file_drift' else hashes,
                inspect=lambda name: copy.deepcopy(current[name]),
                preflight=lambda: ({}, copy.deepcopy(current), {}),
                smoke=lambda directory: probe('xhttp'),
                save_json=lambda path, data: path.write_text(json.dumps(data)))
            scope.update(ROOT=root, H=helper, compose_up=compose_up,
                         ssh_probe=lambda address: probe('ssh'), tls_probe=lambda address, stage: probe('tls'))
            with contextlib.redirect_stdout(io.StringIO()):
                if scenario == 'success':
                    module['apply'](stage)
                else:
                    with self.assertRaises(RuntimeError):
                        module['apply'](stage)
            if scenario == 'success':
                self.assertEqual(source.read_bytes(), candidate)
                if os.name == 'posix':  # NTFS chmod does not represent POSIX access bits.
                    self.assertEqual(source.stat().st_mode & 0o777, 0o640)
                self.assertTrue((stage / 'SUCCESS').is_file())
                self.assertEqual(len(recreates), 1)
            elif scenario in ('file_drift', 'tampered_candidate'):
                self.assertEqual(recreates, [])
                self.assertEqual(source.read_bytes(), original)
            else:
                self.assertEqual(source.read_bytes(), original)
                self.assertTrue((stage / 'ROLLED_BACK').is_file())
                self.assertGreaterEqual(probes['ssh'], 1)
                self.assertGreaterEqual(probes['tls'], 1)
                self.assertGreaterEqual(probes['xhttp'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
