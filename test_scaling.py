"""Scaling guards (TOKEN-189): a changed collect run must not rescan and re-price the whole history once per limit hit or reading.

The guards count operations (price lookups, timestamp parses), never wall-clock time, so they cannot flake on a slow CI machine."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tokenatlas import budget, limits, pricing, prompts
from test_limits import five_hour, iso, obs

TABLE = pricing.load_prices()
T0 = datetime(2026, 9, 3, 10, tzinfo=timezone.utc)


def history(requests=1500, hits=150):
    """A synthetic history: one request every ten minutes (Claude, with a stray Codex request now and then), and `hits` five-hour rejections."""
    records = [obs(f'r{i}', iso(10 * i), 1000 + i, turn=f't{i // 5}', **({'harness': 'codex', 'provider': 'openai'} if i % 7 == 0 else {}))
               for i in range(requests)]
    events = [obs(f'e{i}', iso(60 * i + 280), turn='t0', quota=five_hour(60 * i + 300)) for i in range(hits)]
    return records, events


def reference_fill(hit, records, assigned, table):
    """limits._fill as it was before TOKEN-189: a full scan of the history for every hit."""
    resets, minutes, at = limits._iso(hit['resets_at']), hit['window_minutes'], limits._iso(hit['at'])
    start = at - timedelta(minutes=minutes) if hit.get('rolling') else resets - timedelta(minutes=minutes)
    aliases = table.get('provider_aliases') or {}
    canon = lambda p: aliases.get(p, p)
    provider = canon(limits.PROVIDER.get(hit['harness']))
    turns, requests, priced, total = {}, 0, 0, 0.0
    for record in records:
        if record['harness'] != hit['harness'] or canon(record.get('provider')) != provider or record.get('id_synthetic') or not start <= prompts._t(record['ts']) <= at:
            continue
        requests += 1
        cost = prompts._cost(record, table)
        found = assigned.get(id(record))
        entry = turns.setdefault(tuple(found[:3]), [0.0, 0, 0]) if found else [0.0, 0, 0]
        entry[1] += 1
        if cost is not None:
            priced += 1
            total += cost
            entry[0] += cost
            entry[2] += 1
    return requests, priced, (total if priced else None), sorted(((k, v[0], v[1]) for k, v in turns.items() if v[2]), key=lambda kv: (-kv[1], kv[0]))[:limits.TOP]


def reference_cost_seen(records, table, harness, start, end, plan=None, default_limit=False):
    """budget.cost_seen as it was before TOKEN-189: a full scan in `records` order for every window."""
    total, unpriced, excluded = 0.0, 0, 0
    canon = budget._canon(table)
    for r in records:
        if not budget._provider_ok(r, harness, canon) or r.get('id_synthetic') or not start <= prompts._t(r['ts']) <= end:
            continue
        if default_limit and not budget.DEFAULT_LIMIT(harness, (r.get('quota') or {}).get('limit_id')):
            continue
        if harness in budget.PLANNED:
            p = budget.plan_of(r)
            if p is None:
                excluded += 1
                continue
            if p != plan:
                continue
        c = prompts._cost(r, table)
        if c is None:
            unpriced += 1
        else:
            total += c
    return total, unpriced, excluded


class ScalingTest(unittest.TestCase):
    def test_hit_windows_match_a_full_scan_and_price_each_request_once(self):
        records, events = history()
        calls = []
        real = prompts._cost
        with patch.object(prompts, '_cost', lambda r, t: calls.append(1) or real(r, t)):
            hits = limits.limit_hits(records, events, TABLE)
        self.assertGreaterEqual(len(hits), 100)
        # Before the fix this was hits x window requests (thousands); now every request is priced at most once.
        self.assertLessEqual(len(calls), len(records))
        assigned = {id(r): a for r, a in zip(records, prompts.assign_prompts(records))}
        for hit in hits:
            requests, priced, total, top = reference_fill(hit, records, assigned, TABLE)
            window = hit['window']
            self.assertEqual((window['requests'], window['priced_requests'], window['cost']), (requests, priced, total))
            self.assertEqual([(t['turn'], t['cost'], t['requests']) for t in window['top']], top)

    def test_cost_seen_index_matches_a_full_scan_and_prices_once(self):
        records, _ = history()
        windows = [(T0 + timedelta(minutes=60 * i), T0 + timedelta(minutes=60 * i + 300)) for i in range(150)]
        expected = [reference_cost_seen(records, TABLE, 'claude', a, b) for a, b in windows]
        index = budget.SeenIndex(records, TABLE, 'claude')
        calls = []
        real = prompts._cost
        with patch.object(prompts, '_cost', lambda r, t: calls.append(1) or real(r, t)):
            self.assertEqual([budget.cost_seen(records, TABLE, 'claude', a, b, index=index) for a, b in windows], expected)
        self.assertLessEqual(len(calls), len(records))

    def test_cost_seen_index_matches_the_old_scan_on_shuffled_records_and_exact_bounds(self):
        import random
        records, _ = history(requests=600)
        random.Random(189).shuffle(records)  # input order differs from time order
        bounds = [(T0 + timedelta(minutes=10 * i), T0 + timedelta(minutes=10 * i + 290)) for i in range(0, 580, 7)]  # ends fall exactly on request times
        index = budget.SeenIndex(records, TABLE, 'claude')
        for a, b in bounds:
            for default_limit in (False, True):
                self.assertEqual(budget.cost_seen(records, TABLE, 'claude', a, b, default_limit=default_limit, index=index),
                                 reference_cost_seen(records, TABLE, 'claude', a, b, default_limit=default_limit), (a, b, default_limit))

    def test_codex_plan_and_limit_filters_and_input_order_sums_match_the_old_scan(self):
        import random
        model = next(m['model'] for m in TABLE['models'] if m['provider'] == 'openai' and not m.get('free') and m.get('output'))
        rng = random.Random(1890)
        records = []
        for i in range(400):
            tokens = rng.choice((1, 7, 10 ** 9, 3 * 10 ** 7))  # mixed magnitudes: a float sum depends on its order
            r = obs(f'c{i}', iso(5 * i), tokens, harness='codex', provider='openai', session=f's{i % 9}',
                    plan=rng.choice(('plus', 'pro', None, 'unknown')))
            r['model'] = model if i % 11 else 'not-a-priced-model'  # some unpriced requests
            r['quota'] = {'limit_id': rng.choice((None, 'codex', 'codex_bengalfox')), 'plan_type': rng.choice((None, None, 'Plus'))}
            records.append(r)
        rng.shuffle(records)
        index = budget.SeenIndex(records, TABLE, 'codex')
        bounds = [(T0 + timedelta(minutes=5 * i), T0 + timedelta(minutes=5 * i + 300)) for i in range(0, 390, 13)]
        order_matters = False
        for a, b in bounds:
            for plan in ('plus', 'pro', None):
                for default_limit in (False, True):
                    got = budget.cost_seen(records, TABLE, 'codex', a, b, plan=plan, default_limit=default_limit, index=index)
                    want = reference_cost_seen(records, TABLE, 'codex', a, b, plan=plan, default_limit=default_limit)
                    self.assertEqual(got, want, (a, b, plan, default_limit))
                    kept = [r for r in records if a <= prompts._t(r['ts']) <= b and budget.plan_of(r) == plan and plan is not None
                            and (not default_limit or budget.DEFAULT_LIMIT('codex', r['quota']['limit_id']))]
                    costs = [c for c in (prompts._cost(r, TABLE) for r in sorted(kept, key=lambda r: r['ts'])) if c is not None]
                    order_matters |= sum(costs, 0.0) != want[0]
        self.assertTrue(order_matters, 'the fixture must make time-order and input-order sums differ somewhere, or it cannot catch a reordering')

    def test_timestamps_are_parsed_once(self):
        prompts._t.cache_clear()
        for _ in range(3):
            for i in range(50):
                prompts._t(iso(i))
        self.assertEqual(prompts._t.cache_info().misses, 50)

    def test_collect_logs_the_history_size_next_to_the_timing(self):
        import io, re, tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from tokenatlas import collect
        from tokenatlas.history import History
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'history.sqlite3'
            with History(db):
                pass
            self.assertEqual(collect.history_size(db), 0)
            self.assertIsNone(collect.history_size(Path(d) / 'missing.sqlite3'))
            self.assertFalse((Path(d) / 'missing.sqlite3').exists(), 'a read-only probe creates no file')
            names = ['hist#2026%41.sqlite3'] + (['hist?x.sqlite3'] if os.name != 'nt' else [])  # '?' is not a valid Windows filename character
            for name in names:  # URI-reserved characters must not drop mode=ro or retarget the open
                odd = Path(d) / name
                with History(odd):
                    pass
                self.assertEqual(collect.history_size(odd), 0, name)
            created = {p.name for p in Path(d).iterdir()}
            self.assertFalse({n for n in created if not (n.startswith('history.sqlite3') or any(n.startswith(x) for x in names))}, 'a probe opened another file')
            out = io.StringIO()
            with redirect_stdout(out):
                collect._step('probe', lambda: 0, db)
            self.assertRegex(out.getvalue(), r'probe exit=0 \(\d+\.\ds\) requests=0')


if __name__ == '__main__':
    unittest.main()
