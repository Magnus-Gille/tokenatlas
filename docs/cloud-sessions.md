# Cloud sessions: usage digest (TOKEN-200, TOKEN-201 proof of concept)

Claude Code sessions that run in Anthropic cloud containers (claude.ai/code, project threads) write the ordinary
transcript to `~/.claude/projects/-home-user/<session-id>.jsonl` (`entrypoint: remote_projects`), but the container is
reclaimed after inactivity, so TokenAtlas never sees it. `scripts/cloud_digest.py` is the shipping half of the fix.
The collector that imports what it ships is not built yet.

## What the script does

A `Stop` hook reads only the lines added since the previous run (a byte offset and a first-bytes fingerprint per file, so a
rewritten transcript starts over) from the main transcript and any `<session>/subagents/agent-*.jsonl`, reduces them to a
digest and appends it to a spool that mirrors the transcript layout (`<state>/projects/<project>/...`), so the spool
directory can be read as a Claude root. With `TOKENATLAS_CLOUD_URL` (https only, redirects refused) and
`TOKENATLAS_CLOUD_TOKEN` set it also POSTs the new lines (NDJSON, bearer token, 3 s timeout); a failed POST stays queued in
`<session>.pending.jsonl` and only the bytes that were sent are removed. Overlapping Stops take turns on a file lock. It
always exits 0.

The digest is **the same JSONL shape the Claude collector already reads**, minus all text. Assistant rows keep
`requestId`, `uuid`, `timestamp`, `sessionId`, `cwd`, `entrypoint`, `version`, `effort`, `message.{id,model,role,
stop_reason}` and an **allowlist of numeric usage counters** (including the 5m/1h cache-write split and per-iteration
counters); nothing else of `usage` is copied. A prompt typed by the person (decided by the collector's own rule) becomes an
empty user row so turn grouping still works; tool results, reminders, attachments and all content are dropped. So no new
record grain is needed: a `cloud` source can be one more Claude root deduplicated by the existing `requestId` rule.

## Measured on a real cloud session (this repo, 2026-11-20)

- Main transcript plus one advisor subagent transcript: 72 distinct requests, 2.0 MB of transcripts, 151 KB of digest
  (13x smaller).
- `why.collect_claude` on the transcripts and on the digest returns identical per-request tokens, model, project and
  thread kind.
- A second run sends 0 rows; a half-written last line is left for the next Stop.

## What is verified and what is not

| Question | Result |
| --- | --- |
| Transcript exists in the container with usage, session id, cwd, entrypoint | Yes (observed) |
| Stop payload carries `transcript_path` | Yes: the container's own Stop hooks (`stop-hook-git-check.sh`, `stop-hook-reply-gate.py`) read `transcript_path`, `last_assistant_message`, `stop_hook_active` |
| Transcript can lag the hook | Yes: the harness's own hook has a `caught_up()` wait for it. The offset design tolerates it (the next Stop sends the rest), but the last turn's usage can arrive one Stop late |
| A repo `.claude/settings.json` Stop hook fires in a cloud session | **Not verified.** Settings added mid-session were not picked up (no payload was written). Needs a fresh session started after the file is committed |
| Network reachability and secret readable by the hook | **Not verified**; depends on the environment's network policy and needs Magnus's sink choice |

## Hook configuration for the build ticket

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command",
  "command": "python3 \"$CLAUDE_PROJECT_DIR/scripts/cloud_digest.py\"", "timeout": 5}]}]}}
```

Every repo that runs cloud sessions needs this file (user-level hooks do not carry over), and the script must be
available there: vendor `scripts/cloud_digest.py` into the repo (it is not part of the PyPI package), or fetch it in the
environment's setup script.

## Sending to the ingest service (TOKEN-209)

Set `TOKENATLAS_CLOUD_URL` (the `/v1/ingest` endpoint, `https://` or loopback only) and `TOKENATLAS_CLOUD_TOKEN`
(`tka_...`, minted with `cloud-ingest/admin/tokens.ts`). Without a URL the hook only spools. Registered events:
`Stop`, `SessionStart` and `SessionEnd` (a user-level hook in `~/.claude/settings.json` fires for all three; a committed
repo hook did not fire in TOKEN-206). `SessionStart` and `SessionEnd` also flush queues other sessions left behind.

- Each queued row carries `session` and `_path` (`""`, `subagents/agent-*.jsonl` or
  `subagents/workflows/wf_*/agent-*.jsonl`), and is sent with `X-TokenAtlas-Client: 2`.
- Rows leave the queue only on a per-line receipt of `accepted` or `duplicate`. `conflict`, `rejected`,
  `row_too_large` and `suppressed` are moved to `<state>/quarantine.jsonl` (capped at 1 MB) and counted;
  `quota` and anything unknown stay queued with a 5-minute back-off.
- `413` halves the batch, then quarantines a single row. `429` honours `Retry-After`; 5xx and timeouts back off
  (30 s doubling to 1 h). A `401/403` or `426` turns sending off (`<state>/WARNING.txt`) until the token or the script
  changes. All of it is kept in `<state>/send-state.json`, together with how many rows each session still has queued.
- The hook never exits non-zero and stays inside a 4.5 s budget of the 5 s limit.

## Pulling into the local history (TOKEN-210)

`tokenatlas sync-cloud` (and `tokenatlas refresh --all --cloud`, opt-in so an outage never blocks an ordinary refresh) pulls
`/v1/events` and imports the rows as one more Claude root.

- Configure `TOKENATLAS_CLOUD_READ_TOKEN` (a `read` token) and either `TOKENATLAS_CLOUD_URL` (the ingest URL; the events URL is
  derived from it) or `TOKENATLAS_CLOUD_PULL_URL`. `https://` only (plain http only for loopback); redirects are refused.
- State lives in `cloud/` next to the history database: `events.sqlite3` holds every event once by `dedupe_key`, and the
  cursor commits in the same transaction as each page. Deleting the directory only means a full re-pull.
- A `410` (storage reset or rows deleted) drops the cursor and re-pulls from zero; the event table absorbs the overlap.
  When the last pull is older than `TOKENATLAS_CLOUD_RETENTION_DAYS` (default 90) the result carries a warning.
- Rows are untrusted: ids and paths are validated, rows are reduced to the same allowlist the hook uses, and a cloud `cwd`
  is rewritten to a `cloud:<folder name>` label, so it never reaches local project or git discovery.
- Cloud requests merge by request id, so a Mac and a Pi that both pull (and then exchange snapshots) count them once. Known
  limits: there is no shared origin id yet, so the machine shown for a cloud row is whichever copy wins the merge, and a row
  with neither `requestId` nor `message.id` (the receiver allows it; the hook always sends one) would be counted per machine.
  On a case-insensitive filesystem two session ids that differ only in case share a transcript file.
- The report's coverage section shows a cloud line: sessions seen, rows, the latest event and the last pull. Sessions that
  never sent anything are invisible to it.

## Per-session coverage check (TOKEN-212)

A container can die before the final flush, and a quiet tail looks like a lost one, so completeness cannot be proven from
the rows alone. What can be seen: the hook ships a genuine user prompt and assistant rows with their `stop_reason`, never the
tool results between them. A session whose last main-transcript row is a prompt, or an assistant row that stopped for
`tool_use`, ends on a prompt or tool call with nothing after it. That means one of: the person interrupted it, it is still
running, or its final rows were lost; the rows cannot tell which. `tokenatlas doctor --json` lists those session ids under
`cloud.incomplete_sessions` (the 20 most recently active); the report shows only the count, never ids.
Sessions quiet for under 2 hours are not counted (rows ship only at a Stop, so a long turn is silent), nothing is counted until a
pull has caught up with the server, a lost tail after an intermediate row is invisible, a session that ends cleanly is only
"no gap seen", and a session that never sent anything cannot be seen at all.
