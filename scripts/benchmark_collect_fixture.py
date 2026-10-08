#!/usr/bin/env python3
"""Create a private, deterministic SQLite history fixture for collect benchmarks.

The fixture contains only generated counters and metadata.  It never reads a
user history; generated files stay beside the supplied database path.
"""
import argparse
import contextlib
import io
import os
import time
from unittest.mock import patch
import json
import sys
import subprocess
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def source_root(argv):
    for index, value in enumerate(argv):
        if value == '--source' and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser().resolve()
        if value.startswith('--source='):
            return Path(value.split('=', 1)[1]).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


sys.path.insert(0, str(source_root(sys.argv[1:])))

from tokenatlas import history  # noqa: E402


def create(path, count):
    path = Path(path).expanduser().resolve()
    if path.exists():
        raise SystemExit(f'refusing to replace existing fixture: {path}')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with history.History(path) as database:
        connection, machine = database.connection, database.machine
        fixed = {
            'machine': machine, 'harness': 'codex', 'provider': 'openai', 'harness_version': None,
            'collector': 'benchmark', 'source_type': 'rollout', 'session': 'benchmark',
            'parent_session': None, 'session_started_at': None, 'thread_kind': 'main', 'agent': 'main',
            'origin': 'codex_cli_rs', 'model': 'gpt-5', 'effort': 'medium', 'project_id': 'bench',
            'project_label': 'bench', 'cwd': None, 'turn_id': 'bench-turn', 'turn_confidence': 'observed',
            'tariff': {'service_tier': 'standard'}, 'warnings': [], 'confidence': None, 'quota': None,
            'flags': [],
        }
        string_values = [value for value in fixed.values() if isinstance(value, str)] + ['']
        string_values += [json.dumps(fixed[name], separators=(',', ':'))
                          for name in ('tariff', 'warnings', 'confidence', 'quota', 'flags')]
        connection.executemany('INSERT OR IGNORE INTO strings(value) VALUES (?)', [(value,) for value in string_values])
        connection.execute(
            f"WITH RECURSIVE seq(n) AS (SELECT 0 UNION ALL SELECT n+1 FROM seq WHERE n<{count - 1}) "
            "INSERT INTO strings(value) SELECT 'obs-'||n FROM seq"
        )
        ids = {value: ident for ident, value in connection.execute('SELECT id,value FROM strings')}
        base = datetime(2026, 10, 1, tzinfo=timezone.utc)
        item = dict(
            fixed, id='obs-0', id_synthetic=False, complete=True, output_final=None, v=history.OBSERVATION_VERSION,
            kind='usage_observation', duration_ms=None, accounting_basis='request_top_level', billing_verified=False,
            ts=base.isoformat(),
            tokens={'fresh_input': 1200, 'cache_read': 250, 'cache_write': 0, 'output': 180, 'reasoning': 60},
            raw_usage={'input_tokens': 1450, 'output_tokens': 180},
        )
        encoded = history._encode(item)
        prefix = '.'.join(str(ids[value]) for value in json.loads(encoded[0])[:3])
        expressions, parameters = [], []
        for position, (column, value) in enumerate(zip(history.COLUMNS, encoded)):
            if column == 'key':
                expressions.append("? || '.' || s.id")
                parameters.append(prefix)
            elif column == 'call_id':
                expressions.append('s.id')
            elif column == 'ts_us':
                expressions.append('? + seq.n * 1000000')
                parameters.append(value)
            elif position in history._REF_INDEX:
                expressions.append('?')
                parameters.append(None if value is None else ids[value])
            else:
                expressions.append('?')
                parameters.append(value)
        query = (
            f"WITH RECURSIVE seq(n) AS (SELECT 0 UNION ALL SELECT n+1 FROM seq WHERE n<{count - 1}) "
            f"INSERT INTO observations({','.join(history.COLUMNS)}) SELECT {','.join(expressions)} "
            "FROM seq JOIN strings s ON s.value='obs-' || seq.n"
        )
        connection.execute(query, parameters)
        history.History._bump(connection)
        connection.commit()
        rows = connection.execute('SELECT count(*) FROM observations').fetchone()[0]
        print(json.dumps({'database': str(path), 'observations': rows, 'revision': database.revision}, sort_keys=True), flush=True)


def benchmark_environment(path):
    path = Path(path).resolve()
    home = path.parent / 'home'; home.mkdir(exist_ok=True)
    return {**{key:os.environ[key] for key in ('SystemRoot','WINDIR','COMSPEC','TEMP','TMP') if key in os.environ},
            'HOME':str(home), 'USERPROFILE':str(home), 'PATH':os.defpath,
            'LOCALAPPDATA':str(home/'AppData'), 'XDG_STATE_HOME':str(home/'state'),
            'XDG_CONFIG_HOME':str(home/'config'), 'XDG_DATA_HOME':str(home/'data'),
            'CLAUDE_CONFIG_DIR':str(home/'claude'), 'CODEX_HOME':str(home/'codex'),
            'PI_CODING_AGENT_DIR':str(home/'pi')}


def observation_count(path):
    with contextlib.closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as connection:
        return connection.execute('SELECT count(*) FROM observations').fetchone()[0]


def measure_collect(path):
    """Warm the actual CLI, then measure an unchanged regular collect in an empty fake home."""
    from tokenatlas.__main__ import main as cli
    path = Path(path).resolve()
    env = benchmark_environment(path)
    initial_count = observation_count(path)
    result = {}
    with patch.dict(os.environ, env, clear=True):
        for name, args in [('retain', ['top','--keep-text','-n','10','--json']), ('warm_collect',['collect']), ('unchanged_collect',['collect'])]:
            report = path.with_name('report.html')
            before = report.stat().st_mtime_ns if report.exists() else None
            out, err = io.StringIO(), io.StringIO()
            started, wall = time.process_time(), time.monotonic()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli(['--db',str(path),*args])
            if code: raise RuntimeError(f'{name} failed: {code}: {err.getvalue()}')
            result[name] = {'cpu_seconds':round(time.process_time()-started,6), 'wall_seconds':round(time.monotonic()-wall,6)}
            if observation_count(path) != initial_count:
                raise RuntimeError('benchmark isolation failed: observation count changed')
            if name=='unchanged_collect':
                result[name]['report_unchanged'] = before == report.stat().st_mtime_ns
                result[name]['top_skipped'] = 'top: skipped: history revision' in out.getvalue()
                path.with_name('unchanged-collect.log').write_text(out.getvalue()+err.getvalue(), encoding='utf-8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--measure-existing', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--measure-collect', action='store_true', help='warm and measure a real unchanged collect after creating the fixture')
    parser.add_argument('--observations', type=int, default=400000)
    parser.add_argument('--source', type=Path, help='TokenAtlas source tree (default: this checkout)')
    args = parser.parse_args(argv)
    args.path = args.path.expanduser().resolve()
    if args.observations < 1:
        parser.error('--observations must be positive')
    if args.measure_existing:
        print(json.dumps(measure_collect(args.path), sort_keys=True))
        return
    create(args.path, args.observations)
    if args.measure_collect:
        # Import-time default roots (including Cowork) must see the fake home too.
        subprocess.run([sys.executable, str(Path(__file__).resolve()), '--source',
                        str(source_root(sys.argv[1:])), '--measure-existing', str(args.path.resolve())],
                       env=benchmark_environment(args.path), check=True)


if __name__ == '__main__':
    main()
