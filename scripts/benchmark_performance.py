#!/usr/bin/env python3
"""Measure the expensive TokenAtlas computation steps without printing history.

The fixture is synthetic and deterministic.  The script emits counts, timings,
and hashes only.  A previous JSON result can be supplied with ``--compare`` to
calculate before/after ratios without retaining any records.
"""
import argparse
import hashlib
import inspect
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

def source_root(argv):
    for index, value in enumerate(argv):
        if value == '--source' and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser().resolve()
        if value.startswith('--source='):
            return Path(value.split('=', 1)[1]).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


sys.path.insert(0, str(source_root(sys.argv[1:])))

from tokenatlas import credits, insights, pricing, prompts, quota_share, report


UTC = timezone.utc
BASE = datetime(2026, 10, 1, tzinfo=UTC)


def synthetic(count):
    rows = []
    for i in range(count):
        turn = f't{i // 8}'
        ts = (BASE + timedelta(seconds=i)).isoformat()
        fresh = 1200 + (i % 97) * 17
        output = 180 + (i % 23) * 11
        quota = None
        if i % 8 == 7:
            quota = {'status': 'ok', 'plan_type': 'pro', 'limit_id': None, 'reached': None,
                     'windows': [{'minutes': 300, 'used_percent': (i // 8) % 95, 'resets_at': (BASE + timedelta(hours=5)).isoformat()}]}
        rows.append({
            'id': f'synthetic-{i}', 'id_synthetic': False, 'machine': 'm-benchmark',
            'harness': 'codex', 'provider': 'openai', 'model': 'gpt-5', 'effort': 'medium',
            'session': 'benchmark', 'parent_session': None, 'turn_id': turn, 'project_id': 'bench',
            'project_label': 'bench', 'agent': 'main', 'thread_kind': 'main',
            'origin': 'codex_cli_rs', 'turn_confidence': 'observed', 'cwd': None,
            'ts': ts, 'tokens': {'fresh_input': fresh, 'cache_write': 0, 'cache_read': 250,
                                 'output': output, 'reasoning': output // 3},
            'raw_usage': {'input_tokens': fresh + 250, 'output_tokens': output},
            'complete': True, 'flags': [], 'warnings': [], 'tariff': {'service_tier': 'standard'},
            'quota': quota, 'sources': [],
        })
    return rows


def digest(value):
    def normalize(item):
        if isinstance(item, dict):
            return [[repr(key), normalize(val)] for key, val in sorted(item.items(), key=lambda pair: repr(pair[0]))]
        if isinstance(item, (list, tuple)):
            return [normalize(x) for x in item]
        if isinstance(item, (set, frozenset)):
            return sorted((normalize(x) for x in item), key=repr)
        return item
    return hashlib.sha256(json.dumps(normalize(value), sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()[:16]


def timed(name, fn, result):
    start = time.process_time()
    value = fn()
    result['steps'][name] = {'cpu_seconds': round(time.process_time() - start, 6), 'hash': digest(value)}
    return value


def supports(function, name):
    return name in inspect.signature(function).parameters


def call_with_supported(function, *args, **kwargs):
    return function(*args, **{key: value for key, value in kwargs.items() if supports(function, key)})


def run(records):
    table = pricing.load_prices()
    ctable = credits.packaged()
    out = {'observations': len(records), 'steps': {}}
    assigned = timed('assign_prompts', lambda: prompts.assign_prompts(records), out)
    top = timed('top_prompts', lambda: call_with_supported(prompts.top_prompts, records, table, 10, assigned=assigned), out)
    cost_memo = {}
    cost_of = insights.memo_cost(table, cost_memo)
    snapshots = timed('snapshots', lambda: call_with_supported(quota_share.snapshots_from_records, records, assigned=assigned), out)
    timed('quota_shares', lambda: quota_share.turn_shares(records, snapshots, table, cost_of), out)
    facts = timed('cost_facts', lambda: insights.cost_facts(records, table, memo=cost_memo, credit_table=ctable), out)
    payload = timed('report', lambda: call_with_supported(
        report.build_report, records, {}, redact=True, table=table, credit_table=ctable,
        assigned=assigned, whole_assigned=assigned, quota=False,
        now=BASE + timedelta(seconds=len(records))), out)
    out['outputs'] = {'top': digest(top), 'insights': digest(facts), 'report': digest(payload)}
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='TokenAtlas source tree to benchmark (default: this checkout)')
    parser.add_argument('--observations', type=int, default=20000, help='synthetic observation count (default: 20000)')
    parser.add_argument('--compare', type=Path, help='previous benchmark JSON; print step ratios')
    parser.add_argument('--json', action='store_true', help='emit machine-readable JSON')
    args = parser.parse_args(argv)
    if args.observations < 1:
        parser.error('--observations must be positive')
    records = synthetic(args.observations)
    result = run(records)
    if args.compare:
        prior = json.loads(args.compare.read_text(encoding='utf-8'))
        result['comparison'] = {}
        for name, step in result['steps'].items():
            before = ((prior.get('steps') or {}).get(name) or {}).get('cpu_seconds')
            if before and step['cpu_seconds'] is not None:
                result['comparison'][name] = {'before_cpu_seconds': before,
                                              'ratio': round(step['cpu_seconds'] / before, 3)}
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"observations={result['observations']}")
        for name, step in result['steps'].items():
            print(f"{name}: {step['cpu_seconds']:.3f}s {step['hash']}")
        if result.get('comparison'):
            print('comparison: ' + json.dumps(result['comparison'], sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
