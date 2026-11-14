#!/usr/bin/env python3
"""Offline geodata integrity, bootstrap, clock gate and recovery checks."""
import datetime as dt
from contextlib import closing
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('geodata', Path(__file__).with_name('update-geodata.py'))
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)


def vi(n):
    result = b''
    while n > 127:
        result += bytes([(n & 127) | 128])
        n >>= 7
    return result + bytes([n])


def message(number, data):
    return vi(number * 8 + 2) + vi(len(data)) + data


def geoip(categories=('RU', 'PRIVATE')):
    cidr = message(1, bytes([80, 64, 160, 0])) + bytes([16, 20])
    return b''.join(message(1, message(1, c.encode()) + message(2, cidr)) for c in categories)


class GeodataTests(unittest.TestCase):
    def test_checksum_and_truncated_download_rejected(self):
        data = geoip()
        checksum = hashlib.sha256(data).hexdigest() + '  geoip.dat\n'
        G.verify_checksum(data, checksum, 'geoip.dat')
        with self.assertRaises(ValueError):
            G.verify_checksum(data[:-1], checksum, 'geoip.dat')
        with self.assertRaises(ValueError):
            G.verify_checksum(data, checksum.replace('geoip.dat', 'geosite.dat'), 'geoip.dat')

    def test_semantic_categories_and_schema(self):
        valid = G.validate_asset(geoip(), 'geoip.dat')
        self.assertEqual(valid['required_categories'], {'private': 1, 'ru': 1})
        with self.assertRaises(ValueError):
            G.validate_asset(geoip(('RU-BLOCKED',)), 'geoip.dat')
        with self.assertRaises(ValueError):
            G.validate_asset(geoip()[:-1], 'geoip.dat')
        invalid_cidr = message(1, message(1, b'RU') + message(2, message(1, b'bad')))
        with self.assertRaises(ValueError):
            G.validate_asset(invalid_cidr, 'geoip.dat')
        site = message(1, message(1, b'CATEGORY-RU') + message(2, bytes([8, 2]) + message(2, b'example.ru')))
        self.assertEqual(G.validate_asset(site, 'geosite.dat')['entries'], 1)

    def test_novosibirsk_window_and_no_daytime_catchup(self):
        G.nightly_window(dt.datetime(2026, 11, 14, 21, 0, tzinfo=dt.timezone.utc))
        for hour, minute in [(20, 59), (21, 30), (22, 0), (10, 0)]:
            with self.assertRaises(RuntimeError):
                G.nightly_window(dt.datetime(2026, 11, 14, hour, minute, tzinfo=dt.timezone.utc))

    def test_bootstrap_is_narrow_and_refuses_unknown_shape(self):
        base = Path(__file__).parents[1].joinpath('docker-compose.yml')
        if not base.exists():
            base = G.ROOT / 'docker-compose.yml'
        original = base.read_text(encoding='utf-8')
        changed = G.bootstrap_text(original)
        self.assertEqual(changed.count('XRAY_LOCATION_ASSET='), 1)
        self.assertEqual(changed.replace('      - XRAY_LOCATION_ASSET=' + G.ASSET + '\n', '').replace(
            '      - ./geodata:' + G.MOUNT + ':ro\n', ''), original)
        with self.assertRaises(ValueError):
            G.bootstrap_text(changed)
        with self.assertRaises(ValueError):
            G.bootstrap_text(original.replace('      - TZ=${TIMEZONE:-UTC}\n', ''))

    def test_no_change_requires_committed_pair_and_completed_bootstrap(self):
        hashes = {'geoip.dat': 'a', 'geosite.dat': 'b'}
        self.assertTrue(G.confirmed_no_change(False, hashes, hashes, {'hashes': hashes}))
        self.assertFalse(G.confirmed_no_change(True, hashes, hashes, {'hashes': hashes}))
        self.assertFalse(G.confirmed_no_change(False, hashes, hashes, {}))
        self.assertFalse(G.confirmed_no_change(False, hashes, hashes, {'hashes': {'geoip.dat': 'old'}}))
        self.assertFalse(G.confirmed_no_change(False, hashes, hashes, {'hashes': hashes}, routing_change=True))

    def test_exact_exception_keeps_api_private_and_remaining_ru_policy(self):
        api = {'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'}
        private = {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'blocked'}
        ru_domain = {'type': 'field', 'inboundTag': ['in-10000-tcp'], 'domain': ['geosite:category-ru'], 'outboundTag': 'blocked'}
        ru_ip = {'type': 'field', 'inboundTag': ['in-10000-tcp'], 'ip': ['geoip:ru'], 'outboundTag': 'blocked'}
        catchall = {'type': 'field', 'network': 'tcp,udp', 'outboundTag': 'warp-out'}
        warp = {'tag': 'warp-out', 'protocol': 'socks', 'settings': {'servers': [{'address': 'warp', 'port': 1080}]}}
        old = {'outbounds': [warp], 'routing': {'domainStrategy': 'IPOnDemand', 'rules': [api, ru_domain, ru_ip, private, catchall]}}
        new = G.cache_exception_template(old)
        rules = new['routing']['rules']
        self.assertEqual(rules[:3], [api, private, G.CACHE_RULE])
        self.assertEqual(rules[3:], [ru_domain, ru_ip, catchall])
        self.assertEqual(G.cache_exception_template(new), new)
        self.assertEqual(rules.count(G.CACHE_RULE), 1)
        manifest = Path(__file__).parents[1] / 'config/xray/youtube-cache-exception.json'
        if manifest.exists():
            self.assertEqual(json.loads(manifest.read_text()), G.CACHE_RULE)
        for invalid in (
            {**old, 'outbounds': [{'tag': 'direct', 'protocol': 'freedom'}, warp]},
            {**old, 'outbounds': [{**warp, 'settings': {'servers': [{'address': 'other', 'port': 1080}]}}]},
            {**old, 'routing': {**old['routing'], 'domainStrategy': 'AsIs'}},
            {**old, 'routing': {**old['routing'], 'rules': [api, catchall, ru_domain, ru_ip, private]}},
            {**old, 'routing': {**old['routing'], 'rules': [api, {**catchall, 'outboundTag': 'direct'},
                                                           ru_domain, ru_ip, private, catchall]}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                G.cache_exception_template(invalid)

    def test_template_cas_and_rollback_preserve_other_db_values(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'x-ui.db'
            with closing(sqlite3.connect(db)) as c, c:
                c.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)')
                c.executemany('INSERT INTO settings VALUES (?, ?)', [('xrayTemplateConfig', 'old'), ('unrelated', 'keep')])
            G.cas_template(db, 'old', 'new')
            with closing(sqlite3.connect(db)) as c, c:
                c.execute("UPDATE settings SET value='newer stats' WHERE key='unrelated'")
            G.cas_template(db, 'new', 'old')
            G.cas_template(db, 'new', 'old')  # crash recovery is safe when already restored
            with closing(sqlite3.connect(db)) as c, c:
                self.assertEqual(dict(c.execute('SELECT key,value FROM settings')),
                                 {'xrayTemplateConfig': 'old', 'unrelated': 'newer stats'})
                c.execute("UPDATE settings SET value='user edit' WHERE key='xrayTemplateConfig'")
            with self.assertRaisesRegex(RuntimeError, 'changed concurrently'):
                G.cas_template(db, 'new', 'old')
            with closing(sqlite3.connect(db)) as c, c:
                self.assertEqual(c.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()[0], 'user edit')

    def test_crash_recovery_restores_template_setting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'geodata'
            data.mkdir()
            stage = root / 'backups/geodata-20261114T210000Z-123'
            stage.mkdir(parents=True)
            (root / 'docker-compose.yml').write_bytes(b'candidate')
            (stage / 'base.before.yml').write_bytes(b'old')
            (stage / 'base.candidate.yml').write_bytes(b'candidate')
            db = root / 'test.db'
            with closing(sqlite3.connect(db)) as c, c:
                c.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)')
                c.execute("INSERT INTO settings VALUES ('xrayTemplateConfig','new')")
            G.save(stage / 'routing-change.json', {'db': str(db), 'before': 'old', 'after': 'new'})
            G.save(stage / 'transaction.before.json', {'current': None, 'hashes': {}, 'bootstrap': True})
            G.save(data / 'pending.json', {'stage': str(stage)})
            with patch.object(G, 'ROOT', root), patch.object(G, 'DATA', data), patch.object(G, 'nightly_window'), \
                    patch.object(G, 'set_current'), patch.object(G, 'run'), patch.object(G, 'verify_runtime'):
                G.recover_pending()
            with closing(sqlite3.connect(db)) as c, c:
                self.assertEqual(c.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()[0], 'old')

    def test_only_matching_completed_request_is_consumed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stage = root / 'stage'
            stage.mkdir()
            pending = root / 'pending-youtube-cache-exception.json'
            pending.write_bytes(b'approved one')
            G.save(stage / 'routing-change.json', {'pending_sha256': G.sha(pending.read_bytes())})
            with patch.object(G, 'ROOT', root):
                G.clear_completed_exception(stage)
                self.assertFalse(pending.exists())
                pending.write_bytes(b'another request')
                G.clear_completed_exception(stage)
                self.assertTrue(pending.exists())

    def test_post_restart_template_drift_refuses_commit_without_overwriting(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder)
            db = stage / 'x-ui.db'
            with closing(sqlite3.connect(db)) as c, c:
                c.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)')
                c.execute("INSERT INTO settings VALUES ('xrayTemplateConfig','approved')")
            G.save(stage / 'routing-change.json', {'db': str(db), 'before': 'old', 'after': 'approved'})
            G.verify_cache_template(stage, True)
            with closing(sqlite3.connect(db)) as c, c:
                c.execute("UPDATE settings SET value='user edit' WHERE key='xrayTemplateConfig'")
            with self.assertRaisesRegex(RuntimeError, 'Persisted panel template differs'):
                G.verify_cache_template(stage, True)
            with closing(sqlite3.connect(db)) as c, c:
                self.assertEqual(c.execute("SELECT value FROM settings").fetchone()[0], 'user edit')
                c.execute("UPDATE settings SET value='old' WHERE key='xrayTemplateConfig'")
            G.verify_cache_template(stage, False)

    def test_failed_start_or_smoke_restores_and_verifies_old_state(self):
        for phase in ['activate', 'verify']:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                stage = Path(folder)
                state = {'value': 'old'}
                def activate():
                    state['value'] = 'candidate'
                    if phase == 'activate':
                        raise RuntimeError('partial recreate failure')
                def verify():
                    raise RuntimeError('candidate smoke failed')
                def rollback():
                    state['value'] = 'old'
                def verify_old():
                    self.assertEqual(state['value'], 'old')
                with self.assertRaisesRegex(RuntimeError, 'previous state restored'):
                    G.transaction(stage, activate, verify, rollback, verify_old)
                self.assertTrue(json.loads((stage / 'rollback-result.json').read_text())['success'])
                self.assertFalse((stage / 'SUCCESS.json').exists())

    def test_rollback_failure_is_reported_separately(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder)
            def fail():
                raise RuntimeError('failure')
            with self.assertRaisesRegex(RuntimeError, 'AND rollback'):
                G.transaction(stage, fail, lambda: None, fail, lambda: None)
            self.assertFalse(json.loads((stage / 'rollback-result.json').read_text())['success'])

    def test_partial_bootstrap_recovers_before_a_new_prepare(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'geodata'
            data.mkdir()
            stage = root / 'backups/geodata-20261114T210000Z-123'
            stage.mkdir(parents=True)
            (root / 'docker-compose.yml').write_bytes(b'candidate')
            (stage / 'base.before.yml').write_bytes(b'old')
            (stage / 'base.candidate.yml').write_bytes(b'candidate')
            G.save(stage / 'transaction.before.json', {'current': None, 'hashes': {'geoip.dat': 'old'}, 'bootstrap': True})
            G.save(data / 'pending.json', {'stage': str(stage)})
            with patch.object(G, 'ROOT', root), patch.object(G, 'DATA', data), \
                    patch.object(G, 'nightly_window'), patch.object(G, 'set_current') as pointer, \
                    patch.object(G, 'run') as run, patch.object(G, 'verify_runtime') as verify:
                G.recover_pending()
                self.assertEqual((root / 'docker-compose.yml').read_bytes(), b'old')
                pointer.assert_called_once_with(None)
                verify.assert_called_once_with(stage, {'geoip.dat': 'old'}, False)
                self.assertIn('3x-ui', run.call_args.args)
                self.assertFalse((data / 'pending.json').exists())

    def test_partial_bootstrap_does_not_overwrite_unrelated_compose(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'geodata'
            data.mkdir()
            stage = root / 'backups/geodata-20261114T210000Z-123'
            stage.mkdir(parents=True)
            (root / 'docker-compose.yml').write_bytes(b'new user edits')
            (stage / 'base.before.yml').write_bytes(b'old')
            (stage / 'base.candidate.yml').write_bytes(b'candidate')
            G.save(stage / 'transaction.before.json', {'current': None, 'hashes': {}, 'bootstrap': True})
            G.save(data / 'pending.json', {'stage': str(stage)})
            with patch.object(G, 'ROOT', root), patch.object(G, 'DATA', data), patch.object(G, 'nightly_window'), \
                    patch.object(G, 'set_current') as pointer:
                with self.assertRaisesRegex(RuntimeError, 'unrelated Compose'):
                    G.recover_pending()
                pointer.assert_not_called()
                self.assertEqual((root / 'docker-compose.yml').read_bytes(), b'new user edits')


if __name__ == '__main__':
    unittest.main(verbosity=2)
