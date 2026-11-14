"""Scaling guards (TOKEN-189): a changed collect run must not rescan and re-price the whole history once per limit hit or reading.

The guards count operations (price lookups, timestamp parses), never wall-clock time, so they cannot flake on a slow CI machine."""
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
        expected = [budget.cost_seen(records, TABLE, 'claude', a, b) for a, b in windows]
        index = budget.SeenIndex(records, TABLE, 'claude')
        calls = []
        real = prompts._cost
        with patch.object(prompts, '_cost', lambda r, t: calls.append(1) or real(r, t)):
            self.assertEqual([budget.cost_seen(records, TABLE, 'claude', a, b, index=index) for a, b in windows], expected)
        self.assertLessEqual(len(calls), len(records))

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
            out = io.StringIO()
            with redirect_stdout(out):
                collect._step('probe', lambda: 0, db)
            self.assertRegex(out.getvalue(), r'probe exit=0 \(\d+\.\ds\) requests=0')


if __name__ == '__main__':
    unittest.main()
