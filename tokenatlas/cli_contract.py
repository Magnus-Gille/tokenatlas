"""Versioned CLI output and errors. No history/report imports (also used by help)."""
import argparse
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


class CLIError(ValueError):
    def __init__(self, message, status=2, hint=None, command=None):
        super().__init__(message)
        self.status = status
        self.hint = hint
        self.command = command


class NotFound(CLIError):
    def __init__(self, message, hint=None):
        super().__init__(message, 3, hint)


class NoData(CLIError):
    def __init__(self):
        super().__init__('No observations match the selected range or filters.', 4,
                         'Run refresh --all, widen --start/--end, or remove filters.')


def payload(command, data):
    return dict(data, schema=f'tokenatlas.{command}/1')


def emit(command, data):
    print(json.dumps(payload(command, data), indent=2, sort_keys=True))


def error(command, exc, json_mode):
    status = exc.status if isinstance(exc, CLIError) else (3 if isinstance(exc, FileNotFoundError) else 2 if isinstance(exc, ValueError) else 1)
    code = {1: 'internal', 2: 'usage', 3: 'not_found', 4: 'no_data'}.get(status, 'internal')
    hint = getattr(exc, 'hint', None) or ('See tokenatlas' + ('' if command=='cli' else ' '+command) + ' --help.' if status != 1 else 'Run tokenatlas doctor --json; report this failure with the command and diagnostics.')
    message = str(exc)
    if json_mode:
        print(json.dumps(payload(command, {'ok': False, 'error': {'code': code, 'message': message, 'hint': hint, **({'details': exc.details} if hasattr(exc,'details') else {})}})), file=sys.stderr)
    else:
        from tokenatlas.terminal import terminal_safe
        print(f'{code}: {terminal_safe(message)}\nHint: {hint}', file=sys.stderr)
    return status


def iso_date(text, name, timezone='UTC'):
    try:
        value = datetime.fromisoformat(text.replace('Z', '+00:00'))
        return value if value.tzinfo else value.replace(tzinfo=ZoneInfo(timezone))
    except ValueError as exc:
        raise CLIError(f'--{name}: dates are ISO 8601, e.g. 2026-10-03 or 2026-10-03T14:00; optionally add Z or an offset.',
                       hint=f'Use --{name} 2026-10-03T14:00Z for UTC; without an offset the command timezone is used.') from exc


EXAMPLES = {
    '': 'tokenatlas refresh --all --json\n  tokenatlas top -n 1 --json',
    'refresh': 'tokenatlas refresh --all --json\n  tokenatlas refresh --harness codex --root /tmp/demo/codex --json',
    'open': 'tokenatlas open --no-browser\n  tokenatlas open --no-refresh --json',
    'show': 'tokenatlas show --print-path\n  tokenatlas show --json',
    'snapshot': 'tokenatlas snapshot /tmp/history.sqlite3 --json',
    'import': 'tokenatlas import /tmp/history.sqlite3 --label demo --json',
    'report': 'tokenatlas report --start 2026-10-03 --end 2026-10-04 --json\n  tokenatlas report --html /tmp/report.html --json',
    'session': 'tokenatlas session codex:SESSION_ID --json',
    'rate': 'tokenatlas rate claude:SESSION_ID --json\n  tokenatlas rate claude:SESSION_ID --unit task-1 --thread claude:agent:AGENT_ID --outcome pass --json',
    'top': 'tokenatlas top -n 1 --start 2026-10-03 --end 2026-10-04 --json',
    'insights': 'tokenatlas insights --days 7 --json\n  tokenatlas insights --start 2026-10-03 --end 2026-10-04 --json',
    'overhead': 'tokenatlas overhead --refresh --json',
    'collect': 'tokenatlas collect --no-report --json',
    'schedule': 'tokenatlas schedule --status --json\n  tokenatlas schedule --every 30m --dry-run --json',
    'upgrade': 'tokenatlas upgrade --check --json\n  tokenatlas upgrade --yes --json',
    'statusline': 'tokenatlas statusline --setup',
    'quota': 'tokenatlas quota show --json',
    'quota calibrate': 'tokenatlas quota calibrate --harness claude --window 5h --used 52% --json',
    'quota set': 'tokenatlas quota set --harness claude --window 5h --budget-usd 100 --json',
    'quota show': 'tokenatlas quota show --json',
    'quota forget': 'tokenatlas quota forget --harness claude --window 5h --json',
    'plan': 'tokenatlas plan show --json',
    'plan set': 'tokenatlas plan set --harness claude max-5x --json',
    'plan remove': 'tokenatlas plan remove --harness claude --json',
    'plan show': 'tokenatlas plan show --json',
    'profile': 'tokenatlas profile provider show --json',
    'profile provider': 'tokenatlas profile provider show --json',
    'profile provider add': 'tokenatlas profile provider add local --json',
    'profile provider remove': 'tokenatlas profile provider remove local --json',
    'profile provider show': 'tokenatlas profile provider show --json',
    'doctor': 'tokenatlas doctor --json',
}

# Descriptions for options which predate the agent-facing help contract.
DESCRIPTIONS = {
    'harness': 'Harness to select; omitted includes all where supported.',
    'granularity': 'Time bucket size.', 'timezone': 'IANA timezone for buckets and dates without offsets.',
    'session': 'Exact session ID filter.', 'turn': 'Exact turn ID filter.',
    'effort': 'Exact reasoning effort filter.', 'provider': 'Exact provider filter.', 'agent': 'Exact stored agent label or name, which may differ from a thread ID. For thread IDs and ratings, use rate SESSION --json.',
    'out': 'Destination for the private SQLite snapshot.', 'snapshot': 'Source SQLite snapshot file.',
    'unit': 'Your unit-of-work label; omit to list rateable threads.',
    'thread': 'Thread key from rate output; repeat to combine threads into a unit.',
    'outcome': 'Recorded outcome; requires --unit and --thread.', 'note': 'Optional outcome note.',
    'limit': 'Maximum number of ranked turns.', 'by': 'Ranking metric.',
    'json': 'Write one versioned JSON result to stdout; errors are JSON on stderr (default: command normal output).',
    'setup': 'Print Claude Code settings snippet without changing settings.',
    'record_quota': 'Compatibility flag; quota recording is enabled by default.',
    'no_record_quota': 'Disable recording quota snapshots.',
    'budget_usd': 'Known list-price size of the window in USD.',
    'window': 'Quota window to select; omit to select all where supported.',
    'name': 'Local printable label (1–120 characters).',
}


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


class Parser(argparse.ArgumentParser):
    """Examples and explicit defaults for every command, including nested ones."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('formatter_class', HelpFormatter)
        super().__init__(*args, **kwargs)
        command = self.prog.removeprefix('tokenatlas').strip()
        self.epilog = 'Examples:\n  ' + EXAMPLES.get(command, 'tokenatlas --help')
        if not command:
            self.epilog += '\n\nAgent guide: docs/agents.md (task → command → JSON fields). Exit codes: 0 ok, 1 internal, 2 usage, 3 not found, 4 no data. TOKENATLAS_NO_BROWSER=1 disables browser launch.'

    def format_help(self):
        for action in self._actions:
            if action.help is None and not isinstance(action, argparse._SubParsersAction):
                action.help = DESCRIPTIONS.get(action.dest, action.dest.replace('_', ' ').capitalize() + '.')
        return super().format_help()

    def error(self, message):
        raise CLIError(message, command=self.prog.split()[1] if len(self.prog.split()) > 1 else None)

    def exit(self, status=0, message=None):
        if status:
            raise CLIError((message or 'Invalid arguments.').removeprefix('usage: ').strip(), status)
        return super().exit(status, message)


def configure_json(parser):
    """Opt in at command level (not on the statusline stream)."""
    for action in parser._actions:
        if action.dest == 'json':action.default=argparse.SUPPRESS
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                if name == 'statusline':
                    continue
                if not any(a.dest == 'json' for a in sub._actions):
                    sub.add_argument('--json', action='store_true', help=DESCRIPTIONS['json'])
                configure_json(sub)
