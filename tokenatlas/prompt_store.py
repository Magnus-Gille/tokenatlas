"""Opt-in prompt text for the current top prompts: a 0600 side file next to the history, never in SQLite or snapshots."""
import hashlib
import json
import os
import stat
import sys
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas import prompt_text, prompts, provenance, turn_context

FILE = 'top-prompts.json'
SELECTION_VERSION = 1


def store_path(db_path):
    return Path(db_path).expanduser().with_name(FILE)


def _str(v):return v if isinstance(v,str) else None


def _count(v):return v if isinstance(v,int) and not isinstance(v,bool) and v>=0 else None


def _strs(v,n):return [x for x in v if isinstance(x,str)][:n] if isinstance(v,list) else []


def _norm_context(c):
    """The full turn-context shape with every invalid part dropped to None or empty; a non-dict is no context (None)."""
    if not isinstance(c,dict):return None
    g=lambda k:c.get(k) if isinstance(c.get(k),dict) else {}
    i,a,o=g('inputs'),g('activity'),g('outcomes')
    return {**{k:_str(c.get(k)) for k in ('title','title_source','cwd','branch','repository','final')},
            'inputs':{'count':_count(i.get('count')),'first':_str(i.get('first')),'followups':_strs(i.get('followups'),5)},
            'activity':{k:_count(a.get(k)) for k in ('shell','edits','web','subagents')},
            'outcomes':{'prs':_strs(o.get('prs'),5),'commits':_strs(o.get('commits'),5)}}


def _entries(raw):
    """Validated entry dicts from file bytes, contexts normalized; ValueError when the file is not a well-formed store."""
    try:
        entries=json.loads(raw)['entries']
        for e in entries:
            if not (isinstance(e['text'],(str,type(None))) and all(isinstance(e[k],str) for k in ('harness','session','turn_id'))):raise ValueError('bad entry')
            e['context']=_norm_context(e.get('context'))
        return entries
    except (ValueError,KeyError,TypeError) as exc:raise ValueError(f'{type(exc).__name__}: {exc}') from exc


def _warn(message):
    print(f'usage: warning: {message}',file=sys.stderr)


def _unsafe(st):
    """Why a store file must not be trusted (POSIX: symlink, non-regular, extra hard links, foreign owner, group/other access), else None.
    Windows has no such checks: the file there relies on the user profile's ACLs."""
    if os.name=='nt':return None
    if stat.S_ISLNK(st.st_mode):return 'a symlink'
    if not stat.S_ISREG(st.st_mode):return 'not a regular file'
    if st.st_nlink!=1:return 'hard-linked elsewhere'
    if st.st_uid!=os.getuid():return 'owned by another user'
    if st.st_mode&0o077:return f'accessible by others (mode {stat.S_IMODE(st.st_mode):o})'
    return None


def _read(path):
    """(raw bytes or None, refused reason or None). A missing file is (None, None); an unsafe or unreadable one is never read."""
    path=str(path)
    try:st=os.lstat(path)
    except FileNotFoundError:return None,None
    except OSError as exc:return None,f'cannot read: {exc}'
    bad=_unsafe(st)
    if bad:return None,bad
    try:
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        try:
            if os.name!='nt':
                now=os.fstat(fd)
                if (now.st_dev,now.st_ino)!=(st.st_dev,st.st_ino) or _unsafe(now):return None,'changed while opening'
            with os.fdopen(fd,'rb',closefd=False) as stream:return stream.read(),None
        finally:os.close(fd)
    except OSError as exc:return None,f'cannot read: {exc}'


def _blank(v):
    """True for a context with nothing known: None, empty containers, or only blank parts (a 0 count is known)."""
    if isinstance(v,dict):return all(_blank(x) for x in v.values())
    if isinstance(v,list):return all(_blank(x) for x in v)
    return v is None or v==''


def valid_choice(k,by):return isinstance(k,int) and not isinstance(k,bool) and k>=1 and by in ('cost','tokens')


def load_meta(path):
    """({key: entry}, k, by) with key (harness, session, turn_id); a missing file is empty, a corrupt or unsafe one is empty plus a stderr warning.
    Version 1 files (no context) read as entries without one."""
    raw,bad=_read(path)
    if bad:_warn(f'ignoring {path}: {bad}');return {},None,None
    if raw is None:return {},None,None
    try:
        data=json.loads(raw);entries=_entries(raw)
        return {(e['harness'],e['session'],e['turn_id']):e for e in entries},data.get('k'),data.get('by')
    except ValueError as exc:
        _warn(f'ignoring corrupt {path} ({exc})');return {},None,None


def store_token(path):
    """A cheap, content-sensitive identity for the side store.

    Report cache checks use this before loading history.  Unsafe and malformed files
    still get a distinct token, while their contents are never trusted for capture.
    """
    raw, bad = _read(path)
    if bad:
        return hashlib.sha256(f'unsafe:{bad}'.encode()).hexdigest()[:32]
    if raw is None:
        return None
    return hashlib.sha256(raw).hexdigest()[:32]


def _state(path):
    """The trusted cache identity written by ``update``, or ``None`` for old/invalid stores."""
    raw, bad = _read(path)
    if bad or raw is None:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(value, dict):return None
    return {key:value.get(key) for key in ('history_revision','history_token','machine','prices','selection_version')}


def _selection_token(entries, k, by):
    values = entries.values() if isinstance(entries, dict) else entries
    entries = sorted(values, key=lambda entry: (entry.get('harness'), entry.get('session'), entry.get('turn_id')))
    body = json.dumps({'k': k, 'by': by, 'entries': entries}, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(body.encode()).hexdigest()[:32]


def load_revision(path):
    """The history revision recorded by ``update``, or ``None`` for old/invalid stores."""
    state = _state(path)
    value = state and state.get('history_revision')
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def up_to_date(path, revision, history_token=None, machine=None, prices=None):
    """Whether a valid store records this history identity and still has its choice."""
    entries, k, by = load_meta(path)
    state = _state(path)
    if not (bool(entries or Path(path).is_file()) and valid_choice(k, by) and state and state.get('history_revision') == revision):
        return False
    raw, _ = _read(path)
    try:stored_semantics = json.loads(raw).get('selection_token') if raw else None
    except (ValueError, TypeError, AttributeError):stored_semantics = None
    return all(state.get(key) == value for key, value in (('history_token', history_token), ('machine', machine), ('prices', prices)) if value is not None) \
        and state.get('selection_version') == SELECTION_VERSION and stored_semantics == _selection_token(entries, k, by)


def load(path):
    """{key: text or None}."""
    return {k:e['text'] for k,e in load_meta(path)[0].items()}


def load_context(path):
    """{key: context dict or None}."""
    return {k:e.get('context') for k,e in load_meta(path)[0].items()}


def visible_all(path,records,table,ranked=None,assigned=None,ranked_choice=None):
    """(texts, contexts) of the stored prompts in the current global top k (the store's recorded k and by): all a private report may embed.
    Contexts that are blank are left out."""
    entries,k,by=load_meta(path)
    if not entries or not valid_choice(k,by):return {},{}
    ranked = ranked if ranked is not None and ranked_choice == (k, by) else prompts.top_prompts(records,table,k,by,assigned=assigned)
    top={(p['harness'],p['session'],p['turn_id']) for p in ranked['prompts'][:k]}
    return ({key:e['text'] for key,e in entries.items() if key in top},
            {key:e['context'] for key,e in entries.items() if key in top and not _blank(e.get('context'))})


def visible(path,records,table):
    """The stored texts whose prompt is in the current global top k."""
    return visible_all(path,records,table)[0]


def visible_context(path,records,table):
    """The stored non-blank contexts whose prompt is in the current global top k."""
    return visible_all(path,records,table)[1]


def texts_hash(texts,context=None):
    """Stable digest of loaded texts and contexts for the report state; None when there are none."""
    if not texts and not context:return None
    listed=sorted([*k,v] for k,v in (texts or {}).items())
    body=json.dumps([listed,sorted([*k,v] for k,v in context.items())] if context else listed,sort_keys=True,separators=(',',':'),ensure_ascii=True)
    return hashlib.sha256(body.encode()).hexdigest()[:32]


def _clean_temps(directory):
    """Drop stale temp files of an interrupted write: ours, owned by this user, older than an hour."""
    cutoff=time.time()-3600
    for entry in Path(directory).glob('.top-prompts-*'):
        try:
            st=entry.lstat()
            if stat.S_ISREG(st.st_mode) and st.st_mtime<cutoff and (os.name=='nt' or st.st_uid==os.getuid()):entry.unlink()
        except OSError:pass


def _write(path,data):
    path=Path(path)
    fd,tmp=tempfile.mkstemp(prefix='.top-prompts-',dir=path.parent)  # replace swaps the path itself, so a symlink there is replaced, never followed
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        if os.name!='nt':os.chmod(tmp,0o600)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def update(path,records,table,machine,k=5,by='cost',extract=prompt_text.extract_prompt,context=turn_context.turn_context,local=None,ranked=None,assigned=None,revision=None,history_token=None,prices=None):
    """Keep text and turn context for the global top k prompts only: keep known entries, add local new ones, retry unreadable or
    blank ones (also upgrading version 1 entries), evict the rest. Remote prompts get neither. `local` is the set of
    source paths the history recorded as collected locally (History.local_source_paths()); without it nothing is readable."""
    local=frozenset(local or ())
    _clean_temps(Path(path).parent)
    ranked = ranked or prompts.top_prompts(records,table,k,by)
    top=ranked['prompts']
    old_raw,bad=_read(path)  # an unsafe file is not trusted: start over and replace it
    try:old={(e['harness'],e['session'],e['turn_id']):e for e in _entries(old_raw)} if old_raw else {}
    except ValueError:old={}  # corrupt: start over
    own={}  # prompt key -> sources of its own (non-subagent) observations
    assigned = prompts.assign_prompts(records) if assigned is None else assigned
    for r,found in zip(records,assigned):
        if found and r['thread_kind']!='subagent':own.setdefault(found[:3],[]).extend(r.get('sources') or [])
    entries,added,kept=[],0,0
    for p in top:
        key=(p['harness'],p['session'],p['turn_id'])
        known=old.get(key)
        need_text=known is None or known['text'] is None
        need_ctx=known is None or _blank(known.get('context'))
        if known and not need_text and not need_ctx or known and p.get('machine')!=machine:entries.append(known);kept+=1;continue
        if p.get('machine')!=machine:continue  # only this machine's own logs can be read
        sources=sorted(s for s in set(provenance.local_sources(own.get(key,()))) if s in local)
        text=None
        for source in sources if need_text else ():
            text=extract(p['harness'],source,p['session'],p['turn_id'])
            if text:break
        ctx=None
        if need_ctx and sources:
            found=context(p['harness'],sources,p['session'],p['turn_id'],p['first_ts'],p['last_ts'])
            ctx=None if _blank(found) else found
        if known and not text and not ctx:entries.append(known);kept+=1;continue  # still unreadable: keep the entry as is
        entries.append({'harness':p['harness'],'session':p['session'],'turn_id':p['turn_id'],
                        'captured_at':known['captured_at'] if known else datetime.now(timezone.utc).isoformat(timespec='seconds'),
                        'text':text if need_text else known['text'],'context':ctx if need_ctx else known.get('context')});added+=1
    evicted=len(set(old)-{(e['harness'],e['session'],e['turn_id']) for e in entries})
    body={'version':2,'k':k,'by':by,'entries':[{**e,'context':e.get('context')} for e in entries]}
    body['selection_token'] = _selection_token(body['entries'], k, by)
    if revision is not None:
        body.update(history_revision=revision, history_token=history_token, machine=machine,
                    prices=prices, selection_version=SELECTION_VERSION)
    data=json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()+b'\n'
    if data!=old_raw or bad:_write(path,data)
    return {'kept':kept,'added':added,'evicted':evicted,'path':str(path)}


def forget(path):
    """Delete the store; a symlink is unlinked, never followed. Warns when other hard links still hold the text."""
    _clean_temps(Path(path).parent)
    try:
        st=os.lstat(path)
        if stat.S_ISREG(st.st_mode) and st.st_nlink>1:_warn(f'{path}: other hard links still hold the text')
        os.unlink(path)
    except FileNotFoundError:pass
