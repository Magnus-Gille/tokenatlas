#!/usr/bin/env python3
"""Tests for turn context extraction; synthetic fixtures only."""

import json
import os
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from tokenatlas import turn_context as tc, why
from tokenatlas.turn_context import git_commits, turn_context
from test_prompt_text import (
    END, START, TS, TmpCase, codex_ctx, codex_meta, codex_tokens, codex_user, write_jsonl,
)

UNKNOWN = {"title": None, "title_source": None, "cwd": None, "branch": None, "repository": None,
           "inputs": {"count": None, "first": None, "followups": []}, "final": None,
           "activity": {"shell": None, "edits": None, "web": None, "subagents": None},
           "outcomes": {"prs": [], "commits": []}}
KEY = "sk-abcdefghijklmnop"


def use(name, **inp):
    return {"type": "tool_use", "id": "t" + name + json.dumps(inp), "name": name, "input": inp}


def claude_row(uuid, role, content, **extra):
    msg = {"role": role, "content": content}
    if role == "assistant":
        msg |= {"id": "m" + uuid, "usage": {"input_tokens": 5, "output_tokens": 7}}
    return {"type": role, "uuid": uuid, "timestamp": TS, "sessionId": "s1", "requestId": "r" + uuid,
            "message": msg, **extra}


class ClaudeTests(TmpCase):
    def fixture(self):
        return write_jsonl(self.tmp / "s1.jsonl", [
            {"type": "custom-title", "customTitle": "old title"},
            claude_row("u1", "user", "fix  the bug", cwd="/w", gitBranch="feat/x"),
            claude_row("a1", "assistant", [{"type": "text", "text": "looking"},
                                           use("Bash", command="gh pr create --fill"), use("Edit", file_path="/w/a"),
                                           use("Write", file_path="/w/b"), use("WebFetch", url="u"), use("Agent", prompt="p")]),
            claude_row("r1", "user", [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]),
            claude_row("c1", "assistant", [{"type": "text", "text": "CHILD"}], isSidechain=True),
            claude_row("a2", "assistant", [use("Bash", command="gh pr merge 45 --squash"),
                                           {"type": "text", "text": f"Done, see PR #46 {KEY}"}]),
            {"type": "custom-title", "customTitle": f"new title {KEY}"},
            claude_row("u2", "user", "second ask"),
            claude_row("a3", "assistant", [{"type": "text", "text": "other"}]),
        ])

    def test_full(self):
        path = self.fixture()
        records = why.collect_claude(self.tmp, START, END, paths=[path])
        self.assertEqual({r.turn_id for r in records}, {"u1", "u2"})
        got = turn_context("claude", [str(path)], "s1", "u1")
        self.assertEqual(got["title"], "new title [redacted]")
        self.assertEqual(got["title_source"], "custom-title")
        self.assertEqual((got["cwd"], got["branch"], got["repository"]), ("/w", "feat/x", None))
        self.assertEqual(got["inputs"], {"count": 1, "first": "fix the bug", "followups": []})
        self.assertEqual(got["final"], "Done, see PR #46 [redacted]")
        self.assertEqual(got["activity"], {"shell": 2, "edits": 2, "web": 1, "subagents": 1})
        self.assertEqual(got["outcomes"]["prs"], ["created", "#45", "#46"])
        self.assertEqual(turn_context("claude", [str(path)], "s1", "u2")["final"], "other")

    def test_sparse_and_unreadable(self):
        path = write_jsonl(self.tmp / "s1.jsonl", [claude_row("u1", "user", "hi")])
        got = turn_context("claude", [str(path)], "s1", "u1")
        self.assertEqual((got["title"], got["cwd"], got["final"]), (None, None, None))
        self.assertEqual(got["activity"], {"shell": 0, "edits": 0, "web": 0, "subagents": 0})
        self.assertEqual(turn_context("claude", [str(path)], "s1", "nope"), UNKNOWN)
        self.assertEqual(turn_context("claude", [str(self.tmp / "gone.jsonl"), None], "s1", "u1"), UNKNOWN)
        self.assertEqual(turn_context("claude", str(self.tmp), "s1", "u1"), UNKNOWN)
        self.assertEqual(turn_context("claude", [str(path)], None, "u1"), UNKNOWN)
        self.assertEqual(turn_context("other", [str(path)], "s1", "u1"), UNKNOWN)

    def test_subagent_files_skipped(self):
        sub = self.tmp / "s1" / "subagents"
        sub.mkdir(parents=True)
        path = write_jsonl(sub / "agent-1.jsonl", [claude_row("u1", "user", "child instructions")])
        self.assertEqual(turn_context("claude", [str(path)], "s1", "u1"), UNKNOWN)


def msg(role, text, **extra):
    return {"timestamp": TS, "type": "response_item", "payload": {
        "type": "message", "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}], **extra}}


def event(kind, turn, **extra):
    return {"timestamp": TS, "type": "event_msg", "payload": {"type": kind, "turn_id": turn, **extra}}


def item_user(turn, text):
    return event("item_completed", turn, item={"type": "UserMessage", "id": "i" + text, "content": [{"type": "text", "text": text}]})


def call(name, **payload):
    return {"timestamp": TS, "type": "response_item", "payload": {"type": "function_call" if "arguments" in payload else "custom_tool_call", "name": name, **payload}}


class ClaudeIsMetaTests(TmpCase):
    def test_meta_rows_are_not_inputs_and_do_not_end_the_turn(self):
        path = write_jsonl(self.tmp / "s1.jsonl", [
            claude_row("u1", "user", "real ask"),
            claude_row("a1", "assistant", [use("Skill", skill="x")]),
            claude_row("m1", "user", "Base directory for this skill: /s/x\n\nbody", isMeta=True),
            claude_row("m2", "user", "<local-command-caveat>Caveat</local-command-caveat>", isMeta=True),
            claude_row("m3", "user", [{"type": "text", "text": "injected"}], isMeta=True),
            claude_row("a2", "assistant", [{"type": "text", "text": "done"}]),
            claude_row("u2", "user", "<command-name>/model</command-name>"),
            claude_row("a3", "assistant", [{"type": "text", "text": "other"}]),
        ])
        got = turn_context("claude", [str(path)], "s1", "u1")
        self.assertEqual((got["inputs"]["count"], got["inputs"]["first"]), (1, "real ask"))
        self.assertEqual(got["final"], "done")
        self.assertEqual(turn_context("claude", [str(path)], "s1", "m1"), UNKNOWN)
        self.assertEqual(turn_context("claude", [str(path)], "s1", "u2")["inputs"]["first"], "/model")


class CodexTests(TmpCase):
    def fixture(self):
        meta = codex_meta()
        meta["payload"]["git"] = {"branch": "main", "repository_url": f"https://bob:{KEY}@github.com/o/r.git?token=1"}
        return write_jsonl(self.tmp / "rollout-x.jsonl", [
            meta, event("task_started", "T1"), codex_ctx(turn_id="T1"),
            msg("user", "<environment_context><cwd>/w</cwd></environment_context>"),
            msg("user", "do the thing"), codex_user("do the thing"), item_user("T1", "do the thing"),
            call("exec_command", arguments=json.dumps({"cmd": "ls"})),
            call("exec", input="await tools.apply_patch('x'); gh pr create --fill"),
            call("exec", input="gh pr merge 45 --squash"), call("apply_patch", input="*** Begin Patch"),
            {"timestamp": TS, "type": "response_item", "payload": {"type": "web_search_call"}},
            call("spawn_agent", arguments="{}"),
            msg("assistant", "working"), codex_tokens(1),
            item_user("T1", f"also this {KEY}"), item_user("T1", "and that"), msg("assistant", "Finished, PR #46"),
            event("task_complete", "T1"),
            msg("user", "next"), codex_ctx(turn_id="T2"), msg("assistant", "n"), codex_tokens(2),
        ])

    def test_full(self):
        path = self.fixture()
        records = why.collect_codex(self.tmp, START, END, paths=[path])
        self.assertEqual({r.turn_id for r in records}, {"T1", "T2"})
        index = write_jsonl(self.tmp / "idx.jsonl", [
            {"id": "cs1", "thread_name": "old", "updated_at": "2026-01-01"},
            {"id": "cs1", "thread_name": f"Thread {KEY}", "updated_at": "2026-02-01"},
            {"id": "other", "thread_name": "nope", "updated_at": "2027-01-01"}])
        got = turn_context("codex", [str(path)], "cs1", "T1", codex_index=index)
        self.assertEqual((got["title"], got["title_source"]), ("Thread [redacted]", "thread_name"))
        self.assertEqual((got["cwd"], got["branch"], got["repository"]), ("/w", "main", "https://github.com/o/r.git"))
        self.assertEqual(got["inputs"]["count"], 3)
        self.assertEqual(got["inputs"]["first"], "do the thing")
        self.assertEqual(got["inputs"]["followups"], ["also this [redacted]", "and that"])
        self.assertEqual(got["final"], "Finished, PR #46")
        self.assertEqual(got["activity"], {"shell": 2, "edits": 2, "web": 1, "subagents": 1})
        self.assertEqual(got["outcomes"]["prs"], ["created", "#45", "#46"])
        t2 = turn_context("codex", [str(path)], "cs1", "T2", codex_index=index)
        self.assertEqual((t2["inputs"]["count"], t2["inputs"]["first"], t2["final"]), (1, "next", "n"))

    def test_multi_agent_turn_counts_only_user_message_items(self):
        rows = [codex_meta(), event("task_started", "T1"), codex_ctx(turn_id="T1"), msg("user", "kick off")]
        for i in range(13):
            rows += [msg("user", f"owner input {i}"), item_user("T1", f"owner input {i}")]
        rows += [msg("user", f"<agent_message>inter-agent {i}</agent_message>") for i in range(100)]
        rows += [call("exec", input="tools.multi_agent_v1__spawn_agent({a:1}); tools.multi_agent_v1__spawn_agent({b:2}); spawn_agent({})"),
                 call("spawn_agent", arguments="{}"), msg("assistant", "done"), codex_tokens(1)]
        path = write_jsonl(self.tmp / "rollout-m.jsonl", rows)
        got = turn_context("codex", [str(path)], "cs1", "T1", codex_index=self.tmp / "none")
        self.assertEqual(got["inputs"]["count"], 14)
        self.assertEqual(got["inputs"]["first"], "kick off")
        self.assertEqual(got["inputs"]["followups"], [f"owner input {i}" for i in range(5)])
        self.assertEqual(got["activity"]["subagents"], 4)

    def test_initiating_input_that_is_an_item_is_not_added_twice(self):
        path = write_jsonl(self.tmp / "rollout-n.jsonl", [
            codex_meta(), codex_ctx(turn_id="T1"), msg("user", "first"), item_user("T1", "first"), item_user("T1", "second")])
        got = turn_context("codex", [str(path)], "cs1", "T1", codex_index=self.tmp / "none")
        self.assertEqual((got["inputs"]["count"], got["inputs"]["followups"]), (2, ["second"]))

    def test_legacy_ids_and_unknown(self):
        path = write_jsonl(self.tmp / "rollout-y.jsonl", [
            codex_meta(), codex_user("one", id="e1"), codex_ctx(), msg("assistant", "a1"), codex_tokens(1),
            codex_user("two", id="e2"), codex_ctx(), codex_tokens(2)])
        got = turn_context("codex", [str(path)], "cs1", "e1", codex_index=self.tmp / "none")
        self.assertEqual((got["inputs"]["count"], got["inputs"]["first"], got["final"], got["title"]), (1, "one", "a1", None))
        self.assertEqual(turn_context("codex", [str(path)], "cs1", "zz", codex_index=self.tmp / "none"), UNKNOWN)

    def test_turn_without_any_input_has_unknown_count(self):
        path = write_jsonl(self.tmp / "rollout-z.jsonl", [codex_meta(), codex_ctx(turn_id="T9"), codex_tokens(1)])
        got = turn_context("codex", [str(path)], "cs1", "T9", codex_index=self.tmp / "none")
        self.assertEqual((got["inputs"]["count"], got["inputs"]["first"]), (None, None))


def make_db(path):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT, title TEXT,
            directory TEXT NOT NULL, version TEXT NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, data TEXT NOT NULL);
    """)
    ms = int(datetime(2026, 9, 3, 9, tzinfo=timezone.utc).timestamp() * 1000)
    c.execute("INSERT INTO session VALUES ('ses1','p',NULL,?, '/w','1',?,?)", (f"My title {KEY}", ms, ms))
    usage = {"tokens": {"input": 5, "output": 3, "cache": {"read": 0, "write": 0}}}
    for mid, role, extra in [("msgU", "user", {}), ("msgA", "assistant", {"parentID": "msgU"}),
                             ("msgB", "assistant", {"parentID": "msgU"}), ("msgU2", "user", {}),
                             ("msgC", "assistant", {"parentID": "msgU2"})]:
        c.execute("INSERT INTO message VALUES (?,?,?,?,?)", (mid, "ses1", ms, ms + 9, json.dumps(
            {"role": role, **extra, **(usage if role == "assistant" else {}), "time": {"created": ms, "completed": ms + 9}})))
    tool = lambda name, **inp: {"type": "tool", "tool": name, "state": {"input": inp}}
    for i, (mid, data) in enumerate([
            ("msgU", {"type": "text", "text": f"build it {KEY}"}),
            ("msgA", tool("bash", command="gh pr create")), ("msgA", tool("apply_patch")), ("msgA", {"type": "patch"}),
            ("msgA", tool("webfetch")), ("msgA", {"type": "text", "text": "first reply"}),
            ("msgB", tool("task")), ("msgB", tool("bash", command="ls")), ("msgB", {"type": "text", "text": "See /pull/77"}),
            ("msgU2", {"type": "text", "text": "other"}), ("msgC", {"type": "text", "text": "elsewhere"})]):
        c.execute("INSERT INTO part VALUES (?,?,?,?,?)", (f"p{i}", mid, "ses1", ms + i, json.dumps(data)))
    c.commit()
    c.close()


class OpenCodeTests(TmpCase):
    def test_full_and_missing(self):
        db = self.tmp / "oc.db"
        make_db(db)
        self.assertEqual({r.turn_id for r in why.collect_opencode(db, START, END)}, {"msgU", "msgU2"})
        got = turn_context("opencode", [str(db)], "ses1", "msgU")
        self.assertEqual((got["title"], got["title_source"], got["cwd"]), ("My title [redacted]", "session.title", "/w"))
        self.assertEqual((got["branch"], got["repository"]), (None, None))
        self.assertEqual(got["inputs"], {"count": 1, "first": "build it [redacted]", "followups": []})
        self.assertEqual(got["final"], "See /pull/77")
        self.assertEqual(got["activity"], {"shell": 2, "edits": 1, "web": 1, "subagents": 1})
        self.assertEqual(got["outcomes"]["prs"], ["created", "#77"])
        self.assertEqual(turn_context("opencode", [str(db)], "ses1", "msgU2")["final"], "elsewhere")
        self.assertEqual(turn_context("opencode", [str(db)], "ses1", "nope"), UNKNOWN)
        self.assertEqual(turn_context("opencode", [str(db)], "other", "msgU"), UNKNOWN)
        for n, bad in enumerate((1e20, -1e20, 1e300)):  # SQLite cannot hold 10**20 as an integer; as a REAL or inside the JSON it can
            db2 = self.tmp / f"x{n}.db"
            make_db(db2)
            c = sqlite3.connect(db2)
            c.execute("UPDATE message SET time_created=?, time_updated=?, data=json_set(data, '$.time.completed', json('100000000000000000000'))", (bad, bad))
            c.commit()
            c.close()
            got = turn_context("opencode", [str(db2)], "ses1", "msgU")  # must not raise
            self.assertEqual((got["title"], got["inputs"]["count"], got["activity"]["shell"]), ("My title [redacted]", 1, 2))
            self.assertEqual(got["outcomes"]["prs"], ["created", "#77"])
        junk = self.tmp / "junk.db"
        junk.write_text("not sqlite")
        self.assertEqual(turn_context("opencode", [str(junk)], "ses1", "msgU"), UNKNOWN)

    def test_patch_part_counts_as_edit_and_old_schema(self):
        db = self.tmp / "old.db"
        c = sqlite3.connect(db)
        c.executescript("""CREATE TABLE session (id TEXT, directory TEXT);
            CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
            INSERT INTO session VALUES ('s','/d');
            INSERT INTO message VALUES ('u','s',1,1,'{"role":"user"}'), ('a','s',2,2,'{"role":"assistant","parentID":"u"}');
            INSERT INTO part VALUES ('p1','u','s',1,'{"type":"text","text":"hi"}'), ('p2','a','s',2,'{"type":"patch"}');""")
        c.commit()
        c.close()
        got = turn_context("opencode", [str(db)], "s", "u")
        self.assertEqual((got["title"], got["cwd"], got["activity"]["edits"]), (None, "/d", 1))


class PiTests(TmpCase):
    def test_full(self):
        def m(i, role, content):
            return {"type": "message", "id": i, "timestamp": TS, "message": {"role": role, "content": content,
                    **({"usage": {"input": 5, "output": 3}} if role == "assistant" else {})}}
        call = lambda name, **a: {"type": "toolCall", "id": name, "name": name, "arguments": a}
        path = write_jsonl(self.tmp / "p.jsonl", [
            {"type": "session", "id": "ps1", "timestamp": TS, "cwd": "/w"},
            {"type": "session_info", "name": "old"}, {"type": "session_info", "name": f"Pi {KEY}"},
            m("e1", "user", f"ask {KEY}"),
            m("e2", "assistant", [{"type": "text", "text": "x"}, call("bash", command="gh pr create"), call("edit"),
                                  call("write"), call("web_search"), call("fetch_content"), call("subagent")]),
            m("e3", "toolResult", [{"type": "text", "text": "out"}]),
            m("e4", "assistant", [{"type": "text", "text": "PR #12 is up"}]),
            m("e5", "user", "later")])
        records = why.collect_pi(self.tmp, START, END, paths=[path])
        self.assertEqual({r.turn_id for r in records}, {"e1"})
        got = turn_context("pi", [str(path)], "ps1", "e1")
        self.assertEqual((got["title"], got["title_source"], got["cwd"]), ("Pi [redacted]", "session_info", "/w"))
        self.assertEqual(got["inputs"], {"count": 1, "first": "ask [redacted]", "followups": []})
        self.assertEqual(got["final"], "PR #12 is up")
        self.assertEqual(got["activity"], {"shell": 1, "edits": 2, "web": 2, "subagents": 1})
        self.assertEqual(got["outcomes"]["prs"], ["created", "#12"])
        self.assertEqual(turn_context("pi", [str(path)], "ps1", "e2"), UNKNOWN)


class GitTests(TmpCase):
    def git(self, *args, when=None):
        env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(self.tmp),
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
        if when:
            env |= {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, env=env)

    def test_window_and_non_repo(self):
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        for subject, when in [("before", "2026-09-03T08:00:00Z"), ("in one", "2026-09-03T09:10:00Z"),
                              (f"in two {KEY}", "2026-09-03T09:20:00Z"), ("after", "2026-09-03T11:00:00Z")]:
            self.git("commit", "-q", "--allow-empty", "-m", subject, when=when)
        self.assertEqual(git_commits(self.repo, "2026-09-03T09:00:00Z", "2026-09-03T10:00:00Z"),
                         ["in two [redacted]", "in one"])
        s, e = (datetime(2026, 9, 3, h, tzinfo=timezone.utc) for h in (9, 10))
        self.assertEqual(len(git_commits(str(self.repo), s, e)), 2)
        self.git("checkout", "-q", "-b", "side")
        self.git("commit", "-q", "--allow-empty", "-m", "on side", when="2026-09-03T09:30:00Z")
        self.git("checkout", "-q", "-")
        self.assertEqual(git_commits(self.repo, "2026-09-03T09:25:00Z", "2026-09-03T09:45:00Z"), ["on side"])
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertEqual(git_commits(plain, s, e), [])
        self.assertEqual(git_commits(self.tmp / "gone", s, e), [])
        self.assertEqual(git_commits(self.repo, None, None), [])

    def test_commits_flow_into_context(self):
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "made it", when="2026-09-03T09:00:00Z")
        path = write_jsonl(self.tmp / "s1.jsonl", [
            claude_row("u1", "user", "hi", cwd=str(self.repo)), claude_row("a1", "assistant", [{"type": "text", "text": "ok"}])])
        got = turn_context("claude", [str(path)], "s1", "u1", start="2026-09-03T08:59:00Z", end="2026-09-03T09:01:00Z")
        self.assertEqual(got["outcomes"]["commits"], ["made it"])

    def test_commit_window_is_the_parent_turn_span_not_the_rollup(self):
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "during", when="2026-09-03T09:00:30Z")
        self.git("commit", "-q", "--allow-empty", "-m", "after parent", when="2026-09-03T09:30:00Z")
        path = write_jsonl(self.tmp / "s1.jsonl", [
            claude_row("u1", "user", "hi", cwd=str(self.repo), timestamp="2026-09-03T09:00:00Z"),
            claude_row("a1", "assistant", [{"type": "text", "text": "ok"}], timestamp="2026-09-03T09:01:00Z")])
        got = turn_context("claude", [str(path)], "s1", "u1", start="2026-09-03T08:59:00Z", end="2026-09-03T10:00:00Z")
        self.assertEqual(got["outcomes"]["commits"], ["during"])
        # no extracted span: the passed window is the fallback
        with patch("tokenatlas.turn_context.git_commits", return_value=[]) as gc:
            from tokenatlas import turn_context as tc
            tc._finish({**tc._raw(), "cwd": str(self.repo)}, 400, "S", "E")
            self.assertEqual(gc.call_args.args[1:], ("S", "E"))

    @unittest.skipIf(os.name == "nt", "marker program is a POSIX shell script; Git for Windows runs programs differently")
    def test_repository_config_cannot_run_programs(self):
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        marker = self.tmp / "marker"
        script = self.tmp / "evil.sh"
        script.write_text(f"#!/bin/sh\necho \"$0 $@\" >> '{marker}'\nexit 1\n")
        script.chmod(0o755)
        for key, value in (("log.showSignature", "true"), ("gpg.program", script), ("gpg.ssh.program", script),
                           ("gpg.x509.program", script), ("core.pager", script), ("pager.log", str(script)),
                           ("diff.external", script), ("core.alternateRefsCommand", script)):
            self.git("config", key, str(value))
        # commit object carrying a (fake) signature header, so `git log --show-signature` would call gpg
        tree = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-t", "tree", "-w", "--stdin"], input=b"",
                              capture_output=True, check=True).stdout.decode().strip()
        body = (f"tree {tree}\nauthor t <t@x> 1788424200 +0000\ncommitter t <t@x> 1788424200 +0000\n"
                "gpgsig -----BEGIN PGP SIGNATURE-----\n \n abcd\n -----END PGP SIGNATURE-----\n\nsigned subject\n")
        sha = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-t", "commit", "-w", "--stdin"],
                             input=body.encode(), capture_output=True, check=True).stdout.decode().strip()
        self.git("update-ref", "refs/heads/master", sha)  # 2026-09-03T08:30:00Z
        window = ("2026-09-03T08:00:00Z", "2026-09-03T09:00:00Z")
        # positive control: the same query without our protections (repository log.showSignature applies) runs gpg.program
        subprocess.run(["git", "-C", str(self.repo), "log", "--all", f"--since={window[0]}",
                        f"--until={window[1]}", "--format=%s", "-n", "5"], capture_output=True,
                       env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(self.tmp)})
        self.assertTrue(marker.exists(), "test premise: unprotected git runs gpg.program")
        marker.unlink()
        self.assertEqual(git_commits(self.repo, *window), ["signed subject"])
        self.assertFalse(marker.exists(), "repository-configured program ran")
        self.assertEqual(git_commits(self.repo, "2026-09-03T09:00:00Z", "2026-09-03T10:00:00Z"), [])
        self.assertFalse(marker.exists())

    @unittest.skipIf(os.name == "nt", "marker program is a POSIX shell script")
    def test_repository_config_cannot_enable_ext_transport(self):
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        marker = self.tmp / "marker"
        script = self.tmp / "evil.sh"
        script.write_text(f"#!/bin/sh\necho run >> '{marker}'\nexit 1\n")
        script.chmod(0o755)
        self.git("config", "protocol.ext.allow", "always")
        url = f"ext::{script}"
        base = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(self.tmp)}
        run = lambda cfg, env: subprocess.run(["git", *cfg, "-C", str(self.repo), "ls-remote", url],
                                              capture_output=True, env=env, timeout=10)
        run([], base)  # control: repository config alone lets git run the program
        self.assertTrue(marker.exists(), "test premise: protocol.ext.allow=always runs the program")
        marker.unlink()
        env = tc._git_env()
        self.assertEqual(env.get("GIT_ALLOW_PROTOCOL"), "")
        done = run(tc._GIT_SAFE_CONFIG, {**base, **env})
        self.assertFalse(marker.exists(), "ext:: program ran")
        self.assertNotEqual(done.returncode, 0)

    def test_git_env_disables_lazy_fetch(self):
        with patch("tokenatlas.turn_context.subprocess.run") as run:
            run.return_value.returncode, run.return_value.stdout = 0, ""
            git_commits(self.tmp, "2026-09-03T09:00:00Z", "2026-09-03T10:00:00Z")
        env = run.call_args.kwargs["env"]
        self.assertEqual(env.get("GIT_NO_LAZY_FETCH"), "1")
        self.assertEqual((env["GIT_TERMINAL_PROMPT"], env["GIT_OPTIONAL_LOCKS"]), ("0", "0"))

    def test_location_fields_are_sanitized_but_git_uses_the_raw_cwd(self):
        self.repo = self.tmp / f"{KEY}"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "made it", when="2026-09-03T09:00:00Z")
        path = write_jsonl(self.tmp / "s1.jsonl", [
            claude_row("u1", "user", "hi", cwd=str(self.repo), gitBranch=f"feat/{KEY}"),
            claude_row("a1", "assistant", [{"type": "text", "text": "ok"}])])
        got = turn_context("claude", [str(path)], "s1", "u1", start="2026-09-03T08:59:00Z", end="2026-09-03T09:01:00Z")
        self.assertEqual(got["outcomes"]["commits"], ["made it"])
        for field in ("cwd", "branch"):
            self.assertNotIn(KEY, got[field])
            self.assertIn("[redacted]", got[field])
        from tokenatlas import turn_context as tc
        out = tc._finish({**tc._raw(), "repo": f"https://example.test/o/{KEY}.git"}, 400, None, None)
        self.assertNotIn(KEY, out["repository"])


if __name__ == "__main__":
    unittest.main()
