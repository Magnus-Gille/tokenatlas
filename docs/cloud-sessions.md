# Cloud sessions: usage digest (TOKEN-200, TOKEN-201 proof of concept)

Claude Code sessions that run in Anthropic cloud containers (claude.ai/code, project threads) write the ordinary
transcript to `~/.claude/projects/-home-user/<session-id>.jsonl` (`entrypoint: remote_projects`), but the container is
reclaimed after inactivity, so TokenAtlas never sees it. `scripts/cloud_digest.py` is the shipping half of the fix.
The collector that imports what it ships is not built yet.

## What the script does

A `Stop` hook reads only the transcript lines added since the previous run (a byte offset per session), reduces them
to a digest and appends it to a spool (`~/.cache/tokenatlas-cloud/<session>.digest.jsonl`). With
`TOKENATLAS_CLOUD_URL` and `TOKENATLAS_CLOUD_TOKEN` set, it also POSTs the new lines (NDJSON, bearer token, 3 s
timeout); a failed POST stays queued in `<session>.pending.jsonl` for the next Stop. It always exits 0.

The digest is **the same JSONL shape the Claude collector already reads**, minus all text: assistant rows keep
`requestId`, `uuid`, `timestamp`, `sessionId`, `cwd`, `entrypoint`, `version`, `effort`, `message.{id,model,role,
stop_reason,usage}` (including the 5m/1h cache-write split); a prompt typed by the person becomes an empty user row so
turn grouping still works; tool results, reminders, attachments and all content are dropped. So no new record grain
is needed: a `cloud` source can be one more Claude root that points at the directory the sink stores, deduplicated by
the existing `requestId` rule.

## Measured on a real cloud session (this repo, 2026-11-20)

- 26 assistant rows, 15 distinct requests; transcript 830 KB, digest 31 KB (27x smaller).
- `why.collect_claude` on the transcript and on the digest returns identical per-request tokens, model, project and
  turn presence (`test_cloud_digest.py` asserts this on a synthetic fixture).
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
available there (vendor it, or `pip install tokenatlas` in the environment's setup script).
