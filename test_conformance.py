#!/usr/bin/env python3
"""Accounting conformance cases (TOKEN-29).

Each test pins one accounting hazard from the AgentMeasure audit (roy-tong/AgentMeasure, MIT:
campaigns/audit-report-2026-09.md, campaigns/measurement-casebook.md) against the importers of the four harnesses
TokenAtlas reads. The test name carries the case id; the docstring names its source. All data is synthetic.
The coverage table (covered / not applicable / known deviation) lives in docs/accounting-conformance.md.

Token convention checked throughout: fresh_input + cache_read + cache_write + output is the total of a call;
reasoning is a subset of output and is never added again.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas import why
from test_why_harnesses import create_opencode_db, pi_message, write_pi_session

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 30, tzinfo=timezone.utc)


def total(records):
    return sum(r.fresh_input + r.cache_read + r.cache_write + r.output for r in records)


def jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def claude_row(request, block, *, usage=None, stop="end_turn", session="s1", ts="2026-09-03T10:00:00Z", **extra):
    usage = usage if usage is not None else {"input_tokens": 10, "cache_read_input_tokens": 20,
                                             "cache_creation_input_tokens": 5, "output_tokens": 50}
    message = {"id": "msg-" + request, "model": "test-model", "role": "assistant", "usage": usage,
               "content": [{"type": "text", "text": f"block {block}"}]}
    if stop is not None:
        message["stop_reason"] = stop
    return {"type": "assistant", "uuid": f"{request}-{block}", "requestId": request, "sessionId": session,
            "cwd": "/work/app", "timestamp": ts, "message": message, **extra}


class ClaudeCases(unittest.TestCase):
    def collect(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, rows in files.items():
                jsonl(root / name, rows)
            return why.collect_claude(root, START, END)

    def test_claude_one_line_per_content_block_is_one_call(self):
        """Audit class 1 'one assistant message is not one line' (per-block re-summation, 2x-5x inflation)."""
        rows = [claude_row("req-1", block) for block in range(3)]
        records = self.collect({"proj/s1.jsonl": rows})
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0].fresh_input, records[0].cache_read, records[0].cache_write, records[0].output),
                         (10, 20, 5, 50))
        self.assertEqual(total(records), 85)  # not 3 x 85

    def test_claude_streamed_partial_rows_keep_the_final_output(self):
        """Audit class 1 variant: early rows carry placeholder output counts; the final row settles (first-wins undercounts)."""
        partial = {"input_tokens": 10, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5, "output_tokens": 1}
        rows = [claude_row("req-1", 0, usage=partial, stop=None),
                claude_row("req-1", 1, stop="end_turn")]
        records = self.collect({"proj/s1.jsonl": rows})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].output, 50)

    def test_claude_output_without_a_stop_reason_is_not_final(self):
        """TokenAtlas hazard (`output_not_final`, not an AgentMeasure case): a request that never reached a stop reason is a lower bound, not a settled count."""
        rows = [claude_row("req-1", 0, stop=None)]
        records = self.collect({"proj/s1.jsonl": rows})
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].output_final)
        settled = self.collect({"proj/s1.jsonl": [claude_row("req-1", 0, stop="end_turn")]})
        self.assertTrue(settled[0].output_final)

    def test_claude_resumed_or_forked_session_copies_history_once(self):
        """Audit class 5 resume/fork: a resumed transcript replays the parent's rows; the same request in two files counts once."""
        parent = [claude_row("req-1", 0, session="orig"), claude_row("req-2", 0, session="orig", ts="2026-09-03T10:05:00Z")]
        resumed = [claude_row("req-1", 0, session="orig"), claude_row("req-2", 0, session="orig", ts="2026-09-03T10:05:00Z"),
                   claude_row("req-3", 0, session="orig", ts="2026-09-03T10:10:00Z")]
        records = self.collect({"proj/orig.jsonl": parent, "proj/resumed.jsonl": resumed})
        self.assertEqual(sorted(r.call_id for r in records), ["req-1", "req-2", "req-3"])
        self.assertEqual(total(records), 3 * 85)

    def test_claude_resume_does_not_drop_history_before_the_resume(self):
        """Audit class 5: a replace-per-session store loses everything before the resume. Both files' own requests survive."""
        old = [claude_row("req-old", 0, session="orig")]
        resumed = [claude_row("req-new", 0, session="orig", ts="2026-09-04T10:00:00Z")]
        records = self.collect({"proj/orig.jsonl": old, "proj/resumed.jsonl": resumed})
        self.assertEqual(sorted(r.call_id for r in records), ["req-new", "req-old"])

    def test_claude_subagent_transcript_is_separate_from_its_parent(self):
        """Subagent hazard (TokenAtlas-derived). Own requests count once, as subagent calls under the parent session."""
        parent = [claude_row("req-main", 0, session="parent")]
        child = [claude_row("req-child", 0, session="parent", agentId="a1")]
        records = self.collect({"proj/parent.jsonl": parent, "proj/parent/subagents/agent-a1.jsonl": child})
        by_id = {r.call_id: r for r in records}
        self.assertEqual(set(by_id), {"req-main", "req-child"})
        self.assertEqual(by_id["req-child"].thread_kind, "subagent")
        self.assertEqual(by_id["req-main"].thread_kind, "main")
        self.assertEqual(total(records), 2 * 85)

    def test_claude_subagent_rows_copied_into_the_parent_count_once(self):
        """Subagent hazard (TokenAtlas-derived). A request copied into both files is one call."""
        row = claude_row("req-child", 0, session="parent", agentId="a1")
        records = self.collect({"proj/parent.jsonl": [row], "proj/parent/subagents/agent-a1.jsonl": [row]})
        self.assertEqual(len(records), 1)
        self.assertEqual(total(records), 85)

    def test_claude_input_excludes_cache_and_cache_classes_are_separate(self):
        """Audit class 3: Anthropic input_tokens excludes cache; cache read and write are their own classes, never folded into input."""
        usage = {"input_tokens": 7, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 300, "output_tokens": 4}
        records = self.collect({"proj/s1.jsonl": [claude_row("req-1", 0, usage=usage)]})
        r = records[0]
        self.assertEqual((r.fresh_input, r.cache_read, r.cache_write, r.output), (7, 1000, 300, 4))
        self.assertEqual(total(records), 1311)

    def test_claude_cache_write_ttl_split_sums_without_double_counting(self):
        """Audit class 3: 5-minute and 1-hour cache writes are both cache writes; the split must sum to the total, not add to it."""
        usage = {"input_tokens": 1, "output_tokens": 2,
                 "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 40}}
        records = self.collect({"proj/s1.jsonl": [claude_row("req-1", 0, usage=usage)]})
        self.assertEqual(records[0].cache_write, 140)
        both = {**usage, "cache_creation_input_tokens": 140}
        records = self.collect({"proj/s1.jsonl": [claude_row("req-1", 0, usage=both)]})
        self.assertEqual(records[0].cache_write, 140)  # the total field and its split describe the same tokens

    def test_claude_thinking_tokens_are_a_subset_of_output(self):
        """Casebook 'exposing reasoning tokens without adding them twice' (output 1000, reasoning 700 stays 1000)."""
        usage = {"input_tokens": 1, "output_tokens": 1000, "output_tokens_details": {"thinking_tokens": 700}}
        records = self.collect({"proj/s1.jsonl": [claude_row("req-1", 0, usage=usage)]})
        self.assertEqual((records[0].output, records[0].reasoning), (1000, 700))
        self.assertEqual(total(records), 1001)

    def test_claude_failed_retry_without_usage_adds_nothing(self):
        """Retry hazard (TokenAtlas-derived): a failed attempt that logged no usage is not a billed call; the successful retry counts once."""
        failed = {"type": "assistant", "uuid": "e1", "requestId": "req-fail", "sessionId": "s1", "cwd": "/work/app",
                  "timestamp": "2026-09-03T09:59:00Z", "isApiErrorMessage": True,
                  "message": {"id": "msg-fail", "model": "<synthetic>", "role": "assistant",
                              "content": [{"type": "text", "text": "API Error"}]}}
        records = self.collect({"proj/s1.jsonl": [failed, claude_row("req-ok", 0)]})
        self.assertEqual([r.call_id for r in records], ["req-ok"])


def codex_meta(session="codex-1"):
    return {"timestamp": "2026-09-03T08:00:00Z", "type": "session_meta",
            "payload": {"id": session, "model_provider": "openai", "source": "cli", "originator": "codex-tui", "cwd": "/work/app"}}


def codex_tokens(ts, ordinal, last, cumulative):
    return {"timestamp": ts, "ordinal": ordinal, "type": "event_msg",
            "payload": {"type": "token_count", "info": {"last_token_usage": last, "total_token_usage": cumulative}}}


class CodexCases(unittest.TestCase):
    CALL1 = {"input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 80, "reasoning_output_tokens": 50}
    CALL2 = {"input_tokens": 200, "cached_input_tokens": 50, "output_tokens": 20, "reasoning_output_tokens": 10}
    CUM1 = CALL1
    CUM2 = {"input_tokens": 1200, "cached_input_tokens": 650, "output_tokens": 100, "reasoning_output_tokens": 60}

    def collect(self, rows, extra=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl(root / "rollout-a.jsonl", rows)
            for name, other in (extra or {}).items():
                jsonl(root / name, other)
            return why.collect_codex(root, START, END)

    def test_codex_reemitted_token_count_with_identical_totals_counts_once(self):
        """Audit class 2: Codex re-emits token_count after compaction, settings or rate-limit refreshes with identical totals."""
        rows = [codex_meta(),
                codex_tokens("2026-09-03T09:01:00Z", 1, self.CALL1, self.CUM1),
                codex_tokens("2026-09-03T09:01:05Z", 2, self.CALL1, self.CUM1),  # settings refresh: same totals
                codex_tokens("2026-09-03T09:02:00Z", 3, self.CALL2, self.CUM2),
                codex_tokens("2026-09-03T09:02:10Z", 4, self.CALL2, self.CUM2)]  # rate-limit refresh: same totals
        records = self.collect(rows)
        self.assertEqual(len(records), 2)
        self.assertEqual(sum(r.output for r in records), 100)  # the final cumulative total, not 200

    def test_codex_per_call_usage_reconciles_with_the_final_cumulative_total(self):
        """Audit class 2: summed per-call deltas must equal the rollout's final cumulative counter."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-r.jsonl"
            jsonl(path, [codex_meta(),
                         codex_tokens("2026-09-03T09:01:00Z", 1, self.CALL1, self.CUM1),
                         codex_tokens("2026-09-03T09:01:05Z", 2, self.CALL1, self.CUM1),
                         codex_tokens("2026-09-03T09:02:00Z", 3, self.CALL2, self.CUM2)])
            result = why.reconcile_codex_rollout(path)
        self.assertTrue(result["matches"])
        self.assertEqual(result["calls"], 2)
        records = self.collect([codex_meta(), codex_tokens("2026-09-03T09:01:00Z", 1, self.CALL1, self.CUM1),
                                codex_tokens("2026-09-03T09:01:05Z", 2, self.CALL1, self.CUM1),
                                codex_tokens("2026-09-03T09:02:00Z", 3, self.CALL2, self.CUM2)])
        self.assertEqual(sum(r.fresh_input + r.cache_read for r in records), self.CUM2["input_tokens"])
        self.assertEqual(sum(r.output for r in records), self.CUM2["output_tokens"])

    def test_codex_cached_input_is_a_subset_of_input(self):
        """Audit class 3: OpenAI input_tokens includes cached tokens, so fresh input is input minus cache (never double-charged)."""
        records = self.collect([codex_meta(), codex_tokens("2026-09-03T09:01:00Z", 1, self.CALL1, self.CUM1)])
        r = records[0]
        self.assertEqual((r.fresh_input, r.cache_read), (400, 600))
        self.assertEqual(r.fresh_input + r.cache_read, 1000)

    def test_codex_reasoning_is_a_subset_of_output(self):
        """Casebook reasoning-as-subset: output 80 with reasoning 50 is 80 output tokens, not 130."""
        records = self.collect([codex_meta(), codex_tokens("2026-09-03T09:01:00Z", 1, self.CALL1, self.CUM1)])
        self.assertEqual((records[0].output, records[0].reasoning), (80, 50))
        self.assertEqual(total(records), 1080)

    def test_codex_lower_counter_after_resume_adds_instead_of_subtracting(self):
        """Audit class 5: a resumed rollout restarts its cumulative counter; the lower counter must add, not subtract or be dropped."""
        small = {"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0}
        rows = [codex_meta(),
                codex_tokens("2026-09-03T09:01:00Z", 1, self.CALL1, self.CUM1),
                codex_tokens("2026-09-03T10:01:00Z", 2, small, small)]
        records = self.collect(rows)
        self.assertEqual(len(records), 2)
        self.assertEqual(total(records), 1080 + 55)

    def test_codex_resumed_rollout_replaying_the_prefix_counts_once(self):
        """Audit class 5: resume writes a new rollout with the original session id and replays earlier events; a replayed call is one call."""
        first = [codex_meta(), codex_tokens("2026-09-03T09:01:00Z", 1, self.CALL1, self.CUM1)]
        resumed = [codex_meta(), codex_tokens("2026-09-03T09:01:00Z", 1, self.CALL1, self.CUM1),
                   codex_tokens("2026-09-03T09:02:00Z", 2, self.CALL2, self.CUM2)]
        records = self.collect(first, extra={"rollout-b.jsonl": resumed})
        self.assertEqual(len(records), 2)
        self.assertEqual(total(records), 1080 + 220)

    @unittest.expectedFailure
    def test_codex_fork_under_a_new_session_id_counts_the_replayed_prefix_once(self):
        """Audit class 5 fork, KNOWN DEVIATION (unverified against a real rollout): if a fork writes a new session id and replays the parent's
        token_count events, the call id (prefixed with the session id) differs and the prefix is counted again."""
        first = [codex_meta("codex-1"), codex_tokens("2026-09-03T09:01:00Z", 1, self.CALL1, self.CUM1)]
        fork = [codex_meta("codex-2"), codex_tokens("2026-09-03T09:01:00Z", 1, self.CALL1, self.CUM1),
                codex_tokens("2026-09-03T09:02:00Z", 2, self.CALL2, self.CUM2)]
        records = self.collect(first, extra={"rollout-b.jsonl": fork})
        self.assertEqual(total(records), 1080 + 220)

    def test_codex_event_with_zero_counters_adds_no_tokens(self):
        """A zero-token event (one counter key absent, so the zero is not fully explicit) is kept as a request and adds nothing to a total."""
        zero = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
        records = self.collect([codex_meta(), codex_tokens("2026-09-03T09:01:00Z", 1, zero, zero)])
        self.assertEqual(len(records), 1)
        self.assertEqual(total(records), 0)


class OpenCodeCases(unittest.TestCase):
    def collect(self, build):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.db"
            con = create_opencode_db(path)
            build(con)
            con.commit()
            con.close()
            return why.collect_opencode(path, START, END)

    @staticmethod
    def add(con, message_id, tokens, *, error=None, ms=1788436800000, session="ses-1"):
        con.execute("INSERT OR IGNORE INTO session VALUES (?,?,?,?,?,?,?)",
                    (session, "proj", None, "/work/app", "1.0", ms, ms))
        data = {"role": "assistant", "modelID": "m", "providerID": "p", "time": {"created": ms, "completed": ms}}
        if tokens is not None:
            data["tokens"] = tokens
        if error:
            data["error"] = error
        con.execute("INSERT INTO message VALUES (?,?,?,?,?)", (message_id, session, ms, ms, json.dumps(data)))

    def test_opencode_reasoning_is_reported_as_a_subset_of_output(self):
        """Casebook reasoning-as-subset. why.collect_opencode treats OpenCode's tokens.output as excluding reasoning (its own stats add them),
        so output reported = output + reasoning, counted once. That premise comes from the importer's documented reading of OpenCode, not from a
        fresh real-database check."""
        tokens = {"input": 10, "output": 5, "reasoning": 3, "cache": {"read": 7, "write": 2}}
        records = self.collect(lambda con: self.add(con, "m1", tokens))
        r = records[0]
        self.assertEqual((r.fresh_input, r.cache_read, r.cache_write, r.output, r.reasoning), (10, 7, 2, 8, 3))
        self.assertEqual(total(records), 27)  # 10 + 7 + 2 + 8; reasoning is not added again

    def test_opencode_failed_request_without_tokens_adds_nothing(self):
        """Retry hazard (TokenAtlas-derived): a provider error or abort that logged no tokens is not a billed call."""
        def build(con):
            self.add(con, "m1", None, error={"name": "ProviderAuthError"})
            self.add(con, "m2", {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}})
        records = self.collect(build)
        self.assertEqual([r.call_id for r in records], ["m2"])

    def test_opencode_message_present_in_both_generations_counts_once(self):
        """Migration hazard (TokenAtlas-derived, resembling class 5): a message id in both the legacy and the V2 table is one call."""
        tokens = {"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}}

        def build(con):
            self.add(con, "m1", tokens)
            con.executescript("""
                CREATE TABLE session_v2 (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, directory TEXT, version TEXT,
                                         time_created INTEGER, time_updated INTEGER);
                CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, type TEXT,
                                              time_created INTEGER, time_updated INTEGER, data TEXT);
            """)
            data = {"role": "assistant", "modelID": "m", "providerID": "p", "tokens": tokens,
                    "time": {"created": 1788436800000, "completed": 1788436800000}}
            con.execute("INSERT INTO session_v2 VALUES ('ses-1','proj',NULL,'/work/app','1.0',1,1)")
            con.execute("INSERT INTO session_message VALUES ('m1','ses-1',1,'assistant',1788436800000,1788436800000,?)", (json.dumps(data),))
        records = self.collect(build)
        self.assertEqual(len(records), 1)
        self.assertEqual(total(records), 15)


class PiCases(unittest.TestCase):
    def collect(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, (session, messages) in files.items():
                write_pi_session(root / name, session, "2026-09-03T09:00:00Z", "/work/app", messages)
            return why.collect_pi(root, START, END)

    USAGE = {"input": 10, "output": 5, "cacheRead": 20, "cacheWrite": 2, "reasoning": 3}

    def test_pi_forked_session_replaying_the_parent_counts_once(self):
        """Audit class 5 fork: a fork re-plays the parent's prefix; the same response id in two session files is one call."""
        shared = pi_message("e1", "resp-1", "2026-09-03T10:00:00Z", self.USAGE)
        fork_only = pi_message("e2", "resp-2", "2026-09-03T10:05:00Z", self.USAGE)
        records = self.collect({"parent.jsonl": ("parent", [shared]), "fork.jsonl": ("fork", [shared, fork_only])})
        self.assertEqual(sorted(r.call_id for r in records), ["resp-1", "resp-2"])
        self.assertEqual(total(records), 2 * 37)

    def test_pi_cache_classes_are_separate_from_input(self):
        """Audit class 3: cache read and write are their own classes and are never folded into input."""
        records = self.collect({"s.jsonl": ("s", [pi_message("e1", "resp-1", "2026-09-03T10:00:00Z", self.USAGE)])})
        r = records[0]
        self.assertEqual((r.fresh_input, r.cache_read, r.cache_write, r.output), (10, 20, 2, 5))

    def test_pi_streamed_duplicate_entries_keep_the_final_counts(self):
        """Audit class 1 variant: the same response written twice (partial then final) is one call with the larger counts."""
        partial = {**self.USAGE, "output": 1}
        rows = [pi_message("e1", "resp-1", "2026-09-03T10:00:00Z", partial),
                pi_message("e2", "resp-1", "2026-09-03T10:00:05Z", self.USAGE)]
        records = self.collect({"s.jsonl": ("s", rows)})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].output, 5)

    def test_pi_response_without_usage_adds_nothing(self):
        """Retry hazard (TokenAtlas-derived): an assistant entry without usage is not a billed call."""
        failed = pi_message("e0", "resp-0", "2026-09-03T09:59:00Z", None)
        failed["message"].pop("usage")
        ok = pi_message("e1", "resp-1", "2026-09-03T10:00:00Z", self.USAGE)
        records = self.collect({"s.jsonl": ("s", [failed, ok])})
        self.assertEqual([r.call_id for r in records], ["resp-1"])


if __name__ == "__main__":
    unittest.main()
