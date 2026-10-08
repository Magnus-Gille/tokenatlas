# Roadmap delivery validation (1.18.0)

Scope: #55, #56, #79, #143 and #146. Baseline source: `3e22cf6` (1.17.0).

## Correctness and privacy

- Reused turn assignments are mapped by observation identity before internal sorting. Regression fixtures cover equal timestamps, reversed input order, retained prompt text, and limit-hit costs.
- The top-store skip identity includes revision counter and random revision token, machine, price-table digest, retained selection semantics, selection algorithm version and package version. Statusline reuse also includes package version and local timezone/day.
- Report reuse checks the store and quota-budget file content before reading history. Manual display profiles remain part of report identity.
- Work groups use retained top-k context only. Shared reports omit those groups and custom provider names. Public client names and public plan tiers are explicitly allowed by #146; redaction revision advances from 1 to 2.
- All scheduler tests use temporary homes and fake scheduler tools. No native scheduler was installed during validation. Current installation, database, environment quoting, ownership, failure preservation and CRLF cron round-trips have regression coverage.

## Independent review

Claude Opus 5.5 (`claude-opus-5-5`, observed runtime model), requested `xhigh`; runtime effort telemetry unavailable. The review route had no tools, MCP servers, session persistence, browser access or private history. Inputs were scoped source, diffs and test evidence.

Grounded findings fixed: assignment/sort alignment; fail-closed cron inspection; CRLF cron removal; systemd rollback order; top-store package-version invalidation and normalized context hashing; profile CLI error handling; macOS removal of owned cron leftovers; safe systemd log-path rendering; hermetic scheduler tests; full cron argv reporting; raw cron line preservation; truthful status; no unrelated crontab output in ordinary install receipts. The quota progress indicator regression found by CI was restored. Follow-up review also led to cron preflight before removal, filtering unknown location markers before localization, moving profile receipts outside the mutation error handler, and stronger decode-count coverage. The final scoped Opus review reported no correctness regressions.

The benchmark isolation review found a Windows SQLite connection-lifetime issue, which was fixed with explicit closing. Read-only counts, UTF-8 output and path normalization were also verified. Opus reached its weekly quota before the final helper-only follow-up. A native independent reviewer requested as `gpt-5.6-sol`/`xhigh` (runtime model/effort telemetry unavailable) reviewed the frozen final helper/test input `2c3b902efaabbe9b66079c346b6cda61aa871960199305399ce78a6974b257c1` and reported no findings or blockers. No private data was supplied.

Dispositions:
- The conditional cron ownership mismatch is not present: `_cron_owned_lines` and removal use the same marker suffix predicate with CR/LF normalization. `snapshots_from_records` uses original record indexes before sorting snapshots, so it preserves assignment alignment.
- The suggested `quota show` call-order failure is not reachable: `budget.run` calls `automatic` before `cap_fn`, and calls `cap_fn` only when automatic budgets exist. The unchanged `_auto_budgets` wrapper supplies the same records, hits, snapshots, table and keep value to `budget.auto_budgets`.
- Public plan tiers in shared reports are intentional under #146; the package does not expose numeric private quota calibration. Redaction revision 2 is already a bump from baseline 1.
- Dictionary-coding the new client labels is deferred: payload gzip already compresses repeated labels, the 20k payload-size guard passes, and browser tests exercise 20k rows with work groups. No new persistent cache or schema was introduced.
- Native launchd teardown timing, systemd user-session availability and Windows task XML encoding remain OS integration limits of fake-tool tests. Failures are surfaced rather than reported as successful installation.

## Verification status

The first broad local run found a quota-progress regression (fixed), plus timing failures in existing subprocess tests under host load. The packaged-sync timeout failure also reproduced on the unmodified baseline. All six CI jobs passed on product head `5153605bb536bdc9fd01b7077584a4d8446d995f`: Python 3.10/3.11 Ubuntu, 3.12 macOS, 3.13 Windows, and Chromium/WebKit. Follow-up corrections passed 45 focused tests. The final benchmark-helper regression suite passed all 7 tests; the same six CI jobs must pass again on the final documentation/helper commit before merge. Final desktop/mobile report screenshots were inspected with no overflow.

## Delegation

Three native implementation leaves requested `gpt-5.6-luna`, `high` (runtime model/effort telemetry not exposed). Usefulness: partial for performance, scheduling and report work; parent found and corrected substantive defects, integrated changes, and owns final verification. The bounded history-query optimization leaf passed output/source/date parity and decode-count regressions (usefulness: pass; requested Luna/high, runtime telemetry unavailable). The final native helper review was useful (pass), with deterministic regression tests and the full-size benchmark independently checked by the parent. Independent review findings were validated as hypotheses, not accepted blindly.

## Synthetic performance and golden comparison

400,000 deterministic observations, same script and Python 3.10 on the owner's Mac. Baseline archive verified file-for-file against `3e22cf6aa61bed92afae742621649bbea415bfe2`; performance-only candidate frozen in local commit `8b689bf44d24573af5dbadac6ffd98886b74e01b`. Feature payload additions/version changes are deliberately excluded from the performance-only comparison. Benchmark script SHA-256: `ed5193e107c6ff0e6491b15d7193f860326d285fe4a71fdc21905f69005daeb9`.

| Step | Before CPU s | After CPU s |
|---|---:|---:|
| assign_prompts | 2.643 | 2.621 |
| cost_facts | 25.025 | 25.640 |
| quota_shares | 78.058 | 89.247 |
| report | 77.523 | 78.758 |
| snapshots | 4.538 | 5.217 |
| top_prompts | 12.666 | 10.121 |

All step hashes match. Final output hashes (SHA-256 prefixes): insights `15dc51bbbf17a6fb`, report `f7ec256fa6a378fc`, top `e8aa869699fed461`. The top computation improved; this fixture does not establish a general speedup in quota allocation or report building. Host load was substantial, so wall time is not used as a performance claim. Full command measurements on the fixed real snapshot and the unchanged-collect acceptance check follow separately.

The performance-only diff is preserved as `research/performance-only-1.18.patch`: apply it to a disposable checkout of the baseline to reproduce the comparison without the intentional new report fields. Run the current `scripts/benchmark_performance.py --source <baseline-or-patched-checkout> --observations 400000 --json` for each source. The application source snapshot, rather than the local-only commit name, is therefore reviewable in this PR.

The collect measurement uses a fresh subprocess with a fake home set before imports. A regression test proves that an outside synthetic Cowork log cannot enter the fixture; the helper also checks that its observation count stays fixed after every CLI step. An earlier smoke fixture was discarded after this isolation defect was found. No private fixture data is included in the repository.

## Fixed real-history snapshot

427,122 retained observations in a private SQLite backup. No source logs or prompt store were copied into review/public artifacts. Commands ran with a fixed clock and identical output path; every complete canonical JSON/report-payload SHA-256 matched baseline.

| Command | Before CPU s | After CPU s | After wall s | Change |
|---|---:|---:|---:|---:|
| top | 71.842 | 51.228 | 54.147 | -28.7% |
| insights | 94.886 | 44.731 | 45.220 | -52.9% |
| quota | 146.205 | 88.977 | 90.897 | -39.1% |
| report | 138.503 | 137.361 | 137.874 | -0.8% |

The first optimized top/insights timing samples were distorted by severe memory pressure (222.763/177.104 CPU s; 784.789/387.812 wall s). Those outputs already matched; only the timing samples were repeated after memory pressure subsided, on unchanged source/data, and matched again. Both samples are retained in local evidence. Quota/report samples did not need a repeat. Large report builds remain costly; no substantial report-build speedup is claimed.

Golden SHA-256 values:
- top: `3b809edf596e45eca3786e9b0fc64073ce4eca9d0415e097a8e70e412175a308`
- insights: `c084760b6b36c0e29ce2788069f384279263ed1b2235ff11fc8b08ad70e002b3`
- quota: `d9bfaba85a4c29a53e9c2c5058c6c4beb7713d0819cbb850d86dd6dfa24e65ae`
- report: `2c4adb0b9910b0a694503c16536c310a16f58a09f69f2b8ab9a49dde48d92f33`

## Unchanged collection acceptance

Final full 1.18.0 candidate, 400,000 synthetic observations, empty fake harness roots and no remotes. The observation count remained 400,000 after every command.

| Step | CPU s | Wall s |
|---|---:|---:|
| Initial retention | 10.950425 | 11.169240 |
| Warm collection | 78.207721 | 78.586898 |
| Unchanged collection | **0.043368** | **0.043770** |

Unchanged collection skipped top ranking and left the report unchanged. This meets #56's less-than-five-CPU-second criterion. Counts are checked using read-only SQLite connections outside the timed section. Reproduce in a fresh private directory with `python3 scripts/benchmark_collect_fixture.py <directory>/history.sqlite3 --observations 400000 --measure-collect`.
