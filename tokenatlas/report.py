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
from tokenatlas import __version__, progress
from tokenatlas import budget, credits as credit_rates, energy, insights, limits, pricing, prompts, quota_share, usage_profiles
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
    speed=frozenset('standard fast'.split()),
    service_tier=frozenset('standard fast flex'.split()),
)
CONSERVATIVE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._: -]{0,120}')


def public_model_checker(*tables):
    """(provider, model) -> True only for an exact, known public identifier: the model (or one of its aliases) is an entry of the given rate
    tables (packaged prices.json, credits.json) for that provider, after each table's own provider_aliases. A family prefix proves nothing:
    a private suffix such as `claude-sonnet-4-6-acme-internal` is not a public identifier."""
    known = [({(e['provider'], name) for e in t.get('models', ()) for name in (e['model'], *(e.get('aliases') or ()))}, t.get('provider_aliases') or {})
             for t in tables]
    return lambda provider, model: isinstance(provider, str) and isinstance(model, str) and any((aliases.get(provider, provider), model) in names for names, aliases in known)


# Part of every report's identity (report_state), so a cached report built under an older redaction policy is never reused or throttled
# (`open`, `report --if-changed`, `--max-age`). Bump it with ANY change to what a shared report reveals or how it pseudonymizes.
# 1: model names are shown only when exact packaged public identifiers (#133).
# 2: public client labels, local-inference counts and public plan tiers are allowed (#146); work context remains private.
# 3: compact public cloud-reference rate cards are allowed for explicit local what-if comparisons.
# 4: canonical public speed and service-tier values are included in request analytics metadata.
REDACTION_REVISION = 4
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
    key['files_by_harness'] = source_status.get('files_by_harness')
    return key


def report_state(revision, machine, spec, coverage, token=None, texts_hash=None, day=None, quota=None):
    """(identity, data) 32-hex pair. Identity: version, options, database, template and, for private reports, the embedded prompt previews; data: revision token, counter, coverage and, when given, the UTC day the rolling 30-day cost facts were computed for."""
    dump = lambda body: json.dumps(body, sort_keys=True, separators=(',', ':'))
    template = hashlib.sha256(Path(__file__).with_name('report_template.html').read_bytes()
                              + Path(__file__).with_name('report_i18n.json').read_bytes()
                              + Path(__file__).with_name('efficiency.js').read_bytes()).hexdigest()
    identity = dump({'format': 2, 'redaction': REDACTION_REVISION, 'version': __version__, 'spec': spec, 'machine': machine,
                     **({'prompt_texts': texts_hash} if texts_hash else {})})
    data = dump({'token': token, 'revision': int(revision), 'coverage': coverage, **({'insights_day': day} if day else {}), **({'quota': quota} if quota else {})})
    return tuple(hashlib.sha256(text.encode()).hexdigest()[:32] for text in (identity + template, data))


def _usage_code(kind, value):
    if not value:
        return None
    digest = hashlib.sha256(str(value).encode('utf-8', 'replace')).hexdigest()
    return f'{kind} {int(digest[:6], 16) % 1000 + 1:03d}'


def _usage_payload(records, rows, assigned, shown, prompt_texts, prompt_context, redact, profile, table):
    """Filter-following metadata for the work and client cards.

    Context and prompt text are already limited by prompt_store to its global
    top-k entries. This function only associates that retained material with
    report rows; it never reads source logs or extracts new text.
    """
    profile = profile or {}
    configured = profile.get('local_providers') or []
    clients = [usage_profiles.client_key(r.get('harness'), r.get('origin')) for r in records]
    local_rows = [i for i, r in enumerate(records)
                  if usage_profiles.is_local_provider(r.get('provider'), configured)
                  or pricing.is_local_provider(r.get('provider'), table)]
    cloud_references = pricing.reference_rate_card(table)
    if redact:
        public = public_model_checker(pricing.load_prices(), credit_rates.packaged())
        cloud_references = [r for r in cloud_references if r['provider'] in PUBLIC_NAMES['provider'] and public(r['provider'], r['model'])]
    safe_plan = lambda value, kind: value if not redact or value in usage_profiles.KNOWN_PLAN_NAMES else f'{kind} plan'
    plans, seen = [], set()
    for i, record in enumerate(records):
        quota = record.get('quota') or {}
        raw = quota.get('plan_type') if record.get('harness') == 'codex' else None
        if not raw:
            continue
        plan = safe_plan(str(raw), 'Codex')
        key = ('codex', plan, 'snapshot')
        if key not in seen:
            plans.append(dict(harness='codex', plan=plan, source='snapshot', rows=[]))
            seen.add(key)
        next(x for x in plans if (x['harness'], x['plan'], x['source']) == key)['rows'].append(i)
    manual = (profile.get('plans') or {}).get('claude')
    if manual:
        plans.append(dict(harness='claude', plan=safe_plan(manual, 'Claude'), source='manual',
                          rows=[i for i, r in enumerate(records) if r.get('harness') == 'claude']))

    covered = {h: {i for p in plans if p['harness'] == h for i in p['rows']} for h in ('claude', 'codex')}
    for harness in ('claude', 'codex'):
        missing = [i for i, r in enumerate(records) if r.get('harness') == harness and i not in covered[harness]]
        if missing:
            plans.append(dict(harness=harness, plan='unknown', source='unknown', rows=missing))

    groups = {}
    for i, (record, row, found) in enumerate(zip(records, rows, assigned)):
        key = tuple(found[:3]) if found else None
        context = (prompt_context or {}).get(key) if key else None
        context = context if isinstance(context, dict) else {}
        raw_repo, raw_branch = context.get('repository'), context.get('branch')
        repo = raw_repo if not redact else _usage_code('repository', raw_repo)
        branch = raw_branch if not redact else _usage_code('branch', raw_branch)
        project = row.get('project_label') or UNKNOWN_PROJECT
        group_key = (project, repo or UNKNOWN_PROJECT, branch or UNKNOWN_PROJECT)
        group = groups.setdefault(group_key, dict(project=project, repository=repo, branch=branch, rows=[], turns=[], unknown=0))
        group['rows'].append(i)
        turn_key = key
        if key is None:
            group['unknown'] += 1
            continue
        turn_map = group.setdefault('_turn_map', {})
        if turn_key not in turn_map:
            text = (prompt_texts or {}).get(key) if key else None
            raw_title = context.get('title')
            title = raw_title or text or branch or raw_branch
            title = _usage_code('title', title) if redact else title
            # Keep a retained initiating prompt alongside a distinct stored
            # session title. Both values already come from the prompt store's
            # top-k boundary; this does not read source transcripts.
            prompt = text if text and raw_title and text != raw_title else None
            turn_map[turn_key] = dict(rows=[], title=title, prompt=prompt)
            group['turns'].append(turn_map[turn_key])
        turn_map[turn_key]['rows'].append(i)
    for group in groups.values():
        group.pop('_turn_map', None)
    return dict(source_keys=clients, local_rows=local_rows, cloud_references=cloud_references, plans=plans, groups=list(groups.values()))


def build_report(records, source_status, timezone_name='Europe/Stockholm', redact=True, prompt_texts=None, table=None, lang='auto',
                 prompt_context=None, prompt_inputs=None, now=None, credit_table=None, demo=False, limit_hits=None, universe=None, quota=True, all_hits=None, quota_events=None, claude_quota=None, budgets=None, assigned=None, whole_assigned=None, profile=None):
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
    assignment_by_row = None if assigned is None else {id(r): a for r, a in zip(records, assigned)}
    records = sorted(records, key=lambda r: (r['ts'], r['harness'], r['id']))
    if assignment_by_row is not None:assigned = [assignment_by_row[id(r)] for r in records]
    del assignment_by_row
    is_public_model = public_model_checker(pricing.load_prices(), credit_rates.packaged()) if redact else None  # the packaged tables, never a caller-supplied one: they decide what is public
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
            public = record.get('provider') in PUBLIC_NAMES['provider'] and is_public_model(record.get('provider'), value)
        elif record is None:
            public = CONSERVATIVE_NAME.fullmatch(str(value))
        else:
            public = isinstance(value, str) and value in PUBLIC_NAMES[kind]
        return value if public else alias(kind, str(value))
    assigned = prompts.assign_prompts(records) if assigned is None else assigned
    whole_assigned_arg = whole_assigned
    whole, whole_assigned = records, assigned  # the history quota shares are computed over: account-wide counters need every request
    if universe is not None:  # a filtered report: the cards use the whole history's assignment, as the limit hits do
        whole, whole_assigned = universe, whole_assigned_arg if whole_assigned_arg is not None else prompts.assign_prompts(universe)
        full = {prompts.ident(r): a for r, a in zip(universe, whole_assigned)}
        assigned = [full.get(prompts.ident(r), a) for r, a in zip(records, assigned)]
    with progress.step('Build report rows'):
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
    coverage['files_by_harness'] = source_status.get('files_by_harness') or {}  # counts only, never paths (#151)
    coverage['ranges'] = []
    for harness in sorted({r['harness'] for r in rows}):
        group = [r for r in rows if r['harness'] == harness]
        coverage['ranges'].append(dict(harness=harness, observations=len(group),
                                       first_event=group[0]['ts'], last_event=group[-1]['ts']))
    now = now or datetime.now(timezone.utc)
    display = lambda provider, model: metadata('model', model, {'provider': provider})
    memo = {'assigned': {id(r): a for r, a in zip(whole, whole_assigned)}}
    cost_of = insights.memo_cost(table, memo)
    # one captured `now` is the exclusive end of the 30-day window: later-dated observations are not 'the last 30 days'
    with progress.step('Quota shares and cost insights'):
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
    usage = _usage_payload(records, rows, assigned, shown, prompt_texts, prompt_context, redact, profile, table)
    # Shared reports deliberately omit work context; the client summary uses
    # fixed labels and aggregate row associations only.
    if redact:
        usage['groups'] = []
    request_meta_dict, request_meta_idx = {'speed': [], 'service_tier': []}, {'speed': [], 'service_tier': []}
    request_meta_lookup = {'speed': {}, 'service_tier': {}}
    for record in records:
        tariff = record.get('tariff') if isinstance(record.get('tariff'), dict) else {}
        for field in ('speed', 'service_tier'):
            value = tariff.get(field)
            if not isinstance(value, str) or not value:
                request_meta_idx[field].append(None)
                continue
            if redact and value not in PUBLIC_NAMES[field]:
                value = None
            if value is None:
                request_meta_idx[field].append(None)
                continue
            index = request_meta_lookup[field].get(value)
            if index is None:
                request_meta_dict[field].append(value)
                index = len(request_meta_dict[field]) - 1
                request_meta_lookup[field][value] = index
            request_meta_idx[field].append(index)

    # This is the earliest retained usage observation assigned to a prompt, not necessarily user-input time.
    observed_starts = {}
    observed_start_instants = {}
    for record, found in zip(records, assigned):
        key = tuple(found[:3]) if found else None
        ordinal = shown.get(key) if key else None
        if ordinal is None:
            continue
        instant = datetime.fromisoformat(record['ts'])
        if ordinal not in observed_start_instants or instant < observed_start_instants[ordinal]:
            observed_start_instants[ordinal] = instant
            observed_starts[str(ordinal)] = instant.astimezone(zone).date().isoformat()

    activity_dimensions = ('shell', 'edits', 'web', 'subagents')
    retained_contexts = [
        context for key, context in (prompt_context or {}).items()
        if not redact and context and tuple(key) in shown
    ]
    turns_with_activity = sum(
        1 for context in retained_contexts
        if isinstance(context.get('activity'), dict)
        and any(isinstance(context['activity'].get(field), int)
                and not isinstance(context['activity'].get(field), bool)
                for field in activity_dimensions)
    )
    analytics_metadata = dict(
        request_meta=dict(dict=request_meta_dict, idx=request_meta_idx),
        efficiency=dict(
            auto_review=[r.get('model') == 'codex-auto-review' for r in records],
            rolled_up=[bool(a and a[3] == 'rolled_up') for a in assigned],
        ),
        observed_turn_start_dates=observed_starts,
        activity_coverage=dict(
            source='opt_in_top_k_turn_context', retained_context_turns=len(retained_contexts),
            turns_with_activity=turns_with_activity, dimensions=list(activity_dimensions),
            full_tool_events=False, skills=False,
        ),
    )
    report = dict(version=2, generated_at=now.isoformat(),
                  timezone=timezone_name, lang=lang, privacy='redacted' if redact else 'local',
                  prices_retrieved=table.get('retrieved_on'),
                  columns=encode_columns(rows), coverage=coverage, analytics_metadata=analytics_metadata,
                  energy=dict(per_1k=energy.PER_1K, uncertainty=energy.UNCERTAINTY, tier_multipliers=energy.TIERS, multipliers=weights), insights=dict(days=INSIGHT_DAYS, big_turn=insights.BIG_TURN, windows=windows), usage=usage)
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
    with progress.step('Limit hits and quota windows'):
        if snapshots or getattr(snapshots, 'events', ()):
            report.update(_quota_payload(shares or {}, quota_share.windows(snapshots, last=4, records=whole, cost_of=cost_of, hits=limit_hits if all_hits is None else all_hits), shown, metadata, lambda name: alias('limit', name) if redact else name))  # a limit id is pseudonymized whatever it looks like
    with progress.step('Automatic budgets and calibration'):
        if not redact:  # a shared report has no calibrated share, manual or automatic: with the turn costs it would give the budget (the plan size) away
            auto, _ = budget.auto_budgets(whole, all_hits if all_hits is not None else limit_hits, snapshots, table)  # computed here from the history, never stored (#116)
            merged = budget.combine({(b['harness'], b['minutes'], b.get('plan')): b for b in budgets or ()}, auto)
            if merged:
                report.update(_calibration_payload(budget.public(merged), whole, whole_assigned, cost_of, report.get('quota_shares', {}), shown, table))
    if demo:
        report['demo'] = True
    if shown:  # every request of a card's turn, in the whole history: the page labels a share as a whole-turn figure when the selection holds fewer
        sizes = {}
        for found in whole_assigned:
            if found and tuple(found[:3]) in shown:
                sizes[shown[tuple(found[:3])]] = sizes.get(shown[tuple(found[:3])], 0) + 1
        report['prompt_requests'] = sizes
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
            low, up = quota_share.bounds(share) or (None, None)
            found[shown[tuple(turn)]] = dict(harness=metadata('harness', share['window_key'][0], {'harness': share['window_key'][0]}), minutes=share['window_key'][2], label=share['label'], percent=quota_share.value(share),
                                             lower=low, upper=up, shared_with=obs and obs['shared_with'])
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
        if ordinal in shares and shares[ordinal]['label'] in ('observed', 'estimate', 'range'):
            continue
        item = budget.share(derived, key[0], *costs[key]) if key in costs else None
        if item:
            shares[ordinal] = dict(harness=key[0], minutes=item['window_minutes'], label=item['label'], percent=item['exact_percent'], shared_with=None,
                                   date=item['calibration']['date'], lower_bound=item['lower_bound'], **({'source': item['calibration']['source'], 'hits': (item['calibration'].get('by_source') or {}).get('limit_hit', 0), 'readings': (item['calibration'].get('by_source') or {}).get('statusline', 0), 'unfit': bool(item.get('unfit'))} if item['label'] == 'auto-calibrated' else {}))
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
    if '__EFFICIENCY_JS__' in html:
        html = html.replace('__EFFICIENCY_JS__', Path(__file__).with_name('efficiency.js').read_text(encoding='utf-8'), 1)
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
