"""Remote sync back-off state is visible in doctor and in the report (TOKEN-198)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tokenatlas import remote_state
from tokenatlas.history import History
from tokenatlas.report import build_report, coverage_key

NOW = 1795300000


def seed(home, tag, text):
    path = remote_state.remote_dir(home) / f'{tag}.sync-state'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class RemoteStateTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_read_reports_failures_backoff_and_last_success(self):
        seed(self.home, 'pi', f'fails=4\nlast_ok=1795000000\nlast_try={NOW - 60}\nnext_try={NOW + 3600}\n')
        seed(self.home, 'm5', 'fails=0\nlast_ok=1795299000\nlast_try=1795299000\nnext_try=0\n')
        by_host = {e['host']: e for e in remote_state.read(self.home, now=NOW)}
        pi = by_host['pi']
        self.assertEqual((pi['consecutive_failures'], pi['backing_off']), (4, True))
        self.assertEqual(pi['last_success'], '2026-11-18T11:06:40Z')
        self.assertTrue(pi['next_attempt'].endswith('Z'))
        self.assertEqual((by_host['m5']['consecutive_failures'], by_host['m5']['backing_off'], by_host['m5']['next_attempt']), (0, False, None))

    def test_unreadable_or_odd_files_are_ignored(self):
        seed(self.home, 'bad', 'fails=many\n')
        seed(self.home, 'we ird', 'fails=1\n')
        self.assertEqual(remote_state.read(self.home, now=NOW), [])
        self.assertEqual(remote_state.read(self.home / 'nope', now=NOW), [])

    def test_failing_keeps_only_stable_fields_of_failing_hosts(self):
        entries = remote_state.read(self.home, now=NOW)
        self.assertEqual(remote_state.failing(entries), [])
        seed(self.home, 'pi', f'fails=2\nlast_ok=0\nlast_try={NOW}\nnext_try={NOW + 99}\n')
        seed(self.home, 'm5', 'fails=0\nlast_ok=5\nlast_try=5\nnext_try=0\n')
        failing = remote_state.failing(remote_state.read(self.home, now=NOW))
        self.assertEqual(failing, [dict(host='pi', consecutive_failures=2, last_success=None)])

    def test_doctor_and_report_carry_the_failing_host(self):
        seed(self.home, 'pi', f'fails=3\nlast_ok=1795000000\nlast_try={NOW}\nnext_try=4102444800\n')
        with mock.patch.object(Path, 'home', return_value=self.home), tempfile.TemporaryDirectory() as tmp:
            with History(Path(tmp) / 'h.sqlite3') as history:
                status = history.doctor()
            self.assertEqual(status['remote_sync'][0]['host'], 'pi')
            self.assertTrue(status['remote_sync'][0]['backing_off'])
            payload = build_report([], status)
            self.assertEqual(payload['coverage']['remote_sync'], [dict(host='pi', consecutive_failures=3, last_success='2026-11-18T11:06:40Z')])
            key_failing = coverage_key(status)
            seed(self.home, 'pi', f'fails=3\nlast_ok=1795000000\nlast_try={NOW + 999}\nnext_try=4102444900\n')
            with History(Path(tmp) / 'h.sqlite3') as history:
                self.assertEqual(coverage_key(history.doctor()), key_failing)  # a retry that changes only timestamps does not rebuild the report
            seed(self.home, 'pi', 'fails=0\nlast_ok=1795400000\nlast_try=1795400000\nnext_try=0\n')
            with History(Path(tmp) / 'h.sqlite3') as history:
                self.assertNotEqual(coverage_key(history.doctor()), key_failing)


if __name__ == '__main__':
    unittest.main()
