import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from tokenatlas import progress
from tokenatlas.history import History
from tokenatlas.progress import Progress

ROOT = Path(__file__).resolve().parent


class Stream(io.StringIO):
    def __init__(self, tty=True, encoding='utf-8'):
        super().__init__(); self.tty, self._encoding = tty, encoding
    def isatty(self): return self.tty
    @property
    def encoding(self): return self._encoding


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


def make(stream, env=None, **kw):
    clock = Clock()
    return Progress.for_stream(stream, env={} if env is None else env, clock=clock, interval=0.005, width=80, **kw), clock


class Modes(unittest.TestCase):
    def test_tty_is_live_and_finished_steps_keep_a_line_with_their_time(self):
        out = Stream(); p, clock = make(out)
        self.assertEqual(p.mode, 'live')
        with p:
            with p.step('Read history'):
                p.count(1240, 3410, 'files'); clock.now = 12.3
                p._draw()
        text = out.getvalue()
        self.assertIn('\r', text)
        self.assertIn('Read history 1,240/3,410 files 12.3 s', text)
        self.assertTrue(text.endswith('✓ Read history (12.3 s)\n'), repr(text))

    def test_the_live_line_is_cleared_with_spaces_not_ansi(self):
        out = Stream(); p, clock = make(out)
        with p:
            with p.step('Quota'):
                p.note('claude'); p._draw()
        self.assertNotIn('\x1b', out.getvalue())
        last = out.getvalue().rsplit('\r', 2)
        self.assertEqual(last[1].strip(), '')  # the blanking pass before the final line

    def test_non_tty_gets_nothing(self):
        out = Stream(tty=False); p, _ = make(out)
        with p:
            with p.step('Read history'):
                p.count(1, 2); p.note('x'); p._draw()
        self.assertEqual((p.mode, out.getvalue()), ('off', ''))

    def test_env_1_gives_plain_lines_without_animation_or_carriage_returns(self):
        for tty in (False, True):
            out = Stream(tty=tty); p, clock = make(out, {'TOKENATLAS_PROGRESS': '1'})
            with p:
                with p.step('Read history'):
                    p.count(1, 2); clock.now = 2.5
            self.assertEqual(out.getvalue(), 'Read history ...\nRead history done (2.5 s)\n')
            self.assertIsNone(p.thread)

    def test_env_0_disables_even_a_tty(self):
        out = Stream(); p, _ = make(out, {'TOKENATLAS_PROGRESS': '0'})
        with p:
            with p.step('Read history'):pass
        self.assertEqual(out.getvalue(), '')

    def test_nested_step_renames_the_live_line_but_leaves_no_line(self):
        out = Stream(); p, _ = make(out)
        with p:
            with p.step('Report'):
                with p.step('Inner'):p._draw()
        self.assertIn('Report: Inner', out.getvalue())
        self.assertNotIn('Inner (', out.getvalue())

    def test_suspend_keeps_the_line_off_the_screen(self):
        out = Stream(); p, _ = make(out)
        with p, p.step('Refresh'):
            with p.suspend():
                n = len(out.getvalue()); p._draw()
                self.assertEqual(len(out.getvalue()), n)


class Failure(unittest.TestCase):
    def test_exception_marks_the_step_failed_clears_the_line_and_stops_the_thread(self):
        out = Stream(); p, clock = make(out)
        with self.assertRaises(KeyboardInterrupt):
            with p:
                with p.step('Read history'):
                    thread = p.thread
                    self.assertTrue(thread.is_alive())
                    raise KeyboardInterrupt
        self.assertFalse(thread.is_alive())
        self.assertIn('✗ Read history', out.getvalue())
        self.assertNotIn('✓', out.getvalue())
        self.assertTrue(out.getvalue().endswith('\n'))
        self.assertEqual(p.drawn, 0)

    def test_close_stops_the_thread_with_a_step_still_open(self):
        out = Stream(); p, _ = make(out)
        cm = p.step('Open'); cm.__enter__()
        thread = p.thread
        p.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(p.drawn, 0)

    def test_failed_flag_ends_as_failed_without_an_exception(self):
        out = Stream(); p, _ = make(out)
        with p:
            with p.step('Report') as step:step.failed = True
        self.assertIn('✗ Report', out.getvalue())

    def test_a_broken_stream_never_breaks_the_command(self):
        out = Stream(); out.close(); p, _ = make(out)
        with p:
            with p.step('Read history'):pass

    def test_the_redraw_thread_moves_the_line_on_its_own(self):
        out = Stream(); p, _ = make(out)
        with p, p.step('Slow'):
            for _ in range(200):
                if out.getvalue().count('\r') > 2:break
                threading.Event().wait(0.005)
        self.assertGreater(out.getvalue().count('\r'), 2)


class Ascii(unittest.TestCase):
    def test_a_stream_that_cannot_encode_the_glyphs_gets_ascii(self):
        out = Stream(encoding='cp1252'); p, clock = make(out)
        with p:
            with p.step('Read history'):p._draw()
        text = out.getvalue()
        self.assertTrue(text.isascii(), repr(text))
        self.assertIn('| Read history', text)
        self.assertTrue(text.endswith('ok Read history (0.0 s)\n'), repr(text))

    def test_unknown_encoding_falls_back_too(self):
        self.assertTrue(Progress.for_stream(Stream(encoding='no-such-codec'), env={}).tick == 'ok')


class Module(unittest.TestCase):
    def tearDown(self): progress.stop()

    def test_the_default_is_a_silent_noop_and_start_is_not_reentrant(self):
        with progress.step('x'):progress.count(1, 2); progress.note('y')
        out = Stream(); self.assertTrue(progress.start(out, env={}))
        self.assertFalse(progress.start(Stream(), env={}))
        with progress.step('Read'):pass
        progress.finish()  # closes the live line; later steps are silent
        with progress.step('Later'):pass
        self.assertIn('✓ Read', out.getvalue())
        self.assertNotIn('Later', out.getvalue())
        self.assertIs(progress.current(), progress.NOOP)


def run(db, *argv, progress='0'):
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'TOKENATLAS_PROGRESS': progress}
    return subprocess.run([sys.executable, '-m', 'tokenatlas', '--db', str(db), *argv], cwd=ROOT, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        path = root / 'logs/proj/S.jsonl'; path.parent.mkdir(parents=True)
        row = {'type': 'assistant', 'uuid': 'u1', 'requestId': 'r1', 'sessionId': 'S', 'cwd': '/w/app', 'timestamp': '2026-09-10T10:00:00Z',
               'message': {'id': 'm1', 'model': 'claude-opus-5-5', 'stop_reason': None,
                           'usage': {'input_tokens': 10, 'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 0, 'output_tokens': 5}}}
        path.write_text(json.dumps(row) + '\n')
        self.logs, self.db = root / 'logs', root / 'h.sqlite3'
        with History(self.db) as h:h.refresh('claude', self.logs)

    def test_json_stdout_is_identical_with_progress_forced_on(self):
        for argv in (('top', '--json'), ('insights', '--json'), ('report',), ('quota', 'show', '--json'), ('session', 'S', '--json')):
            off, on = run(self.db, *argv), run(self.db, *argv, progress='1')
            self.assertEqual(off.returncode, 0, off.stderr)
            self.assertEqual(on.stdout, off.stdout, argv)
            self.assertEqual(off.stderr, '', argv)
            self.assertIn('Read history ...', on.stderr) if argv[0] != 'quota' else self.assertIn('...', on.stderr)

    def test_refresh_reports_its_steps_on_stderr_only(self):
        off = run(self.db, 'refresh', '--harness', 'claude', '--root', str(self.logs))
        on = run(self.db, 'refresh', '--harness', 'claude', '--root', str(self.logs), progress='1')
        self.assertEqual(json.loads(on.stdout)['status'], json.loads(off.stdout)['status'])
        self.assertIn('Refresh claude done', on.stderr)

    def test_report_html_writes_phase_lines_and_keeps_the_receipt(self):
        out = Path(self.tmp.name) / 'r.html'
        proc = run(self.db, 'report', '--html', str(out), '--private', progress='1')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for label in ('Read history', 'Build report rows', 'Render and write report'):
            self.assertIn(f'{label} done', proc.stderr)
        self.assertIn('Report: ', proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['observations'], 1)

    def test_statusline_and_doctor_show_no_progress(self):
        for argv in (('doctor',), ('statusline',)):
            self.assertNotIn('...', run(self.db, *argv, progress='1').stderr)


if __name__ == '__main__':
    unittest.main()
