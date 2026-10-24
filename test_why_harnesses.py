#!/usr/bin/env python3
"""Regression tests for Pi and OpenCode attribution adapters."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas import why


START = datetime(2026, 9, 3, tzinfo=timezone.utc)
END = datetime(2026, 9, 4, tzinfo=timezone.utc)


def write_pi_session(path, session_id, started_at, cwd, messages):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{
        "type": "session", "version": 3, "id": session_id,
        "timestamp": started_at, "cwd": cwd,
    }]
    rows.extend(messages)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def pi_message(entry_id, response_id, timestamp, usage, provider="test", model="model"):
    return {
        "type": "message", "id": entry_id, "timestamp": timestamp,
        "message": {
            "role": "assistant", "provider": provider, "model": model,
            "responseId": response_id, "usage": usage,
            "content": "PRIVATE ASSISTANT TEXT",
        },
    }


BERGET_ENVIRONMENT = {
    "co2e_grams": 0.42,
    "energy_kwh": 0.0012,
    "operational": {"co2e_grams": 0.31, "energy_kwh": 0.0012},
    "embodied": {"co2e_grams": 0.11},
    "grid": {"region": "SE3", "carbon_intensity_gco2e_per_kwh": 258.0},
    "methodology": "Berget AI CO2 Calculator",
    "methodology_version": "1.0.0",
}


def create_opencode_db(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
            directory TEXT NOT NULL, version TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
    """)
    return connection


class PiAttributionTests(unittest.TestCase):
    def test_extracts_only_complete_berget_emissions_and_deduplicates_it(self):
        usage = {"input": 1, "cacheRead": 0, "cacheWrite": 0, "output": 2,
                 "emissions": BERGET_ENVIRONMENT, "secret": "DROP"}
        partial = pi_message("entry-partial", "response", "2026-09-03T09:59:59Z",
                             {"input": 1, "cacheRead": 0, "cacheWrite": 0, "output": 1}, provider="berget")
        copied = pi_message("entry", "response", "2026-09-03T10:00:00Z", usage, provider="berget")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_pi_session(root / "a.jsonl", "session", "2026-09-03T09:00:00Z", "/work/app", [partial, copied])
            records = why.collect_pi(root, START, END)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].output, 2)
        self.assertEqual(records[0].environment, {**BERGET_ENVIRONMENT, "source": "provider_reported_estimate"})
        self.assertNotIn("emissions", records[0].raw_usage)

    def test_rejects_partial_or_non_berget_emissions_without_dropping_tokens(self):
        for provider, emissions in (("berget", {**BERGET_ENVIRONMENT, "grid": {"region": "SE3"}}),
                                    ("openai", BERGET_ENVIRONMENT)):
            with self.subTest(provider=provider):
                usage = {"input": 1, "cacheRead": 0, "cacheWrite": 0, "output": 2, "emissions": emissions}
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    write_pi_session(root / "s.jsonl", "session", "2026-09-03T09:00:00Z", "/work/app",
                                     [pi_message("entry", "response", "2026-09-03T10:00:00Z", usage, provider=provider)])
                    records = why.collect_pi(root, START, END)
                self.assertEqual(len(records), 1)
                self.assertIsNone(records[0].environment)
                self.assertEqual(records[0].output, 2)

    def test_maps_usage_and_keeps_original_session_when_a_fork_sorts_first(self):
        usage = {
            "input": 10, "cacheRead": 20, "cacheWrite": 3,
            "output": 8, "reasoning": 2, "totalTokens": 41,
            "secret": "DROP",
        }
        copied = pi_message("entry", "response", "2026-09-03T10:00:00Z", usage)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_pi_session(root / "a-fork.jsonl", "fork", "2026-09-03T09:00:00Z", "/work/fork", [copied])
            write_pi_session(root / "z-original.jsonl", "original", "2026-09-02T09:00:00Z", "/work/original", [copied])
            records = why.collect_pi(root, START, END)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.session_id, "original")
        self.assertEqual(record.project_id, "/work/original")
        self.assertEqual((record.fresh_input, record.cache_read, record.cache_write), (10, 20, 3))
        self.assertEqual((record.output, record.reasoning, record.total_tokens), (8, 2, 41))
        self.assertEqual(record.raw_usage, {
            "input": 10, "cacheRead": 20, "cacheWrite": 3,
            "output": 8, "reasoning": 2, "totalTokens": 41,
        })
        self.assertNotIn("PRIVATE", json.dumps(record.raw_usage))

    def test_response_identity_is_provider_scoped_and_fallback_is_synthetic(self):
        usage = {"input": 1, "cacheRead": 0, "cacheWrite": 0, "output": 1}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            messages = [
                pi_message("one", "same", "2026-09-03T10:00:00Z", usage, provider="a"),
                pi_message("two", "same", "2026-09-03T10:01:00Z", usage, provider="b"),
                pi_message("fallback", "", "2026-09-03T10:02:00Z", usage, provider="a"),
            ]
            write_pi_session(root / "session.jsonl", "session", "2026-09-03T09:00:00Z", "/work/app", messages)
            records = why.collect_pi(root, START, END)

        self.assertEqual(len(records), 3)
        self.assertEqual({r.provider for r in records if r.call_id == "same"}, {"a", "b"})
        self.assertEqual(sum(r.id_synthetic for r in records), 1)


class OpenCodeAttributionTests(unittest.TestCase):
    def test_reads_completed_assistant_calls_without_retaining_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            connection = create_opencode_db(db)
            connection.execute(
                "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
                ("child", "project", "parent", "/work/app", "1.18.32", 1, 2),
            )
            data = {
                "role": "assistant", "providerID": "openai", "modelID": "gpt-test",
                "agent": "explore", "variant": "high",
                "time": {"created": 1788429600000, "completed": 1788429601000},
                "path": {"cwd": "/work/app", "root": "/work"},
                "tokens": {
                    "input": 10, "output": 5, "reasoning": 3,
                    "cache": {"read": 20, "write": 2}, "total": 37,
                },
                "content": "PRIVATE ASSISTANT TEXT",
            }
            connection.execute(
                "INSERT INTO message VALUES (?,?,?,?,?)",
                ("message-1", "child", 1788429600000, 1788429601000, json.dumps(data)),
            )
            connection.commit()
            connection.close()

            records = why.collect_opencode(db, START, END)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.provider, "openai")
        self.assertEqual(record.model, "gpt-test")
        self.assertEqual(record.effort, "high")
        self.assertEqual(record.agent, "explore")
        self.assertEqual(record.thread_kind, "subagent")
        self.assertEqual(record.parent_session_id, "parent")
        self.assertEqual(record.harness_version, "1.18.32")
        self.assertEqual(record.project_id, "/work/app")
        self.assertEqual((record.fresh_input, record.cache_read, record.cache_write), (10, 20, 2))
        self.assertEqual(record.output, 8)
        self.assertEqual(record.reasoning, 3)
        self.assertEqual(record.total_tokens, 40)
        self.assertNotIn("PRIVATE", json.dumps(record.raw_usage))



class InterruptedFlagTests(unittest.TestCase):
    U = {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0}

    @staticmethod
    def user(entry_id, ts):
        return {"type": "message", "id": entry_id, "timestamp": ts, "message": {"role": "user", "content": "go"}}

    def collect_pi(self, messages):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            write_pi_session(path, "s", "2026-09-03T10:00:00Z", "/work/app", messages)
            return {r.call_id: r.flags for r in why.collect_pi(Path(tmp), START, END)}

    def test_pi_aborted_assistant_message_flags_its_record(self):
        aborted = pi_message("e2", "r1", "2026-09-03T10:00:02Z", self.U)
        aborted["message"]["stopReason"] = "aborted"
        ok = pi_message("e4", "r2", "2026-09-03T10:00:05Z", self.U)
        ok["message"]["stopReason"] = "stop"
        got = self.collect_pi([self.user("e1", "2026-09-03T10:00:01Z"), aborted,
                               self.user("e3", "2026-09-03T10:00:04Z"), ok])
        self.assertEqual(got, {"r1": ["interrupted"], "r2": None})

    def test_pi_aborted_without_usage_flags_the_last_record_of_its_turn(self):
        first = pi_message("e2", "r1", "2026-09-03T10:00:02Z", self.U)
        aborted = {"type": "message", "id": "e3", "timestamp": "2026-09-03T10:00:03Z",
                   "message": {"role": "assistant", "stopReason": "aborted", "content": ""}}
        got = self.collect_pi([self.user("e1", "2026-09-03T10:00:01Z"), first, aborted])
        self.assertEqual(got, {"r1": ["interrupted"]})

    def test_opencode_message_aborted_error_flags_the_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            connection = create_opencode_db(db)
            connection.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)", ("s", "p", None, "/work/app", "1", 1, 2))
            for name, error in (("m1", {"name": "MessageAbortedError", "data": {"message": "x"}}),
                                ("m2", {"name": "APIError"}), ("m3", None)):
                data = {"role": "assistant", "providerID": "openai", "modelID": "m",
                        "time": {"created": 1788429600000, "completed": 1788429601000},
                        "tokens": {"input": 1, "output": 2, "cache": {"read": 0, "write": 0}}}
                if error:
                    data["error"] = error
                connection.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                                   (name, "s", 1788429600000, 1788429601000, json.dumps(data)))
            connection.commit()
            connection.close()
            got = {r.call_id: r.flags for r in why.collect_opencode(db, START, END)}
        self.assertEqual(got, {"m1": ["interrupted"], "m2": None, "m3": None})


    def _opencode(self, entries):
        """entries: (id, turn parentID, completed ms, tokens or None, error name or None); returns {call id: flags}."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            connection = create_opencode_db(db)
            connection.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)", ("s", "p", None, "/work/app", "1", 1, 2))
            for name, turn, ms, tokens, error in entries:
                data = {"role": "assistant", "providerID": "openai", "modelID": "m", "parentID": turn,
                        "time": {"created": ms, "completed": ms}}
                if tokens is not None:
                    data["tokens"] = tokens
                if error:
                    data["error"] = {"name": error}
                connection.execute("INSERT INTO message VALUES (?,?,?,?,?)", (name, "s", ms, ms, json.dumps(data)))
            connection.commit()
            connection.close()
            return {r.call_id: r.flags for r in why.collect_opencode(db, START, END)}

    BILLED = {"input": 1, "output": 2, "cache": {"read": 0, "write": 0}}
    ZERO = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    T0 = 1788429600000

    def test_opencode_zero_usage_abort_flags_the_last_billed_request_of_its_turn(self):
        got = self._opencode([("m1", "u1", self.T0, self.BILLED, None), ("m2", "u1", self.T0 + 1000, self.BILLED, None),
                              ("m3", "u1", self.T0 + 2000, self.ZERO, "MessageAbortedError"),
                              ("m4", "u2", self.T0 + 3000, self.BILLED, None)])
        self.assertEqual(got, {"m1": None, "m2": ["interrupted"], "m4": None})

    def test_opencode_abort_without_usage_flags_the_last_billed_request_of_its_turn(self):
        got = self._opencode([("m1", "u1", self.T0, self.BILLED, None), ("m2", "u1", self.T0 + 1000, None, "MessageAbortedError")])
        self.assertEqual(got, {"m1": ["interrupted"]})

    def test_opencode_abort_with_no_earlier_request_in_the_turn_flags_nothing(self):
        got = self._opencode([("m1", "u1", self.T0, self.BILLED, None), ("m2", "u2", self.T0 + 1000, self.ZERO, "MessageAbortedError")])
        self.assertEqual(got, {"m1": None})

    def test_pi_zero_usage_abort_flags_the_last_record_of_its_turn(self):
        first = pi_message("e2", "r1", "2026-09-03T10:00:02Z", self.U)
        aborted = pi_message("e3", "r2", "2026-09-03T10:00:03Z", {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0})
        aborted["message"]["stopReason"] = "aborted"
        got = self.collect_pi([self.user("e1", "2026-09-03T10:00:01Z"), first, aborted])
        self.assertEqual(got, {"r1": ["interrupted"]})


    def test_opencode_abort_flags_only_requests_up_to_its_own_time_fresh_or_incremental(self):
        entries = [("m1", "u1", self.T0, self.BILLED, None), ("m2", "u1", self.T0 + 1000, self.ZERO, "MessageAbortedError"),
                   ("m3", "u1", self.T0 + 2000, self.BILLED, None)]
        self.assertEqual(self._opencode(entries), {"m1": ["interrupted"], "m3": None})
        later = datetime.fromtimestamp((self.T0 + 500) / 1000, tz=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:  # a window starting after m1: m3 must stay clean, m1 is out of the read
            db = Path(tmp) / "o.db"
            connection = create_opencode_db(db)
            connection.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)", ("s", "p", None, "/work/app", "1", 1, 2))
            for name, ms, tokens, error in (("m1", self.T0, self.BILLED, None), ("m2", self.T0 + 1000, self.ZERO, "MessageAbortedError"),
                                            ("m3", self.T0 + 2000, self.BILLED, None)):
                data = {"role": "assistant", "providerID": "openai", "modelID": "m", "parentID": "u1",
                        "time": {"created": ms, "completed": ms}, "tokens": tokens}
                if error:
                    data["error"] = {"name": error}
                connection.execute("INSERT INTO message VALUES (?,?,?,?,?)", (name, "s", ms, ms, json.dumps(data)))
            connection.commit()
            connection.close()
            got = {r.call_id: r.flags for r in why.collect_opencode(db, later, END)}
        self.assertEqual(got, {"m3": None})


if __name__ == "__main__":
    unittest.main()
