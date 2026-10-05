import base64
import gzip
import json
import os
import re
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tokenatlas.history import ALL_FIELDS
from tokenatlas.report import WARNING_SEPARATOR, build_report, render_report, write_report as _write_report
write_report = _write_report


def decode_html(html):
    """Payload embedded in the report page: base64 -> gunzip -> JSON."""
    match = re.search(r'<script id="report-data" type="application/octet-stream\+base64">([A-Za-z0-9+/=]*)</script>', html)
    return json.loads(gzip.decompress(base64.b64decode(match.group(1))).decode('ascii'))


def page_text(html):
    """The page without its base64 payload, whose random-looking text can contain any short string."""
    return re.sub(r'(<script id="report-(?:data|i18n)"[^>]*>)[A-Za-z0-9+/=]*(</script>)', r'\1\2', html)


def expand(report):
    """Python twin of the template's expand(): per-record dicts from the columnar block."""
    c, out, ms = report['columns'], [], 0
    for i in range(c['n']):
        ms += c['ts'][i]
        off = c['dict']['off'][c['idx']['off'][i]]
        sign = '-' if off < 0 else '+'
        suffix = f'{sign}{abs(off) // 60:02d}:{abs(off) % 60:02d}'
        local = datetime.fromtimestamp((ms + off * 60000) / 1000, ZoneInfo('UTC')).replace(tzinfo=None)
        row = {k: c['dict'][k][c['idx'][k][i]] for k in c['dict'] if k not in ('off', 'warnings')}
        warnings = c['dict']['warnings'][c['idx']['warnings'][i]]
        row.update(ms=ms, date=local.date().isoformat(),
                   hour=local.replace(minute=0, second=0, microsecond=0).isoformat() + suffix,
                   minute=local.replace(second=0, microsecond=0).isoformat() + suffix,
                   id=None if c['id'][i] is None else (c['id_prefix'] + f"{c['id'][i]:03d}" if c['id_prefix'] else c['id'][i]),
                   tokens={k: c['tokens'][k][i] for k in ALL_FIELDS},
                   complete=bool(c['complete'][i]), id_synthetic=bool(c['id_synthetic'][i]),
                   warnings=warnings.split(WARNING_SEPARATOR) if warnings else [], prompt=c['prompt'][i])
        v, t, h = None if c['price'][i] is None else c['price_classes'][c['price'][i]], row['tokens'], c['cw1h'][i]
        row['cost'] = v and ((t['fresh_input'] or 0) * v[0] + ((t['cache_write'] or 0) - h) * v[1] + h * v[2]
                             + (t['cache_read'] or 0) * v[3] + (t['output'] or 0) * v[4])
        out.append(row)
    return out


def observation(identity='one', **changes):
    item = dict(id=identity, ts='2026-10-25T00:30:00+00:00', harness='claude',
                provider='anthropic', project_id='/private/client/app', project_label='app',
                session='private-session', parent_session=None, turn_id='private-turn',
                turn_confidence='observed', model='model-a', effort='high',
                thread_kind='main', agent='private-agent', origin='cli',
                tokens=dict(fresh_input=10, cache_read=20, cache_write=5, output=7, reasoning=3),
                complete=True, id_synthetic=False, warnings=[],
                cwd='/private/client/app', sources=['/private/log.jsonl'],
                raw_usage={'secret': 'PRIVATE PROMPT'}, machine='private-machine')
    item.update(changes)
    return item


class ReportTests(unittest.TestCase):
    def test_redaction_is_default_and_allowlist_excludes_private_fields(self):
        report = build_report([observation()], {'imports': [{'root': '/private/logs', 'harness': 'claude', 'status': 'ok'}]})
        html = render_report(report)
        encoded = json.dumps(decode_html(html))
        for secret in ('/private/', 'PRIVATE PROMPT', 'private-session', 'private-agent', 'private-turn', 'private-machine'):
            self.assertNotIn(secret, page_text(html))
        for secret in ('/private/', 'PRIVATE PROMPT', 'private-session', 'private-agent', 'private-turn', 'private-machine'):
            self.assertNotIn(secret, encoded)
        self.assertEqual(report['privacy'], 'redacted')
        self.assertEqual(expand(report)[0]['tokens']['reasoning'], 3)

    def test_imported_harness_name_is_in_no_shared_insight_fact(self):  # #108
        rows = [observation(harness='private-harness-xyz'), observation('two', harness='claude', turn_id='t2')]
        report = build_report(rows, {})
        html = render_report(report)
        facts = json.dumps(report['insights'])
        self.assertIn('"other"', facts)
        self.assertNotIn('private-harness-xyz', json.dumps(decode_html(html)))
        self.assertNotIn('private-harness-xyz', page_text(html))

    def test_project_collisions_are_distinct_and_private_labels_unique(self):
        rows = [observation(), observation('two', project_id='/other/client/app')]
        result = expand(build_report(rows, {}, redact=False))
        self.assertNotEqual(result[0]['project_id'], result[1]['project_id'])
        self.assertNotEqual(result[0]['project_label'], result[1]['project_label'])
        self.assertTrue(all('app' in r['project_label'] for r in result))
        self.assertNotIn('/private/', json.dumps(result))

    def test_session_identity_is_scoped_by_harness(self):
        rows = [observation(), observation('two', harness='pi', provider='test')]
        redacted = expand(build_report(rows, {}))
        private = expand(build_report(rows, {}, redact=False))
        self.assertNotEqual(redacted[0]['session'], redacted[1]['session'])
        self.assertEqual({row['session'] for row in private}, {
            'claude:private-session', 'pi:private-session',
        })

    def test_template_uses_neutral_copy_and_cache_comparison_mounts(self):
        html = render_report(build_report([], {}))
        self.assertIn('<h1 data-t="hero_title"></h1>', html)
        self.assertIn('<title>TokenAtlas', html)
        self.assertIn('<span>↗</span>TokenAtlas</div>', html)
        self.assertNotIn('Tokenatlas', html)
        texts = json.loads((Path(__file__).parent / 'tokenatlas/report_i18n.json').read_text(encoding='utf-8'))
        for lang in ('sv', 'en'):
            for key, label in (('tok_input', 'Input'), ('tok_cw', 'Cache write'), ('tok_cr', 'Cache read'), ('tok_out', 'Output')):
                self.assertEqual(texts[lang][key], label)
        self.assertEqual(texts['sv']['total'], 'Totalt')
        self.assertIn('id="cache-comparisons"', html)
        for dimension in ('session', 'harness', 'model', 'project_id'):
            self.assertIn(f'data-cache-dimension="{dimension}"', html)
        for old_copy in ('Din användning, förklarad', 'Vart tog alla tokens vägen?',
                         'Se mönstret. Hitta toppen.', 'Vad driver användningen?',
                         'Synlig täckning. Ärliga gränser.'):
            self.assertNotIn(old_copy, html)

    def test_page_ships_both_languages_in_one_dictionary(self):
        html = render_report(build_report([], {}))
        match = re.search(r'<script id="report-i18n" type="application/octet-stream\+base64">([A-Za-z0-9+/=]+)</script>', html)
        i18n = json.loads(gzip.decompress(base64.b64decode(match.group(1))).decode('utf-8'))
        self.assertEqual(set(i18n), {'sv', 'en'})
        base = lambda lang: {k for k in i18n[lang] if not k.endswith('_one')}  # singular forms are per language
        self.assertEqual(base('sv'), base('en'))
        self.assertEqual(i18n['sv']['p4_title'], 'Dyraste turerna')
        self.assertEqual(i18n['en']['p4_title'], 'Costliest turns')
        used = set(re.findall(r'data-t(?:-[a-z-]+)?="([a-z_0-9]+)"', html))
        self.assertTrue(used)
        self.assertLessEqual(used, set(i18n['sv']))
        # the usage data block stays free of UI copy
        self.assertNotIn('Dyraste', json.dumps(decode_html(html)))

    def test_lang_is_recorded_in_the_payload(self):
        self.assertEqual(build_report([], {})['lang'], 'auto')
        for lang in ('auto', 'sv', 'en'):
            self.assertEqual(decode_html(render_report(build_report([], {}, lang=lang)))['lang'], lang)
        with self.assertRaises(ValueError):
            build_report([], {}, lang='de')

    def test_shared_payload_has_only_neutral_codes(self):
        rows = [observation(), observation('two', project_id='/other/client/app', turn_id='t2', warnings=['/private/x: bad!']),
                observation('zthree', project_id=None, warnings=['/private/x: bad!', '/private/y: worse!'])]
        for redact in (True,):
            report = build_report(rows, {}, redact=redact)
            text = json.dumps(report, ensure_ascii=False)
            for swedish in ('Projekt', 'Okänt', 'Varning', 'Tur ', 'Okänd'):
                self.assertNotIn(swedish, text, (redact, swedish))
        shared = expand(build_report(rows, {}))
        self.assertEqual([r['project_label'] for r in shared], ['\x01p001', '\x01p002', None])
        self.assertEqual([r['project_id'] for r in shared][:2], ['\x01p001', '\x01p002'])
        self.assertIsNone(shared[2]['project_id'])
        self.assertEqual(shared[0]['turn_id'], '\x01t001')
        self.assertEqual(shared[1]['warnings'], ['\x01w001'])
        self.assertEqual(shared[2]['warnings'], ['\x01w001', '\x01w002'])
        self.assertNotIn('/private/', json.dumps(build_report(rows, {})))

    def test_private_unknown_project_and_empty_name_are_sentinels(self):
        rows = [observation(), observation('two', project_id='/'), observation('zthree', project_id=None)]
        labels = [r['project_label'] for r in expand(build_report(rows, {}, redact=False))]
        self.assertEqual(labels, ['app', '\x01p', '\x01u'])

    def test_pseudonyms_do_not_leak_and_stay_stable(self):
        rows = [observation(project_id='/private/client/app', project_label='app'),
                observation('two', project_id='/private/client/app'),
                observation('zthree', project_id='/private/other/web')]
        ids = [r['project_id'] for r in expand(build_report(rows, {}))]
        self.assertEqual(ids[0], ids[1])
        self.assertNotEqual(ids[0], ids[2])
        for key in ('project_id', 'project_label'):
            for value in build_report(rows, {})['columns']['dict'][key]:
                self.assertRegex(value, r'^\x01p\d{3}$')
        self.assertNotIn('client', json.dumps(build_report(rows, {})))

    def test_dst_repeated_hour_remains_distinct(self):
        rows = [observation(), observation('two', ts='2026-10-25T01:30:00+00:00')]
        result = expand(build_report(rows, {}))
        self.assertEqual(result[0]['date'], result[1]['date'])
        self.assertNotEqual(result[0]['hour'], result[1]['hour'])
        self.assertTrue(result[0]['hour'].endswith('+02:00'))
        self.assertTrue(result[1]['hour'].endswith('+01:00'))

    def test_nulls_and_ambiguous_identity_are_preserved(self):
        row = observation(id_synthetic=True, complete=False)
        row['tokens']['cache_read'] = None
        result = expand(build_report([row], {}))[0]
        self.assertIsNone(result['tokens']['cache_read'])
        self.assertTrue(result['id_synthetic'])
        self.assertFalse(result['complete'])

    def test_safe_json_cannot_break_out_of_script(self):
        report = build_report([observation(model='</script><script>alert(1)</script>')], {}, redact=False)
        html = render_report(report, template='<script type="application/json">__USAGE_DATA__</script>')
        self.assertEqual(html.count('</script>'), 1)
        self.assertNotIn('<script>alert', html)
        self.assertEqual(expand(decode_html(html.replace('application/json', 'application/octet-stream+base64').replace('<script ', '<script id="report-data" ')))[0]['model'],
                         '</script><script>alert(1)</script>')

    def test_empty_report_coverage_is_not_claimed_complete(self):
        report = build_report([], {})
        self.assertEqual(report['columns']['n'], 0)
        self.assertEqual(expand(report), [])
        self.assertFalse(report['coverage']['coverage_complete'])
        self.assertFalse(report['coverage']['billing_verified'])

    def test_write_is_private_and_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.html'
            write_report(path, 'one')
            write_report(path, 'two')
            self.assertEqual(path.read_text(), 'two')
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)


class ReportReviewTests(unittest.TestCase):
    def test_lone_surrogate_renders_and_writes(self):
        report = build_report([observation(project_id='/work/\ud800')], {}, redact=False)
        html = render_report(report)
        with tempfile.TemporaryDirectory() as tmp:
            write_report(Path(tmp) / 'r.html', html)
            self.assertTrue((Path(tmp) / 'r.html').is_file())

    def test_redacted_report_only_shows_public_provider_and_model_names(self):
        rows = [observation('a', provider='m5', model='qwen3-coder', origin='cli'),
                observation('b', provider='inference-gille', model='gpt-oss-120b', origin='my-host-tui'),
                observation('c', provider='anthropic', model='claude-opus-5-5'),
                observation('d', provider='openai', model='gpt-5.6-luna', effort='xhigh')]
        html = render_report(build_report(rows, {}))
        shown = json.dumps(decode_html(html))
        for private in ('m5', 'inference-gille', 'qwen3-coder', 'gpt-oss-120b', 'my-host-tui'):
            self.assertNotIn(private, shown)
            self.assertNotIn(private, page_text(html))
        for public in ('anthropic', 'claude-opus-5-5', 'openai', 'gpt-5.6-luna', 'xhigh'):
            self.assertIn(public, shown)
        private = render_report(build_report(rows, {}, redact=False))
        self.assertNotIn('inference-gille', private)
        self.assertIn('inference-gille', json.dumps(decode_html(private)))

    def test_redacted_models_show_only_exact_packaged_public_identifiers(self):  # #133
        visible = (('anthropic', 'claude-opus-5-5'), ('anthropic', 'claude-haiku-4-5-20251001'), ('openai', 'gpt-5.6-luna'),
                   ('openai', 'gpt-6-astra'), ('openai-codex', 'gpt-5.5'), ('openrouter', 'qwen/qwen3-coder'),
                   ('openrouter', 'openai/gpt-oss-120b'), ('openrouter', 'z-ai/glm-5.3'))
        hidden = ('ft:gpt-4o-2024-08-06:acme-corp::abc123', 'magnus-macbook', 'stealth/ox-alpha', 'big-pickle', '<synthetic>',
                  'claude-opus-5-5-acme-internal', 'gpt-5.5:ft-acme', 'Claude-Opus-5-5')
        rows = [observation(f'v{i}', provider=p, model=m) for i, (p, m) in enumerate(visible)]
        rows += [observation(f'w{i}', provider='openai', model=m) for i, m in enumerate(hidden)]
        rows += [observation('x1', provider='m5', model='claude-opus-5-5'),
                 observation('x2', provider='inference-gille', model='gpt-5.6-luna'),
                 observation('x3', provider='anthropic', model='gpt-5.6-luna')]  # a public name under the wrong provider is not verified
        shown = [r['model'] for r in expand(build_report(rows, {}))]
        self.assertEqual(shown[:len(visible)], [m for _, m in visible])
        encoded = json.dumps(shown)
        for model in hidden + ('m5', 'inference-gille'):
            self.assertNotIn(model, encoded)
        self.assertTrue(all(v.startswith('model ') for v in shown[len(visible):] if v != 'unknown'))
        local = [r['model'] for r in expand(build_report(rows, {}, redact=False))]
        self.assertEqual(local, [r['model'] for r in rows])

    def test_private_suffix_under_every_family_is_in_no_shared_payload_or_page(self):  # #133
        families = ('claude', 'gpt', 'o1', 'o3', 'codex', 'gemini', 'gemma', 'mistral', 'codestral', 'ministral', 'magistral', 'pixtral',
                    'devstral', 'qwen', 'llama', 'deepseek', 'glm', 'kimi', 'grok')
        orgs = ('openai', 'anthropic', 'google', 'qwen', 'z-ai', 'zai-org', 'mistralai', 'meta-llama', 'deepseek', 'moonshotai', 'x-ai')
        providers = ('anthropic', 'openai', 'openai-codex', 'openrouter', 'opencode', 'berget', 'google', 'mistral')
        marker = 'zq-acme-internal'
        models = [f'{f}-5-{marker}' for f in families] + [f'{o}/{f}-{marker}' for o in orgs for f in ('gpt', 'qwen')] + [f'claude-opus-5-5-{marker}:free']
        rows, n = [], 0
        for provider in providers:
            for model in models:
                n += 1
                rows.append(observation(f'p{n}', provider=provider, model=model, ts=f'2026-10-{1 + n % 20:02d}T10:{n % 60:02d}:00+00:00',
                                        tokens=dict(fresh_input=1000, cache_read=10, cache_write=0, output=100, reasoning=0)))
        rows.append(observation('pub', provider='anthropic', model='claude-opus-5-5'))
        rows.append(observation('pubx', provider='openai', model='gpt-5.5'))
        html = render_report(build_report(rows, {}, now=datetime.fromisoformat('2026-10-25T00:00:00+00:00')))
        payload = json.dumps(decode_html(html))
        for text in (payload, page_text(html)):
            self.assertNotIn(marker, text)
        self.assertIn('claude-opus-5-5', payload)
        self.assertIn('gpt-5.5', payload)
        private = json.dumps(decode_html(render_report(build_report(rows, {}, redact=False, now=datetime.fromisoformat('2026-10-25T00:00:00+00:00')))))
        self.assertIn(marker, private)

    def test_private_models_are_pseudonymized_in_insight_facts_and_energy_keys(self):  # #133
        marker = 'zq-acme-internal'
        big = dict(fresh_input=2_000_000, cache_read=0, cache_write=0, output=1_000_000, reasoning=0)
        rows = [observation('a', provider='openai', harness='codex', model=f'gpt-5.5-{marker}', tokens=big),   # openai, not rated: credits "unrated"
                observation('b', provider='openai', harness='codex', model='gpt-5.5', tokens=big),
                observation('c', provider='anthropic', model=f'claude-opus-5-5-{marker}', tokens=big),
                observation('d', provider='anthropic', model='claude-opus-5-5', tokens=big)]
        report = build_report(rows, {}, now=datetime.fromisoformat('2026-10-25T00:00:00+00:00'))
        text = json.dumps(report)
        self.assertNotIn(marker, text)
        facts = {f['id']: f for w in report['insights']['windows'] for f in w['facts']}
        credits = facts['credits']['values']
        codex_private = [r['model'] for r in expand(report) if r['harness'] == 'codex' and r['model'].startswith('model ')]
        self.assertEqual([x['name'] for x in credits['unrated']], codex_private)  # the same pseudonym the rows carry
        self.assertEqual([m['name'] for m in credits['models']], ['gpt-5.5'])
        self.assertIn('claude-opus-5-5', json.dumps(facts['model_share']))
        for key in report['energy']['multipliers'].values():
            self.assertNotIn(marker, json.dumps(key))


class PayloadV2Tests(unittest.TestCase):
    def rows(self):
        stamps = ['2026-10-25T00:30:00+00:00', '2026-10-25T01:30:00+00:00', '2026-03-29T00:59:59.123+00:00',
                  '2026-03-29T01:00:00+00:00', '2026-06-01T12:00:00+02:00']
        rows = [observation(f'id{i}', ts=t, session=f's{i % 2}', model=f'claude-m{i % 3}',
                            warnings=['w one', 'w two'] if i == 1 else []) for i, t in enumerate(stamps)]
        rows[0]['tokens']['cache_read'] = None
        rows[2].update(complete=False, id_synthetic=True, parent_session='s0', effort=None)
        rows[3]['tokens'] = dict.fromkeys(ALL_FIELDS)
        return rows

    def test_columns_round_trip_matches_python_local_time_and_fields(self):
        for redact in (True, False):
            rows = self.rows()
            report = decode_html(render_report(build_report(rows, {}, redact=redact)))
            self.assertEqual(report['version'], 2)
            self.assertNotIn('records', report)
            got = expand(report)
            zone = ZoneInfo('Europe/Stockholm')
            expected = sorted(rows, key=lambda r: (r['ts'], r['harness'], r['id']))
            self.assertEqual(len(got), len(expected))
            for g, r in zip(got, expected):
                dt = datetime.fromisoformat(r['ts']).astimezone(zone)
                self.assertEqual(g['date'], dt.date().isoformat())
                self.assertEqual(g['hour'], dt.replace(minute=0, second=0, microsecond=0).isoformat())
                self.assertEqual(g['minute'], dt.replace(second=0, microsecond=0).isoformat())
                self.assertEqual(g['ms'], round(dt.timestamp() * 1000))
                self.assertEqual(g['tokens'], r['tokens'])
                self.assertEqual((g['complete'], g['id_synthetic']), (r['complete'], r['id_synthetic']))
                self.assertEqual(g['warnings'], r['warnings'])
                self.assertEqual(g['effort'], r['effort'])
                if redact:
                    self.assertNotIn(r['session'], g['session'])
                    self.assertNotIn(r['id'], g['id'])
                else:
                    self.assertEqual((g['session'], g['id'], g['project_label']),
                                     (f"claude:{r['session']}", r['id'], 'app'))
            self.assertIn('2026-10-25T02:00:00+02:00', {g['hour'] for g in got})
            self.assertIn('2026-10-25T02:00:00+01:00', {g['hour'] for g in got})

    def test_render_is_deterministic_and_html_has_no_plain_private_strings(self):
        report = build_report([observation()], {}, redact=False)
        self.assertEqual(render_report(report), render_report(report))
        html = render_report(report)
        for private in ('private-session', 'private-turn', 'private-agent'):
            self.assertNotIn(private, page_text(html))
            self.assertIn(private, json.dumps(decode_html(html)))

    def test_twenty_thousand_observations_stay_small(self):
        rows = [observation(f'o{i}', ts=f'2026-05-{1 + i % 28:02d}T{i % 24:02d}:{i % 60:02d}:{i % 50:02d}+00:00',
                            session=f's{i // 40}', turn_id=f't{i // 4}', model=f'claude-m{i % 5}',
                            tokens=dict(fresh_input=i % 977, cache_read=(i * 37) % 150000, cache_write=i % 311,
                                        output=(i * 13) % 4000, reasoning=i % 50)) for i in range(20000)]
        report = build_report(rows, {})
        size = len(render_report(report).encode())
        print(f'20k-row report: {size} bytes')
        self.assertLess(size, SIZE_LIMIT_20K)
        # The prompt card's columns must stay cheap: the same payload with them removed is the baseline.
        bare = dict(report, columns={k: v for k, v in report['columns'].items() if k not in PROMPT_CARD_COLUMNS})
        base = len(render_report(bare).encode())
        print(f'20k-row report without prompt-card columns: {base} bytes (+{100 * (size - base) / base:.1f}%)')
        self.assertLess(size, base * 1.08)


PROMPT_CARD_COLUMNS = ('prompt', 'price', 'price_classes', 'cw1h', 'credit', 'credit_classes')
SIZE_LIMIT_20K = 700_000  # measured ~311 KB (was ~9 MB as v1 JSON); margin for dictionary growth


class WholeTurnSize(unittest.TestCase):
    def test_payload_gives_each_card_turn_its_whole_request_count_even_when_the_report_holds_part_of_it(self):
        a, b, c = (observation(i, ts=f'2026-10-25T00:3{n}:00+00:00') for n, i in enumerate('abc'))
        d = observation('d', ts='2026-10-25T00:35:00+00:00', turn_id='other-turn')
        for redact in (False, True):
            full = build_report([a, b, c, d], {}, redact=redact)
            self.assertEqual(sorted(full['prompt_requests'].values()), [1, 3])
            cut = build_report([a, d], {}, redact=redact, universe=[a, b, c, d])  # a filtered report: one request of three
            self.assertEqual(sorted(cut['prompt_requests'].values()), [1, 3])
            if redact:
                self.assertNotIn('private-turn', json.dumps(cut))

    def test_template_labels_partial_selections_in_both_languages(self):
        root = Path(__file__).parent / 'tokenatlas'
        template = (root / 'report_template.html').read_text(encoding='utf-8')
        self.assertIn('partialTurn(p.id,p.requests)', template)
        texts = json.loads((root / 'report_i18n.json').read_text(encoding='utf-8'))
        self.assertEqual((texts['sv']['qs_whole'], texts['en']['qs_whole']), ('(hela turen)', '(whole turn)'))
        self.assertEqual((template.count('data-t="lh_scope"'), template.count('data-t="qw_scope"')), (1, 1))
        self.assertEqual(texts['en']['lh_scope'], 'Hits included when this report was built; not affected by the filters above.')
        self.assertEqual(texts['sv']['qw_scope'], 'De senaste fönstren för varje gräns; påverkas inte av filtren ovan.')
        self.assertNotIn('All imported history', json.dumps(texts['en']['lh_scope'] + texts['en']['qw_scope']))


class InterruptedBadge(unittest.TestCase):
    def test_column_marks_flagged_rows_and_shared_reports_keep_no_ids(self):
        rows = [observation('a'), observation('b', flags=['interrupted'], ts='2026-10-25T00:31:00+00:00')]
        for redact in (False, True):
            report = build_report(rows, {}, redact=redact)
            self.assertEqual(report['columns']['interrupted'], [0, 1])
            if redact:
                self.assertNotIn('private-turn', json.dumps(report))

    def test_template_renders_the_badge_and_both_languages_have_the_text(self):
        root = Path(__file__).parent / 'tokenatlas'
        template = (root / 'report_template.html').read_text(encoding='utf-8')
        self.assertIn("t('pr_interrupted')", template)
        self.assertIn('interrupted:rs.some(r=>r.interrupted)', template)
        texts = json.loads((root / 'report_i18n.json').read_text(encoding='utf-8'))
        self.assertEqual((texts['sv']['pr_interrupted'], texts['en']['pr_interrupted']), ('Avbruten', 'Interrupted'))


class AtAGlance(unittest.TestCase):
    KEYS = ('glance_e', 'glance_turns', 'glance_turns_one', 'glance_head', 'glance_head_nocost', 'glance_empty', 'glance_top', 'glance_day', 'glance_hits',
            'glance_hits_one', 'glance_h_5h', 'glance_h_week', 'glance_h_other', 'glance_int', 'glance_int_one', 'glance_int_nopct', 'glance_int_nopct_one',
            'glance_turns_part', 'glance_turns_part_one', 'glance_top_priced', 'glance_int_priced', 'glance_int_priced_one', 'glance_int_unknown', 'glance_int_unknown_one',
            'glance_head_ambig', 'glance_top_recorded', 'glance_top_both', 'glance_int_recorded', 'glance_int_recorded_one', 'glance_int_both', 'glance_int_both_one', 'glance_day_priced', 'glance_ambig_tail', 'glance_day_recorded', 'glance_day_both', 'glance_qs_est', 'glance_qs')

    def test_template_has_the_block_above_the_kpis_and_uses_only_defined_keys(self):
        root = Path(__file__).parent / 'tokenatlas'
        template = (root / 'report_template.html').read_text(encoding='utf-8')
        self.assertLess(template.index('id="glance-text"'), template.index('<div class="kpis"'))
        self.assertIn('renderPrompts();renderGlance();renderLimitHits();', template)
        texts = json.loads((root / 'report_i18n.json').read_text(encoding='utf-8'))
        for key in set(re.findall(r"t\('(glance_\w+)'", template)) | set(self.KEYS):
            for lang in ('sv', 'en'):
                self.assertIn(key, texts[lang], (lang, key))
        for lang in ('sv', 'en'):
            for key in ('glance_turns', 'glance_hits', 'glance_int', 'glance_int_nopct'):
                self.assertIn(key + '_one', texts[lang])
            self.assertIn('≥', template)
            self.assertIn('{pct}', texts[lang]['glance_top'])
        self.assertEqual((texts['sv']['glance_e'], texts['en']['glance_e']), ('I korthet', 'At a glance'))

    def test_hit_payload_carries_its_local_date_in_the_report_timezone(self):
        hit = {'harness': 'claude', 'at': '2026-09-03T22:15:00+00:00', 'reached': 'five_hour', 'window_minutes': 300, 'resets_at': None, 'retries': 1,
               'turn': None, 'window': None}
        from tokenatlas.report import _hit_payload
        for zone, day in (('Europe/Stockholm', '2026-09-04'), ('UTC', '2026-09-03'), ('America/Los_Angeles', '2026-09-03')):
            payload = _hit_payload(hit, {}, lambda k, v, r=None: v, zone=ZoneInfo(zone))
            self.assertEqual(payload['local_date'], day, zone)

    def test_a_known_model_with_missing_output_tokens_has_no_price_class(self):
        record = dict(id='o1', harness='claude', session='s', agent='main', thread_kind='main', parent_session=None, turn_id='t', turn_confidence='derived',
                      ts='2026-09-03T10:00:00+00:00', model='claude-sonnet-4-5', provider='anthropic', machine='m', project_id='/w', project_label='w', effort=None, origin='cli',
                      raw_usage={}, tariff=None, tokens=dict(fresh_input=1000, cache_write=0, cache_read=0, output=None, reasoning=0), complete=False,
                      id_synthetic=False, warnings=[], sources=[])
        self.assertEqual(build_report([record], {})['columns']['price'], [None])

    def test_the_summary_text_is_not_in_the_data_block(self):
        html = render_report(build_report([], {}))
        self.assertNotIn('I korthet', json.dumps(decode_html(html)))


if __name__ == '__main__':
    unittest.main()


class ReportClarity(unittest.TestCase):  # #145
    root = Path(__file__).parent / 'tokenatlas'

    def texts(self):
        return json.loads((self.root / 'report_i18n.json').read_text(encoding='utf-8'))

    def test_glance_is_a_list_with_a_plain_text_form_and_a_collapsed_glossary(self):
        template = (self.root / 'report_template.html').read_text(encoding='utf-8')
        self.assertIn('<ul id="glance-text"', template)
        self.assertIn('function glanceItems(f)', template)
        self.assertIn("const glanceText=f=>glanceItems(f).map(i=>i.text).join(' ')", template)
        self.assertIn("el('strong')", template)  # key numbers via DOM nodes, never innerHTML
        self.assertNotIn('glance-text\').innerHTML', template)
        self.assertRegex(template, r'<details class="glossary" id="glossary">(?!.* open)')

    def test_glossary_terms_exist_in_both_languages_with_one_sentence_each(self):
        texts = self.texts()
        for lang in ('sv', 'en'):
            t = texts[lang]
            for term in ('turn', 'call', 'nocall', 'list', 'int'):
                self.assertTrue(t['gl_t_' + term] and t['gl_d_' + term].endswith('.'), (lang, term))
                self.assertTrue(t['gl_re_' + term], (lang, term))
            self.assertTrue(t['gl_d_qs'])
            self.assertIn('{d}', t['gl_d_list'])
            self.assertIn('{date}', t['gl_list_date'])
        self.assertEqual((texts['sv']['gl_title'], texts['en']['gl_title']), ('Ordlista', 'Glossary'))
        self.assertIn('pristabell', texts['sv']['gl_d_list'])
        self.assertIn('price table', texts['en']['gl_d_list'])
        base = lambda lang: {k for k in texts[lang] if not k.endswith('_one')}
        self.assertEqual(base('sv'), base('en'))

    def test_payload_carries_the_price_table_date(self):
        report = build_report([observation()], {})
        self.assertRegex(report['prices_retrieved'], r'^\d{4}-\d{2}-\d{2}$')

    def test_private_and_shared_notes_under_the_prompts(self):
        texts = self.texts()
        self.assertNotIn('aldrig', texts['sv']['p4_p'])
        self.assertNotIn('never', texts['en']['p4_p'])
        self.assertEqual(texts['sv']['p4_priv_shared'], 'Text och kontext ingår aldrig i delade rapporter, bara antalet inmatningar.')
        self.assertIn('privat', texts['sv']['p4_priv_local'])
        self.assertIn('because this report is private', texts['en']['p4_priv_local'])
        for lang in ('sv', 'en'):
            self.assertIn('--shared', texts[lang]['p4_priv_local'])
        template = (self.root / 'report_template.html').read_text(encoding='utf-8')
        self.assertIn("DATA.privacy==='redacted'?'p4_priv_shared'", template)

    def test_token_unit_on_the_cards(self):
        texts = self.texts()
        self.assertEqual((texts['sv']['tok_unit'], texts['en']['tok_unit']), ('tokens', 'tokens'))
        template = (self.root / 'report_template.html').read_text(encoding='utf-8')
        self.assertIn("unit($('total'))", template)
        self.assertIn('if(!unk(k))unit($(id))', template)

    def test_limit_share_range_wording_is_plain(self):
        texts = self.texts()
        sv, en = texts['sv'], texts['en']
        self.assertEqual(sv['qs_pl_range'].format(lo=8, up=38), 'minst 8 %, högst 38 %')
        self.assertEqual(en['qs_pl_range'].format(lo=8, up=38), 'at least 8%, at most 38%')
        self.assertEqual(sv['qs_pl_atleast'].format(n=4), 'minst 4 %')  # the one-sided case
        self.assertEqual(en['qs_pl_atleast'].format(n=4), 'at least 4%')
        self.assertEqual((sv['qs_with_one'], en['qs_with_one']), (' (1 annan tur samtidigt)', ' (1 other turn at the same time)'))
        for lang in ('sv', 'en'):
            t = texts[lang]
            for k in ('qs_with', 'qs_with_one', 'qs_shared', 'qs_shared_one', 'glance_qs_range', 'glance_qs_range_one', 'glance_qs_range_alone', 'qs_alone'):
                self.assertNotRegex(t[k], r'delad med|shared with', (lang, k))
        self.assertIn('rounded percentage for the whole account', en['glance_qs_range'])
        self.assertIn('avrundad procentsats för hela kontot', sv['glance_qs_range'])
        self.assertEqual(sv['glance_qs_range'].format(pct='minst 8 %, högst 38 %', w='veckogränsen för Codex', n=603, who='Codex'),
                         'Den dyraste turen tillskrivs minst 8 %, högst 38 % av veckogränsen för Codex – 603 andra turer pågick samtidigt, och Codex visar bara en avrundad procentsats för hela kontot, där även användning som loggarna inte ser ingår.')


    def test_a_one_sided_range_is_explained_by_the_missing_reading(self):
        texts = self.texts()
        self.assertIn('bara den nedre gränsen är känd', texts['sv']['qs_open'])
        self.assertIn('only the lower bound is known', texts['en']['qs_open'])
        self.assertIn('only the lower bound is known', texts['en']['glance_qs_range_open'])
        template = (self.root / 'report_template.html').read_text(encoding='utf-8')
        self.assertIn("sp.title=q.upper==null?t('qs_open')", template)
        self.assertIn("t(q.upper==null?'glance_qs_range_open'", template)

    def test_limit_share_bounds_name_unseen_usage(self):
        texts = self.texts()  # the bounds attribute observed account movement; usage the logs do not see is in the meter too (docs/quota.md)
        for lang, phrase in (('sv', 'loggarna inte ser'), ('en', 'the logs do not see')):
            for k in ('gl_d_qs', 'qs_shared', 'qs_shared_one', 'qs_alone', 'qs_open', 'glance_qs_range', 'glance_qs_range_one', 'glance_qs_range_alone', 'glance_qs_range_open'):
                self.assertIn(phrase, texts[lang][k], (lang, k))
        self.assertIn('kan vara lägre än det lägsta värdet', texts['sv']['gl_d_qs'])
        self.assertIn('can be lower than the lowest value', texts['en']['gl_d_qs'])

    def test_glossary_definitions_match_what_is_computed(self):
        texts = self.texts()
        self.assertIn('inte kan koppla till någon tur', texts['sv']['gl_d_nocall'])  # orphans and missing turn metadata too, not only work you did not start
        self.assertIn('cannot link to any turn', texts['en']['gl_d_nocall'])
        self.assertIn('markerade ett anrop som avbrutet', texts['sv']['gl_d_int'])  # explicit interruption flags only
        self.assertIn('marked a request as interrupted', texts['en']['gl_d_int'])

class CoverageSummary(unittest.TestCase):  # #147
    def test_strings_have_singular_forms_and_an_explanation_in_both_languages(self):
        texts = json.loads((Path(__file__).parent / 'tokenatlas' / 'report_i18n.json').read_text(encoding='utf-8'))
        self.assertEqual((texts['sv']['n_files_one'], texts['sv']['n_diag_one'], texts['sv']['n_roots_one']), ('1 fil', '1 källdiagnos', '1 källmapp'))
        self.assertEqual((texts['en']['n_files_one'], texts['en']['n_diag_one'], texts['en']['n_roots_one']), ('1 file', '1 source diagnostic', '1 source folder'))
        self.assertIn('inte gick att läsa eller tolka', texts['sv']['diag_note'])
        self.assertIn('could not be read or parsed', texts['en']['diag_note'])
        for lang in ('sv', 'en'):
            self.assertNotIn('filer ·', texts[lang]['import'])  # units come with the counts now
            self.assertIn('{n}', texts[lang]['imp_all'])
        template = (Path(__file__).parent / 'tokenatlas' / 'report_template.html').read_text(encoding='utf-8')
        self.assertIn('function appendImports(', template)


class BackToTop(unittest.TestCase):  # #148
    def test_button_is_accessible_offline_and_hidden_in_print(self):
        root = Path(__file__).parent / 'tokenatlas'
        template = (root / 'report_template.html').read_text(encoding='utf-8')
        texts = json.loads((root / 'report_i18n.json').read_text(encoding='utf-8'))
        self.assertEqual((texts['sv']['to_top'], texts['en']['to_top']), ('Till toppen', 'Back to top'))
        self.assertRegex(template, r'<button type="button" id="to-top" class="to-top hidden" data-t-aria-label="to_top"><svg ')
        self.assertIn('@media print{.to-top{display:none!important}}', template)
        self.assertIn('prefers-reduced-motion: reduce', template)
        self.assertIn('env(safe-area-inset-bottom)', template)
        self.assertNotRegex(template[template.index('id="to-top"'):][:600], r'https?://(?!www\.w3\.org)')


class TopTurnsCardSize(unittest.TestCase):
    def test_card_shows_ten_turns(self):
        template = (Path(__file__).parent / 'tokenatlas' / 'report_template.html').read_text(encoding='utf-8')
        self.assertIn('function topPrompts(rows,n=10)', template)


class SectionOrder(unittest.TestCase):
    def test_turns_come_first_and_eyebrows_follow_the_visible_order(self):
        root = Path(__file__).parent / 'tokenatlas'
        template = (root / 'report_template.html').read_text(encoding='utf-8')
        marks = ['<div class="kpis"', '<section id="prompts"', '<section id="cost-facts"', '<section id="energy"',
                 'data-t="e1"', 'data-t="e2"', 'data-t="e3"', '<section id="sessions"', '<section id="coverage"']
        positions = [template.index(mark) for mark in marks]
        self.assertEqual(positions, sorted(positions))
        texts = json.loads((root / 'report_i18n.json').read_text(encoding='utf-8'))
        # Visible numbers follow the page order: turns, over time, distribution, cache, details, basis.
        for lang in ('sv', 'en'):
            for number, key in enumerate(('e4', 'e1', 'e2', 'e3', 'e5', 'e6'), 1):
                self.assertTrue(texts[lang][key].startswith(f'{number:02d} / '), (lang, key, texts[lang][key]))


class I18nPlurals(unittest.TestCase):
    def test_count_strings_have_singular_forms(self):
        import json, re
        texts = json.loads((Path(__file__).parent / 'tokenatlas' / 'report_i18n.json').read_text(encoding='utf-8'))
        for lang, noun in (('en', r'\{n\} (?:[a-z]+ )?(?:requests|sessions)\b'), ('sv', r'\{n\} sessioner\b')):
            for key, value in texts[lang].items():
                if isinstance(value, str) and re.search(noun, value) and not key.endswith('_one'):
                    self.assertIn(key + '_one', texts[lang], (lang, key))
        template = (Path(__file__).parent / 'tokenatlas' / 'report_template.html').read_text(encoding='utf-8')
        self.assertIn("+'_one'", template)
