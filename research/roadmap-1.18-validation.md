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

Dispositions:
- The conditional cron ownership mismatch is not present: `_cron_owned_lines` and removal use the same marker suffix predicate with CR/LF normalization. `snapshots_from_records` uses original record indexes before sorting snapshots, so it preserves assignment alignment.
- The suggested `quota show` call-order failure is not reachable: `budget.run` calls `automatic` before `cap_fn`, and calls `cap_fn` only when automatic budgets exist. The unchanged `_auto_budgets` wrapper supplies the same records, hits, snapshots, table and keep value to `budget.auto_budgets`.
- Public plan tiers in shared reports are intentional under #146; the package does not expose numeric private quota calibration. Redaction revision 2 is already a bump from baseline 1.
- Dictionary-coding the new client labels is deferred: payload gzip already compresses repeated labels, the 20k payload-size guard passes, and browser tests exercise 20k rows with work groups. No new persistent cache or schema was introduced.
- Native launchd teardown timing, systemd user-session availability and Windows task XML encoding remain OS integration limits of fake-tool tests. Failures are surfaced rather than reported as successful installation.

## Verification status

The first broad local run found a quota-progress regression (fixed), plus timing failures in existing subprocess tests under host load. The packaged-sync timeout failure also reproduced on the unmodified baseline. All six CI jobs passed on `f889b898b727376eaea03d7ba68bdc21e371b6c7`: Python 3.10/3.11 Ubuntu, 3.12 macOS, 3.13 Windows, and Chromium/WebKit. Final small follow-up corrections passed 45 focused tests; their exact final head will be checked again. Final performance measurements will be recorded here before readiness.

## Delegation

Three native implementation leaves requested `gpt-5.6-luna`, `high` (runtime model/effort telemetry not exposed). Usefulness: partial for performance, scheduling and report work; parent found and corrected substantive defects, integrated changes, and owns final verification. The bounded history-query optimization leaf passed output/source/date parity and decode-count regressions (usefulness: pass; requested Luna/high, runtime telemetry unavailable). Independent review findings were validated as hypotheses, not accepted blindly.
