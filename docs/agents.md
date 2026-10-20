# TokenAtlas for coding agents

Start with `tokenatlas --help`, then `<command> --help` for examples and defaults.
Analytics are offline. Put `--db /absolute/path/history.sqlite3` **before** the
command to select a database; otherwise the default is
`$XDG_STATE_HOME/tokenatlas/history.sqlite3` (`~/.local/state` when unset).

Use `--json` on reporting/configuration commands, including `doctor`, `refresh`,
`report`, `collect`, `rate`, `snapshot`, `import`, `open`, `show`, `schedule`,
`plan`, `profile`, `quota`, and `upgrade`. For nested commands put it after the
leaf, e.g. `quota show --json`. `statusline` is deliberately a terminal/stdin
protocol (and can wrap another command); it is not a JSON analytics endpoint.
`statusline --setup` prints a configuration snippet, without editing settings.

## Tasks → commands → fields

| Task | Command (append `--json`) | Fields to read |
|---|---|---|
| Read changed logs | `refresh --all` | `status`, `harnesses[]` with statuses/counters; absent harnesses are normal |
| Costliest turn on a day | `top -n 1 --start 2026-10-03 --end 2026-10-04` | Read `prompts[0].cost`, `cost_complete`, `total_tokens`, `first_ts`, `last_ts`, `session`, `turn_id`, and `project_id`; `total_prompts` confirms the ranked population |
| Cost by project in a week | `top -n 10000 --start 2026-09-21 --end 2026-09-28` | Sum non-null `prompts[].cost` by `project_id`; qualify each sum with `cost_complete`, and check `total_prompts`, `unattributed_observations`, and `by` for truncation/unattributed usage |
| Compare two days' priced costs | `insights --start 2026-10-03 --end 2026-10-04` (repeat for next day) | Read `window`, request counts, and `facts[]` where `id == "cost_parts"`: `values.priced_cost`, `values.parts[]`, `price_assumptions`, `values.unpriced_requests`, and `values.unpriced_share` |
| Why is Codex missing? | `doctor` | `roots.codex` (`path`, `source`), `imports[]` (`harness`, `status`, `errors`), `files_by_harness`, `notes` |
| Count observations / group usage | `report --start 2026-10-03 --end 2026-10-04 --timezone UTC` | `totals.observations`, `groups.project_id`, `buckets`, `coverage_complete` |
| Session and linked children | `session claude:SESSION_ID` | `root`, `root_key`, `total`, `models`, `coordination`, `efficiency`, `unassigned` |
| Find rateable threads | `rate claude:SESSION_ID` | `root_session`, `threads[]` (`key`, `models`, `tokens`, `cost`) |
| Record an outcome | `rate claude:SESSION_ID --unit task-1 --thread claude:agent:AGENT_ID --outcome pass` | `root_session`, `unit`, `threads`, `outcome`, `note`, `ts`, `v` |
| Existing report location | `show` with `TOKENATLAS_NO_BROWSER=1` | `html`, `shown: false`, `age_seconds` (no history read or rebuild) |
| Build a report without a browser | `report --html /tmp/report.html` | `html`, `observations`, `privacy`; conditional skips instead have `skipped`, `reason` |
| Refresh and build, headless | `open --no-browser` | `html`, `observations`, `privacy` (or `skipped`, `reason`) |
| Scheduled collection | `collect --no-report` | `steps[]` (`name`, `exit_code`, `ok`, optional `skipped`), `skipped`, `diagnostics` |
| Fixed context overhead | `overhead --refresh` | `harnesses`, `chars_per_token`, `token_basis` |
| Export / merge history | `snapshot /tmp/history.sqlite3`; `import /tmp/history.sqlite3 --label laptop` | `snapshot`, `machine`, `schema_version`; import: `new`, `merged`, `skipped`, `source_machine`, optional `warning` |
| Local plan / provider labels | `plan show`; `profile provider show` | `plans`, `local_providers` |
| Quota budgets | `quota show` | `derived`, `automatic`, `readings`, `budgets`, `invalid_entries` |
| Scheduler state, no installation | `schedule --status` | Platform scheduler receipt; `--dry-run` previews installation/removal |
| Check an available upgrade | `upgrade --check` | `current`, `target`, `installation`, `status`, `details` (explicit network operation) |

These are **list-price estimates, not bills**. Unknown/unpriced cost is `null`,
not zero. Incomplete costs can be lower bounds. Project sums above cover attributed
turns only, not unattributed usage. `report` token totals are not cost totals.
Use exact `project_id` values for project filters, not directory basenames.
`rate` outcomes are `pass`, `partial`, `redo`, `wrong`; only returned thread keys
are rateable (the root is coordination). Listing and recording are noninteractive.

Dates are ISO 8601, e.g. `2026-10-03`, `2026-10-03T14:00`, or
`2026-10-03T14:00Z`. Start is inclusive, end exclusive. Without an offset,
`top` uses UTC; `report` uses `--timezone` (default Europe/Stockholm), and
`insights` uses `--timezone` (default UTC). Prefer explicit `Z` for comparisons;
resolve “yesterday”/“this week” yourself. `insights --days 7` means a trailing
seven-day window ending now, not a calendar week.

## Output and errors

Every JSON result/error has `schema: "tokenatlas.<command>/1"` (nested commands
use their parent name, e.g. `tokenatlas.quota/1`). Existing fields are retained;
new fields may be added. Ignore unfamiliar fields; reject unsupported schema
versions. A field rename/removal or meaning change requires a schema-version
change and a **breaking-change entry in the CHANGELOG**.

| Exit | Meaning | `error.code` |
|---|---|---|
| 0 | Success (including an unchanged report or a busy collection skipped safely) | — |
| 1 | Internal/runtime failure, including a partial refresh | `internal` |
| 2 | Invalid options, dates, values, or combinations | `usage` |
| 3 | Missing session, thread, database, report, snapshot, or requested source | `not_found` |
| 4 | No observations match an analytics range/filter | `no_data` |

With `--json`, errors go to stderr, not stdout:

```json
{"schema":"tokenatlas.session/1","ok":false,"error":{"code":"not_found","message":"no session 'x' in history","hint":"See tokenatlas session --help."}}
```

`error.hint` is always present; `error.details` may carry source statuses or
collection step results/diagnostics. Results go to stdout, progress to stderr.
Collection's JSON mode embeds its diagnostics in the receipt/error details to
keep both streams machine-readable; human collection logs go to stderr.
Installer progress can precede the final error JSON line on stderr.
A filtered empty `report` exits 4 without writing HTML. An **unfiltered** empty
report can be built successfully to show source availability. Doctor can run
before the first import. POSIX signal termination can still use conventional
128+signal exit statuses.

## Stable `top` and `insights` fields (schema 1)

`top` has `schema: "tokenatlas.top/1"`, `by`, `total_prompts`,
`unattributed_observations`, and `prompts[]`.
Each turn contains:

- Identity: `harness`, `session`, `turn_id`, `thread_kind`, `machine`,
  `project_id`, `project_label`, `plan` (`unknown`/`mixed` are explicit labels).
- Timing: `first_ts`, `last_ts`, `duration_s` (observed span, not active time).
- Work: `models`, `requests`, `subagent_requests`, `subagents`, `tokens`
  (`fresh_input`, `cache_read`, `cache_write`, `output`, `reasoning`),
  `total_tokens` (excludes reasoning to avoid double counting).
- Estimates: `cost` (USD priced sum or null), `cost_complete`, `credits`,
  `credits_lower_bound`, and `quota_share` (null when no quota share is known);
  `limit_hit` is added only to matched turns.
- Continuation: `interrupted`, `stopped_request_at`, `resume` (nullable, quoted command).
- Opt-in additions: `text`, `context` with `--with-text`; top-level `text_store`
  with `--keep-text`. Text retention is off by default. `--forget-text` instead
  returns a `forgotten` path receipt.

`insights` has `schema: "tokenatlas.insights/1"`, `window.start/end`,
`requests`, `priced_requests`, `unpriced_requests`, `ambiguous_requests`,
`incomplete_requests`, `price_table.retrieved_on`, `big_turn`, `facts[]`,
`subscriptions`, and `token_efficiency`. Identified request counts exclude
ambiguous identities; `ambiguous_requests` reports those separately.

Each cost fact has `id`, `title_key`, `values`, `params`, `provenance`,
`computation_key`, `assumption_keys`, `computation`, `assumptions`, and
`price_assumptions`. `values` carries the fact's numbers and, where applicable,
`lower_bound`, `lower_bound_requests`, `ambiguous_requests`, and
`incomplete_requests`; `params` carries the matching evidence counts. For
`id == "cost_parts"`, `values` includes `priced_cost`, `parts[]` (each with
`part`, `cost`, and `share`), `requests`, `priced_requests`,
`unpriced_requests`, and `unpriced_share`. Select facts by `id`, never their
position or translated text. Fact populations can overlap: do not sum
different facts' costs.

`subscriptions` has `plans[]` (each with `plan`, `label`, `observations`,
`turns`, `cost`, `priced`, `unpriced`, and `lower_bound`) and a `note` about
overlapping turn counts. `token_efficiency` contains `schema_version`,
`window`, `previous_window`, `settings`, `filters`, `coverage`,
`previous_coverage`, `totals`, `facts`, and `scenarios`; an opt-in
`scenario` is added only when requested and is hypothetical arithmetic, not a
prediction of savings. Its `facts[]` entries expose `id`, `formula`, `unit`,
`numerator`, `denominator`, `share`, `complete`, `values`, and `provenance`.

## Side effects and evaluation

`open --no-browser` and `show --print-path` print **only the path** to stdout
unless `--json` is also supplied, in which case they return a receipt.
`TOKENATLAS_NO_BROWSER=1` has the same effect for both commands. `open` still
refreshes/builds; `--no-refresh` skips only refresh. `show` never builds anything.
Default browser behavior is unchanged. Upgrade requires `--yes` with `--json` or without a TTY;
no command prompts an agent to rate or select a thread.

The reproducible ten-task synthetic fixture, baseline, and independent model
evaluation protocol are in [agent-eval.md](agent-eval.md). A deterministic
baseline is **not** a model usability score.

### Nested insight evidence

Fact-specific `values` are keyed by the fact `id`. Model cost comparisons use
`models[]` (`name`, `cost`, `share`, `priced_requests`), optional `other`,
`priced_cost`, `ladder_models`, and `ladder[]` (`name`, `cost`, `actual`). The
ladder compares the same observed tokens with alternative prices; it does not
predict different model behavior. Context evidence uses `harnesses[]`
(`harness`, `requests`, `median`, `p90`) and `excluded_requests`. Premium facts
compare `actual` with `standard`, reporting `premium` or `extra`, `tiers`, and
`incomplete_left_out`. Turn facts may add `threshold`, `attributed_cost`,
`median_requests`, `unattributed_requests`, `unpriced_turns`, and
`partly_priced_turns`. Subagent cost uses `subagent_cost` and `total_cost`.

Credit evidence distinguishes `credited_requests`, `openai_requests`,
`unrated[]`, `unrated_requests`, `nonstandard`, `nonstandard_requests`,
`unknown_token_requests`, and `cache_write_requests`; unsupported observations
are not guessed. Limit evidence uses `count` and `limits[]` (`limit`, `harness`,
`count`). Quota evidence uses `window_minutes`, `considered`, `turns[]`
(`cost`, `percent`, `lower`, `upper`, `label`, `harness`), `observed_turns`,
and `estimated_turns`.

The energy fact is a proxy, not measured electricity: `mid_mwh`, `low_mwh`,
`high_mwh`, `parts[]` (`part`, `mwh`, `share`), `unweighted_requests`, and `tiers`.
Its formula parameters `e_in`, `e_out`, `e_cr`, and `e_cw` are the fresh-input,
output, cache-read, and cache-write coefficients; `factor` expresses the
uncertainty range. `m_haiku`, `m_sonnet`, and `m_opus` are tier weights;
`tiers` counts observed `haiku`, `sonnet`, and `opus` tiers. Other fact `params` include `retrieved`, `ambiguous`, `incomplete`, and
`lower`. Each `price_assumptions[]` entry has `key`, `text`, and `requests`.

Efficiency `window` fields include `snapshot`, `timezone`, and `partial`;
`settings` contains `top_n`, `context_threshold`, and `contributor_limit`.
`coverage` and `previous_coverage` include `synthetic_excluded`,
`incomplete_requests`, `unlinked_requests`, `rolled_up_requests`, and
`derived_requests`. Summary objects report `requests`, token counters,
`missing`, `known_tokens`, `known_input`, and `complete`.

Efficiency fact values include `top_turns[]` with pseudonymous `turn` and the
summary fields. Concentration adds `top_n`, `turns`, `unlinked`, and
`auto_review`. Context volume adds `threshold`, `eligible_requests`,
`excluded_unknown_input`, `median`, `p90`, and `large`. Delegation separates
`main`, `subagent`, `auto_review`, and `other`. Change evidence contains
`previous`, `projects[]` (`project`, `current`, `previous`, `delta`), `turns[]`
(`turn`, `current`, `previous`, `delta`), and `comparison`:
`equal_elapsed_windows`. Contributions are bounded by `contributor_limit`.

`scenarios.populations` contains `large_context_input` and `subagent_input`.
`overlap` warns that these populations can overlap; `semantics` identifies
hypothetical arithmetic. An explicit `scenario` also includes `population`,
`percent`, `input_tokens`, and `hypothetical_reduction`, together with its
`formula` and completeness. It is not a forecast of cost or token savings.
