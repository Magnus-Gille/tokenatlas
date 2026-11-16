"""Project identity (TOKEN-187): one project per git repository, not one per working directory.

A request's working directory is a poor project name: one repository shows up as its root, its subdirectories, every
worktree and every checkout on another machine. This module resolves each (machine, directory) to a project:

1. **Git facts recorded at refresh.** For a directory that exists on the machine that ran the refresh, `locate` reads
   the repository's own files (`.git`, `commondir`, `config`, `HEAD`); it never runs git and never touches the network.
   The facts (main worktree root, normalized remote, worktree name) are kept in the history's `meta` table under
   `project_roots`, per machine, so a worktree that is deleted later keeps its project and a snapshot import brings
   another machine's facts along.
2. **Same remote, one project.** Directories whose repository has the same normalized remote (`github.com/owner/repo`)
   are one project, whatever the machine or path.
3. **Path fallbacks** for directories without facts (removed before this version recorded them, or from a machine
   that runs an older version): a directory under a known repository root rolls up to it; `.../.claude/worktrees/<n>`
   and `.../worktrees/<n>` roll up to the directory that holds them; a sibling named `<repo>-<suffix>` of a known
   repository rolls up to it; a directory on another machine named exactly like a known repository joins it.
4. **Buckets** for what is left and is not a project: Cowork `outputs` folders, subagent `agent-<id>` folders, and
   scratch directories (temporary directories, the home directory, names of four characters or fewer directly in a home or scratch folder).

Every other directory stays its own project with its original id, so nothing that has no better answer changes.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

META_KEY = 'project_roots'
BUCKETS = {'cowork': 'Cowork', 'subagents': 'Subagents', 'other': 'Other'}
BUCKET_PREFIX = 'bucket:'
GIT_PREFIX = 'git:'
REPO_PREFIX = 'repo:'
_AGENT = re.compile(r'agent-[0-9A-Za-z]{6,}')
_TEMP = re.compile(r'^(/private)?/(tmp|var/folders|var/tmp)(/|$)|^/dev/shm(/|$)|[\\/](Temp|tmp)[\\/]', re.IGNORECASE)
_SCRATCH_NAMES = frozenset('tmp temp scratch test tests sandbox playground desktop downloads documents'.split())
_WORKTREE_PARENTS = ('/.claude/worktrees/', '/.worktrees/', '/worktrees/')
_HOME = re.compile(r'^(/Users/[^/]+|/home/[^/]+|/root|[A-Za-z]:[\\/]Users[\\/][^\\/]+)[\\/]?$')
MAX_DEPTH = 40
# Directory names that sit at a repository's root: a directory that holds a saved directory by one of these names is a repository
# even without git facts (a folder of repositories such as ~/repos is not: its children have project names).
_SOURCE_DIRS = frozenset('src src-tauri crates lib libs app apps packages pkg cmd internal web www backend frontend server client '
                         'services infra k8s scripts tests test docs examples tools bin api ui core'.split())
MAX_CONFIG = 256 * 1024


def _path(text):
    return PureWindowsPath(text) if re.match(r'^[A-Za-z]:[\\/]', text) or '\\' in text and '/' not in text else PurePosixPath(text)


def _name(text):
    return _path(text).name or str(text)


def normalize_remote(url):
    """`host/owner/repo` (lower-case host, no scheme, user, port, `.git` or trailing slash), or None when it is not a remote URL."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > 1024:
        return None
    if '://' in url:
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        host, path = (parts.hostname or '').lower(), parts.path
        if parts.scheme == 'file' or not host:
            return None
    elif re.match(r'^[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)+/[^:]+$', url):  # already normalized: host/owner/repo
        host, path = url.split('/', 1)
        host = host.lower()
    else:
        match = re.match(r'^(?:[^@/]+@)?([^:/]+):(?!/)(.+)$', url)  # scp-like: [user@]host:owner/repo
        if not match:
            return None
        host, path = match.group(1).lower(), match.group(2)
    path = path.strip('/')
    if path.endswith('.git'):
        path = path[:-4]
    path = path.strip('/')
    if not path or not re.fullmatch(r'[0-9a-z.-]+', host) or any(part in ('', '.', '..') for part in path.split('/')):
        return None
    return f'{host}/{path}'


def _read(path, limit=MAX_CONFIG):
    try:
        with open(path, 'rb') as stream:
            return stream.read(limit).decode('utf-8', 'replace')
    except OSError:
        return None


def _remote(config_text):
    """The `origin` remote URL from a git config, else the first remote's; None when there is none."""
    if not config_text:
        return None
    section, found = None, {}
    for raw in config_text.splitlines():
        line = raw.strip()
        if not line or line[0] in '#;':
            continue
        head = re.match(r'^\[\s*remote\s+"([^"]*)"\s*\]', line)
        if line.startswith('['):
            section = head.group(1) if head else None
            continue
        if section is not None:
            key, _, value = line.partition('=')
            if key.strip().lower() == 'url' and section not in found:
                found[section] = value.strip().strip('"')
    url = found.get('origin') or next(iter(found.values()), None)
    return normalize_remote(url)


def locate(cwd, home=None):
    """Git facts for an existing local directory: {'root', 'remote', 'worktree'} or None. Reads files only (never runs git).

    `root` is the main worktree's directory, `worktree` the linked worktree's directory when `cwd` is inside one (else None).
    A repository at the home directory or the filesystem root (a dotfiles repository) is not a project and gives None."""
    if not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or not os.path.isabs(cwd):
        return None
    try:
        if not os.path.isdir(cwd):
            return None
    except (OSError, ValueError):
        return None
    home = os.path.realpath(home or os.path.expanduser('~'))
    current = os.path.normpath(cwd)
    for _ in range(MAX_DEPTH):
        marker = os.path.join(current, '.git')
        if os.path.isdir(marker):
            git_dir, top, worktree = marker, current, None
            break
        if os.path.isfile(marker):
            text = _read(marker, 4096) or ''
            match = re.match(r'^gitdir:\s*(.+?)\s*$', text.splitlines()[0] if text.splitlines() else '')
            if not match:
                return None
            git_dir = match.group(1)
            if not os.path.isabs(git_dir):
                git_dir = os.path.normpath(os.path.join(current, git_dir))
            top, worktree = current, current
            break
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    else:
        return None
    common = git_dir
    pointer = _read(os.path.join(git_dir, 'commondir'), 4096)
    if pointer and pointer.strip():
        common = os.path.normpath(os.path.join(git_dir, pointer.strip()))
    elif worktree is not None and os.path.basename(os.path.dirname(git_dir)) == 'worktrees':
        common = os.path.dirname(os.path.dirname(git_dir))
    if worktree is not None and os.path.basename(common) != '.git':
        worktree = None if os.path.normpath(common) == os.path.normpath(git_dir) else worktree  # a submodule's gitdir is not a worktree
    root = os.path.dirname(common) if os.path.basename(common) == '.git' else top
    if worktree is not None and os.path.normpath(worktree) == os.path.normpath(root):
        worktree = None
    try:
        if os.path.realpath(root) in (home, os.path.realpath(os.sep)):
            return None
    except (OSError, ValueError):
        return None
    return dict(root=root, remote=_remote(_read(os.path.join(common, 'config'))), worktree=worktree)


def load(text):
    """{machine: {directory: facts}} from the meta value (malformed parts are dropped)."""
    try:
        value = json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return {}
    out = {}
    for machine, entries in (value or {}).items() if isinstance(value, dict) else ():
        if not isinstance(machine, str) or not isinstance(entries, dict):
            continue
        for cwd, facts in entries.items():
            if isinstance(cwd, str) and isinstance(facts, dict) and isinstance(facts.get('root'), str):
                remote = facts.get('remote')
                worktree = facts.get('worktree')
                out.setdefault(machine, {})[cwd] = dict(root=facts['root'], remote=normalize_remote(remote) if isinstance(remote, str) else None,
                                                        worktree=worktree if isinstance(worktree, str) else None)
    return out


def dump(roots):
    return json.dumps(roots, sort_keys=True, separators=(',', ':'))


def merge(base, incoming):
    """Facts of `incoming` added to `base` (a machine's own facts win: a key already present is kept)."""
    out = {machine: dict(entries) for machine, entries in base.items()}
    for machine, entries in incoming.items():
        target = out.setdefault(machine, {})
        for cwd, facts in entries.items():
            target.setdefault(cwd, facts)
    return out


def _bucket(cwd, name, home, short=True):
    lowered = cwd.replace('\\', '/').lower()
    if name == 'outputs' or '/local-agent-mode-sessions/' in lowered:
        return 'cowork'
    if _AGENT.fullmatch(name):
        return 'subagents'
    parent = str(_path(cwd).parent).rstrip('/\\')
    loose = parent in home or _HOME.match(parent) or _path(parent).name.lower() in _SCRATCH_NAMES  # directly in a home or scratch folder
    if _TEMP.search(cwd) or cwd.rstrip('/\\') in home or _HOME.match(cwd) or short and loose and len(name) <= 4 or name.lower() in _SCRATCH_NAMES:
        return 'other'
    return None


def _first(path, root):
    """The first component of `path` below `root`."""
    return path[len(root):].replace('\\', '/').strip('/').split('/', 1)[0]


def _under(path, root):
    return path != root and path.startswith(root.rstrip('/\\') + ('\\' if '\\' in root and '/' not in root else '/'))


class Resolver:
    """Resolve (machine, directory) pairs to (project id, label, second level). Built once per history over every saved pair."""

    def __init__(self, roots, pairs, homes=None):
        self.roots = roots or {}
        self.homes = {h.rstrip('/\\') for h in (homes or ()) if h} | {os.path.expanduser('~').rstrip('/\\')}
        self.cache = {}
        self.names = {}      # project id -> repository name
        self.root_ids = {}   # (machine, root directory) -> project id
        for machine, entries in self.roots.items():
            for facts in entries.values():
                pid = self._git_id(facts)
                self.root_ids[(machine, facts['root'])] = pid
                self.names.setdefault(pid, facts['remote'].rsplit('/', 1)[-1] if facts.get('remote') else _name(facts['root']))
        pairs = [(m, d) for m, d in pairs if isinstance(d, str) and d and d != 'unknown']
        # Directories without facts that hold other saved directories (or a worktrees folder) are repositories too.
        by_machine = {}
        for machine, cwd in pairs:
            by_machine.setdefault(machine, set()).add(cwd)
        inferred = {}
        for machine, dirs in by_machine.items():
            for cwd in dirs:
                if self.roots.get(machine, {}).get(cwd) or self._under_root(machine, cwd):
                    continue
                holder = self._holder(cwd)
                if holder and not self.roots.get(machine, {}).get(holder) and not self._under_root(machine, holder) \
                        and not _bucket(holder, _name(holder), self.homes, short=False):
                    inferred.setdefault((machine, holder), _name(holder))
            for cwd in dirs:
                if cwd in inferred or self.roots.get(machine, {}).get(cwd) or self._under_root(machine, cwd) \
                        or _bucket(cwd, _name(cwd), self.homes, short=False) or self._holder(cwd):
                    continue
                if any(_under(other, cwd) and not self._holder(other) and _first(other, cwd) in _SOURCE_DIRS for other in dirs):
                    inferred.setdefault((machine, cwd), _name(cwd))
        # An inferred repository named like a repository with facts joins it; inferred ones that share a name across machines are one.
        known_by_name = {}
        for pid, name in self.names.items():
            known_by_name.setdefault(name.lower(), set()).add(pid)
        first_path = {}
        for (machine, root), name in sorted(inferred.items(), key=lambda kv: (kv[0][1], str(kv[0][0]))):
            facts_ids = known_by_name.get(name.lower())
            if facts_ids and len(facts_ids) == 1:
                pid = next(iter(facts_ids))
            else:
                pid = first_path.setdefault(name.lower(), root)
                self.names.setdefault(pid, name)
            self.root_ids[(machine, root)] = pid
        self.by_name = {}
        for pid, name in self.names.items():
            self.by_name.setdefault(name.lower(), set()).add(pid)
        for machine, cwd in pairs:
            self.resolve(machine, cwd)
        self._label()

    @staticmethod
    def _git_id(facts):
        return GIT_PREFIX + facts['remote'] if facts.get('remote') else REPO_PREFIX + facts['root']

    @staticmethod
    def _holder(cwd):
        """(directory that holds a worktrees folder) for `.../.claude/worktrees/<n>`-like paths, else None."""
        normal = cwd.replace('\\', '/')
        for marker in _WORKTREE_PARENTS:
            at = normal.find(marker)
            if at > 0:
                return cwd[:at]
        return None

    def _under_root(self, machine, cwd):
        best = None
        for (m, root), pid in self.root_ids.items():
            if m == machine and _under(cwd, root) and (best is None or len(root) > len(best[0])):
                best = (root, pid)
        return best

    def _unique(self, name):
        found = self.by_name.get(name.lower())
        return next(iter(found)) if found and len(found) == 1 else None

    def _find(self, machine, cwd):
        """(project id, second level) or None for a directory without a better answer."""
        facts = self.roots.get(machine, {}).get(cwd)
        if facts:
            pid = self._git_id(facts)
            if facts.get('worktree'):
                return pid, _name(facts['worktree'])
            return pid, (cwd[len(facts['root']):].strip('/\\') or None) if _under(cwd, facts['root']) else None
        if (machine, cwd) in self.root_ids:
            return self.root_ids[(machine, cwd)], None
        holder = self._holder(cwd)
        if holder:
            tail = cwd.replace('\\', '/')[len(holder):].strip('/').split('/')
            second = tail[-1] if tail[-1] and len(tail) >= 2 and tail[-2] in ('worktrees',) else '/'.join(tail[2:3]) or None
            found = self._find(machine, holder)
            return (found[0] if found else holder), second
        best = self._under_root(machine, cwd)
        if best:
            return best[1], cwd[len(best[0]):].strip('/\\')
        name = _name(cwd)
        same = self._unique(name)
        if same:  # another machine's (or a re-cloned) checkout named exactly like a known repository
            return same, None
        # a sibling worktree `<repo>-<suffix>` (also `_` or `.`): the longest known repository name wins
        for at in range(len(name) - 1, 0, -1):
            if name[at] in '-_.':
                candidate = self._unique(name[:at])
                if candidate:
                    return candidate, name
        return None

    def resolve(self, machine, cwd):
        key = (machine, cwd)
        if key in self.cache:
            return self.cache[key]
        if not isinstance(cwd, str) or not cwd or cwd == 'unknown':
            result = (cwd if cwd else None, None, None)
        else:
            found = self._find(machine, cwd)
            if found:
                pid, second = found
                result = (pid, self.names.get(pid) or _name(pid), second)
            else:
                bucket = _bucket(cwd, _name(cwd), self.homes)
                result = (BUCKET_PREFIX + bucket, BUCKETS[bucket], _name(cwd)) if bucket else (cwd, _name(cwd), None)
        self.cache[key] = result
        return result

    def _label(self):
        """The repository name, made unique with its owner (git remote) or a number when two projects share it."""
        used = {}
        for pid, label, _ in self.cache.values():
            if pid is not None and label is not None:
                used.setdefault(label, set()).add(pid)
        self.labels = {}
        for label, ids in used.items():
            if len(ids) == 1:
                self.labels[next(iter(ids))] = label
                continue
            for n, pid in enumerate(sorted(ids), 1):
                parts = pid[len(GIT_PREFIX):].split('/') if pid.startswith(GIT_PREFIX) else []
                self.labels[pid] = f'{parts[-2]}/{parts[-1]}' if len(parts) >= 3 else f'{label} · {n}'

    def label(self, pid, fallback=None):
        return self.labels.get(pid, fallback)


def apply(records, roots, homes=None):
    """Resolve every record's project in place: `project_id` and `project_label` become the project's, `project_path` keeps the
    directory id it was saved with and `project_sub` names the worktree, subdirectory or bucket member (None at a project's root).
    Returns the Resolver (its `aliases` map a directory or label to the project id, for --project)."""
    pairs = {(r.get('machine'), r.get('project_id')) for r in records}
    resolver = Resolver(roots, sorted(pairs, key=lambda p: (str(p[0]), str(p[1]))), homes)
    resolved = {}
    for pair in pairs:
        pid, label, second = resolver.resolve(*pair)
        resolved[pair] = (pid, resolver.label(pid, label), second)
    for r in records:
        if 'project_path' in r:
            continue  # already resolved
        raw = r.get('project_id')
        pid, label, second = resolved[(r.get('machine'), raw)]
        r['project_path'] = raw
        r['project_id'] = pid
        if label is not None:
            r['project_label'] = label
        r['project_sub'] = second
    aliases = {}
    for (machine, raw), (pid, label, _) in resolved.items():
        if pid is None:
            continue
        aliases.setdefault(pid, pid)
        if isinstance(raw, str):
            aliases.setdefault(raw, pid)
        if isinstance(label, str):
            aliases.setdefault(label, pid)
    resolver.aliases = aliases
    return resolver


def canonical(wanted, resolver):
    """The project id a --project value names (a project id, a saved directory or a project label); the value itself when unknown."""
    if wanted is None or resolver is None:
        return wanted
    return getattr(resolver, 'aliases', {}).get(wanted, wanted)


def summary(records, cost_of, limit=None, redact=False):
    """Projects by list-price cost: [{project, label, directories, requests, tokens, cost, cost_complete, second_level}], costliest first.
    `directories` lists the saved directories that rolled up into the project; `second_level` the worktrees, subdirectories or bucket
    members with their request counts (largest first, at most 10). With `redact` the project is a pseudonym (Project 001 in cost order)
    and no name or path is kept: only counts."""
    groups = {}
    for r in records:
        pid = r.get('project_id')
        g = groups.get(pid)
        if g is None:
            g = groups[pid] = dict(project=pid, label=r.get('project_label'), directories=set(), requests=0, tokens=0, cost=0.0,
                                   priced=0, second={})
        g['requests'] += 1
        g['tokens'] += sum(v or 0 for k, v in (r.get('tokens') or {}).items() if k != 'reasoning')
        if r.get('project_path', pid) is not None:
            g['directories'].add(r.get('project_path', pid))
        cost = cost_of(r)
        if cost is not None:
            g['cost'] += cost
            g['priced'] += 1
        sub = r.get('project_sub')
        if sub:
            g['second'][sub] = g['second'].get(sub, 0) + 1
    out = []
    for g in groups.values():
        out.append(dict(project=g['project'], label=g['label'], directories=sorted(g['directories']), requests=g['requests'], tokens=g['tokens'],
                        cost=g['cost'] if g['priced'] else None, cost_complete=g['priced'] == g['requests'],
                        second_level=[dict(name=k, requests=v) for k, v in sorted(g['second'].items(), key=lambda kv: (-kv[1], kv[0]))[:10]]))
    out.sort(key=lambda p: (p['cost'] is None, -(p['cost'] or 0), -p['tokens'], str(p['project'])))
    for i, p in enumerate(out, 1):
        p['directory_count'] = len(p['directories'])
        if redact:
            p.update(project=f'Project {i:03d}', label=f'Project {i:03d}', directories=[],
                     second_level=[dict(name=None, requests=s['requests']) for s in p['second_level']])
    return out[:limit] if limit else out
