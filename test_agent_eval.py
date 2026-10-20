"""The eval baseline exercises real CLI subprocesses against demo.py's synthetic HOME."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import agent_eval


class AgentEval(unittest.TestCase):
    def test_ten_task_baseline_and_artifact_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'eval'
            env, db = agent_eval.prepare(root)
            self.assertEqual(env['TOKENATLAS_NO_BROWSER'], '1')
            self.assertTrue(db.is_relative_to(root.resolve()))
            self.assertEqual(len(json.loads((root / 'tasks.json').read_text())), 10)
            summary = agent_eval.baseline(root, env, db)
            self.assertEqual(summary['passed'], 10, summary)
            self.assertEqual(summary['model_eval'], 'not run')
            self.assertEqual(json.loads((root / 'tickets.json').read_text()), [])
            with self.assertRaises(ValueError):
                agent_eval.prepare(root)

    def test_baseline_failures_become_tickets_not_successes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(agent_eval.subprocess, 'run', side_effect=agent_eval.subprocess.TimeoutExpired('synthetic', 1)):
                summary = agent_eval.baseline(tmp, {}, Path(tmp) / 'history.sqlite3')
            tickets = json.loads((Path(tmp) / 'tickets.json').read_text())
            self.assertEqual(summary['passed'], 0)
            self.assertEqual(len(tickets), 10)
            self.assertEqual(tickets[0]['task'], 'costliest-yesterday')
            self.assertIn('timed out', tickets[0]['evidence'])


if __name__ == '__main__':
    unittest.main()
