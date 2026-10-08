import base64
import gzip
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class DemoTests(unittest.TestCase):
    def test_demo_builds_synthetic_history_without_screens(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, str(ROOT / 'scripts/demo.py'), tmp, '--no-screens'],
                                  capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = Path(tmp)
            summary = json.loads((out / 'demo-summary.json').read_text())
            self.assertEqual(set(summary['harnesses']), {'claude', 'codex', 'pi', 'opencode'})
            self.assertTrue(all(h['observations'] > 0 for h in summary['harnesses'].values()))
            self.assertGreaterEqual(summary['session']['subagent_nodes'], 3)
            self.assertGreaterEqual(summary['session']['inferred_children'], 1)
            self.assertTrue((out / 'demo-report.html').is_file())
            overhead = (out / 'overhead.txt').read_text()
            self.assertRegex(overhead, r'(?m)^claude ')
            self.assertRegex(overhead, r'(?m)^codex ')
            self.assertFalse((out / 'overview.png').exists())
            top = summary['top_turns']
            self.assertEqual([t['rank'] for t in top], list(range(1, 11)))
            self.assertTrue(all(t['cost'] is not None for t in top))
            self.assertEqual(summary['seed'], 7)
            self.assertGreaterEqual(sum(t['interrupted'] for t in top), 1)  # the demo shows the Interrupted badge
            self.assertGreaterEqual(len(summary['quota_windows']), 2)
            self.assertEqual({(w['harness'], w['minutes']) for w in summary['quota_windows']}, {('codex', 10080), ('claude', 300), ('claude', 10080)})
            self.assertGreater(summary['claude_quota_snapshots'], 10)
            self.assertGreater(summary['quota_share_labels']['observed'], 0)
            self.assertEqual({t['harness'] for t in top}, {'claude', 'codex', 'pi', 'opencode'})
            self.assertEqual(summary['report_features'], {
                'local_provider': 'ollama', 'manual_claude_plan': 'max-5x',
                'retained_top_k_only': True, 'unknown_project_or_branch': True,
            })

    def test_demo_report_embeds_realistic_prompt_text_for_top_turns(self):
        def build(seed):
            with tempfile.TemporaryDirectory() as tmp:
                proc = subprocess.run([sys.executable, str(ROOT / 'scripts/demo.py'), tmp, '--no-screens', '--seed', str(seed)],
                                      capture_output=True, text=True, cwd=ROOT)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                page = (Path(tmp) / 'demo-report.html').read_text(encoding='utf-8')
            match = re.search(r'<script id="report-data" type="application/octet-stream\+base64">([A-Za-z0-9+/=]*)</script>', page)
            return json.loads(gzip.decompress(base64.b64decode(match.group(1))).decode('ascii'))
        payload = build(7)
        self.assertTrue(payload['usage']['local_rows'], 'demo includes local-provider rows')
        manual = [p for p in payload['usage']['plans']
                   if p['harness'] == 'claude' and p['plan'] == 'max-5x' and p['source'] == 'manual']
        self.assertEqual(len(manual), 1)
        self.assertTrue(manual[0]['rows'])
        self.assertTrue(payload['usage']['groups'], 'demo includes project/branch groups')
        self.assertTrue(any(g.get('branch') is None or g.get('repository') is None
                            for g in payload['usage']['groups']),
                        'demo includes an unknown project or branch')
        texts = [t for t in payload['prompt_texts'].values() if t]
        self.assertEqual(len(texts), 10)
        self.assertEqual(len(set(texts)), len(texts), 'top-turn prompts repeat')
        lorem = {'lorem', 'ipsum', 'dolor', 'consectetur', 'adipiscing', 'eiusmod', 'tempor', 'incididunt'}
        for text in texts:
            self.assertFalse(lorem & set(re.findall(r'[a-z]+', text.lower())), text)
        for context in payload['prompt_context'].values():
            self.assertTrue(context['title'] and context['cwd'] and context['final'], context)
            self.assertFalse(lorem & set(re.findall(r'[a-z]+', context['final'].lower())), context['final'])
        self.assertEqual(payload['prompt_texts'], build(7)['prompt_texts'])
        # Resume data is fictional but present, and the page is told it is a demo (it explains instead of opening or copying).
        self.assertIs(payload['demo'], True)
        commands = [r['command'] for r in payload['prompt_resume'].values()]
        self.assertEqual(len(commands), 10)
        self.assertTrue(all(re.match(r'cd (/d )?/Users/demo/code/', c) for c in commands), commands)  # cd /d on Windows
        links = [r['codex_link'] for r in payload['prompt_resume'].values() if r['codex_link']]
        self.assertTrue(links and all(re.fullmatch(r'codex://threads/[0-9a-f-]{36}', x) for x in links), links)


if __name__ == '__main__':
    unittest.main()
