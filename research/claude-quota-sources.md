# Claude subscription-limit usage without owning the statusline (issue #118)

Retrieved 2026-10-04 from primary sources only. Doc base: https://code.claude.com/docs/en/ . Changelog: https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md (raw: https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md). No local files were read.

## Summary and recommendation

The only documented, supported channel for `rate_limits` (5-hour and 7-day used percentage plus reset epoch) is the **statusline stdin JSON**. Nothing else documented carries it: not hook payloads, not OTel, not transcripts, not a CLI/API.

Recommendation: implement a **statusline wrapper (pass-through)**. The tokenatlas statusLine command reads stdin once, records `rate_limits` (plus session_id/timestamp) to its own store, then pipes the same stdin to the user's original command and prints its stdout unchanged. Stability: **medium-high**. The field is documented, versioned (added v2.1.80, extended v2.1.251/v2.1.284) and actively maintained (a v2.1.243 fix concerns it). Wrapping is not mentioned in the docs, but it is just "a command that prints to stdout", so it needs no undocumented behaviour. Risks are operational (timing, single-slot config, user must opt in), not format drift.

Do not rely on hooks (no data), OTel (no data) or undocumented caches (location undocumented). File the upstream request (draft below) as the long-term fix, which would remove the need to be the statusline at all.

## Findings

### 1. Hooks: no rate_limits in any payload
The hooks reference lists these events: SessionStart, Setup, SessionEnd, UserPromptSubmit, UserPromptExpansion, Stop, StopFailure, PreToolUse, PostToolUse, PostToolUseFailure, PermissionRequest, PermissionDenied, PostToolBatch, SubagentStart, SubagentStop, TaskCreated, TaskCompleted, TeammateIdle, FileChanged, CwdChanged, DirectoryAdded, ConfigChange, InstructionsLoaded, PreCompact, PostCompact, PreModelSwitch, PostModelSwitch, WorktreeCreate, WorktreeRemove, Notification, MessageDisplay, Elicitation, ElicitationResult. Common fields: `session_id, prompt_id, transcript_path, cwd, scratchpad_dir, permission_mode, effort, hook_event_name, agent_id, agent_type`. No `rate_limits`, usage or quota field. The only cost-like field is `estimated_cache_write_usd` on SessionStart for resume/fork. (Extracted via a summarising fetch; a grep-level re-check of the raw page is worth doing before closing.)
Source: https://code.claude.com/docs/en/hooks (2026-10-04).
Side idea: a hook cannot read the value, but a hook-driven tool could reuse the wrapper's last-recorded snapshot. That still needs the wrapper.

### 2. Statusline chaining: feasible, not documented as a feature
- Schema: `rate_limits.five_hour.used_percentage` and `.seven_day.used_percentage` (0-100); `.resets_at` (Unix epoch seconds). Example payload confirms nesting under `rate_limits.five_hour` / `seven_day` (and `spend_limit` behind a Claude apps gateway, with optional `used_usd`, `limit_usd`, `period`).
- Presence rules: only for claude.ai Pro and Max (or gateway spend limit), only after the first API response in the session; each window may be independently absent; a window is dropped once its `resets_at` passes. Handle absence (`// empty`).
- Execution: runs the command with JSON on stdin and shows stdout. Updates are event-driven, **debounced 300 ms**; **an in-flight script is cancelled when a new update triggers**; also re-run when a rate-limit window reaches `resets_at`; optional `refreshInterval` (min 1 s) timer. Config: `statusLine: {type:"command", command, padding?, refreshInterval?}`; `padding` is extra horizontal spacing (default 0).
- Wrapper constraints that follow from the docs: (a) must be fast, because slow scripts get cancelled and a cancelled wrapper may not finish recording, so write the snapshot first (atomic, small) and then run the inner command; (b) forward stdin byte-for-byte and stdout (ANSI/OSC 8 are rendered as-is; multiple lines supported); (c) `padding` and `refreshInterval` belong to the settings entry, so preserve them when installing; (d) stderr is only logged under `--debug`; (e) `statusLine` is a single slot, so install must save the original command and be reversible; managed/`allowManagedHooksOnly` settings can restrict `statusLine`; (f) no documented per-run timeout was found, which is an open question.
- `/usage` and statusline values are the same data source (changelog v2.1.243: statusline `rate_limits` and `/usage` showed stale pre-reset percentages, fixed).
Sources: https://code.claude.com/docs/en/statusline (2026-10-04); changelog entries v2.1.80 "Added `rate_limits` field to statusline scripts ... (5-hour and 7-day windows with `used_percentage` and `resets_at`)", v2.1.97 `refreshInterval`, v2.1.243, v2.1.251 and v2.1.284 (spend_limit fields).
Gaps: the statusline only runs in the interactive TUI, so `claude -p` runs produce no snapshots (docs do not state how it behaves for headless; unverified). In an idle TUI the wrapper may still be invoked (timer, window reset), but with cached values: invocation is not freshness. A changed payload, such as a window that expired, is still recorded.

### 3. OpenTelemetry: nothing
Metrics: session.count, lines_of_code.count, pull_request.count, commit.count, cost.usage, token.usage, code_edit_tool.decision, active_time.total. Events include user_prompt, assistant_response, api_request, api_error, api_refusal, api_retries_exhausted, tool_result, auth and others. No quota, 5-hour or weekly data. `api_error` may carry rate-limit failures but not usage percentage (not verified in detail). Consistent with earlier finding #94.
Source: https://code.claude.com/docs/en/monitoring-usage (2026-10-04).

### 4. Local caches: behaviour documented, storage location not
- Docs: when the usage request fails, `/usage` shows "the last usage bars it loaded on this machine within the past 60 minutes" with a "Showing last-known usage" note (https://code.claude.com/docs/en/costs, "When the usage request fails"; introduced per changelog around v2.1.208). Where it is stored is **not documented**.
- The documented application-data page lists `stats-cache.json` ("Aggregated token and cost counts shown by `/usage`"), `usage-data/` (`/insights` reports), `cache/changelog.md`, `policy-limits.json`, `remote-settings.json`, `backups/` of `~/.claude.json`. None is described as holding plan-limit percentages. The last-known snapshot may be in memory only. Treat any file as undocumented and unstable.
Source: https://code.claude.com/docs/en/claude-directory (2026-10-04).

### 5. Official CLI/API for subscription usage: none for individuals
- `/usage` is interactive only. The costs page names no `claude usage` command.
- Admin-side options exist only for orgs: Enterprise Analytics API (`read:analytics` key, per-user usage/cost), Claude Code Analytics API (Console, Admin API key, API orgs), Teams spend-report CSV. These report consumption and spend, not the individual's 5-hour/weekly window percentage, and do not apply to personal Pro/Max.
- The Agent SDK cost-tracking page (https://code.claude.com/docs/en/agent-sdk/cost-tracking) was listed but not read; check it for rate-limit events in SDK message streams.
Source: https://code.claude.com/docs/en/costs ; https://code.claude.com/docs/llms.txt (2026-10-04).

## Upstream feature request (draft, do not file)

**Title:** Record `rate_limits` per assistant message in the session transcript JSONL

**Problem.** Subscription limit state (5-hour and weekly used percentage and reset time) is exposed only in the statusline stdin JSON. Hooks, OpenTelemetry and transcripts do not carry it. Local tools that want to correlate token usage with plan consumption must become the user's statusLine command, which conflicts with users' own statuslines, is a single configuration slot, and only runs in the interactive TUI. Codex CLI records `token_count` events with rate-limit info in its session logs, so offline analysis works without any UI hook.

**Request.** For each assistant message (or each API response) written to the transcript, add the rate-limit snapshot that Claude Code already has from that response, in the same shape as the statusline payload:

```json
"rate_limits": {
  "five_hour": { "used_percentage": 23.5, "resets_at": 1738425600 },
  "seven_day": { "used_percentage": 41.2, "resets_at": 1738857600 }
}
```

Same semantics as the statusline: only for claude.ai subscribers (or gateway `spend_limit` with `used_usd`/`limit_usd`/`period`), windows independently optional, epoch seconds. Alternatively add the same object to the `Stop` and `SessionEnd` hook inputs.

**Benefits.** Works for headless (`-p`) and SDK runs and subagents (values at response time; periods without responses still leave gaps); no statusline takeover; enables per-session/per-skill attribution of plan consumption and accurate history (the value at each message rather than a poll). Cost is a small constant per record and no new network call, since the data comes from the existing response headers.

## Open questions
- Does the statusline run (and receive `rate_limits`) in `-p`/headless and in IDE/VS Code sessions? Docs silent.
- Any hard timeout on the statusline command beyond "cancel on next update"? Not found.
- Is `rate_limits` present on every update or only after API responses (docs: only after first response in the session)? Wrapper must persist last-seen values.
- Several concurrent sessions each get their own payload; are values identical per account? Likely, but verify, and key snapshots by session_id plus timestamp.
- Where is the `/usage` last-known snapshot stored (if on disk)? Undocumented; not needed if the wrapper is adopted.
- Re-verify hook payload claim against the raw hooks page; agent-sdk cost-tracking page not read.
