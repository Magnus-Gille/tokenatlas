# Changelog

All notable changes to this project are documented in this file. The project follows Semantic Versioning.

## [Unreleased]

## [1.19.0] - 2026-10-08

### Added

- Local inference in the offline report can be compared with a cloud model chosen by the reader. The hypothetical USD list price uses the same recorded input/output counts, standard rates, no cloud cache reuse, and each model's long-context threshold. The comparison follows filters and shows coverage, price date and unknown local operating cost; it does not claim equivalent quality or actual savings.

### Fixed

- Local usage recognizes providers from the active price table, including `m5` and `inference-gille`, as well as the known and manually configured local providers.

## [1.18.0] - 2026-10-08

### Added

- `tokenatlas schedule` explicitly opts into periodic collection, with macOS LaunchAgents, Linux user timers or cron, and Windows Task Scheduler. `--dry-run`, `--status`, and `--remove` preview, inspect, and manage only TokenAtlas-owned jobs. The scheduled command keeps the selected history database, installation, and configured harness locations (#55).
- Private reports group spending by project and known branch, with the costliest turns and their already-retained prompt text. Unknown branches and requests without a turn remain explicit. Shared reports omit this work context (#79).
- Reports summarize the clients, observed Codex plans, and local inference used in the selected period. `plan set --harness claude` supplies an explicitly manual Claude plan; `profile provider add` recognizes custom local providers. Unknown clients/plans stay unknown, and shared reports do not expose custom provider names (#146).

### Changed

- Reuse history and derived calculations during analysis; skip retained-text and report work when the relevant inputs have not changed. Price, history, retention and profile changes invalidate the appropriate cached work (#143, #56).

## [1.17.0] - 2026-10-05

### Fixed

- Coverage summary (#151): file and line-diagnostic totals per harness come from the history's distinct locally collected files (`doctor` adds `files_by_harness`, counts only), so roots refreshed both as a folder and as one of its subfolders no longer count a file twice. Read errors still add up per scan.

### Changed

- Report clarity (#145): "At a glance" is a bulleted list with the key numbers in bold. A collapsed glossary ("Ordlista"/"Glossary") explains turn, request, request without a turn, list price (with the price table's date), interrupted turn and the limit-share range, and the terms in the glance list carry the same text as tooltips. Limit-share ranges read "The costliest turn is credited with at least 8%, at most 38% of the weekly Codex limit – 603 other turns ran at the same time …" instead of "8–38% (shared with 603 turns)", and the glossary, tooltips and summary say that the account meter also counts usage the logs do not see, so a real share can be below the lower bound; a range with no upper bound is explained by the missing final reading. The token cards show the unit, and the prompts section says the text is shown because the report is private (a shared report never carries it).
- Section 6 (coverage) shows one line per harness, e.g. "204 source folders · 907 files · all ok · 0 source diagnostics", lists individually only roots that are not ok or have diagnostics, keeps the full list collapsed, and explains "source diagnostics" (#147).

### Added

- A "Back to top" button in the report (#148).

## [1.16.0] - 2026-10-05

### Added

- Progress on stderr for slow commands (#142): `open`, `refresh`, `report`, `top`, `insights`, `quota show`, `session`, `rate`, `overhead --refresh`, `import`, `snapshot` and `collect` show a live line (spinner, step, file count, elapsed seconds) in a terminal and keep a `✓ step (time)` line per finished step. Not a terminal: silent; `TOKENATLAS_PROGRESS=1` gives plain lines, `=0` turns it off. stdout is never written, so results are unchanged; `collect`'s log lines are as before.

## [1.15.0] - 2026-10-05

### Security

- Opt-in turn context (#134): the local `git log` for commit subjects no longer starts programs named by a project's repository-local git config. Signature verification (`log.showSignature` with `gpg.program`, `gpg.ssh.program`, `gpg.x509.program`), pager, external diff, alternate-refs command, ssh command, hooks and transports such as `ext::` are neutralised with fixed `-c` overrides, and the git environment is an allowlist with `GIT_ALLOW_PROTOCOL` empty (which repository config cannot override), `GIT_CONFIG_NOSYSTEM`, no lazy fetch and no inherited `GIT_*` variables. Commit subjects and time filtering are unchanged.
- Remote sync no longer prints SSH, SCP or import diagnostics raw (#135). A compromised configured peer could put terminal control sequences (clear screen, clipboard writes, BEL, carriage-return overwrites, C1 controls) in an error message. `remote_sync.sh` now shows control bytes as visible `\xNN` escapes (tab and newlines are kept; non-ASCII bytes in these remote messages are escaped too, so they appear as `\xNN`), and the Python side applies the same escaping (`terminal_safe`) to import and collect error text, keeping UTF-8 readable. Host validation, timeouts, cleanup and continuing with the other hosts are unchanged.
- Shared (redacted) reports now show a model name only when it is an exact, known public identifier: listed (as the model or one of its aliases, for that provider) in the packaged price table or ChatGPT credit card. Any other name, including a private suffix under a recognised family such as `claude-sonnet-4-6-acme-internal`, is replaced by a stable pseudonym ("model 001") everywhere: rows, filters, top lists, and the insight facts (model share, price comparison, credits and its unrated-model list). Before, a broad family-prefix pattern (`claude…`, `gpt…`, `gemini…` and so on) treated such names as public. Private reports are unchanged. Reports cached under the old policy are rebuilt: the redaction revision is part of every report's identity, so `open` and `report --if-changed`/`--max-age` never reuse them (#133).
- Opt-in prompt text and turn context capture (`--keep-text`) now reads only locally collected sources (#132). Imported source references (`<machine>:<path>`) and relative paths never reach the local readers, whatever machine id the observation claims; the prompt store, the prompt reader and the context reader each check provenance (absolute, not imported, existing regular file). `import` now rejects a snapshot that contains triggers or views, opens it read-only for the check, and rejects one whose machine id is not the `m-<32 hex>` form tokenatlas generates (a forged id such as `C` could turn an imported path into a Windows drive path), and a source with a `:` before its first separator is only local as a real drive path on Windows. Capture needs positive evidence of local collection: files recorded by a local refresh carry `origin='local'` (an additive column), imports never do, and existing databases are upgraded conservatively (only rows a local refresh checkpointed stay eligible; the rest becomes eligible again when a local refresh sees the file). Mesh imports, merging and deduplication are unchanged: imported observations still appear in reports, they just never get local text or context.

### Changed

- Limit shares: a turn that overlapped others now shows a **range**, not a cost-weighted point (#131). The lower bound is the counter movement while the turn was the only active participant, the upper bound all the movement while it was active, so any true split lies between them. Observed turns keep `~3%`; a shared turn reads "2–28% of the weekly Codex limit (shared with 4 turns)" (sv "2–28 % av veckogränsen för Codex (delad med 4 turer)"), "< 1%–28%" when the lower bound is 0; a range of at most 5 points also shows a point, "≈ 9% (7–11%)". Unpriced participants no longer make a share unknown (the bounds need no prices); only resets, real counter drops and a missing reading before the first request do. `top` and `--json` add `lower_percent`, `upper_percent` and `shared_with` (`delta_percent` stays for observed and narrow cases); cards, phone cards, "At a glance" and the insights fact show the ranges. Calibration (manual and automatic) applies only where there is neither an observed share nor a range. `docs/quota.md` explains the bounds.

## [1.14.1] - 2026-10-04

### Fixed

- Quota budget: an automatic budget (#116) is now used for shares only with enough consistent evidence: at least 3 points from at least 2 distinct windows, the largest at most 4 times the smallest. A single limit hit (for example "≈ $0.65 per 5 hours") no longer turns a normal turn into several hundred percent. A budget without enough evidence is still listed by `quota show` (and `--json`: `used`, `not_used`) with the reason ("not enough evidence yet (1 of 3 points)", "points disagree too much (spread ×5.0)"). An automatic share above 100% of a window is shown as unknown ("share unknown: the automatic estimate does not fit this turn"), never as a number, and `quota show` counts those turns. The share text now names the evidence: "≈ 4% of the weekly Claude limit (estimated from 5 limit hits)", "12 statusline readings" or both (sv equivalents). Manual calibration is unchanged.

## [1.14.0] - 2026-10-04 (not published to PyPI; superseded by 1.14.1)

### Changed

- Claude: `tokenatlas statusline` now records quota snapshots by default (#117, #92). If your Claude Code statusline runs `tokenatlas statusline`, recording starts with its next run after you upgrade. Each snapshot is a timestamp, the session id and the 5-hour and weekly percentages and reset times that Claude Code already shows in its statusline, appended to `claude-quota.jsonl` (0600) next to the history database; it stays local and nothing is sent. Turn it off with `--no-record-quota` on the statusline command or `TOKENATLAS_NO_QUOTA=1`; to remove what was recorded, turn recording off first, then delete `claude-quota.jsonl` and `claude-quota.last` (and `claude-quota.lock` if present) next to the history database. `--record-quota` is still accepted and does nothing; `statusline --setup` prints the command without a flag and documents the opt-out. `doctor` now reports recording as active when the statusline is tokenatlas's and not opted out, and warns when the statusline is not tokenatlas's (no snapshots possible) or recording is disabled. The #92 guarantees are unchanged: 0600, no symlinks, a non-blocking lock, and a failure never affects the status line.

### Fixed

- Report: a turn's share of the weekly or 5-hour limit belongs to the whole turn, so when a filter or zoom selects only part of its requests the card and the "At a glance" sentence now say "(whole turn)" / "(hela turen)" instead of presenting it as the selection's share; the payload gains `prompt_requests` (each card turn's total request count). The Limit hits and Limit windows sections now state that they ignore the filters (the hits included when the report was built; the most recent windows of each limit) (#125).
- Shared reports: the `context_size` insight fact no longer names an imported harness verbatim; it uses the `limit_hits` allowlist (claude, codex, pi, opencode, else "other") in values and text (#108).
- Report: quota shares name the limit the same way everywhere (cards, `top`, insights, "At a glance"): "weekly Codex limit", "5-hour Claude limit" (sv "veckogränsen för Codex", "5-timmarsgränsen för Claude") (#115).
- Tests: `CollectSignalWindowTest` no longer flakes on macOS CI when signalling a process group whose leader has exited (`PermissionError`); `_stop_group` already treated it like `ProcessLookupError`, now covered by a unit test (#113).
- Report: limit windows on phones: the "Limit windows" table becomes one small card per window at 640 px and below instead of clipping the Resets column.
### Added

- Quota budget: automatic calibration from your own history (#116). Without any `quota calibrate` reading, tokenatlas derives budgets at `report`, `top` and `quota show` time (nothing is stored) from Claude limit hits (the list price seen in the hit's window is "100% = $X") and from Claude statusline readings (the cost between readings that rose by at least 5 percentage points over that rise; windows with under $0.50 or an unpriced request are skipped and counted). The budget per harness and window is the median of the last 8 windows' points, with count and spread; Codex `window_full` hits are the fallback. Manual readings and `quota set` always win. Turns with no observed, estimated or manually calibrated share get `label: "auto-calibrated"` (`≈ 4% of the weekly Claude limit (estimated from your limit hits)`; whole percent, `≥` for a floor), so old Claude history has shares. `quota show` (and `--json`, key `automatic`) lists the automatic budgets separately with source, point count, spread and date range. Usage the logs do not see makes the budget too small and shares too large. Private reports only; shared reports carry no automatic shares or budgets.

## [1.13.0] - 2026-10-04

### Added

- Claude: opt-in quota snapshots give turns a share of the 5-hour and weekly limit (#92). `tokenatlas statusline --record-quota` (also `--setup --record-quota`) appends the quota readings of Claude Code's statusline payload to `claude-quota.jsonl` (0600) next to the history when a value changed, pruned to the last 60 days above 5 MB; the default stays read-only and a write failure never changes the status line. `report`, `open`, `top` and `insights` join the readings to Claude turns like Codex ones (`~6% of 5-hour Claude limit`), and `doctor` reports whether recording is on. Snapshots exist only while a Claude Code UI session is open; `claude -p`, SDK runs and chat are not recorded, and the values are account-wide. The limit windows table is now titled "Limit windows".
- Report: an "At a glance" ("I korthet") summary above the KPIs (#78). Two to six sentences computed in the page from the same rows as the cards, so they follow the filters: period, turns and requests, cost at list price (`≥` when a request is unpriced or incomplete), the share of the ten costliest turns, the costliest day, limit hits in the selection, interrupted turns, and the costliest turn's share of its weekly or 5-hour limit when known. No advice, no model call; shared reports add no names or text.
- Quota budget (#93): `tokenatlas quota calibrate | set | show | forget` turns list price into a share of your limit. Copy the used percentage from `/usage` (Claude Code) or the Codex limits display with `quota calibrate --harness claude --window 7d --used 52% --resets "2026-10-09 21:00"`, or state a size you know with `quota set --harness claude --window 7d --budget-usd 900`. A reading is stored with the list price tokenatlas saw for that harness's own provider in the window (Claude: reset minus the window length, or the trailing window marked approximate without `--resets`; Codex: always the trailing window, and only requests on one plan count: readings and budgets are kept per plan, `--plan` names it, default the plan seen in the window, an error when several were active); the budget is the median of cost seen / used over your readings, shown with the min-max spread and the number of readings, and readings older than 8 windows are ignored (`--keep`). A manual budget wins. Turns that have no observed or estimated share (Claude, whose logs carry no percentage) then show `≈ 4% of your weekly Claude limit (your calibration, 2026-10-03)` in `top` and `top --json` (`quota_share.label` `calibrated`, with a `calibration` object) and on the report's turn cards, and the report lists the derived budgets under "Your calibration". A turn with unpriced, incomplete or ambiguous requests shows a floor (`≥ 3%`) or "unknown", never `< 1%`. A calibration is rough: list price per percent varies 2-3x between weeks, a changed model mix drifts it, and usage tokenatlas does not see (chat, other machines, cloud tasks) makes the budget too small and shares too large. Readings are kept in `quota-budget.json` next to the history (0600) and are repriced from the history when another price table is used; nothing leaves the machine. Shared reports carry no calibrated shares and no budgets. Without a calibration nothing changes.

## [1.12.0] - 2026-10-03

### Added

- Codex: the history keeps the plan quota from each `token_count` event on the usage observation (#89), as a new `quota` field: plan and limit id, a reached-limit type if any, and per window (5-hour or weekly) the used percentage, window length and reset time. Nothing visible changes yet; later features can use it to show a turn's share of the limit. The credit balance and other account state are not stored. Existing Codex histories are re-read once.
- Interrupted turns (#96): the collectors record when the user stopped a request, as the harness logged it (Claude Code `[Request interrupted by user` rows, Codex `turn_aborted`, Pi `stopReason: "aborted"`, OpenCode `MessageAbortedError`) in a new observation field `flags` (`["interrupted"]`, additive column within schema 2). A turn with a flagged request is `interrupted` (with `stopped_request_at`, the time of the observation that carries the flag: the request running when the user stopped, or, when the stop message itself has no usage (OpenCode, Pi), the latest earlier request of the same turn) in `tokenatlas top` (text ` · interrupted`, and `--json`), gets an "Interrupted" / "Avbruten" badge on the costliest turns in the report, and an `interrupted_turns` cost fact (number of turns, their list-price cost and share of priced cost; unpriced turns are counted, never guessed) in `tokenatlas insights` and the report. It is a label, not a verdict: an interrupt can be a correct early stop. Existing Claude, Codex, Pi and OpenCode histories are re-read once.
- Limit hits (#91): a rejected Claude request (`quotaLimits.status: rejected`) and a Codex observation that reports a reached limit now name a hit: the report gets a "Limit hits" section (hidden when there are none) with the turn that hit the 5-hour or weekly limit, when it resets, and the three turns that used the most list-price cost in the window as a share of what tokenatlas saw in it. The turn card gets a badge, `tokenatlas top --json` a `limit_hit` key, and the cost facts a `limit_hits` count. Retries of one rejection count as one hit. Usage on claude.ai, ChatGPT or other machines counts toward the same limit but is not in the logs. Rejected requests carry no tokens, so they are kept in the history as limit events but never counted as requests, costs or energy. Existing Claude histories are re-read once.
- Codex: share of the weekly or 5-hour limit per turn and per window (#90), and the quota research behind it (#94, `docs/quota.md`). `top` and its `--json` (`quota_share`), the report's costliest turns (also on phone cards) and a new "Codex limit windows" table (recent windows with peak percent, whether the limit was hit, and the list-price cost tokenatlas saw in them), and an `insights` fact for the three costliest turns show the observed share (`~3%`, `< 1%`), or an estimate (`≈`) when concurrent turns shared the movement. Shared reports keep the percentages. The demo gets synthetic weekly quota snapshots.

## [1.11.0] - 2026-10-02

### Added

- Back to the conversation from a costliest turn (#84). A private report shows the turn's resume command (and `tokenatlas top` prints it under each turn), `cd <cwd> && claude --resume <id>` / `codex resume <id>` / `pi --session <id>` / `opencode --session <id>`, quoted in Python, with a Copy button; Codex turns also get an "Open in Codex" link (`codex://threads/<id>`, UUID ids only), and each turn has a "Copy prompt" button and its time. The link and commands open the whole conversation, not the turn: scroll or search to it. Claude Code has no deep link, so only the command; it needs a known directory. Shared reports carry no ids, commands, links or paths. In demo reports (`demo: true`, set by `scripts/demo.py`) "Open in Codex" and the command's Copy button explain in a small toast instead of opening or copying. The `top` table's old raw `resume` column is replaced by that validated, quoted command line; ids that contain `:` no longer collide in the report payload.
- ChatGPT credit equivalents for OpenAI/Codex usage (#83): a versioned rate card (`credits.json`, source and retrieval date 2026-10-02), a `credits` cost fact in `tokenatlas insights` and the report, and `≈ N credits` next to the cost of each costliest turn (also in `tokenatlas top`), in Swedish and English. It is what the usage corresponds to, not what was drawn; fast mode counts at 2x, other speeds, unknown models, requests with cache writes and unknown token counts are left out and counted.

### Fixed

- Codex: the collector reads `service_tier` from `thread_settings_applied` events. The latest setting applies to every later request in the rollout: `priority` or `fast` is priced with the Fast modifier, `flex` with the Flex modifier, and `default` as explicit standard without the "not recorded" assumption. Requests before any setting, and after a setting without a tier, stay unrecorded; an unknown tier is kept and left unpriced rather than guessed. Existing Codex histories are re-read once (#86).

## [1.10.1] - 2026-10-02

### Changed

- Report: on narrow screens (640 px or less) the costliest turns show as cards. Each card has rank and cost first, then harness, project and time, the labelled figures and the full prompt text, so cost and prompt are visible on a phone without scrolling sideways. The desktop table is unchanged (#80).
- Demo (`scripts/demo.py`): the ten costliest turns span Claude Code, Codex, Pi and OpenCode, not only Claude. `demo-summary.json` lists the top 10 with the same ranking as `tokenatlas top` (#81).

## [1.10.0] - 2026-10-02

### Changed

- Report: the costliest turns come right after the totals, then cost facts and energy; the section numbers follow the new order (#73).
- Demo (`scripts/demo.py`): realistic, clearly fictional developer prompts with titles, branches, follow-ups and final messages instead of lorem ipsum, and the demo stores the top-turn text and context, so the demo report shows them (#73).

### Fixed

- Claude turns (#74): a `type: user` row with `isMeta: true` (skill bodies loaded by the Skill tool, `<local-command-caveat>` rows and other injected text) no longer starts a turn, so one real turn is no longer split into several and injected text no longer shows as the turn's prompt in `top --keep-text` or the turn context. Typed slash commands still count as user input. The Claude harness revision is bumped, so the next refresh re-reads Claude files once and corrects existing histories. Compaction summaries (`isCompactSummary`), interruption markers (`[Request interrupted by user…]`) and rows that hold only `<system-reminder>` blocks are not user inputs either.

## [1.9.0] - 2026-10-02

### Added

- `tokenatlas show` (#69): opens the latest report in the browser at once, without refreshing or rebuilding and without opening the history; prints where it is and how old it is, and says to run `tokenatlas open` when there is no report yet.

### Removed

- The checkout-only energy-monitor scripts and the `why` command line (#63). TokenAtlas is the product; the energy estimate (`tokenatlas/energy.py`, the report card and the `energy` insight) and `tokenatlas statusline` already replaced the parts that mattered. Removed from the repository root: `statusline.py`, `stepcount.py`, `advisor.py`, `analyze_tokens.py`, `api_test.py`, `codex_status.py`, `codex_stepcount.py`, `codex_with_summary.py`, `compare.py`, `energy_constants.py`, `interactive_export.py`, `pi_scanner.py`, `pi_status.py`, `pi_stepcount.py`, `plot_daily.py`, `sum_jsonl.py` and the `why.py` shim, with their tests (`test_pi_status.py`, `test_interactive_export.py`) and the matching README sections and CI step. They remain in git history up to tag `v1.8.0`.
- `tokenatlas/why.py` is no longer a command: its `main()`, the `--date` statusline-coverage reader and the text report are gone. The module keeps the Claude, Codex, Pi and OpenCode collectors that `refresh`, `report`, `overhead` and the turn context use; it was not renamed.
- `remote_sync.sh` no longer pulls the legacy `pi_journal.jsonl`, `pi_daily_rollup.jsonl`, `interactive_journal_raw.jsonl` and `interactive_rollup_raw.jsonl` with rsync (nothing reads them any more); it syncs history snapshots only, with the same timeouts and exit codes, and no longer needs `rsync`.

## [1.8.0] - 2026-10-01

### Added

- `tokenatlas statusline` (#62): the Claude Code statusline as a packaged command. Context and quota come live from Claude Code's payload; day, week and month tokens and energy come from the all-harness history through a small `statusline.json` cache that `refresh` (and so `open` and `collect`) writes next to the database, so the statusline never opens the database, makes no network call and uses no credentials. It appends the cache time when older than 45 minutes, omits totals when the cache is missing, prints a short fallback line on any error, and starts without importing the history modules. `tokenatlas statusline --setup` prints the `statusLine` settings entry and never edits the file.
- Where the report is saved is visible (#64): `tokenatlas open` and `report --html` print `Report: <path>` on stderr (the JSON on stdout is unchanged), a private report's footer shows where it is saved and how to reopen it (shared reports never contain a local path), `open --help` shows the resolved default path, and the README quick start says where the file is.
- Energy estimates in TokenAtlas (#61): an "Energi (uppskattning)" / "Energy (estimate)" card in the report that follows the page filters, and an `energy` fact in `tokenatlas insights` and the report's cost facts card. An order-of-magnitude proxy, not a measurement: the methodology's mid estimates per token class (`tokenatlas/energy.py`) x a Claude tier multiplier (haiku 0.3, sonnet 0.6, opus 1.0), rounded to 1, 2 or 5 per power of ten, with a mid / 3 to mid x 3 range. Models that are not a Claude tier are counted with multiplier 1 and the number of such requests is stated; incomplete observations make it a lower bound (`≥`).

## [1.7.0] - 2026-10-01

### Added

- Harness logs moved with the harness's own variable are found: `CLAUDE_CONFIG_DIR`, `CODEX_HOME` (sessions and `session_index.jsonl`), `PI_CODING_AGENT_DIR` and an absolute `XDG_DATA_HOME` (OpenCode); an explicit root still wins, and `doctor` reports each resolved root and its source (#52).

### Changed

- README: a quick start at the top (install with pipx or uv, `tokenatlas open`, why and how to schedule `tokenatlas collect`, what is read, what is opt-in, and that no skills, `AGENTS.md`, plugins or harness configuration are needed); the original energy monitor is its own section, and its "no cron jobs" sentence is limited to the statusline (#53).

## [1.6.1] - 2026-10-01

### Fixed

- `tokenatlas collect`: the report after the remote sync also uses `--max-age 1h`, so the report is built at most once an hour while only the data changes, as with the old shell collector; it was rebuilt on every run (about 90 s CPU each on 400k observations) (#49).

## [1.6.0] - 2026-10-01

### Changed

- `tokenatlas top` shows and `top --keep-text` stores the top 10 turns by default (was 5), and the report's "Costliest turns" card shows 10. An existing store keeps its recorded k; move it to 10 once with `tokenatlas top --keep-text -n 10`.
- `tokenatlas collect` runs its `top --keep-text` step with the store's recorded `-n` and `--by` instead of the defaults, so a chosen k is no longer reset and entries are no longer evicted; it never raises k on its own. A store without a valid recorded k/by (corrupt, unsafe or hand-edited) makes `collect` skip the step, log why and exit 1 without touching the file; the report shows no stored text for such a store.
- Stored prompt text and context outlive harness log cleanup (for example Claude Code's `cleanupPeriodDays`) while the turn stays in the top k; `top --forget-text` deletes it.

## [1.5.1] - 2026-10-01

### Fixed

- Remote sync with macOS `/usr/bin/rsync` (openrsync): a missing remote file is again a benign "not found" instead of an error that made `collect` exit 1 on every run (#43). openrsync prints a `receiver has empty file list` warning and no `rsync error:` summary; that warning is accepted only together with the sender's missing-file line for the requested file.

## [1.5.0] - 2026-10-01

### Added

- `tokenatlas collect` replaces the reference shell collector `scripts/collect.sh`: one scheduled command that refreshes, stores top-turn text if opted in, builds the conditional private report, runs the remote sync and rebuilds the report after every attempted sync. It takes a kernel lock (`flock`, `msvcrt.locking` on Windows) on `collect.lock`, so runs never overlap and a crashed run leaves nothing stale; a busy lock prints `collect: already running` and exits 0. `--remote tag:host`, `--remote-sync`, `--sync-timeout`, `--no-report`, `--lang`. The remote sync script now ships in the package as `tokenatlas/remote_sync.sh`; the repository-root `remote_sync.sh` is a shim.
- Cost facts: `tokenatlas insights [--days N | --start/--end] [--json]` prints deterministic, rule-based list-price facts (cost by model, a neutral price ladder of the same tokens at every model of the same provider (the model used is marked; no recommendation), cost by token class, input size per request, long-context premium, big turns, subagent share, fast/priority tier extra) with the computation and assumptions of each; no model, no interpretation, aggregate only. The report gets a "Kostnadsfakta" / "Cost facts" card for the last 30 days and all history, computed when the report is built, with measured/computed badges; shared reports apply the usual model-name redaction.
- Cost facts follow the report's reliability rules: ambiguous-identity observations are left out of every fact, incomplete ones make amounts lower bounds (`≥`) (the long-context premium and premium-tier extra cost, which are differences between price tiers, use complete requests only and state how many were left out), both counts and the pricing assumptions (with request counts) are stated per fact, the 30-day window ends at one captured `now`, and the price table used is named with its `retrieved_on` date.
- README: what TokenAtlas adds beyond the vendors' own tools, and what to use the vendor tools for.

### Changed

- `report_state` takes the UTC day of the rolling 30-day window, so a report is rebuilt once per day even when the history is unchanged.

### Fixed

- Remote sync can no longer hang on a stalled host (#39): `remote_sync.sh` bounds every ssh/scp/rsync call (connect timeout, keep-alive, `BatchMode`, rsync `--timeout`, `TOKENATLAS_SSH_OPTS`) and each host as a whole (`TOKENATLAS_HOST_TIMEOUT`, default 300 s; reported as `ERROR (timeout after Ns)`); `tokenatlas collect --sync-timeout` (default 600 s) bounds the whole sync and kills its process group. The script closes the signal window before its worker pid is recorded, and rsync exit 23 is a benign missing file only when the error names the remote path ("No such file or directory" for a local destination is now a failure).

## [1.4.0] - 2026-10-01

### Added

- Turn context: `top --keep-text` also stores, for the current top turns only and in the same 0600 `top-prompts.json` (now version 2; version 1 is still read and upgraded), the title (Claude `custom-title`, Codex `session_index` thread name, OpenCode session title, Pi `session_info` name), working directory, branch, repository, input count, initiating and follow-up inputs, final message, shell/edit/web/subagent counts, PR numbers and up to 5 commit subjects from local `git log --all` over the turn window. No network, no model calls. `top` prints up to four context lines per turn and `--json --with-text` adds `context`. Private reports get an "Inputs" column and an expandable context block per stored turn; shared reports carry only the input count and never any context text. See README "Top turns".
- Prompts are now called turns: `top` ranks the costliest turns (an initiating input plus everything it caused, including follow-up inputs and subagent work) and the report card is "Dyraste turerna" / "Costliest turns". The command name and JSON keys are unchanged.
- English report UI: `report --html` and `open` take `--lang auto|sv|en` (default `auto`: Swedish when the browser language starts with `sv`, otherwise English), and the report header has an SV/EN toggle that switches live and is remembered in `localStorage` (when available). Pseudonym labels in the payload are now language-neutral codes; the UI texts ship as a second compressed block (`report_i18n.json`).
- Release workflow `.github/workflows/publish.yml` publishes to PyPI with trusted publishing (no stored token) when a GitHub release is published, or on demand for an existing tag.

## [1.3.0] - 2026-09-30

### Added

- Top prompts: `tokenatlas top` ranks the costliest prompts (subagent work rolled up); opt-in `--keep-text` / `--forget-text` / `--with-text` manage `top-prompts.json` (0600, top prompts only, never in the database or snapshots); the report gets a "Dyraste prompterna" card that follows the filters, priced client-side from the token columns and a small list of unit-price classes (the report grows about 5%), with prompt previews in private reports only. See README "Top prompts".
- Pi observations get derived turn ids (Pi files are re-read once).

### Fixed

- Codex usage is attributed to the explicit turn ids of current rollouts; assistant and developer messages no longer start a new turn, which had split one prompt into many fragments. Stored turn ids are corrected on the next refresh, which re-reads Codex files once (about 2–3 minutes for 10k rollouts).

## [1.2.0] - 2026-09-30

### Added

- `tokenatlas refresh --all` (every harness from its default roots, missing ones `absent`, one JSON summary), a history `revision` counter and a report state marker (`tokenatlas-state`: option identity plus data revision and coverage; options are never throttled by `--max-age`) with `report --html --if-changed --max-age 1h` (skips without touching the file when unchanged or too recent), and `tokenatlas open` (refresh, private report, browser; reuses an unchanged report); see README "Keeping the report fresh".
- List-price cost per observation (`tokenatlas/pricing.py`, packaged `tokenatlas/prices.json` with verified 2026-09-29 prices and source URLs): input, 5m/1h cache writes, cache read and output priced separately, long-context tiers, Claude fast mode and US inference; unknown prices or tariff dimensions give `n/a`/partial, never zero. Costs are API-equivalent at list price, not what was paid.
- `tokenatlas session <id>`: a session tree (Claude subagents and Workflow runs, explicit children, inferred headless children, unassigned threads) with per-model tokens and cost, conductor overhead, and cost per passed unit from an optional outcomes file; `tokenatlas rate` records outcomes.
- Claude desktop Cowork transcripts (`local-agent-mode-sessions/*/*/local_*/.claude/projects`) are imported by `refresh --harness claude` with origin `local-agent`; `audit.jsonl` is never read.
- `tokenatlas snapshot` and `import` merge history from other machines (see `docs/remote-machines.md`); `remote_sync.sh` pulls it.
- `tokenatlas overhead`: per-harness context floor, fixed-context component sizes (instruction files, skill listing, MCP instructions, system prompt), skill uses and an estimated recurring re-read cost. Only sizes, names and counts are stored.
- Installable `energy-monitor` CLI for durable local usage history and offline Tokenatlas reports.
- Incremental, retained SQLite history for Claude Code, Codex, Pi, and OpenCode observations.
- Cross-harness attribution by project, session, turn, model, effort, agent, and origin.
- Offline HTML reporting with filters, drilldown, exports, and qualified cache-read-share comparisons.
- Wheel installation, reinstall, uninstall, and private-data retention smoke coverage.
- CI coverage for Python 3.10 through 3.13 across Linux, macOS, and Windows, plus Chromium and WebKit.

### Changed

- Renamed to TokenAtlas: command `tokenatlas` (`energy-monitor` remains a deprecated alias), package `tokenatlas`, data directory `~/.local/state/tokenatlas` (migrated automatically from `agentmon`).
- Claude observations record their tariff (speed, service tier, inference geography); the collector version is 5, so existing files are re-read once.
- History database schema 2: typed columns and a string dictionary cut storage roughly sevenfold (990 MB to 140 MB for 379,401 real observations); existing databases migrate automatically on first open.
- `why.py` defaults to all supported harnesses while retaining `--harness both` for Claude and Codex.
- Session identities are harness-scoped in summaries and reports.
- OpenCode reasoning is normalized as a subset of output while retained as a separate subtotal.
- Claude subagent transcripts are linked to their parent session from the transcript path.
- Claude output is flagged as a lower bound (`output_not_final`) when a request's transcript lacks the final usage row; the collector version is bumped so existing files are re-read.
- Hour and minute buckets are ordered by instant across DST changes.
- Tokenatlas embeds a gzip-compressed columnar payload decoded offline in the browser: a 379,401-observation history renders to 10.5 MB (private) or 7.2 MB (shared) instead of 270 MB, and loads in about 2 s.
- Summary cards use plain terms with a one-line explanation (Tokens totalt, Cache-träffar, Anrop, Output-tokens) and Swedish number abbreviations (mdr, milj.).

### Security

- History persistence uses an explicit allowlist and never stores prompts, assistant text, tool content, credentials, hostnames, or hardware identifiers. It generates a random local ID to scope synthetic identities to one database.
- Shared reports show provider, origin, effort, and model names only from explicit public allowlists and pseudonymize everything else, including fine-tune ids and host names; a custom endpoint reusing a public provider id with a family-like model name is still shown, so review shared reports.
- Metadata strings, including iteration `model` and `type`, must be bounded printable text or they are dropped.
- Shared reports pseudonymize project, session, turn, observation, and agent identities by default.
- Generated reports are standalone and make no network or model calls.

## [1.0.0] - 2026-05-30

- Initial Claude Code statusline release with token, cache, energy, and quota monitoring.
