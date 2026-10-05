"""python -m tokenatlas: local refresh, report, and doctor."""
import argparse
import json
import os
import sqlite3
import re
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tokenatlas import __version__
from tokenatlas import statusline
from tokenatlas.terminal import terminal_safe
from tokenatlas.resume import resume_command,shell_command as _shell_command
# The heavy modules (history, report, insights, pricing, why, ...) are imported where they are used, so `tokenatlas statusline`, which Claude Code
# runs on every status update, starts without them.

DEFAULT_TIMEZONE='Europe/Stockholm'
FILTERS=('start','end','harness','project','session','turn','model','effort','provider','agent')


def aggregate(results):
    """Fold per-root refresh results: summed counters, worst status, and the per-root list."""
    order=('ok','partial','missing')
    total={k:sum(r[k] for r in results) for k,v in results[0].items() if type(v) is int}
    return dict(harness=results[0]['harness'],status=max((r['status'] for r in results),key=lambda s:order.index(s) if s in order else len(order)),
                last_attempt=max(r['last_attempt'] for r in results),errors=[e for r in results for e in r['errors']],
                coverage_complete=False,roots=results,**total)


def parse_duration(text):
    """'90s', '30m', '1h' or '2d' to seconds."""
    found=re.fullmatch(r'(\d+)([smhd])',text)
    if not found:raise ValueError(f'invalid duration {text!r}; use e.g. 90s, 30m, 1h or 2d')
    return int(found[1])*{'s':1,'m':60,'h':3600,'d':86400}[found[2]]


def _open_in_browser(path):
    where=f'could not open a browser; the report is at {path}'
    try:opened=webbrowser.open(Path(path).resolve().as_uri())
    except webbrowser.Error as exc:raise ValueError(f'{where} ({exc})') from exc
    if not opened:raise ValueError(where)


def _ago(seconds):
    seconds=max(0,int(seconds))
    for unit,size in (('day',86400),('h',3600),('min',60)):
        if seconds>=size:
            n=seconds//size
            return f'{n} {unit}'+('s' if unit=='day' and n!=1 else '')
    return f'{seconds} s'


def _show(path):
    """Open an existing report as it is: the history is not opened, nothing is refreshed or rebuilt."""
    if not path.is_file():raise ValueError(f'no report at {path.absolute()}; build one with: tokenatlas open')
    age=max(0.0,time.time()-path.stat().st_mtime)  # a file dated in the future reads as just built
    _open_in_browser(path)
    print(f'Report: {path.absolute()} (built {_ago(age)} ago; tokenatlas open refreshes it)',file=sys.stderr)
    print(json.dumps({'html':str(path.absolute()),'shown':True,'age_seconds':int(age)},indent=2,sort_keys=True))
    return 0


def _output_path(path,db):
    """Expanded HTML path; refuses the history database itself, also through a hard or symbolic link."""
    path,db=Path(path).expanduser(),Path(db).expanduser()
    if path.resolve()==db.resolve() or (path.exists() and db.exists() and os.path.samefile(path,db)):
        raise ValueError('HTML output must not replace the history database')
    return path


def _spec(privacy,timezone,granularity,filters,lang='auto'):
    return {'privacy':privacy,'timezone':timezone,'granularity':granularity,'lang':lang,'filters':{k:filters.get(k) for k in FILTERS}}


def _budgets(db,table,records_fn):
    """The derived quota budgets (budget.py) for a report: [] without any, which leaves the report and its reuse state exactly as before."""
    from tokenatlas import budget
    return budget.public(budget.load_derived(budget.path_for(db),table=table,records_fn=records_fn))


def _present(root):
    """False only when the root is definitely not there; an unreadable or wrong-type one is present and fails in refresh."""
    try:os.stat(root)
    except (FileNotFoundError,NotADirectoryError):return False
    except OSError:return True
    return True


def _with_problems(entry,problems):
    """Add Cowork traversal errors to a Claude refresh result and make it at least partial."""
    order=('ok','partial','missing','error')
    rank=lambda s:order.index(s) if s in order else len(order)
    return dict(entry,errors=[*entry.get('errors',[]),*problems],status=max(entry['status'],'partial',key=rank)) if problems else entry


def refresh_all(history):
    """Refresh every harness from its default roots; absent ones are reported, an OSError only fails its own harness. Then rewrites the statusline cache."""
    from tokenatlas import why
    roots={h:why.harness_root(h)[0] for h in ('claude','codex','pi','opencode')}
    order=('ok','partial','missing','error')
    rank=lambda s:order.index(s) if s in order else len(order)
    entries,worst=[],'ok'
    for name,root in roots.items():
        try:
            # Claude: main root, then each Cowork transcript root (macOS; absent elsewhere and simply skipped).
            cowork,problems=why.cowork_scan() if name=='claude' else ([],[])
            found=[r for r in [root,*cowork] if _present(r)]
            if not found and not problems:
                entries.append({'harness':name,'status':'absent'});continue
            results=[history.refresh(name,r) for r in found]
            entry=(results[0] if len(results)==1 else aggregate(results)) if results else {'harness':name,'status':'ok','errors':[]}
            entry=_with_problems(entry,problems)
        except OSError as exc:
            entry={'harness':name,'status':'error','errors':[f'{type(exc).__name__}: {exc}']}
        entries.append(entry)
        worst=max(worst,entry['status'],key=rank)
    statusline.refresh_cache(history)
    return {'status':worst,'harnesses':entries}


def _plural(n,one,many=None):
    return f"{n:,} {one if n==1 else many or one+'s'}"


def context_lines(ctx,text=None):
    """Up to four indented lines of a stored turn context, unknown parts omitted: title (else the initiating input, else the stored
    preview), place, counts and outcomes, final message."""
    inputs,act,out=ctx.get('inputs') or {},ctx.get('activity') or {},ctx.get('outcomes') or {}
    lines=[ctx.get('title') or inputs.get('first') or text]
    repo=(ctx.get('repository') or '').rstrip('/').replace(':','/').rsplit('/',1)[-1].removesuffix('.git')
    place='/'.join(x for x in (repo,ctx.get('branch')) if x)
    cwd=Path(ctx['cwd']).name if ctx.get('cwd') else ''
    lines.append(' · '.join(x for x in (place,cwd) if x))
    parts=[_plural(inputs['count'],'input')] if isinstance(inputs.get('count'),int) and inputs['count'] else []
    parts+=[f"{act[k]:,} {k}" if k in ('shell','web') else _plural(act[k],*u) for k,u in (('shell',()),('edits',('edit',)),('web',()),('subagents',('subagent',))) if isinstance(act.get(k),int) and act[k]]
    if out.get('prs'):parts.append('PRs '+', '.join(out['prs']))
    if out.get('commits'):parts.append('commits: '+out['commits'][0])
    lines.append(' · '.join(parts))
    if ctx.get('final'):lines.append('final: '+(ctx['final'] if len(ctx['final'])<=120 else ctx['final'][:119].rstrip()+'…'))
    return ['    '+x for x in lines if x]


def render_top(result,texts=None,contexts=None):
    """Compact table of ranked turns; cost is list-price, '≥' when some requests could not be priced. Stored context or preview goes on indented lines."""
    from tokenatlas import credits as credit_rates, limits, quota_share
    zone=ZoneInfo(DEFAULT_TIMEZONE)
    rows=[('#','when','harness','project','models','req','sub','Mtok','cost')]  # a Codex/OpenAI turn with a credit rate adds its credit equivalent to the cost cell; the resume command goes on its own line
    for i,p in enumerate(result['prompts'],1):
        cost='n/a' if p['cost'] is None else ('' if p['cost_complete'] else '≥')+f"${p['cost']:.2f}"
        if p.get('credits') is not None:cost+=f" ({'≥' if p['credits_lower_bound'] else '≈'} {credit_rates.fmt(p['credits'])} credits)"
        when=datetime.fromisoformat(p['first_ts']).astimezone(zone).strftime('%Y-%m-%d %H:%M')
        rows.append((str(i),when,p['harness'],p['project_label'] or '-',','.join(p['models']) or '-',str(p['requests']),
                     str(p['subagents']),f"{p['total_tokens']/1e6:.2f}",cost))
    widths=[max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines=['  '.join(c.ljust(w) for c,w in zip(r,widths)).rstrip() for r in rows]
    shown=[]
    for line,p in zip(lines[1:],result['prompts']):
        key=(p['harness'],p['session'],p['turn_id']);shown.append(line+(' · interrupted' if p.get('interrupted') else '')+('  ['+limits.badge(p['limit_hit'])+']' if p.get('limit_hit') else '')+(' · '+quota_share.line(p['quota_share'],p['harness']) if p.get('quota_share') else ''))
        text=(texts or {}).get(key)
        if (contexts or {}).get(key):shown+=context_lines(contexts[key],text)
        elif text:shown.append('    '+text)
        # validated and quoted (resume.resume_command); the stored context's directory when the turn's own row has none
        command=p.get('resume') or resume_command(p['harness'],p['session'],((contexts or {}).get(key) or {}).get('cwd'))
        if command:shown.append('    resume: '+command)
    lines=[lines[0],*shown]
    if len(result['prompts'])<result['total_prompts']:lines.append(f"showing {len(result['prompts'])} of {result['total_prompts']} turns")
    return '\n'.join(lines)


def _state_base():
    return Path(os.environ.get('XDG_STATE_HOME',Path.home()/'.local/state'))


def _reopen(path,db,always=False):
    """The command that reopens exactly this report: `tokenatlas open` (with --db/--html when they are not the defaults). For a report
    written by `report`, only when it is open's own default file (open reuses it); otherwise None: open would rebuild it with other options."""
    home=_state_base()/'tokenatlas'
    default_db_path,default_html=home/'history.sqlite3',Path(db).expanduser().absolute().parent/'report.html'
    if not always and Path(path).absolute()!=default_html:return None
    cmd=['tokenatlas']
    if Path(db).expanduser().absolute()!=default_db_path.absolute():cmd+=['--db',str(Path(db).expanduser().absolute())]
    cmd.append('open')
    if Path(path).absolute()!=default_html:cmd+=['--html',str(Path(path).absolute())]
    return _shell_command(cmd)


def default_db():
    """Default history path; one-time move of the pre-rename agentmon directory (never used with --db)."""
    base=_state_base()
    new,old=base/'tokenatlas',base/'agentmon'
    if old.is_dir():
        if new.exists():
            print(f'warning: {old} left in place; using {new}',file=sys.stderr)
        else:
            os.rename(old,new)
            print(f'moved history from {old} to {new}',file=sys.stderr)
    return new/'history.sqlite3'


def _visible(history,db,records=None):
    """(texts, contexts) a private report may embed (the current global top k); the history is read only when a store exists."""
    from tokenatlas import pricing, prompt_store
    store=prompt_store.store_path(db)
    if not os.path.lexists(store):return {},{}
    return prompt_store.visible_all(store,history.records() if records is None else records,pricing.load_prices())


def _hits(history,records,events=None):
    """Limit hits over the whole history's records (a report's filters never shrink the window a hit is explained from)."""
    from tokenatlas import limits, pricing
    return limits.limit_hits(records,history.limit_events() if events is None else events,pricing.load_prices())  # no shortcut: a full window with no reached type is a hit too


def _auto_budgets(history,db,table,records=None,hits=None,snapshots=None,keep=None):
    """(automatic budgets, skipped counts) from the whole history: limit hits and Claude statusline readings (budget.auto_budgets, #116). Computed, never stored."""
    from tokenatlas import budget, quota_share
    records=history.records() if records is None else records
    if hits is None:hits=_hits(history,records)
    if snapshots is None:snapshots=quota_share.snapshots_from_records(records,claude=_claude_quota(db))
    return budget.auto_budgets(records,hits,snapshots,table,*(() if keep is None else (keep,)))


def _over_cap(history,db,auto,manual):
    """Turns shown as unknown because an automatic budget does not fit them, with the precedence of `top` (observed or estimated share, then manual, then automatic)."""
    from tokenatlas import budget, pricing, quota_share
    records,table=history.records(),pricing.load_prices()
    shares=quota_share.compute(records,table,claude=_claude_quota(db))[1]
    return budget.over_cap(auto,manual,records,table,{k for k,v in shares.items() if v['label'] in ('observed','estimate')})


def _events(events):
    """Codex quota-only observations (zero-token, status 'event'): window readings that are no requests; they only add to a window's peak and hit."""
    return [e for e in events if (e.get('quota') or {}).get('status')=='event']


def _claude_quota(db):
    """The Claude quota snapshot file next to the history database when it exists (recorded by default by `tokenatlas statusline`), else None."""
    path=statusline.quota_path(db)
    return path if path.is_file() else None


def _quota_token(db):
    from tokenatlas import quota_share
    return quota_share.claude_token(statusline.quota_path(db))


def _quota_status(db):
    """doctor: Claude quota recording. `recording_configured` is true when the Claude Code statusline command is tokenatlas's and recording is not opted out
    (--no-record-quota or TOKENATLAS_NO_QUOTA); false when it is not tokenatlas's or recording is disabled; "unknown" when no settings file can be read; `snapshots_file` is present/absent, with the snapshot count,
    unreadable lines, the last snapshot and its age."""
    from tokenatlas import quota_share
    path=statusline.quota_path(db)
    state=statusline.recording_state()
    configured=None if state=='unknown' else state=='enabled'
    problem=statusline.quota_file_problem(path)
    out=dict(recording_configured='unknown' if configured is None else configured,statusline_state=state,settings_file=str(statusline.settings_path()),path=str(path),
             snapshots_file='present' if path.is_file() else 'absent',file_problem=problem,snapshots=0,malformed=0,last_snapshot=None,last_snapshot_age=None,
             note='snapshots exist only while a Claude Code UI session is open; claude -p, SDK runs and claude.ai chat are not recorded')
    if path.is_file():
        rows,bad=quota_share.read_claude(path)
        last=max((r[0] for r in rows),default=None)
        out.update(snapshots=len(rows),malformed=bad,last_snapshot=None if last is None else last.isoformat(),
                   last_snapshot_age=None if last is None else max(0,int((datetime.now(ZoneInfo('UTC'))-last).total_seconds())))
    if problem:out['hint']=f'recording is skipped: the snapshot file {problem}'
    elif state=='foreign':out['warning']='the Claude Code statusline is not tokenatlas\'s, so no quota snapshots can be recorded; see tokenatlas statusline --setup'
    elif state=='disabled':out['warning']='quota recording is disabled (--no-record-quota or TOKENATLAS_NO_QUOTA); Claude turns get no limit share without snapshots'
    elif state=='unknown':out['hint']='Claude Code settings could not be read; recording is on by default when the statusline is tokenatlas\'s (tokenatlas statusline --setup)'
    return out


def _counts(contexts):
    """{key: input count}: input counts of private contexts (never given to a shared report); None when there are none."""
    found={k:c['inputs']['count'] for k,c in contexts.items() if isinstance(c.get('inputs'),dict) and isinstance(c['inputs'].get('count'),int)}
    return found or None


def _statusline_dispatch(argv):
    """Exit code when argv is `[--db PATH] statusline ...`, else None; decided before the full parser (and the heavy imports) is built."""
    db,rest=None,list(argv)
    if rest[:1]==['--db'] and len(rest)>1:db,rest=Path(rest[1]),rest[2:]
    elif rest[:1] and rest[0].startswith('--db='):db,rest=Path(rest[0][5:]),rest[1:]
    return statusline.run(rest[1:],db) if rest[:1]==['statusline'] else None


def main(argv=None):
    # Windows pipes default to a legacy code page without '≥' or '→'; replace such characters rather than crash.
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(errors='replace')
    done=_statusline_dispatch(sys.argv[1:] if argv is None else argv)
    if done is not None:return done
    from tokenatlas import insights, limits, pricing, prompt_store, prompts, quota_share, sessions, why
    from tokenatlas.history import History, summarize
    from tokenatlas.report import build_report, coverage_key, read_report_state, render_report, report_state, write_report
    if Path(sys.argv[0]).name.lower() in ('energy-monitor','energy-monitor.exe','energy-monitor-script.py'):
        print('energy-monitor is deprecated; use tokenatlas',file=sys.stderr)
    parser=argparse.ArgumentParser(prog='tokenatlas',description='Local observed token history; no network or LLM calls.')
    parser.add_argument('--version',action='version',version=f'%(prog)s {__version__}')
    parser.add_argument('--db',type=Path,help='History database; default $XDG_STATE_HOME/tokenatlas/history.sqlite3.')
    commands=parser.add_subparsers(dest='command',required=True)
    refresh=commands.add_parser('refresh',help='Import changed files; preserve retained observations.')
    which=refresh.add_mutually_exclusive_group(required=True)
    which.add_argument('--harness',choices=('claude','codex','pi','opencode'))
    which.add_argument('--all',action='store_true',help='Refresh every harness from its default roots; missing ones are reported as absent.')
    refresh.add_argument('--root',type=Path,help='Override the harness session directory (with --harness).')
    opener=commands.add_parser('open',help='Refresh, build the report (private by default) and open it in the browser.')
    opener.add_argument('--html',type=Path,help=f'Report path; default: report.html next to the database ({_state_base()/"tokenatlas"/"report.html"}).')  # no migration side effect
    opener.add_argument('--shared',action='store_true',help='Pseudonymize the report instead of keeping project labels.')
    opener.add_argument('--lang',choices=('auto','sv','en'),default='auto',help='Report language; auto follows the browser (Swedish for sv, otherwise English).')
    opener.add_argument('--no-refresh',action='store_true',help='Use the saved history as it is.')
    shower=commands.add_parser('show',help='Open the latest report in the browser at once: no refresh, no rebuild (tokenatlas open refreshes it).')
    shower.add_argument('--html',type=Path,help=f'Report path; default: report.html next to the database ({_state_base()/"tokenatlas"/"report.html"}).')
    snapshot=commands.add_parser('snapshot',help='Write a consistent private copy of the history database.')
    snapshot.add_argument('out',type=Path)
    importer=commands.add_parser('import',help="Merge another machine's snapshot into this database.")
    importer.add_argument('snapshot',type=Path)
    importer.add_argument('--label',required=True,help='Name for the source machine, e.g. pi:huginmunin.local.')
    report=commands.add_parser('report',help='Report saved observations without rereading source logs.')
    report.add_argument('--start',help='Inclusive ISO timestamp; offset required.')
    report.add_argument('--end',help='Exclusive ISO timestamp; offset required.')
    report.add_argument('--granularity',choices=('day','hour','minute'),default='day')
    report.add_argument('--timezone',default=DEFAULT_TIMEZONE)
    report.add_argument('--harness',choices=('claude','codex','pi','opencode'))
    report.add_argument('--project',help='Exact full project identity, not basename.')
    report.add_argument('--session')
    report.add_argument('--turn')
    report.add_argument('--model',help='Exact model ID.')
    report.add_argument('--effort')
    report.add_argument('--provider')
    report.add_argument('--agent')
    report.add_argument('--html',type=Path,help='Write a standalone interactive offline HTML report.')
    report.add_argument('--private',action='store_true',help='Keep project labels and session IDs in HTML; default HTML uses pseudonyms.')
    report.add_argument('--if-changed',action='store_true',help='With --html: skip when the history revision matches the existing report.')
    report.add_argument('--max-age',help='With --html: skip when the existing report is younger than this (90s, 30m, 1h, 2d).')
    report.add_argument('--lang',choices=('auto','sv','en'),default='auto',help='With --html: report language; auto follows the browser (Swedish for sv, otherwise English).')
    report.add_argument('--records',action='store_true',help='Include per-observation counters and source-file references. Reports contain private local paths.')
    for name,text in (('session','Show the session tree, per-model totals and outcomes for one root session.'),
                      ('rate','List threads of a session, or record an outcome rating for a unit.')):
        sub=commands.add_parser(name,help=text)
        sub.add_argument('id',help='Root session id, or harness:id when ambiguous.')
        sub.add_argument('--outcomes',type=Path,help='Outcomes JSONL; default outcomes.jsonl next to the database.')
        sub.add_argument('--prices',type=Path,help='Override the price table.')
        if name=='session':
            sub.add_argument('--json',action='store_true')
            sub.add_argument('--no-infer',action='store_true',help='Do not link headless children by time and cwd.')
        else:
            sub.add_argument('--unit');sub.add_argument('--thread',action='append',default=[])
            sub.add_argument('--outcome',choices=sessions.OUTCOMES);sub.add_argument('--note',default='')
    top=commands.add_parser('top',help='Rank the most expensive turns: an initiating input plus everything it caused, including follow-up inputs and subagent work.')
    top.add_argument('-n','--limit',type=int,default=10)
    top.add_argument('--by',choices=('cost','tokens'),default='cost')
    top.add_argument('--start',help='Inclusive ISO timestamp; offset required.')
    top.add_argument('--end',help='Exclusive ISO timestamp; offset required.')
    top.add_argument('--harness',choices=('claude','codex','pi','opencode'))
    top.add_argument('--project',help='Exact full project identity, not basename.')
    top.add_argument('--prices',type=Path,help='Override the price table.')
    top.add_argument('--json',action='store_true')
    top.add_argument('--keep-text',action='store_true',help='Store the text and context of the current global top -n turns in top-prompts.json next to the history (0600).')
    top.add_argument('--forget-text',action='store_true',help='Delete the stored prompt text.')
    top.add_argument('--with-text',action='store_true',help='With --json: include stored text and turn context.')
    facts=commands.add_parser('insights',help='Cost facts: deterministic list-price statements computed from the saved observations (no language model, no interpretation).')
    facts.add_argument('--days',type=int,help='Only the last N days (default: all history).')
    facts.add_argument('--start',help='Inclusive ISO timestamp; offset required.')
    facts.add_argument('--end',help='Exclusive ISO timestamp; offset required.')
    facts.add_argument('--prices',type=Path,help='Override the price table.')
    facts.add_argument('--json',action='store_true')
    overhead=commands.add_parser('overhead',help='Fixed context overhead: floor tokens, instruction and skill sizes.')
    overhead.add_argument('--refresh',action='store_true',help='Rescan the default session roots first.')
    overhead.add_argument('--harness',choices=('claude','codex','pi','opencode'))
    overhead.add_argument('--since',help='Inclusive ISO timestamp of the session start.')
    overhead.add_argument('--json',action='store_true')
    collect=commands.add_parser('collect',help='One scheduled run under a lock: refresh, top text if opted in, report, remote sync, report again.')
    collect.add_argument('--remote',action='append',default=[],metavar='TAG:HOST',help='Remote machine to sync (repeatable); else REMOTE_HOSTS_OVERRIDE or the remote-hosts file in the state directory.')
    collect.add_argument('--remote-sync',type=Path,help='Remote sync script; default the packaged one, or $TOKENATLAS_REMOTE_SYNC.')
    collect.add_argument('--sync-timeout',type=int,default=600,help='Seconds for the whole remote sync before its process group is killed (default 600).')
    collect.add_argument('--no-report',action='store_true',help='Do not build the report.')
    collect.add_argument('--lang',choices=('auto','sv','en'),default='auto',help='Report language.')
    status=commands.add_parser('statusline',help='Claude Code statusline: one line from the stdin payload and the totals cache refresh writes (no network); --setup prints the settings snippet; quota readings are recorded by default (--no-record-quota or TOKENATLAS_NO_QUOTA=1 turns it off).')
    status.add_argument('--setup',action='store_true')
    status.add_argument('--record-quota',action='store_true')
    status.add_argument('--no-record-quota',action='store_true')
    quota=commands.add_parser('quota',help='Your plan size: a manual budget or calibration readings turn list price into a share of a limit (kept in quota-budget.json next to the history, 0600; nothing leaves the machine).').add_subparsers(dest='quota',required=True)
    cal=quota.add_parser('calibrate',help='Store a reading copied from /usage (Claude Code) or the Codex limits display, with the list price tokenatlas saw in that window.')
    cal.add_argument('--harness',choices=('claude','codex'),required=True)
    cal.add_argument('--window',choices=('5h','7d'),required=True)
    cal.add_argument('--used',required=True,help='Used percentage as shown, e.g. 52%%.')
    cal.add_argument('--resets',help='When the window resets, as shown, e.g. "2026-10-09 21:00" (machine-local time unless an offset is given). Claude: anchors the window [resets - window, reading]; omitted, the window is the trailing 5h/7d and the reading is marked approximate. Codex: the window is always the trailing 5h/7d (Codex windows roll); --resets is only recorded.')
    cal.add_argument('--plan',help='Codex only: the plan this reading is for (e.g. pro, plus, team); default the plan seen in the window or on the latest Codex observation, and an error when several plans were active in the window. Readings and budgets are kept per plan, so another account or plan never mixes in.')
    cal.add_argument('--at',help='When you read it (ISO; default now).')
    setb=quota.add_parser('set',help='Store a budget you know: the list-price size of a window in USD.')
    setb.add_argument('--harness',choices=('claude','codex'),required=True)
    setb.add_argument('--window',choices=('5h','7d'),required=True)
    setb.add_argument('--plan',help='Codex only: the plan this budget is for; without it the budget fits any Codex plan that has none of its own.')
    setb.add_argument('--budget-usd',type=float,required=True)
    show=quota.add_parser('show',help='List the readings and budgets and the budget derived per harness and window.')
    show.add_argument('--json',action='store_true')
    show.add_argument('--keep',type=int,default=8,help='Ignore readings older than this many windows (default 8).')
    forget=quota.add_parser('forget',help='Delete readings and budgets (all, or one harness and/or window).')
    forget.add_argument('--harness',choices=('claude','codex'))
    forget.add_argument('--window',choices=('5h','7d'))
    commands.add_parser('doctor',help='Show source availability, import errors and known coverage limits.')
    args=parser.parse_args(argv)
    if args.command=='show':  # before default_db(): show never touches the history, not even its one-time directory move
        try:return _show(Path(args.html or (args.db.expanduser().parent if args.db else _state_base()/'tokenatlas')/'report.html').expanduser())
        except (OSError,ValueError) as exc:parser.exit(2,f'usage: {terminal_safe(exc)}\n')
    if args.db is None:args.db=default_db()
    try:
        start=end=None
        if args.command=='refresh' and args.all and args.root:raise ValueError('--root cannot be used with --all')
        if args.command=='top' and args.limit<1:raise ValueError('--limit must be at least 1')
        if args.command=='top' and args.keep_text and args.forget_text:raise ValueError('--keep-text and --forget-text cannot be combined')
        if args.command=='insights':
            if args.days is not None and (args.days<1 or args.start or args.end):raise ValueError('--days must be at least 1 and cannot be combined with --start or --end')
        if args.command in ('report','top','insights'):
            if args.command=='report':
                max_age=parse_duration(args.max_age) if args.max_age is not None else None
                if (args.if_changed or max_age is not None) and not args.html:raise ValueError('--if-changed and --max-age need --html')
                ZoneInfo(args.timezone)
            for name in ('start','end'):
                value=getattr(args,name)
                if value:
                    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
                    if parsed.tzinfo is None:
                        raise ValueError(f'--{name} needs a timezone offset')
                    if name=='start':start=parsed
                    else:end=parsed
            if start and end and start>=end:raise ValueError('--start must precede --end')
            if args.command=='insights' and args.days:end=datetime.now(ZoneInfo('UTC'));start=end-timedelta(days=args.days)  # one captured now: the exclusive end
            if args.command=='report' and args.html:_output_path(args.html,args.db)
        if args.command=='open':path=_output_path(args.html or args.db.parent/'report.html',args.db)
        if args.command=='collect':
            from tokenatlas import collect as _collect
            return _collect.run(args)
        if args.command=='overhead':
            from tokenatlas import overhead as _overhead
            return _overhead.run(args)
        if args.command=='quota':
            from tokenatlas import budget,pricing
            if args.quota!='calibrate':
                if args.quota=='show' and args.db.expanduser().is_file():
                    with History(args.db) as history:  # the automatic budgets are computed from the history (#116)
                        history.connection.execute('BEGIN')
                        return budget.run(args,args.db,None,pricing.load_prices,print,lambda keep:_auto_budgets(history,args.db,pricing.load_prices(),keep=keep),lambda auto,manual:_over_cap(history,args.db,auto,manual)) or 0
                return budget.run(args,args.db,None,pricing.load_prices,print) or 0
            if not args.db.expanduser().is_file():raise ValueError('history database does not exist; run refresh first')
            with History(args.db) as history:
                history.connection.execute('BEGIN')
                return budget.run(args,args.db,history.records,pricing.load_prices,print) or 0
        if args.command not in ('refresh','import','open') and not args.db.expanduser().is_file():
            raise ValueError('history database does not exist; run refresh first')
        if args.command=='rate' and (args.unit or args.thread or args.outcome) and not (args.unit and args.thread and args.outcome):
            raise ValueError('rating needs --unit, --thread and --outcome')
        with History(args.db) as history:
            if args.command in ('session','rate'):
                history.connection.execute('BEGIN')
                price,retrieved=sessions.default_pricer(args.prices)
                result=sessions.build_tree(history.records(),args.id,infer=not getattr(args,'no_infer',False),price=price)
                path=args.outcomes or Path(args.db).with_name('outcomes.jsonl')
                rated=sessions.load_outcomes(path,args.id)
                if args.command=='session':
                    eff=sessions.efficiency(result,rated) if rated else None
                    if args.json:
                        result['efficiency']=eff;result['prices_retrieved']=retrieved
                        print(json.dumps(result,indent=2,sort_keys=True))
                    else:print(sessions.render(result,eff,retrieved))
                elif not args.unit:
                    threads=[n for n in sessions.iter_nodes(result['root']) if n is not result['root']]
                    if not threads:
                        print('no rateable threads: the root thread is coordination, not a unit of work',file=sys.stderr)
                    for node in threads:
                        print(f"{sessions.thread_key(node)}  {', '.join(node['models']) or '-'}  {sessions._tokens(node)}"
                              f"  {sessions._money(node['cost'],node['cost_coverage'],node['lower_bound'])}")
                else:
                    keys={sessions.thread_key(n) for n in sessions.iter_nodes(result['root']) if n is not result['root']}
                    for key in args.thread:
                        if key not in keys:raise ValueError(f'unknown thread {key!r}; run rate {args.id} to list threads')
                    line={'v':1,'root_session':result['root']['id'],'unit':args.unit,
                          'threads':[sessions._thread_of_key(k) for k in args.thread],'outcome':args.outcome,
                          'note':args.note,'ts':datetime.now(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%SZ')}
                    sessions.append_outcome(path,line)
                    print(json.dumps(line,sort_keys=True))
                return 0
            if args.command=='open':
                path=_output_path(path,args.db)  # the database may have just been created under a case alias
                if not args.no_refresh:
                    summary=refresh_all(history)
                    print('refresh: '+', '.join(f"{e['harness']} {e['status']}" for e in summary['harnesses']),file=sys.stderr)
                history.connection.execute('BEGIN')
                source_status=history.doctor()
                spec=_spec('redacted' if args.shared else 'local',DEFAULT_TIMEZONE,'day',{},args.lang)
                budgets=_budgets(args.db,pricing.load_prices(),history.records)
                if budgets:spec['budgets']=budgets  # a new calibration is a changed report
                if not args.shared:spec['dest']=str(path.absolute())  # a private report names its file: a moved copy is rebuilt, never reused
                texts,ctx=(None,None) if args.shared else _visible(history,args.db)  # shared reports never read the side file
                state=report_state(history.revision,history.machine,spec,coverage_key(source_status),history.revision_token,prompt_store.texts_hash(texts,ctx),datetime.now(ZoneInfo('UTC')).date().isoformat(),_quota_token(args.db))
                if path.exists() and read_report_state(path)==state:
                    result={'html':str(path.resolve()),'skipped':True,'reason':'unchanged'}
                else:
                    records=history.records();events=history.limit_events();hits_all=_hits(history,records,events)  # one read of the limit events serves the hits and the quota windows
                    payload=build_report(records,source_status,DEFAULT_TIMEZONE,redact=args.shared,prompt_texts=texts,lang=args.lang,prompt_context=ctx,prompt_inputs=None if args.shared else _counts(ctx),limit_hits=hits_all,all_hits=hits_all,quota_events=_events(events),claude_quota=_claude_quota(args.db),budgets=budgets)
                    payload['initial_granularity']='day'
                    if not args.shared:payload.update(saved_at=str(path.absolute()),reopen=_reopen(path,args.db,always=True))  # private only: a shared report never carries a local path
                    write_report(path,render_report(payload,state=state))
                    result={'html':str(path.resolve()),'observations':len(records),'privacy':payload['privacy']}
                _open_in_browser(path)
                print(f'Report: {path.absolute()} (reopen any time with: {_reopen(path,args.db,always=True)})',file=sys.stderr)
            elif args.command=='refresh' and args.all:
                result=refresh_all(history)
            elif args.command=='refresh':
                roots={h:why.harness_root(h)[0] for h in ('claude','codex','pi','opencode')}
                if args.root or args.harness!='claude':
                    result=history.refresh(args.harness,args.root or roots[args.harness])
                else:
                    # Main root, then each Cowork transcript root (macOS; absent elsewhere and simply skipped).
                    cowork,problems=why.cowork_scan()
                    results=[history.refresh('claude',root) for root in [roots['claude'],*cowork]]
                    result=_with_problems(results[0] if len(results)==1 else aggregate(results),problems)
                statusline.refresh_cache(history)
            elif args.command=='top':
                from tokenatlas import budget
                history.connection.execute('BEGIN')
                store=prompt_store.store_path(args.db)
                if args.forget_text:
                    prompt_store.forget(store)
                    print(json.dumps({'forgotten':str(store)}));return 0
                table=pricing.load_prices(args.prices)
                everything=history.records()  # rank over the whole history; the filters only choose which rows contribute
                kept=prompt_store.update(store,everything,table,history.machine,args.limit,args.by,local=history.local_source_paths()) if args.keep_text else None
                filtered=any(x is not None for x in (start,end,args.harness,args.project))
                keep={prompts.ident(r) for r in history.records(start,end,args.harness,args.project)} if filtered else None
                result=prompts.top_prompts(everything,table,args.limit,args.by,keep)
                hits=limits.limit_hits(everything,history.limit_events(),table)  # windows come from the whole history; the CLI filters then pick the hits
                all_hits=hits
                if filtered:hits=limits.scope_hits(hits,history.records(start,end,args.harness,args.project),args.harness,start,end,args.project,universe=everything)
                limits.mark_turns(result['prompts'],hits)
                snaps,shares=quota_share.compute(everything,table,only={(p['harness'],p['session'],p['turn_id']) for p in result['prompts']},claude=_claude_quota(args.db))
                quota_share.mark_turns(result['prompts'],shares)
                derived=budget.load_derived(budget.path_for(args.db),table=table,records_fn=lambda:everything)
                if any((p.get('quota_share') or {}).get('label') not in ('observed','estimate') for p in result['prompts']):  # automatic budgets only matter for a turn without its own share (#116)
                    derived=budget.combine(derived,_auto_budgets(history,args.db,table,everything,all_hits,snaps or None)[0])
                if derived:  # only where there is no observed or estimated share; the turn's whole identified cost, whatever the filters keep
                    memo={};budget.mark_turns(result['prompts'],derived,budget.turn_costs(everything,prompts.assign_prompts(everything),insights.memo_cost(table,memo),table))
                texts,ctx=prompt_store.visible_all(store,everything,table)  # only the global top k: never text or context outside it
                if kept:result['text_store']=kept
                if not args.json:
                    print(render_top(result,texts,ctx))
                    if kept:print(f"kept text for {kept['kept']+kept['added']} prompts in {kept['path']} ({kept['added']} new, {kept['evicted']} evicted)",file=sys.stderr)
                    return 0
                if args.with_text:
                    for p in result['prompts']:
                        key=(p['harness'],p['session'],p['turn_id']);p['text']=texts.get(key);p['context']=ctx.get(key)
            elif args.command=='insights':
                history.connection.execute('BEGIN')
                table=pricing.load_prices(args.prices);everything=history.records();memo={}
                result=insights.cost_facts(everything,table,start,end,hits=limits.limit_hits(everything,history.limit_events(),table),quota=quota_share.turn_shares(everything,quota_share.snapshots_from_records(everything,claude=_claude_quota(args.db)),table,insights.memo_cost(table,memo)),memo=memo)
                if not args.json:
                    print(insights.render_text(result));return 0
            elif args.command=='snapshot':
                result=history.snapshot(args.out)
            elif args.command=='import':
                result=history.import_snapshot(args.snapshot,args.label)
                if result.get('warning'):print(f"usage: warning: {terminal_safe(result['warning'])}",file=sys.stderr)
            elif args.command=='doctor':
                history.connection.execute('BEGIN')
                result=history.doctor()
                # Where each harness is read from now, and why; only the harness variables, never the whole environment.
                result['claude_quota']=_quota_status(args.db)
                result['roots']={h:dict(zip(('path','source'),(str(r),src))) for h in ('claude','codex','pi','opencode') for r,src in [why.harness_root(h)]}
            else:
                history.connection.execute('BEGIN')
                source_status=history.doctor()
                if args.html:
                    path=_output_path(args.html,args.db)
                    spec=_spec('local' if args.private else 'redacted',args.timezone,args.granularity,vars(args),args.lang)
                    budgets=_budgets(args.db,pricing.load_prices(),history.records)
                    if budgets:spec['budgets']=budgets
                    if args.private:spec['dest']=str(path.absolute())  # as in open: a private report names its file
                    texts,ctx=_visible(history,args.db) if args.private else (None,None)
                    state=report_state(history.revision,history.machine,spec,coverage_key(source_status),history.revision_token,prompt_store.texts_hash(texts,ctx),datetime.now(ZoneInfo('UTC')).date().isoformat(),_quota_token(args.db))
                    if path.exists() and (args.if_changed or max_age is not None):
                        found=read_report_state(path)
                        age=time.time()-path.stat().st_mtime
                        # Options (identity) are never throttled: only a data change on an otherwise identical report waits.
                        reason=('unchanged' if args.if_changed and found==state else
                                'too recent' if max_age is not None and found is not None and found[0]==state[0] and 0<=age<max_age else None)
                        if reason:
                            print(json.dumps({'html':str(path.resolve()),'skipped':True,'reason':reason}))
                            print(f'Report: {path.absolute()} (unchanged)',file=sys.stderr)
                            return 0
                records=history.records(start,end,args.harness,args.project,args.session,args.turn)
                records=[row for row in records if all(getattr(args,key) is None or row[key]==getattr(args,key)
                    for key in ('model','effort','provider','agent'))]
                # The HTML path prints only a short receipt, so skip the (costly) JSON summary there.
                result={} if args.html else summarize(records,args.granularity,args.timezone)
                result['window']={'start':args.start,'end':args.end}
                result['source_status']=source_status
                if args.records:result['records']=records
                if args.html:
                    filtered=any(getattr(args,key) is not None for key in ('start','end','harness','project','session','turn','model','effort','provider','agent'))
                    universe=history.records() if filtered else records  # the whole history: hits and their turns are computed over it
                    texts,ctx=_visible(history,args.db,universe) if args.private else (None,None)
                    events=history.limit_events();hits_all=_hits(history,universe,events)
                    payload=build_report(records,source_status,args.timezone,redact=not args.private,prompt_texts=texts,lang=args.lang,prompt_context=ctx,prompt_inputs=_counts(ctx) if args.private else None,limit_hits=limits.scope_hits(hits_all,records,args.harness,start,end,args.project,args.session,args.turn,args.model,args.effort,args.provider,args.agent,universe),universe=universe if filtered else None,all_hits=hits_all,quota_events=_events(events),claude_quota=_claude_quota(args.db),budgets=budgets)
                    payload['initial_granularity']=args.granularity
                    if args.private:payload.update(saved_at=str(path.absolute()),reopen=_reopen(path,args.db))  # private only: a shared report never carries a local path
                    write_report(path,render_report(payload,state=state))
                    print(f'Report: {path.absolute()}',file=sys.stderr)
                    result={'html':str(path.resolve()),'observations':len(records),
                            'privacy':payload['privacy'],'billing_verified':False,'coverage_complete':False}
        print(json.dumps(result,indent=2,sort_keys=True))
        return 0 if args.command!='refresh' or result['status']=='ok' else 2
    except (OSError,ValueError,sqlite3.Error,ZoneInfoNotFoundError) as exc:
        parser.exit(2,f'usage: {terminal_safe(exc)}\n')


if __name__=='__main__':raise SystemExit(main())
