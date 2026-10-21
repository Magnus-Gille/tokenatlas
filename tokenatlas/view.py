"""Versioned local report display preferences, independent of history collection.

The order of SECTIONS is a public contract. Missing configuration means the
original layout; an explicitly empty section list means no report sections.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile

SECTIONS = {
    'glance': 'At a glance', 'glossary': 'Terms (collapsed)', 'kpis': 'Token KPIs',
    'analytics': 'Request analytics', 'efficiency': 'Token efficiency',
    'turns': 'Costliest turns', 'limits': 'Limit hits', 'quota-windows': 'Quota windows',
    'facts': 'Cost facts', 'plan-facts': 'Plan facts', 'usage': 'Clients and local inference',
    'work': 'Work context', 'energy': 'Energy', 'charts': 'Timeline and ranking',
    'cache': 'Cache comparisons', 'sessions': 'Sessions', 'coverage': 'Data coverage',
}
FILTERS = ('period', 'harness', 'plan', 'project_id', 'session', 'account', 'model',
           'effort', 'provider', 'agent', 'thread_kind', 'search')
# Allowlist rather than a denylist: newly added defaults stay private until reviewed.
SHARED_FILTERS = frozenset(('period', 'harness', 'plan', 'model', 'effort', 'provider', 'thread_kind'))
PRESETS = {
    'subscription': ['limits', 'quota-windows', 'glance', 'turns', 'plan-facts', 'facts', 'sessions'],
    'api-cost': ['facts', 'turns', 'glance', 'kpis', 'analytics', 'charts', 'cache', 'sessions'],
    'minimal': ['glance', 'kpis', 'turns'],
    'full': list(SECTIONS),
}


def defaults():
    return dict(version=1, sections=list(SECTIONS), defaults={}, top=10, lang='auto', money_first=False)


def path_for(db=None):
    return (Path(db).expanduser().parent if db is not None else
            Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'tokenatlas') / 'view.json'


def warn(message):
    from tokenatlas.terminal import terminal_safe
    print('warning: ' + terminal_safe(message), file=sys.stderr)


def sections(values):
    if not isinstance(values, list) or any(not isinstance(x, str) or x not in SECTIONS for x in values):
        raise ValueError(f'Unknown section id in {values!r}. Valid sections: ' + ', '.join(SECTIONS))
    if len(set(values)) != len(values):
        raise ValueError('Section ids must not repeat. Valid sections: ' + ', '.join(SECTIONS))
    return list(values)


def clean(data):
    if not isinstance(data, dict):
        raise ValueError('view must be a JSON object')
    result = defaults()
    for key in data.keys() - result.keys():
        warn(f'view: ignoring unknown key {key!r}')
    if type(data.get('version', 1)) is not int or data.get('version', 1) != 1:
        raise ValueError('unsupported view version (expected 1)')
    if 'sections' in data:
        result['sections'] = sections(data['sections'])
    if 'top' in data:
        if type(data['top']) is not int or not 1 <= data['top'] <= 10000:
            raise ValueError('view top must be between 1 and 10000')
        result['top'] = data['top']
    if 'lang' in data:
        if data['lang'] not in ('auto', 'sv', 'en'):
            raise ValueError('view lang must be auto, sv or en')
        result['lang'] = data['lang']
    if 'money_first' in data:
        if type(data['money_first']) is not bool:
            raise ValueError('view money_first must be boolean')
        result['money_first'] = data['money_first']
    filters = data.get('defaults', {})
    if not isinstance(filters, dict):
        raise ValueError('view defaults must be an object')
    for key, value in filters.items():
        if key not in FILTERS:
            warn(f'view: ignoring unknown default {key!r}')
            continue
        if not isinstance(value, str) or len(value) > 1000 or not value.isprintable() and value != '':
            raise ValueError(f'view default {key} must be a printable string (at most 1000 characters)')
        if not value:  # empty CLI defaults clear a preference
            continue
        if key == 'period' and not re.fullmatch(r'(all|[1-9][0-9]{0,4}d)', value):
            raise ValueError('view period must be all or a number of days, e.g. 30d')
        if key == 'harness' and value not in ('claude', 'codex', 'pi', 'opencode'):
            raise ValueError('view harness must be claude, codex, pi or opencode')
        result['defaults'][key] = value
    return result


def load(db=None):
    path = path_for(db)
    try:
        return clean(json.loads(path.read_text(encoding='utf-8')))
    except FileNotFoundError:
        return defaults()
    except (OSError, ValueError, TypeError) as exc:
        warn(f'{path}: {exc}; using the default view')
        return defaults()


def save(db, data):
    data = clean(data)
    path = path_for(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.view-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=True, separators=(',', ':'))
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return data


def for_report(data=None, *, redact=True):
    """Shared payload and cache identity use the same privacy-safe configuration."""
    result = clean(load() if data is None else data)
    if redact:
        from tokenatlas import credits, pricing, subscriptions
        from tokenatlas.report import PUBLIC_NAMES, public_model_checker
        filters = {k: v for k, v in result['defaults'].items() if k in SHARED_FILTERS}
        # Display defaults are a new output boundary, not an exemption from the
        # report's existing exact-public-name policy (custom model/plan labels can
        # themselves name private projects or accounts).
        for key in ('provider', 'effort', 'thread_kind'):
            if key in filters and filters[key] not in PUBLIC_NAMES[key]:
                del filters[key]
        if 'plan' in filters and filters['plan'] not in subscriptions.PUBLIC_PLANS:
            del filters['plan']
        if 'model' in filters:
            public = public_model_checker(pricing.load_prices(), credits.packaged())
            providers = (filters['provider'],) if 'provider' in filters else PUBLIC_NAMES['provider']
            if not any(public(provider, filters['model']) for provider in providers):
                del filters['model']
        result['defaults'] = filters
    return result


def add_parser(commands):
    parser = commands.add_parser('view', help='Choose report sections, their order and opening filters.')
    subs = parser.add_subparsers(dest='view_action')
    setter = subs.add_parser('set', help='Set display options; --sections "" selects no sections.')
    setter.add_argument('--sections', help='Comma-separated stable section ids, in display order.')
    setter.add_argument('--top', type=int, help='Costliest turns to show (1–10000).')
    setter.add_argument('--lang', choices=('auto', 'sv', 'en'), help='Default report language.')
    setter.add_argument('--money-first', action='store_true', default=None, help='Put the cost column first in costliest turns.')
    setter.add_argument('--no-money-first', dest='money_first', action='store_false', default=None, help='Restore token-first turn columns.')
    for action in ('hide', 'show'):
        sub = subs.add_parser(action, help=('Hide named sections, preserving remaining order.' if action == 'hide' else 'Show named sections, appending in argument order.'))
        sub.add_argument('sections', nargs='+')
    default = subs.add_parser('default', help='Set opening filters; an empty value clears one default.')
    for key in FILTERS:
        default.add_argument('--' + ('project' if key == 'project_id' else key.replace('_', '-')), dest=key)
    preset = subs.add_parser('preset', help='Replace the view with a built-in preset.')
    preset.add_argument('name', choices=tuple(PRESETS))
    subs.add_parser('reset', help='Remove saved preferences and restore the original layout.')
    subs.add_parser('export', help='Write versioned view JSON suitable for import.')
    importer = subs.add_parser('import', help='Validate and save view JSON (unknown keys warn).')
    importer.add_argument('file', type=Path)


def run(args):
    from tokenatlas.cli_contract import emit
    action = args.view_action
    data = load(args.db) if action not in ('reset', 'import') else defaults()
    if action == 'set':
        if args.sections is not None:
            data['sections'] = sections(args.sections.split(',') if args.sections else [])
        for key in ('top', 'lang', 'money_first'):
            if getattr(args, key) is not None:
                data[key] = getattr(args, key)
    elif action in ('hide', 'show'):
        names = sections(args.sections)
        if action == 'hide':
            data['sections'] = [x for x in data['sections'] if x not in names]
        else:
            data['sections'] += [x for x in names if x not in data['sections']]
    elif action == 'default':
        for key in FILTERS:
            if getattr(args, key) is not None:
                data['defaults'][key] = getattr(args, key)
    elif action == 'preset':
        data = defaults()
        data['sections'] = list(PRESETS[args.name])
    elif action == 'import':
        data = clean(json.loads(args.file.read_text(encoding='utf-8')))
    if action == 'reset':
        path_for(args.db).unlink(missing_ok=True)
    elif action not in (None, 'export'):
        data = save(args.db, data)
    if action == 'export':
        print(json.dumps(data, indent=2))
    elif args.json:
        emit('view', dict(data, available_sections=SECTIONS, presets=list(PRESETS)))
    else:
        print('Report sections: ' + (', '.join(data['sections']) or '(none)'))
        print('Display: ' + json.dumps({k: data[k] for k in ('top', 'lang', 'money_first', 'defaults')}))
        print('Available sections:')
        for key, title in SECTIONS.items():
            print(f'  {key}: {title}')
    return 0
