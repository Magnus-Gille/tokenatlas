"""Project identity (TOKEN-187): worktrees and subdirectories roll up to their repository, one repository on several machines is one
project, Cowork, subagent and scratch directories go into buckets."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas import projects
from tokenatlas.history import History


def repo(base, name, remote=None):
    root = Path(base) / name
    (root / '.git' / 'worktrees').mkdir(parents=True)
    config = '[core]\n\tbare = false\n'
    if remote:
        config += f'[remote "upstream"]\n\turl = https://example.org/x/y.git\n[remote "origin"]\n\turl = {remote}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    (root / '.git' / 'config').write_text(config)
    (root / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
    return root


def worktree(main, path, name):
    path = Path(path)
    path.mkdir(parents=True)
    gitdir = Path(main) / '.git' / 'worktrees' / name
    gitdir.mkdir(parents=True)
    (gitdir / 'commondir').write_text('../..\n')
    (path / '.git').write_text(f'gitdir: {gitdir}\n')
    return path


def rec(machine, cwd):
    return {'machine': machine, 'project_id': cwd, 'project_label': Path(cwd).name if cwd else None}


class RemoteTest(unittest.TestCase):
    def test_normalize_remote(self):
        same = ['git@github.com:Owner/Repo.git', 'https://github.com/Owner/Repo', 'https://user:secret@GitHub.com/Owner/Repo.git/',
                'ssh://git@github.com:22/Owner/Repo.git', 'github.com:Owner/Repo']
        self.assertEqual({projects.normalize_remote(u) for u in same}, {'github.com/Owner/Repo'})
        for bad in (None, '', 'file:///tmp/repo', '/srv/git/repo.git', 'https://github.com/', 'https://github.com/../x', 42):
            self.assertIsNone(projects.normalize_remote(bad), bad)

    def test_origin_wins_over_other_remotes(self):
        text = '[remote "upstream"]\n url = git@a.org:u/one.git\n[remote "origin"]\n url = git@a.org:u/two.git\n'
        self.assertEqual(projects._remote(text), 'a.org/u/two')
        self.assertEqual(projects._remote('[remote "fork"]\n\turl = git@a.org:u/three\n'), 'a.org/u/three')
        self.assertIsNone(projects._remote('[core]\n\tbare = false\n'))


class LocateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_main_worktree_subdirectory_and_linked_worktree(self):
        main = repo(self.base, 'app', 'git@github.com:me/app.git')
        (main / 'src' / 'deep').mkdir(parents=True)
        linked = worktree(main, self.base / 'app-issue-7', 'app-issue-7')
        nested = worktree(main, main / '.claude' / 'worktrees' / 'agent-a1b2c3d4', 'agent-a1b2c3d4')
        expect = dict(root=str(main), remote='github.com/me/app', worktree=None)
        self.assertEqual(projects.locate(str(main), home=str(self.home)), expect)
        self.assertEqual(projects.locate(str(main / 'src' / 'deep'), home=str(self.home)), expect)
        self.assertEqual(projects.locate(str(linked), home=str(self.home)), dict(expect, worktree=str(linked)))
        self.assertEqual(projects.locate(str(nested), home=str(self.home)), dict(expect, worktree=str(nested)))

    def test_missing_relative_or_dotfiles_directories_have_no_facts(self):
        repo(self.base, 'home')  # a dotfiles repository at the home directory is not a project
        (self.home / 'notes').mkdir()
        self.assertIsNone(projects.locate(str(self.home / 'notes'), home=str(self.home)))
        self.assertIsNone(projects.locate(str(self.base / 'gone'), home=str(self.home)))
        self.assertIsNone(projects.locate('relative/path', home=str(self.home)))
        self.assertIsNone(projects.locate(None))
        plain = self.base / 'plain'
        plain.mkdir()
        self.assertIsNone(projects.locate(str(plain), home=str(self.home)))

    def test_a_repository_without_remote_is_keyed_by_its_root(self):
        main = repo(self.base, 'local')
        self.assertEqual(projects.locate(str(main), home=str(self.home)), dict(root=str(main), remote=None, worktree=None))


class ResolveTest(unittest.TestCase):
    MAC, PI = 'm-' + 'a' * 32, 'm-' + 'b' * 32

    def facts(self):
        return {self.MAC: {'/Users/me/repos/app': dict(root='/Users/me/repos/app', remote='github.com/me/app', worktree=None),
                           '/Users/me/repos/app/src-tauri': dict(root='/Users/me/repos/app', remote='github.com/me/app', worktree=None),
                           '/Users/me/repos/app-t12': dict(root='/Users/me/repos/app', remote='github.com/me/app', worktree='/Users/me/repos/app-t12'),
                           '/Users/me/repos/lib': dict(root='/Users/me/repos/lib', remote=None, worktree=None)},
                self.PI: {'/home/me/src/app': dict(root='/home/me/src/app', remote='github.com/me/app', worktree=None)}}

    def resolve(self, pairs, roots=None):
        records = [rec(m, d) for m, d in pairs]
        projects.apply(records, self.facts() if roots is None else roots, homes=['/Users/me', '/home/me'])
        return {(r['project_path']): (r['project_id'], r['project_label'], r['project_sub']) for r in records}

    def test_git_facts_roll_up_worktrees_subdirectories_and_machines(self):
        out = self.resolve([(self.MAC, '/Users/me/repos/app'), (self.MAC, '/Users/me/repos/app/src-tauri'), (self.MAC, '/Users/me/repos/app-t12'),
                            (self.PI, '/home/me/src/app'), (self.MAC, '/Users/me/repos/lib')])
        self.assertEqual(out['/Users/me/repos/app'], ('git:github.com/me/app', 'app', None))
        self.assertEqual(out['/Users/me/repos/app/src-tauri'], ('git:github.com/me/app', 'app', 'src-tauri'))
        self.assertEqual(out['/Users/me/repos/app-t12'], ('git:github.com/me/app', 'app', 'app-t12'))
        self.assertEqual(out['/home/me/src/app'], ('git:github.com/me/app', 'app', None))
        self.assertEqual(out['/Users/me/repos/lib'], ('repo:/Users/me/repos/lib', 'lib', None))

    def test_fallbacks_without_facts(self):
        out = self.resolve([(self.MAC, '/Users/me/repos/app'),
                            (self.MAC, '/Users/me/repos/app/crates/core'),            # under a known root
                            (self.MAC, '/Users/me/repos/app-issue-512'),              # deleted sibling worktree
                            (self.MAC, '/Users/me/repos/app/.claude/worktrees/x1y2'),  # nested worktree
                            (self.PI, '/home/me/repos/app'),                           # other machine, same name, no facts there
                            (self.PI, '/home/me/repos/other'), (self.PI, '/home/me/repos/other/src'),  # a repository inferred from src
                            (self.MAC, '/Users/me/repos/other')])                      # the same inferred name on another machine
        app = ('git:github.com/me/app', 'app')
        self.assertEqual(out['/Users/me/repos/app/crates/core'], app + ('crates/core',))
        self.assertEqual(out['/Users/me/repos/app-issue-512'], app + ('app-issue-512',))
        self.assertEqual(out['/Users/me/repos/app/.claude/worktrees/x1y2'], app + ('x1y2',))
        self.assertEqual(out['/home/me/repos/app'], app + (None,))
        self.assertEqual(out['/home/me/repos/other/src'][0], out['/home/me/repos/other'][0])
        self.assertEqual(out['/Users/me/repos/other'][0], out['/home/me/repos/other'][0])
        self.assertEqual(out['/home/me/repos/other'][1:], ('other', None))

    def test_a_sibling_name_match_needs_the_same_parent_directory(self):
        out = self.resolve([(self.MAC, '/clients/app-mobile'), (self.PI, '/home/me/work/app-t9'), (self.MAC, '/Users/me/repos/app-t9')])
        self.assertEqual(out['/clients/app-mobile'], ('/clients/app-mobile', 'app-mobile', None))
        self.assertEqual(out['/home/me/work/app-t9'][0], '/home/me/work/app-t9')
        self.assertEqual(out['/Users/me/repos/app-t9'][0], 'git:github.com/me/app')

    def test_buckets(self):
        out = self.resolve([(self.MAC, '/Users/me/Library/Application Support/Claude/local-agent-mode-sessions/a/b/outputs'),
                            (self.MAC, '/sessions/eager-hopper/mnt/outputs'),
                            (self.MAC, '/Users/me/.claude/agents/agent-1f2e3d4c'),
                            (self.MAC, '/Users/me/hi'), (self.MAC, '/Users/me'), (self.MAC, '/tmp/abc123'),
                            (self.MAC, '/private/var/folders/xy/T/run'), (self.PI, '/home/pi'), (self.MAC, '/Users/me/scratch'),
                            (self.MAC, '/Users/me/projects/website')], roots={})
        self.assertEqual(out['/sessions/eager-hopper/mnt/outputs'][:2], ('bucket:cowork', 'Cowork'))
        self.assertEqual(out['/Users/me/Library/Application Support/Claude/local-agent-mode-sessions/a/b/outputs'][:2], ('bucket:cowork', 'Cowork'))
        self.assertEqual(out['/Users/me/.claude/agents/agent-1f2e3d4c'], ('bucket:subagents', 'Subagents', 'agent-1f2e3d4c'))
        for other in ('/Users/me/hi', '/Users/me', '/tmp/abc123', '/private/var/folders/xy/T/run', '/home/pi', '/Users/me/scratch'):
            self.assertEqual(out[other][:2], ('bucket:other', 'Other'), other)
        self.assertEqual(out['/Users/me/projects/website'], ('/Users/me/projects/website', 'website', None))

    def test_a_subagent_worktree_of_a_repository_stays_with_the_repository(self):
        out = self.resolve([(self.MAC, '/Users/me/repos/app/.claude/worktrees/agent-a1b2c3d4')])
        self.assertEqual(out['/Users/me/repos/app/.claude/worktrees/agent-a1b2c3d4'], ('git:github.com/me/app', 'app', 'agent-a1b2c3d4'))

    def test_shared_names_get_the_owner_or_a_number(self):
        roots = {self.MAC: {'/a/site': dict(root='/a/site', remote='github.com/one/site', worktree=None),
                            '/b/site': dict(root='/b/site', remote='github.com/two/site', worktree=None),
                            '/c/tool': dict(root='/c/tool', remote=None, worktree=None),
                            '/d/tool': dict(root='/d/tool', remote=None, worktree=None)}}
        out = self.resolve([(self.MAC, p) for p in ('/a/site', '/b/site', '/c/tool', '/d/tool')], roots=roots)
        self.assertEqual(out['/a/site'][1], 'one/site')
        self.assertEqual(out['/b/site'][1], 'two/site')
        self.assertEqual({out['/c/tool'][1], out['/d/tool'][1]}, {'tool · 1', 'tool · 2'})

    def test_unknown_and_missing_directories_are_left_alone(self):
        records = [rec(self.MAC, None), rec(self.MAC, 'unknown'), rec(self.MAC, 'claude:-Users-me-x')]
        resolver = projects.apply(records, {})
        self.assertEqual([r['project_id'] for r in records], [None, 'unknown', 'claude:-Users-me-x'])
        self.assertEqual(projects.canonical('claude:-Users-me-x', resolver), 'claude:-Users-me-x')

    def test_canonical_maps_directories_and_labels_to_the_project(self):
        records = [rec(self.MAC, '/Users/me/repos/app-t12'), rec(self.MAC, '/Users/me/repos/app')]
        resolver = projects.apply(records, self.facts())
        for value in ('/Users/me/repos/app-t12', 'app', 'git:github.com/me/app'):
            self.assertEqual(projects.canonical(value, resolver), 'git:github.com/me/app')
        self.assertEqual(projects.canonical('nope', resolver), 'nope')
        self.assertIsNone(projects.canonical(None, resolver))

    def test_apply_is_idempotent(self):
        records = [rec(self.MAC, '/Users/me/repos/app-t12')]
        projects.apply(records, self.facts())
        first = dict(records[0])
        projects.apply(records, self.facts())
        self.assertEqual(records[0], first)


class HistoryRootsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, db, cwds):
        from types import SimpleNamespace
        from tokenatlas import history
        with History(db) as h:
            c = h.connection
            for i, cwd in enumerate(cwds):
                r = SimpleNamespace(harness='claude', provider='anthropic', call_id=f'msg_{db.name}_{i}', timestamp=datetime(2026, 11, 1, 10, i, tzinfo=timezone.utc),
                                    session_id='s', model='claude-opus-4-5', effort='unknown', project=Path(cwd).name, project_id=cwd, cwd=cwd,
                                    entrypoint='cli', thread_kind='main', agent='main', parent_session_id=None, turn_id='t', turn_confidence='observed',
                                    raw_usage={'input_tokens': 1, 'output_tokens': 1, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0},
                                    id_synthetic=False, output_final=True)
                item = history.normalize(r, h.machine)
                h._insert(c, history._encode(item, history._key(item)))
            history.History._bump(c)
            return h.machine

    def test_refresh_records_facts_for_existing_directories_and_import_brings_them_along(self):
        main = repo(self.base, 'app', 'git@github.com:me/app.git')
        linked = worktree(main, self.base / 'app-t1', 'app-t1')
        gone = str(self.base / 'gone')
        remote_db, local_db = self.base / 'remote.sqlite3', self.base / 'local.sqlite3'
        remote_machine = self.store(remote_db, [str(linked), gone])
        with History(remote_db) as h:
            self.assertEqual(h.update_project_roots(), 1)
            self.assertEqual(h.update_project_roots(), 0)  # already known
            roots = h.project_roots()
        self.assertEqual(roots, {remote_machine: {str(linked): dict(root=str(main), remote='github.com/me/app', worktree=str(linked))}})
        snapshot = self.base / 'snap.sqlite3'
        with History(remote_db) as h:
            h.snapshot(snapshot)
        self.store(local_db, [str(main)])
        with History(local_db) as h:
            h.update_project_roots()
            before = h.revision
            h.import_snapshot(snapshot, 'pi')
            merged = h.project_roots()
            self.assertGreater(h.revision, before)
            records = h.records()
        self.assertEqual(set(merged), {remote_machine, h.machine})
        projects.apply(records, merged)
        self.assertEqual({r['project_id'] for r in records if r['project_path'] != gone}, {'git:github.com/me/app'})


if __name__ == '__main__':
    unittest.main()
