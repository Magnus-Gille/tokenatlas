# Performance of a changed `collect` run (TOKEN-189)

Idle runs (nothing changed) are skipped cheaply (TOKEN-56). A changed run re-reads the whole history, so its cost grows with the
request count. Measured on a copy of a real history: 620,406 requests, 290 MB database, 25 MB report, one machine, CPython 3.10.
All numbers are wall seconds for one command on an otherwise idle machine (the numbers below were taken before the machine became busy).

## Profile (before)

| Step | Before | After |
| --- | --- | --- |
| `top --keep-text -n 10` | 215 s | 72 s |
| `report --html --private` | 333 s | 100 s (measured before the gzip level change, which saves about 14 s more; not re-measured because the machine became loaded) |

Where the time went in `top` (cProfile, relative shares; the profiler inflates absolute time about 3x):

1. **Loading the history** (`History.records`, about 60 s): a generator expression over every column of every row, `json.loads` four to five
   times per row, and the cyclic garbage collector rescanning hundreds of thousands of live dicts. 33 s of it was collection alone.
2. **Per-hit rescans** (`limits._fill`, `budget.cost_seen`): each limit hit or calibration reading scanned and re-priced the whole history
   (hits x requests), about 50 s in `limit_hits` and 30 s in the automatic budgets.
3. **Pricing and timestamp parsing**: `price_observation` was called 2.4 million times (mostly the repeats of 2) and
   `prompts._t` parsed 40 million timestamps, most of them repeats.

In `report`, the same three plus the gzip step: the page payload (138 MB of JSON) was compressed at level 9, 16 s of a 333 s run.

## What changed

- `History.records` resolves reference columns with one `map`, parses stored JSON with the scanner directly, and runs with the garbage
  collector off. The CLI entry point (`main`) also keeps it off for the whole command, so later passes do not rescan the history. Peak
  memory is unchanged within a few percent (3.3 GB vs 3.5 GB for loading alone).
- `limits._WindowIndex` and `budget.SeenIndex` index requests by time once, so each hit or reading bisects and each request is priced at
  most once. Sums are taken in the original request order, so results are bit-identical.
- `prompts._t` is cached; `subscriptions.annotate` parses timestamps only for the
  rows that can supply a plan.
- The report payload uses gzip level 6 (2.4 s, 3 % larger) instead of 9 (16 s). The decoded data is the same.
- `collect` logs the history size next to each step: `report exit=0 (84.1s) requests=620406` (example).

## Sprint 2: one load for top and report

`tokenatlas/shared.py` adds a per-process scope. `main` opens one, and `collect` keeps one over all its steps. Inside it:

- the loaded, project-resolved, plan-annotated history is kept while the database path, revision, revision token and saved project roots are
  unchanged (`__main__._plan_history`); the report uses the same list objects as `top`, so every result keyed by identity is reused:
  prompt assignment, limit hits, the global top ranking (which `prompt_store.visible_all` used to recompute), quota snapshots and automatic budgets;
- `pricing.price_observation`, `price_vector` and the price-table lookup are computed once per request object (each caller gets its own copy).

Outside a scope nothing is shared, so a manual `top` or `report` behaves as before. The report is byte-for-byte the same payload apart from timestamps
(compared on a 150k history).

Measured in this container on a synthetic 700k-request history (4 cores), top then report in one process:

| | top | report | together |
|---|---|---|---|
| before (separate loads) | about 70 s | about 105 s | about 176 s |
| shared scope | 65-68 s | 75-89 s | 143-154 s |

The goal of 90 s for refresh + top + report is still not met: about 1.6x remains. Where the time is now (profile, so inflated): reading and decoding the
rows from SQLite (about 15 % of top), `build_report` row work, `turn_shares` and `snapshots_from_records`, `cost_facts` and its repricing, and the energy and credit
passes. The next step is persisted per-request derived values (cost, window and credit vector) keyed by revision, so a changed run only derives rows added since the last one.

## Guard

`test_scaling.py` builds a synthetic history in the test and asserts operation counts, not time: every request is priced at most once
across all hit windows and calibration readings, hit windows equal a reference full scan, and timestamps parse once. On the old code
the first test fails (3,728 price calls for 1,500 requests).

## What remains

A changed run is still one full load plus several full passes in `top` and again in `report`. In `collect` both run in one process but each loads the history itself; a manual `tokenatlas top` and `tokenatlas report` are two processes.
The remaining hot spots are `quota_share.snapshots_from_records` and `turn_shares`, `insights.cost_facts`, and building the report rows.
A real incremental design (recompute only periods, turns and projects touched since the last revision) would need persisted derived
state and is not part of this change.

Target status: top + report is about 160-170 s on this history, so the 90 s goal for refresh + top + report is not yet met (about 2x remains).
