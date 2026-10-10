# Current state — 2026-10-10 (#157 candidate)

TokenAtlas 1.21.0 adds deterministic token-efficiency facts to the offline report
and the additive `insights --json` extension: concentration, context, delegation,
and equal-window change, with bounded pseudonymous evidence and explicit scenarios.
No automatic workflow changes or predicted savings are introduced.

Verification: full Python suite, packaged install lifecycle, cross-surface parity,
Chromium/WebKit offline suites and Swedish/English desktop/mobile visual checks.
Independent native review configured gpt-5.6-sol/xhigh passed after two fixes:
partial-import snapshot coverage and partial-period completeness. Runtime model
identity/effort metadata was unavailable. Native gpt-6-luna/high leaves provided
implementation (partial; root corrected/reworked integration) and checks (pass).

Ship-it publication/merge is authorized. The 1.21.0 GitHub release triggers PyPI
and still requires separate confirmation of the accepted exact merge SHA; the
prior 1.20.0 approval does not cover it. Preserve the installed 1.20.0 and user
history until an explicit upgrade. No private usage or transcripts enter fixtures.

---

# Current state — 2026-09-29 (after #21)

`master` holds 1.2.0 (#19, `cb0e5ed`) plus #21 (`2670a94`): list-price cost per session and model,
Claude desktop Cowork import, cross-machine snapshot/import, and a fixed-context overhead comparison.
#22 (open) fixes `remote_sync.sh` for user-installed `energy-monitor` on the Raspberry Pi. The
Raspberry Pi (`huginmunin.local`) runs `energy-monitor`, pinned to `2670a94`, in a venv.

**Not yet done:** the release tag and package publication. They wait for the owner's own testing.
Whether the tag is 1.2.0 at `cb0e5ed` plus 1.3.0 at current master, or a single 1.3.0, is still open.

# Project Status

## Session — 2026-09-29 (afternoon): cost, coverage, and overhead (#21), Raspberry Pi

**Pricing:** `usage/pricing.py` and `usage/prices.json` hold 29 models. The conductor verified every
price against its primary source on 2026-09-29.
- Each class is priced separately (input, 5m/1h cache writes, cache read, output), including
  long-context tiers, Claude fast mode and US inference, and service tiers.
- An unknown price or tariff gives `n/a`, never 0.
- Unpriced models: `codex-auto-review`, `gpt-5.3-codex-spark`, and two Berget models.

**Session trees:** `energy-monitor session` and `rate`.
- Subagents are keyed by the agent id from the transcript file name. Workflow runs, explicit
  children and inferred headless children are included; ambiguous threads are listed as unassigned.
- Cost per passed unit is read from an outcomes JSONL; the conductor's delegation ledger works
  as-is.
- In the acceptance session, the conductor thread was $20.74 of ≥$29.24. Sonnet leaves cost ≥$0.56
  per passed unit, Luna reviews $0.25.

**Coverage:**
- Claude desktop Cowork transcripts add 4,090 previously missing calls. `audit.jsonl` is never read,
  and 2,311 of 2,324 audit ids match.
- Ordinary ChatGPT and Claude desktop chat leave no local usage data. The ChatGPT cache is
  encrypted; Claude chat is a web view.
- `snapshot` and `import --label` merge another machine's history. On the Raspberry Pi this gave
  968 observations (Codex 697, OpenCode 197, Pi 74); the round trip is idempotent.

**Overhead:** `energy-monitor overhead` compares harnesses usage-normalized. Fixed share of input:
Pi 6.7%, Claude Code 16.2%, OpenCode 20.2%, Codex 20.7%.
- Component sizes are exact for Claude and Codex.
- Skill use is exact for Claude and OpenCode, heuristic for Codex.

**Reviews:** three read-only `gpt-5.6-luna` xhigh passes. Their 8 + 3 findings are fixed with
regression tests, including a crash on 6,555 real Codex rows without a model. Windows CI failures
were test portability only.

**Housekeeping:** the merged feature branches, worktrees and standalone clones are deleted. Their
SHAs are recorded in the local recovery notes.

## Session — 2026-09-29: review fixes and merge of 1.2.0

- Review fixes: the Windows `tzdata`/`USERPROFILE` CI failures, bounded metadata strings, and
  per-field Claude metadata merge. Claude subagent and workflow observations now link to their parent
  session. DST-ordered buckets, and shared-report allowlists for provider, origin, effort, and public
  model families.
- Declined with real-data evidence:
  - cross-file Codex dedup (0 of 10,746 sessions span two files);
  - Codex rows without an ordinal (0 of 360,729);
  - naive timestamps (0 of 475,244);
  - sub-millisecond timestamps (0 of 380,213).
- N1, a new finding: Claude Code subagent transcripts often lack a request's final usage row. On the
  owner's machine that was 1,555 of 3,150 subagent requests, 209 of 219 workflow-agent requests, and
  0 of 3,283 main-thread requests. Those observations are flagged `output_not_final` with lower-bound
  output.
- Size, measured on 379,401 real observations:
  - history database: 990 MB → 140 MB (schema 2 with automatic migration);
  - Tokenatlas: 270 MB → 10.5 MB (a gzip-compressed columnar payload);
  - load time: about 10 s → about 2 s;
  - `report --html`: 53 s → 15 s.

  Migrated records, fresh imports, and rebuilt report records were compared with the schema 1 output
  and are identical.
- The Tokenatlas summary cards follow the standard split: Totalt · Input · Cache write · Cache read ·
  Output.
- Verification: CI is green on Linux, macOS, Windows, Chromium, and WebKit, and 104 tests pass.
- Delegation:
  - implementation leaves on Sonnet 5.5;
  - four read-only `gpt-5.6-luna` xhigh reviews through the headless Codex CLI;
  - the conductor reproduced every finding and did every real-data check itself.

## Continued implementation — 2026-09-26

- Restored work now lives in the persistent `.worktrees/history` checkout on `feat/history-a1`.
  Changes remain uncommitted and unpublished; the installed monitors and external services are unchanged.
- Added Pi history/`why.py` support from `~/.pi/agent/sessions`: provider-scoped response identity,
  copied-fork deduplication, original-session attribution, project/model/token mapping, and explicit
  synthetic fallback identity. Reasoning keeps Pi's existing subset-of-output semantics.
- Added OpenCode support from its local WAL-backed SQLite database through a query-only connection.
  Message records are parsed transiently; only session/message attribution and allowlisted counters
  are retained. Transcript content, parts, credentials, account tables, and tool data are not retained. OpenCode's additive reasoning counter
  is folded into normalized output and also retained as a separate subtotal.
- Session identities are harness-scoped in JSON summaries and Tokenatlas, preventing equal raw IDs
  from merging across harnesses. OpenCode database/WAL/SHM metadata participate in checkpoints;
  concurrent changes force a retry, and unsupported schemas are recorded as partial imports.
- Tokenatlas copy is shorter and neutral. New cache-read-share comparisons cover session, harness,
  model, and project, with highest/lowest comparable groups, observation counts, input volume, and
  explicit unknown/synthetic coverage. They follow all active filters and time zooms.
- Live acceptance imported 7,138 OpenCode calls and 1,670 Pi observations from 44 session files.
  The combined private report contained 8,808 observations and 2,363,267,733 known tokens.
  Chromium and WebKit passed totals, cache comparison math, bucket conservation, filters, empty state,
  zoom, drilldown, export, mobile overflow, zero console errors, and zero network requests.
- Verification: 69 discovery tests, standalone interactive-export regression, Python compilation,
  JavaScript syntax, and `git diff --check` pass. Desktop/mobile screenshots were visually reviewed.
- Added 1.2.0 packaging with the `energy-monitor` console command, packaged offline template, Windows
  timezone dependency, changelog, install/update/uninstall guidance, and retained-data semantics. The
  release smoke builds a wheel from a clean source copy, installs outside the checkout, refreshes twice,
  runs doctor/report, checks privacy and POSIX permissions, reinstalls, uninstalls, and verifies that the
  user database remains. GitHub Actions covers Python 3.10-3.13 on Linux/macOS/Windows and offline
  Chromium/WebKit; runner execution remains the only unverified release gate until the branch is pushed.
- The first native quality reviewer could not start because its configured API credential was absent;
  M5 file review was unavailable because no blind-context roots were configured. A native general
  reviewer then completed an independent read-only review (runtime model/effort unavailable; usefulness:
  pass). Its four findings were reproduced and fixed: default all-harness failure isolation, scoped session
  filter round trips, malformed OpenCode row diagnostics, and dimension-specific cache-comparison tests.
  Conductor review additionally fixed summary session collisions and unsupported-schema diagnostics.
  Pricing (#12), benchmark integration (#13), and remaining surfaces (#15) remain pending.
- A second independent native general review covered the complete release diff (runtime model/effort
  unavailable; usefulness: pass). It found Windows ownership/mode portability, an occupied 1.1.0 tag,
  Linux screenshot paths, and overstated machine-ID wording; all were fixed and reviewed again. The final
  follow-up reports no local blockers. No commit, push, release tag, package publication, or deployment.

**Last session:** 2026-09-26
**Branch:** feat/history-a1

## Local implementation ready (2026-09-26) — seminar A1 / issue #10

- Issue #10 implemented locally in the isolated `feat/history-a1` worktree at
  `/private/tmp/energy-history-a1`, based on `a5192ebd367e55c77a24c0be6dd47afbdce0f834`.
  Changes are uncommitted and unpublished. The original checkout and installed statusline
  remain unchanged. Issues #10–#16 contain the seminar backlog and staged OK/NOK demos.
- Added `python3 -m usage refresh|report|doctor`: standard-library SQLite storage,
  incremental changed-file import, atomic checkpoints/writer locking, retained history,
  provenance, private file permissions, source/error diagnostics and local-time
  day/hour/minute buckets. Supports Claude Code and locally available Codex rollouts.
- Extended existing readers with stable event identities, full project identity,
  observed/derived/absent turn references, parent session, harness version and allowlisted
  usage counters. Copied logs merge; source removal never removes stored observations.
  Reports group by project/session/turn/model/effort/harness/origin. Prompt and tool text
  are not persisted. Malformed rows, unreadable directories/files, invalid usage timestamps,
  partial tails and files changed during reading are surfaced.
- Accounting decision: v1 stores request **observations**, not verified billed inferences.
  Advisor iterations can have separate model/counters; retain original sanitized iteration
  snapshots, derive compatible counter maxima separately, flag conflicts, and never add the
  whole iteration array to parent usage. Unknown fields remain null. Ambiguous event identities
  are excluded from known subtotals and shown separately. `billing_verified` and
  `coverage_complete` remain false. See `docs/history-accounting.md` for evidence and scope.
- README/FINDINGS now qualify older thinking-token/statusline claims by version/date and
  reference the later validation. Historical findings are preserved rather than presented
  as universal current behavior.
- Verification: **45 unittest discovery tests pass**, standalone `test_interactive_export.py`
  passes, top-level Python plus `usage` compile, and `git diff --check` passes. New regressions
  for review findings and error cases were demonstrated red before correction. The suite
  covers retention, copy/window identity, snapshot merging/replay, missing counters, DST,
  concurrent writers, transactional rollback, read failures and diagnostics. Existing why/Pi/export behavior remains covered.
- Live local import: 121 Claude files and 139 Codex files (Codex limited to 2026-09-26),
  9,582 deduplicated observations, 54 project identities, 13 model groups, 6,960 linked
  turn observations, no normalization warnings; 3.69 seconds. These counts are a snapshot,
  not full-account coverage. A closed Codex session with 340 observations in two source
  files and 60 turn groups reconciled exactly on normalized counters and event IDs.
  Private demo database/reports are outside Git in an OS temporary directory.
- Delegation evidence: parser/identity and historical-doc leaves requested native worker
  `gpt-5.6-luna`, high; runtime-observed model/effort unavailable. Parser leaf **partial**:
  18 focused tests passed, but conductor/reviewer corrections were needed for weak identity,
  strict errors and iteration merging. Docs leaf **pass** after conductor review.
  Independent read-only review requested native `gpt-6-sol`, high; runtime-observed values
  unavailable; **pass** usefulness: four initial grounded defects and two followup gaps
  were reproduced, fixed and regression-tested. Final integration and checks owned by root.
  No separate CLI delegation or paid API evaluation was used. Munin unavailable in this session.
- Next demo step: #11 offline HTML on this history. Cost/provenance is #12, benchmark runs
  and quality/time comparison #13, Pi/OpenCode adapters #14, remaining surfaces #15.
  No scheduler, deployment, sync, billing estimate or HTML was installed in A1.

## Completed This Session (2026-09-03) — cross-harness `why`

- Added `why.py`, a stateless attribution command for Claude Code and Codex. It accepts a local
  calendar date or trailing-hour window and ranks usage by project, thread kind, entrypoint, agent,
  exact model, effort, and session. Text and JSON output are supported; no ledger or cache is written.
- Claude collection scans main and subagent transcripts, filters by response timestamp, and
  deduplicates streamed content-block rows by `requestId` using the maximum of each token field. For
  a full calendar day it reports the older statusline counter's output/cache-read coverage.
- Codex collection uses per-turn `last_token_usage` rather than session totals and attributes each
  event from the latest `turn_context`. This fixes the old calculator's session-start-day bucketing
  and exposes CLI, exec, automation, subagent, and Codex Desktop origins from the shared rollouts.
- Live verification found another Codex invariant the design had missed: identical cumulative token
  snapshots are sometimes emitted repeatedly, including with repeated non-zero deltas. They are now
  deduplicated. A cumulative counter can also reset inside one rollout; reconciliation therefore
  treats the file as monotonic segments rather than comparing only with its final snapshot.
- Confirmed that current Codex rollouts record `turn_context.payload.effort` per turn, resolving the
  open T0.1b question. Corrected stale cache-write documentation.
- Conductor review corrected local-time handling so historical calendar days use the UTC offset for
  the selected date (including DST boundaries), not the offset in effect when the command runs.
- Verification: 20 discovery tests pass; the standalone interactive-export regression passes; all
  top-level Python modules compile; `git diff --check` passes. Twenty current real Codex rollouts
  reconciled 2,116 deduplicated calls exactly across 21 cumulative counter segments. Live text and
  JSON smoke tests passed for both harnesses.
- Review disclosure: the required M5 implementation leaf returned tests for a fabricated schema and
  was rejected as `wrong`. A later read-only M5 review with `qwen3-coder-next-80b` was `partial`: one
  of eight hypotheses exposed a real low-severity missing-`cwd` fallback bug, fixed with a red/green
  regression; the other seven were rejected against the code and observed schema. A third Claude
  Code review attempt had no tools or persistence but still returned no review after 103 seconds;
  although Opus was requested, its result metadata reported `claude-haiku-4-5`. Conductor review
  found no remaining blocker.
- Committed as `4d7c59d` and published for review in PR #9. GitHub reports no configured CI checks
  for the branch; the verification above was run locally before publication.
- Deliberately out of scope: no ledger, persistence, sync, or energy-model changes.

## Completed This Session (2026-08-10) — usage attribution design

Design session, no code changes. Question: "where did my tokens go?" — after burning half a weekly
allowance in ~30 minutes across several parallel agents, with no way to tell whether the cause was a
runaway agent, an expensive setting, or something running unattended on another machine.

- **Measured the coverage gap that motivates the work.** Compared the monitor's own totals against a
  `requestId`-deduplicated sum of every call in the local transcripts (7-day retention, 3 days with
  activity). On a quiet single-session day coverage was **100%** (24,900 output tokens both sides);
  across the whole window it was **44% of output and 43% of cache reads** (185,162 vs 420,056;
  23.8M vs 55.8M), and the worst single day recovered only **10% of cache reads**. The monitor is
  exact when the workload is simple and blind exactly when it is complex.
- **Located the missing mass.** Subagents were 29% of output and 34% of cache reads;
  non-interactive runs (`entrypoint: sdk-cli`) were 41% of output and 35% of cache reads. Both are
  structurally invisible to a statusline-driven collector, which only ever sees the main thread's
  latest response. The split moves with workload, so it is not a correction factor that could be
  applied to an aggregate after the fact. This confirms and quantifies
  [issue #4](https://github.com/Magnus-Gille/claude-code-energy-monitor/issues/4).
- **Found two live attribution defects.** A session stores one "current model", so model-switching
  sessions misattribute; and unknown model ids fall through to the most expensive tier — `fable`
  was being counted as Opus, and `haiku` never appeared at all because it was only ever used by
  subagents.
- **Re-validated the stale token-accounting findings.** On v2.1.226 the documented "transcript output
  excludes thinking, expect ~3×" gap **no longer holds** (matched within 1% on a full day, 0.99–1.12×
  per session), and `input_tokens ≤ 1` has fallen from 75–93% to **6%** of deduplicated calls.
  `FINDINGS.md` is now wrong on both points and is ticketed for correction.
- **Verified the available data surfaces** rather than designing against memory. Transcripts now
  carry `agentId` / `attributionAgent` (exact subagent identity), top-level `effort`, `entrypoint`,
  `service_tier`, `speed`, and the nested 5m/1h cache-write split — but Anthropic documents the
  format as internal and unstable. OpenTelemetry is the documented, supported, complete surface
  (`claude_code.token.usage` and the `api_request` event carry model, all four token types, cost,
  request id, effort, speed and subagent attribution, for headless runs too). The statusline payload
  remains the **only** documented local read of subscription quota
  (`rate_limits.{five_hour,seven_day}` with `resets_at`). The separate per-model weekly limit is not
  programmatically observable at all.
- **Wrote [`docs/token-attribution-architecture.md`](docs/token-attribution-architecture.md)** — the
  data model changes from a daily aggregate to a per-call ledger deduplicated on
  `(provider, request id)`, plus budget pools and meter readings. Because meter readings are
  account-global while the ledger is per-machine, unattributed burn on another machine becomes
  *detectable*. Harness- and provider-agnostic by construction: Claude Code, Codex, Pi and future
  harnesses are collectors; Anthropic (subscription or API), OpenAI, OpenRouter and self-hosted
  models are pools differing only in how their meter is read.
- **Corrected `research/multi-cli-support.md` on Codex**, verified directly against a live rollout
  file. It recorded "no cache-write tracking"; `cache_write_input_tokens` is in fact present in both
  `last_token_usage` and `total_token_usage`. Codex also now records sub-agent spawn structure
  (`source.subagent.thread_spawn.*`, `thread_source`), `model_provider`, credits balance, and
  supports OTLP export. One trap worth remembering: rate-limit windows must be identified by
  `window_minutes`, **not** by whether they land in `primary` or `secondary` — the file inspected had
  the weekly window (10080) in `primary` with `secondary: null`.
- **Wrote [`docs/token-attribution-epics.md`](docs/token-attribution-epics.md)** — 11 epics across 6
  milestones, ticket-level, scoped for an implementing agent. Milestone 0 is a deliberately
  throwaway single script that answers the original question from transcripts with no
  infrastructure, so the output format is validated against a real user before the schema, sync
  protocol and remaining epics are built on top of it.
- **Ran an adversarial review of the draft and rewrote what it broke.** Four findings were verified
  against live data and changed the design:
  - **Content-block lines do NOT carry identical usage.** 199 of 583 multi-line `requestId`s differ —
    early lines hold a placeholder `output_tokens` (2–4), only the terminating line has the real
    value (`4 → 4 → 254` in one case). Last-write-wins would let a placeholder win permanently when a
    scheduled collector reads mid-turn. Dedup is now **max per token field**, verified safe: last
    line equals per-field max on 694/694 request ids.
  - **The quota-regression idea is dead.** `used_percentage` decreased *without a window reset* 214
    times (5h) and 42 times (7d) over 3.6 days — the windows roll, so Δ is `burn added − burn
    expired`. It is also integer-quantized and the four token types are collinear. A four-way fit
    would return negative coefficients and confidently blame an invisible machine. Replaced with a
    single scalar on fixed price-ratio weights, heavily gated.
  - **`resets_at` is a Unix epoch integer**, not an ISO-8601 string.
  - **Meter sampling is useless where it matters most**: median gap between readings 2 s, p90 12 s,
    **max 20.3 hours** — dense while you type, absent during exactly the unattended burn the tool
    exists to catch. Coverage reporting is therefore the permanent answer, not a placeholder.
  - Also fixed: `fidelity` must travel on the record or cross-machine merge is undefined; Gemini's
    reasoning tokens are **additive**, not a subset, and a validator cannot catch the mistake; OTel's
    per-call data is in the **log events**, not the token counter metric; `duration_ms` was missing,
    without which "several agents in parallel" is not computable.
- **Checked the design against the actual incident and re-sequenced it.** The burn described in the
  voice memo was on the **ChatGPT/Codex** subscription, not Claude Code — the Codex rollouts show the
  weekly window swinging 20% → 99% → 1% over three days with readings dropping to 0% around 08-08
  22:10, the signature of a plan being cancelled and replaced. Every milestone had been sequenced
  around Claude Code because that is where the evidence and existing instrumentation were, which
  would have meant four milestones of work before anything could speak to the triggering episode.
  Three corrections applied:
  - **Codex moved from milestone 4 to milestone 0** (new `T0.1b`). Its per-call data is in some
    respects better than Claude Code's — per-turn deltas rather than snapshots, explicit reasoning
    split, cache writes, `model_provider`, and real subagent spawn structure. Open item: whether an
    effort/reasoning level is recorded per turn is **confirmed for Claude Code but not for Codex**.
  - **Spike attribution now keys off the ledger's own token burn rate, not the meter curve**
    (`T7.6`). This is the highest-value rule and needs no calibration at all: for a concentrated
    burst, "everything that ran between 14:02 and 14:31, ranked" is a complete answer.
  - **New ticket `T3.5` for meter-series reconstruction.** Raw readings cannot be concatenated in
    timestamp order: **162 distinct `resets_at` values for one weekly window in three days**, nine
    sessions publishing simultaneously, and a naive merge showing **+83 percentage points in 1.1
    seconds** of pure artefact. `resets_at` on a rolling window slides continuously and is **not** a
    window identifier — `T6.2` was rewritten to use explicit trailing durations instead.
- **Not done:** nothing implemented; no GitHub issues filed. The architecture is for review first —
  filing the ticket set is the next step once it is agreed. Two questions block schema freeze:
  whether `message.usage.iterations[]` means the billing grain is finer than `requestId`, and whether
  quota readings are genuinely account-global.
- **Verification:** `python3 -m unittest discover` (7 tests) and `test_interactive_export.py` pass;
  all modules compile. No functional code changed this session.

## Open observation

- [Overlapping `remote_sync.sh` runs](docs/remote-sync-overlap-observation-2026-08-02.md) records the 2026-08-02 process inspection: approximately 110 overlapping process entries and 74 SSH/rsync workers, with likely missing locking and transfer timeouts. No processes were terminated and no behavior was changed.

## Completed This Session (2026-07-16) — Pi coding harness support

- Added `pi_status.py`, a companion status line that reads Pi's normalized assistant usage from `~/.pi/agent/sessions/` and reports day/week/month token and order-of-magnitude energy totals. Calls are attributed by response date; cache reads/writes are counted separately; reasoning remains a subset of output.
- Added fork/clone-safe response deduplication. Pi copies earlier session entries when branching into a new file, so naïvely summing files double-counts old calls; the parser deduplicates on response identity while preserving new branch calls and session counts.
- Added `pi_stepcount.py` for shareable period and table summaries, plus `--copy` and optional rough-energy output.
- Updated README and multi-CLI research documentation. Clarified that the existing `pi_scanner.py` refers to a Raspberry Pi running headless Claude Code and is unrelated to the Pi coding harness.
- Made `AGENTS.md` explicitly shared across OpenCode, Claude Code, and Pi. Pi consumes it natively and does not need a duplicate adapter file; OpenCode-only persona commands are now clearly scoped.
- Added `test_pi_status.py` regression coverage for response-date attribution, cache fields, reasoning semantics, fork/clone deduplication, malformed lines, and zero-usage errors.
- Independent reviews: Claude Code found no blockers in the initial pass. Codex CLI (`gpt-5.6-sol`, xhigh) then reviewed PR #8 and found four actionable items, all addressed: documented unobservable compaction/branch-summary calls, reused parsed data for `--file`, added a persistent mtime/size-keyed session cache, and corrected the multi-CLI cache-write guidance. Earlier Claude robustness findings were also addressed by preferring provider `responseId` for deduplication and making source-session attribution independent of file ordering.
- Verification: `python3 -m unittest discover -v`; `python3 test_interactive_export.py`; Python compilation; live `pi_status.py` text/JSON and `pi_stepcount.py` day/table smoke tests; instruction audit (0 errors, 0 warnings).

## Completed This Session (2026-07-01) — full mesh between m5 and laptop

Closed the "hub-and-spoke, not full mesh" gap noted in the previous session, scoped to m5↔laptop (Pi stays hub-and-spoke — laptop still just pulls it, one-directional).

- **`interactive_export.py`** (new) — converts a machine's interactive session stats (`statusline_session_history.jsonl` archived days + `statusline_daily.json` today, in-progress) into the same journal/rollup schema `pi_scanner.py` produces for headless sessions, writing `~/.claude/interactive_journal_raw.jsonl` + `interactive_rollup_raw.jsonl`. Fully regenerates both output files from scratch each run (source files are the source of truth, no state of its own → always safe to re-run, no dedup needed). This closes a second, pre-existing gap: previously the laptop never pulled m5's *interactive* stats either, only its headless ones.
- **`pi_sync.sh` → `remote_sync.sh`** (renamed + generalized) — now pulls 4 files per remote (`pi_journal`/`pi_daily_rollup` headless + `interactive_journal`/`interactive_daily_rollup`) instead of 2; missing files (e.g. Pi has no interactive stats) report "not found" only for rsync's expected exit 23 (file absent), anything else (255 = connection/auth failure, etc.) now surfaces as an explicit `ERROR` line instead of being silently swallowed as "not found". New `REMOTE_HOSTS_OVERRIDE` env var (space-separated `tag:host` pairs) lets the same script run with a different host list on a different machine — this is what makes it usable for the reverse leg below instead of needing a second script. `advisor.py`'s stale docstring reference to `remote_sync.sh` (it said this before the file was ever renamed) is now accurate.
- **Reverse leg wired: m5 pulls from laptop.** Laptop had no SSH server at all (`RunSSH: false`, Remote Login off). Tried Tailscale SSH first (tailnet-only, no key management) — dead end, the macOS Tailscale GUI app is sandboxed and explicitly doesn't run the SSH server in that build. Fell back to plain macOS Remote Login (`sudo systemsetup -setremotelogin on`, run manually by Magnus — sudo needs a real TTY, doesn't work through Claude Code's sandboxed Bash). Generated a fresh ed25519 keypair on m5 (it had none — it could only receive inbound SSH, not initiate outbound) and added its public key to the laptop's `~/.ssh/authorized_keys`, with `from="<m5's Tailscale IP>",no-pty,no-agent-forwarding,no-X11-forwarding,no-port-forwarding`. **Scoped honestly**: this restricts the source IP and blocks pty/forwarding, but is *not* read-only-command-restricted — it does not stop arbitrary non-interactive commands from that source (a proper `command="rrsync -ro ..."` forced-command restriction would, but the laptop only has macOS's `openrsync`, not GNU rsync's bundled `rrsync` script, and installing GNU rsync just for this risks shadowing `openrsync` for this machine's own outbound syncs to Pi/m5 — judged not worth it for a 2-node personal tailnet where both ends are already the same person's hardware). m5's `~/.ssh/config` gets a `Host laptop` alias using the Tailscale MagicDNS name `magnus-macbook-air` (resolves on m5; preferred over the raw Tailscale IP in case it ever changes). m5's cron: `*/30 * * * * REMOTE_HOSTS_OVERRIDE="laptop:laptop" remote_sync.sh`.
- **Side effect, not a bug**: the laptop's own local `pi_journal.jsonl`/`pi_daily_rollup.jsonl` are themselves relayed copies of Pi's data (tagged `pi` by the laptop's own `REMOTE_HOSTS`, which happens to collide with `pi_scanner.py`'s hardcoded local filename) — so m5 pulling "the laptop's" journal files transitively picks up Pi's headless data too, landing in `laptop_journal.jsonl` on m5. Not a double-count risk: every entry carries its own embedded `machine` field (`pi_scanner.py`/`interactive_export.py` always set it), which `load_remote_journals()` uses in preference to the filename-derived tag — so attribution stays correct (`machine: "huginmunin"`) regardless of which hop relayed it. Net effect is a small mesh bonus (m5 now also sees Pi's data, one hop removed) rather than a problem.
- **Codex review caught a real critical bug before merge**: the first version named the local export files `interactive_journal.jsonl`/`interactive_daily_rollup.jsonl` — which matches the *same* `*_journal.jsonl`/`*_daily_rollup.jsonl` glob `advisor.py`/`stepcount.py` use to merge in *remote* machines' data. Result: any machine that ran the exporter double-counted its own interactive usage locally (native `statusline_*` data + its own glob-matched export), reproduced with a temp-`HOME` fixture (10/20/30/40 tokens → 20/40/60/80). Fixed by renaming to `interactive_journal_raw.jsonl`/`interactive_rollup_raw.jsonl` (deliberately not ending in the glob's suffix); `remote_sync.sh` reads those two literal names directly, and only the tagged copy it writes on the *other* machine (`<tag>_interactive_journal.jsonl`) is meant to match the glob. Verified post-fix: `advisor._remote_journal_files()` no longer lists the local raw files, and `load_daily_history(include_remote=True)` today-total matches `include_remote=False` exactly (no self-inflation) on both laptop and m5.
- **End-to-end verified both directions**: laptop's `advisor.py --today` and m5's `advisor.py --today` both run clean post-sync with no errors; `m5_interactive_daily_rollup.jsonl` visible on laptop, `laptop_interactive_daily_rollup.jsonl` visible on m5, each with the correct `machine` tag. Pi happened to be offline (mDNS `huginmunin.local` unresolvable) during the final verification pass — surfaced correctly as an `ERROR` line rather than a misleading "not found", which incidentally validated the exit-code fix above.
- **Crontabs**: laptop gained `*/15 * * * * interactive_export.py`; m5 gained `*/15 * * * * interactive_export.py` + the new `*/30` reverse pull. Existing `pi_scanner.py` (m5) and `remote_sync.sh` (laptop, renamed from `pi_sync.sh`) crons unchanged in cadence, just repointed at the renamed script / doing more work per run.
- **Round 2 Codex findings, also fixed**: `remote_sync.sh`'s rc==23 check treated *any* rc=23 failure as "not found", but rc=23 ("partial transfer") also covers permission-denied and other real errors on the remote read, not just a missing file — Codex reproduced this with a faked rc=23 + "Permission denied". Now requires the actual `No such file or directory` text rsync/openrsync both emit for a genuinely absent file; any other rc=23 (or any other exit code) is an `ERROR`. Verified against both a real missing file and a simulated permission-denied rc=23.
- **Not done**: Pi has no reverse leg (still pure hub-and-spoke, one-directional into the laptop) and no interactive stats of its own (headless-only box) — true 3-way full mesh was out of scope for this session, which was scoped to "m5 and laptop" specifically. Test coverage stays narrow: one regression test (`test_interactive_export.py`) tied to the double-counting bug it actually caught, not a general-purpose suite for the repo.

## Completed This Session (2026-06-30) — m5 onboarding, generalized multi-machine sync

Set up the monitor on the m5 home-inference box (3rd machine, alongside laptop + Pi) and generalized the sync mechanism so it scales to N machines instead of being Pi-specific.

- **`pi_sync.sh` generalized** — now loops over a `tag:host` list (`pi:huginmunin.local`, `m5:m5`) instead of pulling a single hardcoded host. Each remote's synced files land at `<tag>_journal.jsonl`/`<tag>_daily_rollup.jsonl` in `~/.claude/` on the laptop, so multiple machines no longer collide on the same filename (the old script always wrote `pi_journal.jsonl` regardless of source — fine for one remote, broken for two). Pi's filenames are unchanged (`tag=pi`), so no migration was needed.
- **`advisor.py`/`stepcount.py` generalized** — `load_pi_journal`/`_merge_pi_rollup` renamed to `load_remote_journals`/`_merge_remote_rollups` and now discover remote files by glob (`*_journal.jsonl`, `*_daily_rollup.jsonl`) instead of one hardcoded Pi path. Adding a 4th machine later needs zero code changes — just adding it to `REMOTE_HOSTS` in `pi_sync.sh`. `advisor.py --no-pi` kept as a CLI alias for the renamed `--no-remote`.
- **`pi_scanner.py`**: added m5's newer JSONL entry types (`mode`, `permission-mode`, `attachment`, `ai-title`, `custom-title`, `agent-name`, `agent-color`) to `KNOWN_ENTRY_TYPES` — these aren't usage-bearing but weren't recognized, so the first scan on m5 logged a warning per entry; would have spammed syslog every 15 min via cron.
- **m5 deployed**: repo already cloned at `~/repos/claude-code-energy-monitor` (same commit as laptop). Symlinked `~/.claude/statusline.py` → repo copy (same pattern as laptop), added `statusLine` to `~/.claude/settings.json`, added `*/15 * * * * python3 .../pi_scanner.py | logger -t pi-energy-scanner` cron (same pattern as Pi). Backfilled 4 existing sessions from m5's `~/.claude/projects/`.
- **Architecture is hub-and-spoke, not full mesh**: laptop pulls from both Pi and m5 via cron (`*/30 * * * * pi_sync.sh`) and merges all three sources when running `advisor.py`/`stepcount.py` locally. Pi and m5 do **not** pull from each other or from the laptop — they only run the local headless scanner. If Magnus wants the combined view from the Pi or m5 directly (not just the laptop), that would need `pi_sync.sh` (or a flipped variant) deployed there too — not done, since the laptop is the practical place this gets checked.
- **End-to-end verified**: `pi_sync.sh` run on laptop after m5 deploy shows `m5_journal.jsonl`/`m5_daily_rollup.jsonl` synced alongside the existing `pi_*` files; `stepcount.py -d` session count rose from 616→620 (the 4 backfilled m5 sessions); `advisor.load_remote_journals()` shows `machines seen: {huginmunin, m5}`.

## Completed This Session (2026-06-24) — external calculator comparison (aifootprintcalculator.org)

Analysis session (no code changes). Helped fill in an external SV energy calculator with this user's real CC usage, then reverse-engineered its method from its JS bundle and compared to ours.

- **Real 30-day usage (rollup, 2026-05-26..06-24):** output 31.7M, fresh input 8.0M, cache_read 5.77B, cache_write 140M tokens; ~3,400 prompts (estimated — only 8 days of raw transcripts survive nightly cleanup, JSONL `input_tokens` are placeholders so rollup is the better monthly source). Our model on this usage: **~2,000–2,500 kWh/yr** (Opus-anchored mult=1.0 → 2,467; fleet-mix ~0.82 → 2,023). Cache = ~77% of our estimate (read 43% + write 34%).
- **aifootprintcalculator.org says 30,307 kWh/yr — ~12–15× our full estimate, ~50× on a per-token basis.** Internally consistent (0.02 kWh/charge × 30,307 ≈ 1.515M phone charges shown).
- **Their method (extracted from `assets/index-*.js`):** top-down per-request. `gpuKwh = avgWhPerStandardRequest × prompts × Sr(out,22/7) × Sr(in,292/173)/1000`, `total = gpuKwh × 2.44` (PUE/overhead), `co2 = total × regionFactor` (Sweden 10.83, Anthropic-blended 509 gCO2e/kWh). `Sr(x,k)=1+(x−300)/600×(k−1)`, standard request = 300/300 tok. Per-model anchors from published data (Claude Opus ≈ 4.05 Wh/std-req, Sonnet ≈ 0.85). No cache term. Training amortization optional, off by default. Output:input weight 3.1× (ours 3.6×).
- **Root cause of the gap:** (1) **multiplicative input×output coupling** (`n·r`) — at ~9.4k out/2.4k in per prompt the input factor 3.35× re-multiplies the whole request → 112× a standard request; no physical basis, explodes on long-context agentic prompts; (2) global **×2.44 overhead** we omit; (3) higher base anchor (~7× our GPU/300-tok figure). Their model is implicitly calibrated for short chat turns.
- **Actionable finding for us:** the **×2.44 PUE/overhead** is a legitimate critique of our GPU-only model — we likely understate *delivered* energy (lit. PUE ~1.4–2.0; +non-GPU ~2.4). Their per-named-model published anchors are also better-sourced than our 3-tier multiplier guesses. Their lack of any cache term conversely **confirms our core thesis** (cache-aware accounting is the agentic-tool differentiator). Offered but NOT yet done: (a) `docs/` comparison note + FINDINGS.md entry, (b) prototype optional PUE factor.

## Completed This Session (2026-06-17) — API-cost reconciliation + filed issue #4

Analysis session (no code changes). Question: "what would the last 30 days have cost via API instead of the Max sub?"

- **Settled answer: ~$3,650 / 30 days** (band $3.5–4k) — ~18× effective discount vs the ~$200 Max sub, *not* the 80–250× a naive transcript estimate implied. Cost is ~96% cache reads (billed 0.1×); split ~$2.0k over the light pre-ramp 22 days + ~$1.7k over the heavy last 8 days (post Max ×20 upgrade).
- **Reconciled two wildly different estimates** (rollup-based ~$3.1k/30d vs another agent's transcript-based ~$11k/9d → implied $20–38k/30d) via a multi-agent workflow + adversarial verify. Found **two opposite-direction measurement defects**:
  - **(A) Rollup undercounts ~15%.** `update_daily()` is fed only main-session per-render snapshots → subagent/workflow (Task/Workflow) calls contribute **zero**, and calls between renders are dropped (`captures_subagent: no`, `captures_workflow: no`).
  - **(B) Naive transcript summation overcounts 2.21×.** CC writes one JSONL record per content block (thinking/text/each tool_use), all sharing one `requestId` + identical `message.usage`. 40,973 blocks → 19,257 real calls (53% dupes). Raw cache_read 5.12B → 2.32B deduped. **Must dedup by `requestId`.**
- **Filed [issue #4](https://github.com/Magnus-Gille/claude-code-energy-monitor/issues/4)** capturing both fixes (rollup subagent-capture path via `sum_jsonl.py` reconciliation + document the requestId-dedup requirement in FINDINGS.md). Added to Grimnir Roadmap board.
- Both defects were latent: (A) was flagged 2026-03-31 (Munin `enhancements-cache-analysis`) and never built; (B) is a third JSONL pitfall not yet in FINDINGS.md (which documents the opposite-direction input/output undercount).

## Completed This Session (2026-05-31) — fix context percentage display

- **Removed `CLAUDE_CODE_DISABLE_1M_CONTEXT=1`** from `~/.claude/settings.json`. The flag was set in Apr 2026 to cap context to 200k for cost control. In CC v2.1.158 it no longer caps real usage (debug log showed `total_input_tokens` routinely reaching 400–600k, peak 598k) but still forces `context_window_size: 200000` in the statusline payload. Result: `used_percentage` was computed against a denominator 3–5× too small, pinning to 100% whenever actual usage exceeded 200k.
- **No statusline.py changes needed** — `Ctx:%` is read verbatim from the payload. After removing the flag and restarting CC, Claude Code should report a 1M window and the percentage will be accurate.
- **Follow-up (low priority):** could harden `statusline.py` to recompute `ctx_pct = total_input_tokens / context_window_size` directly so a future label/reality mismatch can't silently mislead.

## Completed This Session (2026-05-30) — accuracy audit + fixes → **v1.0.0**
Deep-dive audit (Claude Opus 4.8, CC v2.1.157): multi-agent research workflow (49 agents, adversarial verification) + `ENERGY_DEBUG` re-validation. **Merged to master via PR #3 (squash `6aa2619`), Codex-reviewed, and cut as the repo's first tagged release `v1.0.0`.**

- **Token accounting fix (critical).** CC **v2.1.122** redefined `context_window.total_input_tokens`/`total_output_tokens` from cumulative session counters to **current-context snapshots** (`total_input = input+cache_creation+cache_read` of latest response; `total_output` = that response's output, resets per call). The old `update_daily` delta logic was over-counting fresh input **~53×** (tracking context growth) and under-counting output; cache terms unaffected. **Rewrote `update_daily`** to accumulate per-call `current_usage`, detecting call boundaries (input-side change OR output reset — also fixes the consecutive-identical-fully-cached-call miss). Fresh input = `total_input − cache_read − cache_creation`. **Replay-validated against ground truth on 392 captured fires: exact match on all 4 token types.** Fresh-input energy share drops from spurious ~12–15% → ~1%.
- **Per-model multipliers** (decision): Haiku ×0.3 / Sonnet ×0.6 / Opus ×1.0 (price-proxy 1:3:5, discounted for sub-linear scaling; order-of-magnitude). statusline weights today + future via `d['by_model']`; legacy history days fall back to 1.0×. stepcount mirrors with a Pi residual term. Fleet mix: Opus 56% / Sonnet 43% / Haiku 1%.
- **E_CW** (decision): value kept at 490, **relabeled** "prefill + pricing-derived infra surcharge proxy" (cache creation = prefill FLOPs; +25% has no measured energy basis).
- **Quota source**: `/api/oauth/usage` (now 429-dead) → statusline payload `rate_limits.{five_hour,seven_day}.used_percentage` (CC v2.1.80+), OAuth as fallback.
- **Constants unchanged in value** (390/1400/15/490): new 2026 evidence (Oviedo/*Joule* 0.34 Wh + 4–20× overstatement; ML.ENERGY v3.0 + B200 −35%; Jegham Claude-3.7) all sit within ±3× and bracket E_OUT. **E_CACHE is the most leverage-sensitive constant** (cache_read volume ~23× cache_write). Workload inverted Feb→May: output-dominated → cache_write-dominated (~40%), driven by a 23% drop in cache_read reuse (not volume, +9%).
- **Docs**: FINDINGS.md ~5 kWh → ~9 kWh arithmetic fix + audit section; README Opus 4.6→4.8, corrected validated-semantics claims, workload-dependent breakdown; new `docs/energy-constants.md` "2026-05-30 audit update". Noted Opus 4.7+ tokenizer (+0–35% tokens) and Opus 4.8 cache-min 4096→1024.
- **Files**: statusline.py, energy_constants.py, stepcount.py, pi_scanner.py, README.md, FINDINGS.md, docs/energy-constants.md. All compile; statusline replay/e2e/smoke-tested; advisor + stepcount run clean on live data.

## Completed This Session (2026-04-18)
- **Token optimization analysis** — advisor.py + breakdown shows Opus at 89-93% of weekly quota; 9 sessions hit >80% ctx; 257 extended-context sessions (now capped via CLAUDE_CODE_DISABLE_1M_CONTEXT=1, already set on both laptop and Pi).
- **Pinned 4 skills to Sonnet** (close, capture, index-artifacts, submit-task) — added Model Check step: "stop and ask the user to run /model sonnet, wait for confirmation before proceeding." Also pinned submit-task. Reviewed by Codex CLI (PR #1) — wording clarified to make delegation explicit.
- **Deleted duplicate `/index-artifacts` command** — `~/.claude/commands/index-artifacts.md` was stale (old `~/mgc/` path). Deleted from both laptop and Pi.
- **Skills repo PR #1 merged** — `github.com/Magnus-Gille/claude-skills`. Established PR workflow (direct push to main is blocked). Codex cross-model review integrated.
- **grimnir-bot added as collaborator on claude-skills** — Pi's GitHub identity; was blocked from pulling. Invite accepted, Pi remote switched to SSH, `git pull` now works.

## Completed This Session (2026-04-10)
- **plot_daily.py improvements** (3a53332) — stacked bars (compute vs cache), gap-filling for missing days, 7-day trailing average (causal, no edge artifacts), correct window semantics. Identified input undercounting bug on low-activity days and cache_write growth trend as monitor improvement signals.
- **Memory/CLAUDE.md audit and cleanup** — full audit of all 24 project MEMORY.md files vs Munin. Migrated 8 valuable local-only entries to Munin (gavel, hoganas-coach, flowdictate, 4× feedback/hugin, munin-zero hardware). Deleted 12 redundant/duplicate files. Trimmed 9 MEMORY.md files (munin-memory 204→25 lines, AXON 243→20, claude-code-energy-monitor 136→50, etc.). Total reduction: ~1,800 lines → ~600 lines across all project memories.
- **CLAUDE.md global trim** — Munin section 94→55 lines (-40%). Removed redundant prose, verbose Desktop/Web/Mobile instructions, artifact indexing sub-sections. All rules preserved.
- **Removed MEMORY.md auto-memory reference from CLAUDE.md** — no longer instructs Claude to maintain local MEMORY.md files alongside Munin.
- **claude.ai MCP note** — the 3 dead claude.ai MCPs (M365, Gmail, Calendar) are account-synced from claude.ai web, not CLI-managed. Must be removed via claude.ai → Settings → Integrations.

## Completed This Session (2026-04-08)
- **Fixed stale statusline.py deployment** — deployed `~/.claude/statusline.py` was from Mar 1, missing all quota analyzer features (per-session model/project tracking, daily deltas, stale pruning, session history archiving). Per-model and per-project breakdown in `advisor.py --breakdown` was showing "?" for all sessions.
- **Symlinked statusline.py** — `~/.claude/statusline.py` → repo copy. No more manual deploy step; edits are live immediately.
- **Data collection confirmed active** — 47 days of history, Pi sync running, per-model/project data will populate from next API call onward.

## Completed This Session (2026-03-29)
- **Quota analyzer (`advisor.py --breakdown`)** — new per-project/per-model cost breakdown view. Shows top sessions by quota impact, output token share analysis. Per-session daily deltas (`di/do/dc/dcw`) now tracked in statusline.py. Stale baselines (sessions with no metadata) pruned at midnight.
- **Pi headless energy monitoring — implemented and deployed**:
  - `pi_scanner.py` — scans print-mode JSONL from `~/.claude/projects/` on Pi. Dual mode: `--file` for targeted parsing, global scan for cron catch-up. requestId dedup, fcntl locking, schema warnings.
  - `pi_sync.sh` — rsync wrapper syncing journal + rollup from Pi to laptop.
  - `advisor.py` + `stepcount.py` extended to merge Pi data (additive per date, `--no-pi` flag).
  - **Deployed:** Scanner running via cron `*/15` on Pi (`huginmunin`). Sync running via cron `*/30` on laptop. Initial scan found 51 sessions across 7 days.
  - Pi sessions now appear in advisor analysis (model mix shows Opus/Haiku, projects show heimdall/hugin/skuld).
- **Pi implementation adversarial debate** (2 rounds, 12 critique points):
  - Dropped `last-prompt` as completion gate (replaced with "assistant entry with nonzero usage")
  - Changed from rollup-only to full journal sync
  - Confirmed requestId dedup needed even in print mode
  - Scanner is decoupled v1; Hugin-side capture is target architecture

## Completed This Session (2026-03-27)
- **Sonnet-first pilot experiment** — wrote `docs/sonnet-pilot-experiment.md`: 3-phase evaluation (baseline/pilot/evaluate) of routing Cat A+B Hugin tasks to Sonnet. Phase 1 starts today (Mar 27 - Apr 2).
- **Notification pipeline** — tested Telegram (Ratatoskr) and email (Heimdall) channels. Ratatoskr `POST /api/send` endpoint added via Hugin task (ae4f64a). Email deprecated: `grimnir-bot@outlook.com` flagged by Microsoft (AADSTS70000 "service abuse"). Set `NOTIFY_ENABLED=false`.
- **Heimdall EnvironmentFile debate** — 2-round Codex debate on fixing missing `MICROSOFT_MCP_CLIENT_ID`. Applied fix (`EnvironmentFile=/home/magnus/.heimdall/env`), but discovered real root cause is the Microsoft account lockout, not just the missing env var. MSAL cache has 2 accounts; `accounts[0]` happens to be correct (`grimnir-bot`) but is fragile.
- **Email deprecation across repos** — updated grimnir `docs/architecture.md`, heimdall `STATUS.md`, Munin `projects/heimdall/status` and `people/grimnir-bot/status`. All notifications now go through Telegram.
- **Free email research** — evaluated 8 transactional email services. Best free options: Brevo (300/day, no domain needed) or SMTP2GO (200/day). Parked for now — Telegram is sufficient.
- **Session history + advisor tooling** (40bcd18) — statusline.py now tracks per-session metadata (model, project, context size, cost, API calls). advisor.py provides analysis.

## Previous Session
- Codex monitor suite (codex_status.py, codex_stepcount.py, codex_with_summary.py)
- Model tiering and headless energy monitoring debates

## In Progress
- **Per-model/project data collection** — symlink deployed, awaiting first full day of data to verify breakdown works. Backup at `~/.claude/statusline.py.bak` (remove once confirmed).

## Deployment: energy scanner sync (Pi, m5, laptop)
- **Each remote machine (Pi, m5) runs `pi_scanner.py` locally via cron:** `*/15 * * * * python3 ~/repos/claude-code-energy-monitor/pi_scanner.py 2>&1 | logger -t pi-energy-scanner`. Writes `~/.claude/pi_journal.jsonl` (append-only) + `~/.claude/pi_daily_rollup.jsonl` (derived daily totals) — local to that machine, same filenames on every remote (it's a local journal, not the synced copy).
- **Any machine with interactive Claude Code use (laptop, m5) also runs `interactive_export.py` via cron:** `*/15 * * * * python3 ~/repos/claude-code-energy-monitor/interactive_export.py`. Regenerates `~/.claude/interactive_journal_raw.jsonl` + `interactive_rollup_raw.jsonl` from that machine's own `statusline_session_history.jsonl`/`statusline_daily.json` — deliberately named so they don't match the `*_journal.jsonl` glob below (a name that did match caused this machine's own interactive usage to be double-counted locally; see 2026-07-01 session notes).
- **Laptop pulls from Pi + m5 via cron:** `*/30 * * * * /Users/magnus/repos/claude-code-energy-monitor/remote_sync.sh` (logs via `logger -t pi-energy-sync`). Loops over `REMOTE_HOSTS` (`pi:huginmunin.local`, `m5:m5`) and rsyncs each remote's headless + interactive journal/rollup to a per-machine destination, e.g. `~/.claude/m5_journal.jsonl`, `m5_daily_rollup.jsonl`, `m5_interactive_journal.jsonl`, `m5_interactive_daily_rollup.jsonl` (from m5).
- **m5 also pulls from the laptop via cron (the reverse leg):** `*/30 * * * * REMOTE_HOSTS_OVERRIDE="laptop:laptop" /home/magnus/repos/claude-code-energy-monitor/remote_sync.sh` — same script, different host list, via `REMOTE_HOSTS_OVERRIDE`. Reaches the laptop over Tailscale (`Host laptop` alias in m5's `~/.ssh/config`, MagicDNS name `magnus-macbook-air`); laptop has Remote Login enabled and m5's key in `~/.ssh/authorized_keys`.
- **`advisor.py`/`stepcount.py` merge all synced files automatically** by globbing `*_journal.jsonl`/`*_daily_rollup.jsonl` in `~/.claude/` — adding a machine only requires adding it to `REMOTE_HOSTS` in `remote_sync.sh` (or passing `REMOTE_HOSTS_OVERRIDE`), no Python changes.
- **Full mesh between m5 and laptop; Pi stays hub-and-spoke** (laptop-only pull, no interactive stats — headless box).
- **m5 interactive statusline:** `~/.claude/statusline.py` symlinked to the repo copy + `statusLine` registered in `~/.claude/settings.json` (same pattern as the laptop) — interactive Claude Code sessions on m5 now track today/week/month totals locally too, independent of the headless scanner.
- **To update the scanner on a remote:** `ssh <host> 'cd ~/repos/claude-code-energy-monitor && git pull --ff-only'` (both Pi and m5 have the repo cloned from origin) or `scp pi_scanner.py <host>:~/repos/claude-code-energy-monitor/pi_scanner.py`.

## Next Steps
- **Sonnet-first pilot** — baseline week in progress (Mar 27 - Apr 2). Set up scheduled trigger for daily monitoring + Telegram alerts.
- **Model tiering for Hugin** (debate Round 2 completed 2026-03-26):
  - If implemented, do not flip all code tasks to Sonnet by default without telemetry
  - First add a minimal append-only invocation journal from the first pilot run
  - Define a narrow acceptance contract for a small pilot task set before broader Sonnet-first rollout
  - Treat `opusplan` as research only until Sonnet-first pilot data shows a planning gap
- Per-model energy constants (Haiku/Sonnet/Opus use same constants despite ~2-5x size differences)
- Improve active-session heuristics if Codex changes rollout behavior or adds a native status hook
- Consider blog post about JSONL placeholder finding (issue #28197)
- Context-length-dependent input constants
- Gemini CLI support — still research only
- PyPI packaging — defer until multi-CLI or broader adoption
