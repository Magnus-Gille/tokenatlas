#!/usr/bin/env python3
"""Compressed (.jsonl.zst) and archived Codex rollouts: decoder equivalence, multi-frame, truncation, roots."""

import builtins
import io
import json
import locale
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tokenatlas import why
from tokenatlas.__main__ import main, refresh_all
from tokenatlas.history import History
from tokenatlas.report import build_report

try:
    import zstandard
except ImportError:
    zstandard = None

FIXTURE = Path(__file__).parent / "fixtures" / "rollout-fixture.jsonl.zst"
BINARY = shutil.which("zstd")
# CI sets this (comma list of "module", "binary") so a missing decoder fails instead of silently skipping.
REQUIRED = {x.strip() for x in os.environ.get("TOKENATLAS_REQUIRE_ZSTD", "").split(",") if x.strip()}
DECODERS = {name: value for name, value in (("module", ("module", zstandard) if zstandard else None),
                                            ("binary", ("binary", BINARY) if BINARY else None)) if value}
START, END = datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def compress(data: bytes) -> bytes:
    if not (zstandard or BINARY):
        raise unittest.SkipTest("no zstd decoder available")
    if zstandard:
        return zstandard.ZstdCompressor().compress(data)
    return subprocess.run([BINARY, "-q", "-c"], input=data, capture_output=True, check=True).stdout


def rows(session="zst-session", note="plain", output=8):
    return [
        {"timestamp": "2026-09-03T08:00:00Z", "type": "session_meta",
         "payload": {"id": session, "model_provider": "openai", "source": "cli", "cwd": "/work/p", "note": note}},
        {"timestamp": "2026-09-03T09:00:00Z", "type": "turn_context",
         "payload": {"model": "gpt-test", "effort": "high", "cwd": "/work/p"}},
        {"timestamp": "2026-09-03T09:01:00Z", "ordinal": 10, "type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 100, "output_tokens": output},
            "total_token_usage": {"input_tokens": 100, "output_tokens": output}}}},
        {"timestamp": "2026-09-03T09:02:00Z", "ordinal": 11, "type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 50, "output_tokens": 2},
            "total_token_usage": {"input_tokens": 150, "output_tokens": output + 2}}}},
    ]


def content(records):
    """Observations without their source paths (a moved or compressed copy legitimately adds a path)."""
    return [{k: v for k, v in r.items() if k != "sources"} for r in records]


def encode(items, ensure_ascii=True):
    return "".join(json.dumps(r, ensure_ascii=ensure_ascii) + "\n" for r in items).encode("utf-8")


def write_zst(path, items=None, data=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(compress(data if data is not None else encode(items if items is not None else rows())))
    return path


class DecoderCase(unittest.TestCase):
    def decoders(self):
        for name in sorted(REQUIRED - set(DECODERS)):
            self.fail(f"required zstd decoder {name!r} is not available")
        if not DECODERS or not (zstandard or BINARY):
            self.skipTest("no zstd decoder available")
        return DECODERS.items()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class DecoderEquivalenceTests(DecoderCase):
    def test_required_decoders_are_installed(self):
        self.assertFalse(REQUIRED - set(DECODERS), f"missing required zstd decoders: {sorted(REQUIRED - set(DECODERS))}")

    def test_line_separators_inside_strings_do_not_split_lines(self):
        items = rows(note="a b c\u0085d")
        raw = encode(items, ensure_ascii=False)
        self.assertIn(" ".encode(), raw)
        expected = [(n, r) for n, r in enumerate(items, 1)]
        for name, decoder in self.decoders():
            with self.subTest(decoder=name):
                path = write_zst(self.root / "rollout-sep.jsonl.zst", data=raw)
                self.assertEqual(list(why._read_json_lines(path, strict=True, decoder=decoder)), expected)
        if locale.getpreferredencoding(False).lower().replace("-", "") == "utf8":
            plain = self.root / "rollout-sep.jsonl"
            plain.write_bytes(raw)
            self.assertEqual(list(why._read_json_lines(plain, strict=True)), expected)

    def test_records_match_between_plain_and_every_decoder(self):
        plain = self.root / "plain" / "rollout-a.jsonl"
        plain.parent.mkdir()
        plain.write_bytes(encode(rows()))
        baseline = why.collect_codex(plain.parent, START, END)
        self.assertEqual(len(baseline), 2)
        for name, decoder in self.decoders():
            with self.subTest(decoder=name):
                path = write_zst(self.root / name / "rollout-a.jsonl.zst")
                self.assertEqual(why.collect_codex(path.parent, START, END, paths=[path], strict=True, decoder=decoder), baseline)

    def test_checked_in_fixture_decodes(self):
        for name, decoder in self.decoders():
            with self.subTest(decoder=name):
                records = why.collect_codex(FIXTURE.parent, START, END, paths=[FIXTURE], strict=True, decoder=decoder)
                self.assertEqual([r.session_id for r in records], ["fixture-session"])

    def test_every_frame_is_read(self):
        items = rows()
        data = compress(encode(items[:2])) + compress(encode(items[2:]))
        for name, decoder in self.decoders():
            with self.subTest(decoder=name):
                path = self.root / name / "rollout-frames.jsonl.zst"
                path.parent.mkdir()
                path.write_bytes(data)
                self.assertEqual([r for _, r in why._read_json_lines(path, strict=True, decoder=decoder)], items)

    def test_truncated_frame_is_an_error_for_every_decoder(self):
        data = compress(encode(rows() * 200))
        for name, decoder in self.decoders():
            with self.subTest(decoder=name):
                path = self.root / name / "rollout-cut.jsonl.zst"
                path.parent.mkdir()
                path.write_bytes(data[:-12])
                with self.assertRaises(OSError):
                    list(why._read_json_lines(path, strict=True, decoder=decoder))
                with patch.object(why, "resolve_zstd_decoder", return_value=decoder), History(self.root / f"{name}.sqlite3") as history:
                    result = history.refresh("codex", path.parent)
                self.assertEqual((result["status"], result["read_errors"]), ("partial", 1))

    def test_second_frame_truncated_is_an_error_for_every_decoder(self):
        data = compress(encode(rows())) + compress(encode(rows() * 100))[:-9]
        for name, decoder in self.decoders():
            with self.subTest(decoder=name):
                path = self.root / name / "rollout-cut2.jsonl.zst"
                path.parent.mkdir()
                path.write_bytes(data)
                with self.assertRaises(OSError):
                    list(why._read_json_lines(path, strict=True, decoder=decoder))

    @unittest.skipUnless(zstandard, "needs the zstandard module")
    def test_module_output_memory_is_bounded_by_the_read_size(self):
        import tracemalloc
        path = self.root / "rollout-big.jsonl.zst"
        path.write_bytes(zstandard.ZstdCompressor().compress(b"a" * (64 << 20) + b"\n"))
        tracemalloc.start()
        try:
            with why._zstd_text_stream(path, DECODERS["module"]) as text:
                total = sum(len(chunk) for chunk in iter(lambda: text.read(1 << 16), ""))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(total, (64 << 20) + 1)
        self.assertLess(peak, 16 << 20)

    @unittest.skipIf(os.name == "nt", "needs a POSIX shebang script")
    def test_hanging_binary_decoder_fails_fast_and_is_reaped(self):
        script = self.root / "slow-zstd"
        script.write_text(f"#!{__import__('sys').executable}\nimport sys, time\n"
                          "sys.stdout.write('{\"a\":1}\\n'); sys.stdout.flush()\ntime.sleep(60)\n")
        script.chmod(0o755)
        path = self.root / "rollout-hang.jsonl.zst"
        path.write_bytes(b"ignored")
        started = __import__("time").monotonic()
        with patch.object(why, "_zstd_timeout", return_value=0.5):
            with self.assertRaises(OSError) as error:
                list(why._read_json_lines(path, strict=True, decoder=("binary", str(script))))
        self.assertLess(__import__("time").monotonic() - started, 20)
        self.assertIn("did not finish", str(error.exception))

    @unittest.skipIf(os.name == "nt", "needs a POSIX shebang script")
    def test_binary_decoder_that_closes_stdout_and_stalls_is_killed(self):
        script = self.root / "stalling-zstd"
        script.write_text(f"#!{__import__('sys').executable}\nimport os, sys, time\n"
                          "sys.stdout.write('{\"a\":1}\\n'); sys.stdout.flush(); os.close(1)\ntime.sleep(60)\n")
        script.chmod(0o755)
        path = self.root / "rollout-stall.jsonl.zst"
        path.write_bytes(b"ignored")
        started = __import__("time").monotonic()
        with patch.object(why, "_zstd_timeout", return_value=0.5):
            with self.assertRaises(OSError) as error:
                list(why._read_json_lines(path, strict=True, decoder=("binary", str(script))))
        self.assertLess(__import__("time").monotonic() - started, 20)
        self.assertIn("did not finish", str(error.exception))

    @unittest.skipUnless(zstandard, "needs the zstandard module")
    def test_file_of_only_skippable_frames_is_valid_and_empty_file_is_not(self):
        skippable = (0x184D2A50).to_bytes(4, "little") + (3).to_bytes(4, "little") + b"abc"
        path = self.root / "rollout-skip.jsonl.zst"
        path.write_bytes(skippable * 2)
        why._check_zstd_frames(path, zstandard)
        path.write_bytes(skippable[:-1])
        with self.assertRaises(OSError):
            why._check_zstd_frames(path, zstandard)
        path.write_bytes(b"")
        with self.assertRaises(OSError):
            why._check_zstd_frames(path, zstandard)

    def test_empty_file_is_an_error_for_every_decoder(self):
        for name, decoder in self.decoders():
            with self.subTest(decoder=name):
                path = self.root / name / "rollout-empty.jsonl.zst"
                path.parent.mkdir()
                path.write_bytes(b"")
                with self.assertRaises(OSError):
                    list(why._read_json_lines(path, strict=True, decoder=decoder))
                with patch.object(why, "resolve_zstd_decoder", return_value=decoder), History(self.root / f"{name}.sqlite3") as history:
                    result = history.refresh("codex", path.parent)
                self.assertEqual((result["status"], result["read_errors"]), ("partial", 1))

    def test_corrupt_file_is_one_read_error(self):
        for name, decoder in self.decoders():
            with self.subTest(decoder=name):
                path = self.root / name / "rollout-bad.jsonl.zst"
                path.parent.mkdir()
                path.write_bytes(b"not compressed")
                with patch.object(why, "resolve_zstd_decoder", return_value=decoder), History(self.root / f"{name}.sqlite3") as history:
                    result = history.refresh("codex", path.parent)
                self.assertEqual((result["status"], result["read_errors"]), ("partial", 1))
                self.assertIn("cannot read compressed rollout", result["errors"][0])

    def test_diagnostics_are_counted_in_the_single_decode(self):
        data = (encode(rows()[:1]) + b"not json\n" + b'{"type":"event_msg"')
        for name, decoder in self.decoders():
            with self.subTest(decoder=name):
                path = write_zst(self.root / name / "rollout-diag.jsonl.zst", data=data)
                with patch.object(why, "resolve_zstd_decoder", return_value=decoder), History(self.root / f"{name}.sqlite3") as history:
                    result = history.refresh("codex", path.parent)
                self.assertEqual((result["status"], result["malformed_lines"], result["partial_lines"]), ("partial", 1, 1))

    def test_decoder_is_resolved_once_per_refresh(self):
        for n in range(3):
            write_zst(self.root / "r" / f"rollout-{n}.jsonl.zst", rows(session=f"s{n}"))
        name, decoder = next(iter(self.decoders()))
        with patch.object(why, "resolve_zstd_decoder", return_value=decoder) as resolve, History(self.root / "h.sqlite3") as history:
            result = history.refresh("codex", self.root / "r")
        self.assertEqual((result["status"], resolve.call_count), ("ok", 1))

    def test_compressed_and_plain_copies_without_session_meta_share_keys(self):
        bare = rows()[2:]  # no session_meta: identity is content derived, never the file name
        plain = self.root / "sessions" / "rollout-x.jsonl"
        plain.parent.mkdir()
        plain.write_bytes(encode(bare))
        write_zst(self.root / "archived_sessions" / "rollout-x.jsonl.zst", bare)
        with History(self.root / "h.sqlite3") as history:
            history.refresh("codex", self.root / "sessions")
            before = content(history.records())
            history.refresh("codex", self.root / "archived_sessions")
            self.assertEqual(content(history.records()), before)
        self.assertEqual(len(before), 2)


class RootsTests(DecoderCase):
    def home(self):
        home = self.root / "codex"
        (home / "sessions").mkdir(parents=True)
        (home / "archived_sessions").mkdir()
        original = why.harness_root
        def fake(name):  # CODEX_HOME is honoured; every other harness is absent
            return original("codex") if name == "codex" else (self.root / "none", "test")
        patcher = patch.dict(os.environ, {"CODEX_HOME": str(home)})
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(why, "COWORK_SESSIONS", self.root / "no-cowork")
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(why, "harness_root", side_effect=fake)
        patcher.start(); self.addCleanup(patcher.stop)
        return home

    def test_codex_roots_follow_codex_home(self):
        home = self.home()
        self.assertEqual(why.codex_roots(), [home / "sessions", home / "archived_sessions"])

    def test_refresh_all_imports_a_session_moved_to_archive_once(self):
        home = self.home()
        self.decoders()
        active = home / "sessions" / "2026" / "rollout-m.jsonl"
        active.parent.mkdir()
        active.write_bytes(encode(rows(session="moved")))
        with History(self.root / "h.sqlite3") as history:
            first = refresh_all(history)
            codex = next(e for e in first["harnesses"] if e["harness"] == "codex")
            self.assertEqual((codex["status"], codex["files_seen"]), ("ok", 1))
            before = content(history.records())
            active.unlink()
            write_zst(home / "archived_sessions" / "rollout-m.jsonl.zst", rows(session="moved"))
            write_zst(home / "archived_sessions" / "rollout-n.jsonl.zst", rows(session="only-archived", output=5))
            second = refresh_all(history)
            codex = next(e for e in second["harnesses"] if e["harness"] == "codex")
            self.assertEqual(codex["status"], "ok")
            after = content(history.records())
        self.assertEqual(len(before), 2)
        self.assertEqual(sorted(r["session"] for r in after), ["moved", "moved", "only-archived", "only-archived"])
        self.assertEqual([r for r in after if r["session"] == "moved"], before)

    def test_refresh_codex_without_root_covers_both_roots(self):
        home = self.home()
        self.decoders()
        (home / "sessions" / "rollout-a.jsonl").write_bytes(encode(rows(session="a")))
        write_zst(home / "archived_sessions" / "rollout-b.jsonl.zst", rows(session="b"))
        code, out, _ = run("--db", str(self.root / "h.sqlite3"), "refresh", "--harness", "codex")
        result = json.loads(out)
        self.assertEqual((code, result["status"], result["files_seen"], len(result["roots"])), (0, "ok", 2, 2))

    def test_refresh_codex_without_any_root_is_missing_as_before(self):
        self.home()
        shutil.rmtree(Path(os.environ["CODEX_HOME"]))
        code, out, _ = run("--db", str(self.root / "h.sqlite3"), "refresh", "--harness", "codex")
        self.assertEqual((code, json.loads(out)["status"]), (2, "missing"))


class UnreadableTests(DecoderCase):
    def no_decoder(self):
        real_import = builtins.__import__
        def deny(name, *args, **kwargs):
            if name == "zstandard":
                raise ImportError("forced test absence")
            return real_import(name, *args, **kwargs)
        for patcher in (patch.object(shutil, "which", return_value=None), patch.object(builtins, "__import__", side_effect=deny)):
            patcher.start(); self.addCleanup(patcher.stop)

    def test_resolver_names_the_extra_and_the_binary(self):
        self.no_decoder()
        with self.assertRaises(why.ZstdUnavailableError) as error:
            why.resolve_zstd_decoder()
        for text in ("tokenatlas[zstd]", "pipx", "uv tool", "zstd binary"):
            self.assertIn(text, str(error.exception))

    def test_missing_decoder_is_one_diagnostic_and_a_count_not_ok(self):
        for n in range(4):
            (self.root / "r").mkdir(exist_ok=True)
            (self.root / "r" / f"rollout-{n}.jsonl.zst").write_bytes(b"x")
        self.no_decoder()
        with History(self.root / "h.sqlite3") as history:
            result = history.refresh("codex", self.root / "r")
            doctor = history.doctor()
        self.assertEqual((result["status"], result["read_errors"], len(result["errors"])), ("partial", 4, 1))
        self.assertIn("4 compressed", result["errors"][0])
        self.assertIn("tokenatlas[zstd]", result["errors"][0])
        imported = next(i for i in build_report([], doctor)["coverage"]["imports"] if i["harness"] == "codex")
        self.assertEqual((imported["status"], imported["read_errors"]), ("partial", 4))

    def test_unrecognised_rollout_suffix_is_unreadable_not_ok(self):
        (self.root / "r").mkdir()
        (self.root / "r" / "rollout-a.jsonl").write_bytes(encode(rows()))
        for name in ("rollout-b.jsonl.gz", "rollout-c.jsonl.gz.zst", "rollout-d.jsonl.zst.zst"):
            (self.root / "r" / name).write_bytes(b"x")
        with History(self.root / "h.sqlite3") as history:
            result = history.refresh("codex", self.root / "r")
        self.assertEqual((result["status"], result["read_errors"], result["files_seen"]), ("partial", 3, 4))
        self.assertIn("unrecognised suffix", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
