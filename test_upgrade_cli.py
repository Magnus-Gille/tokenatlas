import sys
import types
import unittest
from unittest.mock import patch

import tokenatlas
from tokenatlas import __main__ as cli


class UpgradeCliTests(unittest.TestCase):
    def invoke(self, argv, result=0):
        upgrade = types.ModuleType('tokenatlas.upgrade')
        upgrade.run = unittest.mock.Mock(return_value=result)
        with patch.dict(sys.modules, {'tokenatlas.upgrade': upgrade}), \
             patch.object(tokenatlas, 'upgrade', upgrade, create=True), \
             patch.object(cli, 'default_db') as default_db, \
             patch('tokenatlas.history.History') as history, \
             patch('sqlite3.connect') as connect:
            returned = cli.main(argv)
        return returned, upgrade.run, default_db, history, connect

    def test_check_dispatches_without_resolving_or_opening_history(self):
        returned, run, default_db, history, connect = self.invoke(['upgrade', '--check'])
        self.assertEqual(returned, 0)
        self.assertTrue(run.call_args.args[0].check)
        self.assertIsNone(run.call_args.args[0].version)
        self.assertFalse(run.call_args.args[0].yes)
        default_db.assert_not_called()
        history.assert_not_called()
        connect.assert_not_called()

    def test_version_and_yes_are_passed_to_upgrade_owner(self):
        returned, run, default_db, history, connect = self.invoke(
            ['upgrade', '--version', '1.20.0', '--yes'], result=3)
        self.assertEqual(returned, 3)
        args = run.call_args.args[0]
        self.assertEqual(args.version, '1.20.0')
        self.assertFalse(args.check)
        self.assertTrue(args.yes)
        default_db.assert_not_called()
        history.assert_not_called()
        connect.assert_not_called()

    def test_global_db_is_rejected_before_upgrade_dispatch(self):
        upgrade = types.ModuleType('tokenatlas.upgrade')
        upgrade.run = unittest.mock.Mock()
        with patch.dict(sys.modules, {'tokenatlas.upgrade': upgrade}), \
             patch.object(tokenatlas, 'upgrade', upgrade, create=True), \
             patch.object(cli, 'default_db') as default_db, \
             patch('tokenatlas.history.History') as history, \
             patch('sqlite3.connect') as connect, \
             patch('sys.stderr'):
            with self.assertRaises(SystemExit) as raised:
                cli.main(['--db', '/tmp/history.sqlite3', 'upgrade', '--check'])
        self.assertEqual(raised.exception.code, 2)
        upgrade.run.assert_not_called()
        default_db.assert_not_called()
        history.assert_not_called()
        connect.assert_not_called()

    def test_check_and_version_are_mutually_exclusive(self):
        upgrade = types.ModuleType('tokenatlas.upgrade')
        upgrade.run = unittest.mock.Mock()
        with patch.dict(sys.modules, {'tokenatlas.upgrade': upgrade}), \
             patch.object(tokenatlas, 'upgrade', upgrade, create=True), patch('sys.stderr'):
            with self.assertRaises(SystemExit) as raised:
                cli.main(['upgrade', '--check', '--version', '1.20.0'])
        self.assertEqual(raised.exception.code, 2)
        upgrade.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
