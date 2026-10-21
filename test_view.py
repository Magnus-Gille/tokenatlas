"""Synthetic saved-view storage, CLI and shared report-boundary regressions."""
import contextlib
import io
import json
import os
import stat
import unittest
import signal
from unittest.mock import patch

from test_fresh_report import Base, payload
from tokenatlas import report
from tokenatlas.history import History


class ViewCLI(Base):
    def view(self, *args):
        return self.run_cli('view', *args)

    def current(self):
        code, out, err = self.view('--json')
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_set_hide_show_reset_presets_and_export(self):
        code, out, err = self.view('set', '--sections', 'facts,turns,quota-windows', '--top', '20', '--lang', 'sv', '--money-first')
        self.assertEqual(code, 0, err)
        path = self.db.parent / 'view.json'
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.current()['sections'], ['facts', 'turns', 'quota-windows'])
        self.assertEqual(self.view('hide', 'turns')[0], 0)
        self.assertEqual(self.view('show', 'energy', 'turns')[0], 0)
        self.assertEqual(self.current()['sections'], ['facts', 'quota-windows', 'energy', 'turns'])
        code, out, err = self.view('export')
        self.assertEqual(code, 0, err)
        exported = self.state / 'export.json'
        exported.write_text(out)
        self.assertEqual(self.view('reset')[0], 0)
        self.assertFalse(path.exists())
        self.assertEqual(self.view('import', str(exported))[0], 0)
        self.assertEqual(self.current()['top'], 20)
        for preset in ('subscription', 'api-cost', 'minimal', 'full'):
            self.assertEqual(self.view('preset', preset)[0], 0)
        self.assertEqual(self.current()['sections'][0], 'glance')
        self.assertEqual(self.view('set', '--sections', '')[0], 0)
        self.assertEqual(self.current()['sections'], [])

    def test_bad_sections_and_malformed_file(self):
        code, out, err = self.view('hide', 'wat')
        self.assertEqual(code, 2)
        self.assertIn('Valid sections:', err)
        self.assertIn('quota-windows', err)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        (self.db.parent / 'view.json').write_text('{bad')
        code, out, err = self.view('--json')
        self.assertEqual(code, 0, err)
        self.assertIn('warning:', err)
        self.assertEqual(json.loads(out)['sections'][0], 'glance')
        warning = io.StringIO()
        with contextlib.redirect_stderr(warning):
            data = report.build_report([], {})
        self.assertIn('warning:', warning.getvalue())
        from tokenatlas import view
        self.assertEqual(data['view'], view.defaults())

    def test_unknown_keys_warn_but_round_trip_known_fields(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        (self.db.parent / 'view.json').write_text(json.dumps({'version': 1, 'future': 4, 'defaults': {'future_filter': 'x', 'harness': 'codex'}}))
        code, out, err = self.view('--json')
        self.assertEqual(code, 0, err)
        self.assertIn('future', err)
        self.assertEqual(json.loads(out)['defaults'], {'harness': 'codex'})

    def test_builder_defaults_and_shared_privacy(self):
        self.assertEqual(self.view('set', '--sections', 'quota-windows,facts', '--lang', 'sv', '--top', '20', '--money-first')[0], 0)
        self.assertEqual(self.view('default', '--period', '30d', '--harness', 'codex', '--plan', 'team', '--project', '/private/work', '--session', 'secret', '--account', 'private-account')[0], 0)
        for lang in (None, 'auto', 'en', 'sv'):
            kw = {} if lang is None else {'lang': lang}
            local = report.build_report([], {}, redact=False, **kw)
            self.assertEqual(local['lang'], 'en' if lang == 'en' else 'sv')
            self.assertEqual(local['view']['sections'], ['quota-windows', 'facts'])
            self.assertEqual(local['view']['top'], 20)
            self.assertEqual(local['view']['defaults']['project_id'], '/private/work')
        shared = report.build_report([], {})
        self.assertNotIn('private', json.dumps(shared))
        self.assertNotIn('secret', json.dumps(shared))
        self.assertEqual(shared['view']['defaults'], {'period': '30d', 'harness': 'codex', 'plan': 'team'})

    def test_shared_defaults_obey_the_existing_public_metadata_boundary(self):
        from tokenatlas import view
        config = view.defaults()
        config['defaults'] = dict(period='7d', harness='codex', plan='private-plan',
                                  model='gpt-5.5-private-project', effort='private-effort',
                                  provider='private-account', thread_kind='private-session')
        data = report.build_report([], {}, view=config)
        self.assertEqual(data['view']['defaults'], {'period': '7d', 'harness': 'codex'})
        self.assertNotIn('private-', json.dumps(data))
        config['defaults'].update(plan='team', provider='openai', effort='high', thread_kind='main', model='gpt-5.5')
        safe = report.build_report([], {}, view=config)['view']['defaults']
        self.assertEqual(safe, config['defaults'])

    def test_report_open_and_cache_identity(self):
        with History(self.db):
            pass
        self.assertEqual(self.view('set', '--sections', 'facts', '--lang', 'sv')[0], 0)
        html = self.db.parent / 'report.html'
        for command, flags in [('report', ['--private', '--if-changed']), ('open', ['--no-refresh', '--no-browser'])]:
            code, out, err = self.run_cli(command, '--html', str(html), *flags, '--json')
            self.assertEqual(code, 0, err)
            self.assertEqual(payload(html.read_text())['lang'], 'sv')
            code, out, err = self.run_cli(command, '--html', str(html), *flags, '--lang', 'en', '--json')
            self.assertEqual(code, 0, err)
            self.assertEqual(payload(html.read_text())['lang'], 'en')
        self.assertEqual(self.run_cli('report', '--html', str(html), '--private', '--if-changed', '--json')[0], 0)
        before = report.read_report_state(html)
        self.assertEqual(self.view('show', 'energy')[0], 0)
        code, out, err = self.run_cli('report', '--html', str(html), '--private', '--if-changed', '--json')
        self.assertEqual(code, 0, err)
        self.assertFalse(json.loads(out).get('skipped', False))
        self.assertNotEqual(before[0], report.read_report_state(html)[0])

    def test_invalid_import_is_atomic_and_reset_recovers_corrupt_file(self):
        self.assertEqual(self.view('set', '--sections', 'facts')[0], 0)
        before = (self.db.parent / 'view.json').read_bytes()
        bad = self.state / 'bad-view.json'
        for value in ({'sections': ['not-a-section']}, {'top': 0}, {'version': 9}, {'defaults': []}, {'money_first': 'true'}):
            with self.subTest(value=value):
                bad.write_text(json.dumps(value))
                self.assertEqual(self.view('import', str(bad))[0], 2)
                self.assertEqual((self.db.parent / 'view.json').read_bytes(), before)
        (self.db.parent / 'view.json').write_text('not json')
        self.assertEqual(self.view('reset')[0], 0)
        self.assertFalse((self.db.parent / 'view.json').exists())

    def test_default_clearing_and_custom_database_directory(self):
        self.assertEqual(self.view('default', '--harness', 'codex', '--plan', 'team')[0], 0)
        self.assertEqual(self.view('default', '--plan', '')[0], 0)
        self.assertEqual(self.current()['defaults'], {'harness': 'codex'})
        other = self.state / 'other/history.sqlite3'
        code, out, err = self.run_cli('--db', str(other), 'view', 'set', '--lang', 'en')
        self.assertEqual(code, 0, err)
        self.assertTrue((other.parent / 'view.json').exists())
        self.assertFalse(other.exists(), 'configuring a view needs no database')
        with History(other):
            pass
        target = other.parent / 'report.html'
        code, out, err = self.run_cli('--db', str(other), 'report', '--html', str(target), '--private')
        self.assertEqual(code, 0, err)
        self.assertEqual(payload(target.read_text())['lang'], 'en')
        self.assertEqual(payload(target.read_text())['view']['defaults'], {})

    def test_scheduled_collect_applies_view_and_rebuilds_when_it_changes(self):
        from tokenatlas import collect
        from tokenatlas import __main__ as cli
        self.assertEqual(self.view('preset', 'minimal')[0], 0)
        self.assertEqual(self.view('set', '--lang', 'sv')[0], 0)
        for name in ('SIGTERM', 'SIGINT', 'SIGHUP'):
            if hasattr(signal, name):
                self.addCleanup(signal.signal, getattr(signal, name), signal.getsignal(getattr(signal, name)))
        with patch.object(collect, '_hosts', return_value=[]), patch.object(cli, 'refresh_all', return_value={'status': 'ok'}):
            code, out, err = self.run_cli('collect', '--json')
            self.assertEqual(code, 0, err)
            target = self.db.parent / 'report.html'
            self.assertEqual(payload(target.read_text())['view']['sections'], ['glance', 'kpis', 'turns'])
            self.assertEqual(payload(target.read_text())['lang'], 'sv')
            before = report.read_report_state(target)
            self.assertEqual(self.view('show', 'energy')[0], 0)
            code, out, err = self.run_cli('collect', '--json')
            self.assertEqual(code, 0, err)
            self.assertNotEqual(before[0], report.read_report_state(target)[0], 'view changes bypass max-age throttling')


class PageSourceContract(unittest.TestCase):
    """Offline wiring checks, not DOM execution or visual acceptance."""
    def test_layout_recovery_and_filter_wiring(self):
        from pathlib import Path
        text = Path('tokenatlas/report_template.html').read_text()
        self.assertIn('function applySavedView(', text)
        self.assertIn('function restoreSections(', text)
        self.assertIn("$('show-everything').onclick", text)
        self.assertIn('viewStash.querySelectorAll(selector)', text)
        self.assertIn('function applyDefaultFilters(', text)
        self.assertIn("id=\"view-note\"", text)

    def test_section_ids_match_the_page(self):
        import re
        from pathlib import Path
        from tokenatlas import view
        text = Path('tokenatlas/report_template.html').read_text()
        mapping = re.search(r'const sectionIds=\{([^}]+)\}', text).group(1)
        keys = [part.split(':')[0].strip("'") for part in mapping.split(',')]
        self.assertEqual(keys, list(view.SECTIONS))

    def test_translations_and_collapsed_glossary(self):
        from pathlib import Path
        data = json.loads(Path('tokenatlas/report_i18n.json').read_text())
        for lang in ('sv', 'en'):
            self.assertIn('view_active', data[lang])
            self.assertIn('tokenatlas view', data[lang]['view_customise'])
            self.assertIn('view_everything', data[lang])
        self.assertIn('<details class="glossary" id="glossary">', Path('tokenatlas/report_template.html').read_text())


def browser_fixture():
    """Deterministic, wholly fictional HTML for test_view_browser.cjs; no disk data."""
    from datetime import datetime, timedelta, timezone
    from test_plans_cli import row, PRICES
    from tokenatlas import view
    records = []
    for i in range(40):
        stamp = (datetime(2026, 8, 12, 10, tzinfo=timezone.utc) + timedelta(days=i)).isoformat()
        record = row(f'view-{i}', stamp, f'turn-{i}', plan='team', session=f'session-{i}')
        if i % 2:
            record['harness'] = 'claude'
        records.append(record)
    data = report.build_report(records, {}, timezone_name='UTC', redact=False, table=PRICES, quota=False,
                               now=datetime(2026, 9, 21, tzinfo=timezone.utc), view=view.defaults())
    data['quota_windows'] = [dict(harness='codex', account='synthetic-account', plan='team',
        minutes=10080, resets_at='2026-09-22T00:00:00+00:00', peak_percent=70,
        peak_at='2026-09-20T10:00:00+00:00', hit=False, cost=2)]
    return report.render_report(data)
