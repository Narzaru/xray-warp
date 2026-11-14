#!/usr/bin/env python3
"""Offline failure-injection checks. Run on Linux: python3 test-update-containers.py."""
import contextlib
import copy
import io
import json
from pathlib import Path
import runpy
import tempfile
import time
import types
import unittest


class RolloutTests(unittest.TestCase):
    def test_project_profile_and_recovery(self):
        module = runpy.run_path(str(Path(__file__).with_name('update-containers.py')))
        services = module['SERVICES']
        without_panel = tuple(s for s in services if s != '3x-ui')
        for names, ipv6, recovering, accepted in [
            (services, False, False, True), (services, True, False, False),
            ((*services, 'hysteria'), False, False, False),
            (without_panel, False, True, True),
            (without_panel, False, False, False),
        ]:
            with self.subTest(names=names, ipv6=ipv6, recovering=recovering):
                def run(*args):
                    if args[1] == 'ps':
                        return '\n'.join(names)
                    return json.dumps([{'EnableIPv6': ipv6}])
                check = module['check_project_profile']
                check.__globals__['run'] = run
                if accepted:
                    check(recovering_panel=recovering)
                else:
                    with self.assertRaises(RuntimeError):
                        check(recovering_panel=recovering)

    def test_local_warp_is_not_pulled(self):
        module = runpy.run_path(str(Path(__file__).with_name('update-containers.py')))
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            return json.dumps([{'Id': 'local-tested-image'}])
        module['refresh_image'].__globals__['run'] = run
        with contextlib.redirect_stdout(io.StringIO()):
            actual = module['refresh_image']('warp', {
                'image': module['LOCAL_WARP_IMAGE'], 'build': {'context': '/test'},
                'pull_policy': 'never'}, None)
        self.assertEqual(actual, 'local-tested-image')
        self.assertEqual(calls, [('docker', 'image', 'inspect', module['LOCAL_WARP_IMAGE'])])
        with self.assertRaises(RuntimeError):
            module['refresh_image']('3x-ui', {'image': 'custom', 'build': {'context': '/test'}}, None)

    def scenario(self, scenario):
        module = runpy.run_path(str(Path(__file__).with_name('update-containers.py')))
        scope = module['apply'].__globals__
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'backups' / 'prepared'
            stage.mkdir(parents=True)
            (stage / 'PREPARED').write_text('ok')
            services = module['SERVICES']
            current = {s: {'Id': f'old-{s}', 'Image': f'image-old-{s}',
                           'Mounts': [], 'RestartCount': 0} for s in services}
            hashes = {'config': 'original'}
            manifest = {'prepared_at': time.time(), 'files': hashes,
                        'container_ids': {s: c['Id'] for s, c in current.items()},
                        'old_images': {s: c['Image'] for s, c in current.items()},
                        'new_images': {s: c['Image'] for s, c in current.items()},
                        'refs': {s: f'image:{s}' for s in services},
                        'changed': [] if scenario == 'no_update' else ['caddy']}
            manifest['new_images']['caddy'] = 'image-new-caddy'
            (stage / 'manifest.json').write_text(json.dumps(manifest))
            current['caddy']['Mounts'] = [{'Destination': '/data', 'Source': tmp}]
            calls, smokes, waits = [], [], []

            def run(*args, **kwargs):
                calls.append(args)
                if args[0] == 'tar' and scenario == 'backup_failure':
                    raise RuntimeError('injected backup failure')
                return ''

            def compose_up(directory, images, service):
                calls.append(('up', images, service))
                if images == 'new-images.json':
                    current['caddy'].update(Id='new-caddy', Image='image-new-caddy')
                    if scenario == 'partial_recreate_failure':
                        raise RuntimeError('injected partial recreation failure')
                else:
                    current['caddy'].update(Id='rollback-caddy', Image='image-old-caddy')

            def wait_caddy(directory):
                waits.append(1)
                if scenario == 'health_failure' and len(waits) == 1:
                    raise RuntimeError('injected health failure')

            def smoke(directory):
                smokes.append(1)
                if scenario == 'smoke_failure' and len(smokes) == 2:
                    raise RuntimeError('injected final XHTTP check failure')
                return {'warp': 'on'}

            class Connection:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def settimeout(self, seconds):
                    pass

                def sendall(self, value):
                    pass

                def recv(self, size):
                    return b'invalid' if scenario == 'ssh_failure' else b'SSH-2.0-Test\r\n'

            scope.update(ROOT=root, run=run, compose_up=compose_up, wait_caddy=wait_caddy,
                         smoke=smoke, file_hashes=lambda: {'config': 'changed'}
                         if scenario == 'config_drift' else hashes,
                         preflight=lambda: ({}, copy.deepcopy(current), {}),
                         inspect=lambda name: copy.deepcopy(current[name]),
                         socket=types.SimpleNamespace(create_connection=lambda *a, **kw: Connection()))
            expected_failure = scenario not in ('success', 'no_update')
            with contextlib.redirect_stdout(io.StringIO()):
                if expected_failure:
                    with self.assertRaises(RuntimeError):
                        module['apply'](stage)
                else:
                    module['apply'](stage)
            if scenario in ('config_drift', 'no_update'):
                self.assertEqual(calls, [])
            elif scenario == 'backup_failure':
                self.assertIn(('docker', 'start', 'certbot'), calls)
                self.assertFalse(any(c[0] == 'up' for c in calls))
            elif expected_failure:
                self.assertEqual(current['caddy']['Image'], 'image-old-caddy')
                self.assertTrue((stage / 'ROLLED_BACK').is_file())
                self.assertGreaterEqual(len(smokes), 2)
            else:
                self.assertTrue((stage / 'SUCCESS').is_file())
                self.assertEqual(current['caddy']['Image'], 'image-new-caddy')
            self.assertFalse(any(c[0] == 'up' and c[-1] != 'caddy' for c in calls))

    def test_rollout_and_failures(self):
        for scenario in ('success', 'no_update', 'config_drift', 'backup_failure',
                         'partial_recreate_failure', 'health_failure', 'smoke_failure', 'ssh_failure'):
            with self.subTest(scenario=scenario):
                self.scenario(scenario)


if __name__ == '__main__':
    unittest.main(verbosity=2)
