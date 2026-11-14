"""Offline regression checks for recovery of the one-time Hev migration."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch


spec = importlib.util.spec_from_file_location('deploy', Path(__file__).with_name('deploy-warp-hev.py'))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.stage = Path(folder.name)
        self.path = self.stage / 'cron'
        self.path.write_bytes(b'new')
        (self.stage / 'file-0.new').write_bytes(b'new')
        (self.stage / 'file-0.old').write_bytes(b'old')
        self.state = {'db': 'db', 'old_template': 'old', 'new_template': 'new',
                      'files': [{'path': str(self.path), 'mode': 0o644}]}
        self.helper = Mock()
        self.helper.inspect.return_value = {'State': {'Running': True}}
        self.panel = Mock()
        self.panel.read_template.return_value = 'new'
        for name, value in [('ROOT', self.stage), ('H', self.helper), ('E', self.panel)]:
            p = patch.object(deploy, name, value)
            p.start()
            self.addCleanup(p.stop)

    def restore(self, **options):
        deploy.restore(self.stage, self.state, template_written=True, warp_changed=False, **options)

    def test_external_rollback_refuses_conflicts_before_stop(self):
        self.path.write_bytes(b'unrelated')
        with self.assertRaisesRegex(RuntimeError, 'Operational file'):
            self.restore()
        self.helper.run.assert_not_called()
        self.path.write_bytes(b'new')
        self.panel.read_template.return_value = 'later'
        with self.assertRaisesRegex(RuntimeError, 'template'):
            self.restore()
        self.helper.run.assert_not_called()

    def test_queued_geo_change_blocks_historical_rollback(self):
        (self.stage / 'pending-youtube-cache-exception.json').touch()
        with self.assertRaisesRegex(RuntimeError, 'geodata'):
            self.restore()
        self.helper.run.assert_not_called()

    def test_cas_failure_after_stop_starts_panel_and_propagates_error(self):
        with patch.object(deploy, 'set_template', side_effect=RuntimeError('CAS failed')):
            with self.assertRaisesRegex(RuntimeError, 'CAS failed'):
                self.restore()
        self.assertEqual(self.helper.run.call_args_list,
                         [call('docker', 'stop', '--time', '20', '3x-ui'), call('docker', 'start', '3x-ui')])
        self.assertEqual(self.path.read_bytes(), b'new')
        self.assertFalse((self.stage / 'ROLLED_BACK').exists())

    def test_forward_apply_already_stopped_panel_and_recovery_encounters_conflict(self):
        self.path.write_bytes(b'unrelated')
        with self.assertRaisesRegex(RuntimeError, 'Operational file'):
            self.restore(partial=True, panel_stop_attempted=True)
        self.helper.run.assert_called_once_with('docker', 'start', '3x-ui')


if __name__ == '__main__':
    unittest.main(verbosity=2)
