"""Self-contained offline report with an allowlisted, pseudonymized data boundary."""
from __future__ import annotations
import base64
import gzip
import hashlib
import json
import os
import re
import stat
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from tokenatlas import __version__
from tokenatlas import budget, credits as credit_rates, energy, insights, limits, pricing, prompts, quota_share
from tokenatlas.history import ALL_FIELDS
from tokenatlas.resume import resume_info

PUBLIC_NAMES = dict(
    provider=frozenset('anthropic openai openai-codex openrouter opencode berget google mistral'.split()),
    origin=frozenset(('cli', 'claude-desktop', 'sdk-cli', 'sdk-py', 'sdk-ts', 'codex-tui', 'codex_cli_rs',
                      'codex_exec', 'Codex Desktop', 'codex_work_desktop', 'vscode')),
    effort=frozenset('none minimal low medium high xhigh max ultra auto'.split()),
    harness=frozenset('claude codex pi opencode'.split()),
    thread_kind=frozenset('main subagent automation'.split()),
    turn_confidence=frozenset('observed derived absent'.split()),
)
CONSERVATIVE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._: -]{0,120}')
PUBLIC_MODEL = re.compile(
    r'(?:(?:openai|anthropic|google|qwen|z-ai|zai-org|mistralai|meta-llama|deepseek|moonshotai|x-ai)/)?'
    r'(?:claude|gpt|o[0-9]|codex|gemini|gemma|mistral|codestral|ministral|magistral|pixtral|devstral|qwen|llama|deepseek|glm|kimi|grok)'
    r'[A-Za-z0-9._-]{0,100}(?::free)?', re.IGNORECASE)


INSIGHT_DAYS = 30
MAX_QUOTA_WINDOWS = 12
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DICT_FIELDS = ('harness', 'provider', 'model', 'effort', 'thread_kind', 'origin', 'turn_confidence', 'session',
               'parent_session', 'turn_id', 'agent', 'project_id', 'project_label', 'warnings')
WARNING_SEPARATOR = '\x1f'
LANGS = ('auto', 'sv', 'en')
# Language-neutral labels: \x01 + kind + optional zero-padded number (+ suffix), localized by the page. Kinds: p project,
# u unknown project, t turn, w warning. They only ever appear as dictionary entries, never per row.
CODES = {'Projekt': 'p', 'Tur': 't', 'Varning': 'w'}
UNKNOWN_PROJECT, PROJECT = '\x01u', '\x01p'


def encode_columns(rows):
    """Columnar payload: dictionaries plus integer index columns (`prompt` = prompt ordinal or null; `price` = index into
    `price_classes`, null when unpriced; `credit` = index into `credit_classes` (credit rates per 1M tokens), null when the row has no credit rate; the page computes each cost from the tokens, `cw1h` and the class's unit prices); ts is delta-coded epoch ms and `off`
    (minutes east of UTC, dictionary-coded) lets the page derive local date/hour/minute exactly as Python did."""
    def dictionary(values):
        table, index = {}, []
        for value in values:
            index.append(table.setdefault(value, len(table)))
        return list(table), index
    dicts, idx = {}, {}
    for key in DICT_FIELDS:
        values = ([WARNING_SEPARATOR.join('' if w is None else w for w in r['warnings']) for r in rows]
                  if key == 'warnings' else [r[key] for r in rows])
        dicts[key], idx[key] = dictionary(values)
    dicts['off'], idx['off'] = dictionary(r['off'] for r in rows)
    ms = [r['ms'] for r in rows]
    ids = [r['id'] for r in rows]
    credit_classes = {}  # [input, cached input, output] ChatGPT credits per 1M tokens -> class number (null `credit` = no rate)
    credit = [None if r['credit_rates'] is None else credit_classes.setdefault(tuple(r['credit_rates']), len(credit_classes)) for r in rows]
    classes = {}  # unit-price vector -> class number, in order of first appearance
    price = [None if r['unit_prices'] is None else classes.setdefault(tuple(r['unit_prices']), len(classes)) for r in rows]
    return dict(n=len(rows), dict=dicts, idx=idx, ts=[b - a for a, b in zip([0] + ms, ms)],
                id=ids, id_prefix='Observation ' if ids and all(isinstance(i, (int, type(None))) for i in ids) else None,
                tokens={k: [r['tokens'][k] for r in rows] for k in ALL_FIELDS},
                prompt=[r['prompt'] for r in rows], price=price, price_classes=[list(v) for v in classes], credit=credit, credit_classes=[list(v) for v in credit_classes],
                cw1h=[r['cw1h'] for r in rows], complete=[int(r['complete']) for r in rows], interrupted=[int(r['interrupted']) for r in rows], id_synthetic=[int(r['id_synthetic']) for r in rows])


COVERAGE_FIELDS = ('observations', 'first_event', 'last_event', 'missing_source_files',
                   'incomplete_observations', 'unlinked_turns')
IMPORT_FIELDS = ('harness', 'status', 'files_seen', 'files_parsed', 'malformed_lines', 'partial_lines',
                 'read_errors', 'unparsed_usage_lines', 'last_success')


def coverage_key(source_status):
    """Coverage subset embedded in a report, minus the volatile last_success timestamps."""
    key = {k: source_status.get(k) for k in COVERAGE_FIELDS}
    key['imports'] = [{k: imp.get(k) for k in IMPORT_FIELDS if k != 'last_success'}
                      for imp in source_status.get('imports', [])]
    return key


def report_state(revision, machine, spec, coverage, token=None, texts_hash=None, day=None, quota=None):
    """(identity, data) 32-hex pair. Identity: version, options, database, template and, for private reports, the embedded prompt previews; data: revision token, counter, coverage and, when given, the UTC day the rolling 30-day cost facts were computed for."""
    dump = lambda body: json.dumps(body, sort_keys=True, separators=(',', ':'))
    template = hashlib.sha256(Path(__file__).with_name('report_template.html').read_bytes()
                              + Path(__file__).with_name('report_i18n.json').read_bytes()).hexdigest()
    identity = dump({'format': 2, 'version': __version__, 'spec': spec, 'machine': machine,
                     **({'prompt_texts': texts_hash} if texts_hash else {})})
    data = dump({'token': token, 'revision': int(revision), 'coverage': coverage, **({'insights_day': day} if day else {}), **({'quota': quota} if quota else {})})
    return tuple(hashlib.sha256(text.encode()).hexdigest()[:32] for text in (identity + template, data))


def build_report(records, source_status, timezone_name='Europe/Stockholm', redact=True, prompt_texts=None, table=None, lang='auto',
                 prompt_context=None, prompt_inputs=None, now=None, credit_table=None, demo=False, limit_hits=None, universe=None, quota=True, all_hits=None, quota_events=None, claude_quota=None, budgets=None):
    """prompt_texts ({(harness, session, turn_id): text or None} from prompt_store) and prompt_context ({key: turn_context dict}) are for
    prompt_inputs ({key: input count or None}) are for private reports only (any of them with redact=True raises);
    credit_table is the ChatGPT credit rate card behind `credit_classes` and the credits fact (None = packaged credits.json); table is the price table behind the `price_classes` unit prices (None = packaged prices). `insights` holds the cost facts (insights.py) for the
    last 30 days before `now` (default: the current time) and for all given records, computed here and never following the page filters; model names
    go through the same redaction as the rows. A private report with texts or contexts also carries `prompt_resume` ({id: {command, codex_link}}, resume.py),
    and demo=True marks it as a fictional demo (the page then explains instead of opening or copying)."""
    if lang not in LANGS:
        raise ValueError(f'unknown report language {lang!r}; use one of {", ".join(LANGS)}')
    if redact and (prompt_texts is not None or prompt_context is not None or prompt_inputs is not None):
        raise ValueError('prompt text, turn context and input counts cannot be included in a redacted report')
    table = table or pricing.load_prices()
    credit_table = credit_table or credit_rates.packaged()
    zone = ZoneInfo(timezone_name)
    records = sorted(records, key=lambda r: (r['ts'], r['harness'], r['id']))
    aliases = {}
    def alias(kind, value):
        if value in (None, '', 'unknown'):
            return None
        table = aliases.setdefault(kind, {})
        if value not in table:
            table[value] = (f'\x01{CODES[kind]}{len(table) + 1:03d}' if kind in CODES
                            else f'{kind} {len(table) + 1:03d}')
        return table[value]
    projects = sorted({r.get('project_id') for r in records if r.get('project_id')})
    labels = {key: Path(key).name.lstrip('\x01') or PROJECT for key in projects}
    counts, seen = Counter(labels.values()), Counter()
    for key in projects:
        label = labels[key]
        if counts[label] > 1:
            seen[label] += 1
            labels[key] = f'{label} · {seen[label]}'
    def metadata(kind, value, record=None):
        if value is None or not redact:
            return value
        if kind == 'model':
            public = record.get('provider') in PUBLIC_NAMES['provider'] and PUBLIC_MODEL.fullmatch(str(value))
        elif record is None:
            public = CONSERVATIVE_NAME.fullmatch(str(value))
        else:
            public = isinstance(value, str) and value in PUBLIC_NAMES[kind]
        return value if public else alias(kind, str(value))
    assigned = prompts.assign_prompts(records)
    whole, whole_assigned = records, assigned  # the history quota shares are computed over: account-wide counters need every request
    if universe is not None:  # a filtered report: the cards use the whole history's assignment, as the limit hits do
        whole, whole_assigned = universe, prompts.assign_prompts(universe)
        full = {prompts.ident(r): a for r, a in zip(universe, whole_assigned)}
        assigned = [full.get(prompts.ident(r), a) for r, a in zip(records, assigned)]
    shown = {}  # prompt key -> ordinal, numbered by first appearance in row order
    rows = []
    for index, record in enumerate(records):
        dt = datetime.fromisoformat(record['ts']).astimezone(zone)
        row = {key: metadata(key, record.get(key), record) for key in
               ('harness', 'provider', 'model', 'effort', 'thread_kind', 'origin', 'turn_confidence')}
        oid = alias('Observation', record.get('id')) if redact else record.get('id')
        row['id'] = None if oid is None else int(oid.rsplit(' ', 1)[1]) if redact else oid
        for key, kind in (('session', 'Session'),
                           ('parent_session', 'Session'), ('turn_id', 'Tur'), ('agent', 'Agent')):
            value = record.get(key)
            if key in ('session', 'parent_session') and value not in (None, '', 'unknown'):
                value = f"{record['harness']}:{value}"
            row[key] = alias(kind, value) if redact else value
        unit, cw1h = pricing.price_vector(record, table)
        found = assigned[index]
        key = found and tuple(found[:3])  # a tuple, never joined: ids may contain ':' and must not collide
        if key and key not in shown:
            shown[key] = len(shown)
        row.update(prompt=key and shown[key], unit_prices=unit, credit_rates=credit_rates.credit_vector(record, credit_table), cw1h=cw1h, ts=record['ts'], ms=(dt - EPOCH) // timedelta(milliseconds=1),
                   off=int(dt.utcoffset().total_seconds() // 60),
                   project_id=alias('Projekt', record.get('project_id')),
                   project_label=(alias('Projekt', record.get('project_id')) if redact else
                                  labels.get(record.get('project_id'), UNKNOWN_PROJECT)),
                   tokens={key: record['tokens'].get(key) for key in ALL_FIELDS},
                   complete=bool(record['complete']), id_synthetic=bool(record['id_synthetic']),
                   interrupted='interrupted' in (record.get('flags') or ()),
                   warnings=[metadata('Varning', x) for x in record.get('warnings', [])])
        rows.append(row)
    coverage = {key: source_status.get(key) for key in COVERAGE_FIELDS}
    coverage.update(coverage_complete=False, billing_verified=False)
    coverage['imports'] = [{key: imp.get(key) for key in IMPORT_FIELDS} for imp in source_status.get('imports', [])]
    coverage['ranges'] = []
    for harness in sorted({r['harness'] for r in rows}):
        group = [r for r in rows if r['harness'] == harness]
        coverage['ranges'].append(dict(harness=harness, observations=len(group),
                                       first_event=group[0]['ts'], last_event=group[-1]['ts']))
    now = now or datetime.now(timezone.utc)
    display = lambda provider, model: metadata('model', model, {'provider': provider})
    memo = {}
    cost_of = insights.memo_cost(table, memo)
    # one captured `now` is the exclusive end of the 30-day window: later-dated observations are not 'the last 30 days'
    snapshots = quota_share.snapshots_from_records(whole, whole_assigned, quota_events or (), claude=claude_quota) if quota else []  # one per window per request that carries a quota
    shares = quota_share.turn_shares(whole, snapshots, table, cost_of) if snapshots else None
    windows = [dict(id=wid, **insights.public(insights.cost_facts(records, table, start, end, name=display, memo=memo, credit_table=credit_table, hits=limit_hits, universe=universe, quota=shares)))
               for wid, start, end in (('30d', now - timedelta(days=INSIGHT_DAYS), now), ('all', None, None))]
    # the page's energy card (filter-following) sums tokens x per-class constant x a multiplier per (provider, model); only Claude tiers have one
    # (the rest is unweighted, multiplier 1), keyed by the provider and model names as the rows carry them (after redaction)
    weights = {}
    for record, row in zip(records, rows):
        mult, weighted = energy.multiplier(record.get('provider'), record.get('model'))
        if weighted:
            weights.setdefault(row['provider'], {})[row['model']] = mult
    report = dict(version=2, generated_at=now.isoformat(),
                  timezone=timezone_name, lang=lang, privacy='redacted' if redact else 'local',
                  columns=encode_columns(rows), coverage=coverage,
                  energy=dict(per_1k=energy.PER_1K, uncertainty=energy.UNCERTAINTY, tier_multipliers=energy.TIERS, multipliers=weights), insights=dict(days=INSIGHT_DAYS, big_turn=insights.BIG_TURN, windows=windows))
    if prompt_texts is not None:
        report['prompt_texts'] = {shown[tuple(k)]: t for k, t in prompt_texts.items() if t and tuple(k) in shown}
    if prompt_context is not None:
        report['prompt_context'] = {shown[tuple(k)]: c for k, c in prompt_context.items() if c and tuple(k) in shown}
    if prompt_texts is not None or prompt_context is not None:
        found = {}
        for k in {*(prompt_texts or {}), *(prompt_context or {})}:
            info = resume_info(k[0], k[1], ((prompt_context or {}).get(k) or {}).get('cwd')) if tuple(k) in shown else None
            if info:
                found[shown[tuple(k)]] = info
        if found:
            report['prompt_resume'] = found
    def hit_scope(hit):
        # private reports only: the hit's own origin row, in the same encoding as the rows' filter values (so the page can match its filters without the hit's turn)
        scope = hit.get('scope') or {}
        session = scope.get('session')
        return dict(project_id=alias('Projekt', scope.get('project_id')), session=None if session in (None, '', 'unknown') else f"{hit['harness']}:{session}",
                    provider=scope.get('provider'), model=scope.get('model'), effort=scope.get('effort'), agent=scope.get('agent'))
    if limit_hits:
        report['limit_hits'] = [_hit_payload(h, shown, metadata, prompt_texts, redact, zone, None if redact else hit_scope) for h in limit_hits]
    if snapshots or getattr(snapshots, 'events', ()):
        report.update(_quota_payload(shares or {}, quota_share.windows(snapshots, last=4, records=whole, cost_of=cost_of, hits=limit_hits if all_hits is None else all_hits), shown, metadata, lambda name: alias('limit', name) if redact else name))  # a limit id is pseudonymized whatever it looks like
    if not redact:  # a shared report has no calibrated share, manual or automatic: with the turn costs it would give the budget (the plan size) away
        auto, _ = budget.auto_budgets(whole, all_hits if all_hits is not None else limit_hits, snapshots, table)  # computed here from the history, never stored (#116)
        merged = budget.combine({(b['harness'], b['minutes'], b.get('plan')): b for b in budgets or ()}, auto)
        if merged:
            report.update(_calibration_payload(budget.public(merged), whole, whole_assigned, cost_of, report.get('quota_shares', {}), shown, table))
    if demo:
        report['demo'] = True
    if prompt_inputs is not None:
        report['prompt_inputs'] = {shown[tuple(k)]: n for k, n in prompt_inputs.items() if isinstance(n, int) and tuple(k) in shown}
    return report


def _quota_payload(shares, windows, shown, metadata, account):
    """Quota shares for the page (quota_share.py): per turn by prompt ordinal, never session or turn ids, and the recent windows of the account.
    `quota_shares` only has turns that are in this report; both keys are left out when there is nothing to show. `account` names a limit id (a
    non-default one such as a model-specific limit; the harness's own default is left out)."""
    found = {}
    for turn, share in quota_share.largest(shares).items():
        if tuple(turn) in shown:
            obs = share['observed']
            found[shown[tuple(turn)]] = dict(harness=metadata('harness', share['window_key'][0], {'harness': share['window_key'][0]}), minutes=share['window_key'][2], label=share['label'], percent=quota_share.value(share),
                                             shared_with=obs and obs['shared_with'])
    out = {}
    if found:
        out['quota_shares'] = found
    windows = sorted(windows, key=lambda w: w['resets_at'])[-MAX_QUOTA_WINDOWS:]  # keeps the table compact when an account has several limits
    if windows:
        out['quota_windows'] = [dict(harness=metadata('harness', w['harness'], w), account=None if w['account'] == w['harness'] else account(w['account']), minutes=w['minutes'], resets_at=w['resets_at'], start=w['start'],
                                     peak_percent=w['peak_percent'], peak_at=w['peak_at'], hit=w['hit'], snapshots=w['snapshots'],
                                     cost=w.get('cost'), unpriced_requests=w.get('unpriced_requests'), uncertain_requests=w.get('uncertain_requests', 0), lower_bound=w.get('lower_bound', False)) for w in windows]
    return out


def _calibration_payload(budgets, whole, assigned, cost_of, found, shown, table):
    """The user's quota calibration (budget.py): `quota_shares` entries labeled 'calibrated' for the cards that have no observed or estimated
    share (list price of the turn over the derived budget, the weekly one first), and `quota_calibration`, the note with the derived budgets.
    Private reports only (build_report): percentages next to turn costs would reveal the plan size."""
    derived = {(b['harness'], b['minutes'], b.get('plan')): b for b in budgets}
    costs = budget.turn_costs(whole, assigned, cost_of, table)  # the whole turn's identified cost, as `top` computes it
    out = {}
    shares = dict(found)
    for key, ordinal in shown.items():
        if ordinal in shares and shares[ordinal]['label'] in ('observed', 'estimate'):
            continue
        item = budget.share(derived, key[0], *costs[key]) if key in costs else None
        if item:
            shares[ordinal] = dict(harness=key[0], minutes=item['window_minutes'], label=item['label'], percent=item['exact_percent'], shared_with=None,
                                   date=item['calibration']['date'], lower_bound=item['lower_bound'], **({'source': item['calibration']['source']} if item['label'] == 'auto-calibrated' else {}))
    if shares:
        out['quota_shares'] = shares
    out['quota_calibration'] = [dict(harness=b['harness'], minutes=b['minutes'], plan=b.get('plan'), source=b['source'], budget_usd=round(b['budget_usd'], 2), readings=b['readings'],
                                     spread=b['spread'] and [round(v, 2) for v in b['spread']], date=b['date'],
                                     **({'first_date': b.get('first_date')} if budget.is_auto(b) else {})) for b in budgets]
    return out


def _hit_payload(hit, shown, metadata, texts=None, redact=True, zone=None, scope_of=None):
    """A limit hit for the page: turns are prompt ordinals (as the rows carry them), never session ids or turn ids; ordinal None = not in this report.
    `label` is what names a turn that has no card: its time and agent, plus (private reports only) a stored prompt preview if there is one.
    A shared report names only the allowlisted limit types; any other type is "other". `local_date` is the hit's date in the report's timezone (as the rows' dates are), so the page needs no timezone database of its own. `scope` (private reports only, `scope_of`) is the hit's own origin row metadata as the rows' filter values, for matching the page's project, session, provider, model, effort and agent filters."""
    window = hit.get('window')
    ordinal = lambda turn: shown.get(tuple(turn)) if turn else None
    def label(turn, at):
        if not turn:
            return None
        text = None if redact else (texts or {}).get(tuple(turn))
        return dict(at=at, harness=metadata('harness', turn[0], hit), text=text[:200] if isinstance(text, str) and text else None)
    local = datetime.fromisoformat(hit['at']).astimezone(zone).date().isoformat() if zone else hit['at'][:10]
    payload = dict(harness=metadata('harness', hit['harness'], hit), at=hit['at'], local_date=local, reached=limits.public_reached(hit['reached']) if redact else hit['reached'],
                window_minutes=hit['window_minutes'], resets_at=hit['resets_at'], retries=hit['retries'], prompt=ordinal(hit['turn']),
                label=label(hit['turn'], hit['at']),
                window=window and dict(start=window['start'], end=window['end'], requests=window['requests'], unpriced_requests=window['unpriced_requests'],
                                       cost=window['cost'], lower_bound=window['lower_bound'],
                                       top=[dict(prompt=ordinal(t['turn']), label=label(t['turn'], t['first_ts']), requests=t['requests'], cost=t['cost'], share=t['share'], lower_bound=t['lower_bound'])
                                            for t in window['top']]))
    if scope_of:
        payload['scope'] = scope_of(hit)
    return payload


STATE_META = re.compile(rb'<meta name="tokenatlas-state" content="([0-9a-f]{32})\.([0-9a-f]{32})">')


def read_report_state(path):
    """(identity, data) recorded in an existing regular report file (head only); None when absent, special or unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            found = STATE_META.search(stream.read(4096))
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)
    return tuple(g.decode() for g in found.groups()) if found else None


def pack(value, ascii_only=True):
    payload = json.dumps(value, ensure_ascii=ascii_only, separators=(',', ':'), allow_nan=False)
    return base64.b64encode(gzip.compress(payload.encode('utf-8'), compresslevel=9, mtime=0)).decode('ascii')


def render_report(report, template=None, state=None):
    """The page carries its UI texts (report_i18n.json: {sv, en}) as a second gzip block, apart from the usage data."""
    if template is None:
        template = Path(__file__).with_name('report_template.html').read_text(encoding='utf-8')
    if template.count('__USAGE_DATA__') != 1:
        raise ValueError('report template must contain exactly one data placeholder')
    html = template.replace('__USAGE_DATA__', pack(report))
    if '__I18N__' in html:
        html = html.replace('__I18N__', pack(json.loads(Path(__file__).with_name('report_i18n.json').read_text(encoding='utf-8')), ascii_only=False), 1)
    if state is not None:  # right after the charset meta, so it sits within the first bytes of the file
        marker = f'<meta name="tokenatlas-state" content="{state[0]}.{state[1]}">'
        html = html.replace('<meta charset="utf-8">', '<meta charset="utf-8">' + marker, 1)
    return html


def write_report(path, html):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.usage-report-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(html)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != 'nt':
            os.chmod(temporary, 0o600)  # mkstemp already creates 0600; keep it explicit
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
