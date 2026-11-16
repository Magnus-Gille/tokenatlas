# Local observed-usage history (issue #10)

## T0.2: request identity is not a billing grain

Decision, 2026-09-26: keep **request observations**, not "one verified billable
inference per request". The v1 SQLite format is an observed-usage schema; it is
not the billing schema proposed in the earlier architecture document. Each
observation preserves allowlisted raw counters and `iterations` (including
iteration type/model), source/collector versions and provenance. When streaming
arrays grow or change order, original sanitized `iteration_snapshots` are retained;
incompatible type/model positions are never merged. The representative `iterations`
array may contain derived maxima; `iteration_snapshots` contains only original
sanitized arrays. Layout changes are warnings. Reports set
`billing_verified: false` and never attach an invoice amount.

Anthropic's [advisor documentation](https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool#usage-and-billing),
checked 2026-09-26, states that advisor iterations can use another model and
are excluded from the top-level executor counters. Therefore `(provider,
request_id)` identifies an envelope, not necessarily one billed inference.
Adding the envelope and its iterations would also double-count overlapping
executor work. This is sufficient to reject the old universal billing-grain
assumption; it does not independently verify a customer's bill.

A local structural check of retained Claude Code transcripts on 2026-09-26
(versions 2.1.270 through 2.1.283) found populated iteration arrays containing
one iteration, with each of the four counters equal to the parent counters.
Empty/null arrays also occurred. These were streaming rows, not independently
deduplicated paid requests. There was no independent invoice or API usage
reconciliation, and no paid model calls were made for this investigation.

The importer counts top-level observed usage once. Multiple iterations,
non-message iterations or mismatching single-iteration counters mark the
observation incomplete. Reports expose a **known subtotal** and set complete
total to null in such cases; the retained iterations permit later, explicit
reprocessing. They are not silently discarded or added to the parent.
`known_tokens` excludes observations with ambiguous synthetic identity. Their
counters appear separately in `ambiguous_identity_tokens` and
`ambiguous_identity_token_fields`; these may overlap and are not additive to the
known subtotal. Any ambiguous identity makes complete totals null.
No number in this feature claims full-account coverage or verified billing.
Before issue #12 prices such records, it must implement versioned iteration
normalization and model/rate attribution, with independent billing evidence
where an exact charge is claimed.

## Codex quota (issue #89)

Codex `token_count` events carry the account's `rate_limits`. A Codex observation keeps a compact `quota`
object from the event that produced it (null when absent or unusable): `limit_id`, `plan_type`, `reached`
(the reached-limit type) and `windows`, a list of `{slot: primary|secondary, minutes, used_percent, resets_at}`
(`resets_at` is UTC ISO-8601 or null). `used_percent` may exceed 100. The credit balance, limit name and other
account state are deliberately not stored. The object is stored as JSON through the strings dictionary in an
additive `quota` column (schema stays 2), merges as a whole object (the newer observation wins, else the other),
travels in snapshots, and the Codex harness revision 3 re-reads existing Codex files once. Other harnesses
have no quota. No report or total uses it yet.

## Limit events (issue #91)

A rejected Claude request (transcript row with `quotaLimits.status == "rejected"`, zero tokens, model `<synthetic>`) is kept as an observation
with an unknown model and a `quota` of `{reached: <rateLimitType>, status: "rejected", windows: [{slot: five_hour|seven_day, minutes: 300|10080,
used_percent: 100, resets_at}]}` (no window for an unknown type). `History.records()` leaves such limit events out by default
(`include_limit_events=True` returns them; snapshot import uses that), so they are never a request, cost, energy, turn or session count;
`History.limit_events()` returns them. `limits.limit_hits` turns them (deduplicated per harness, limit type and reset time) and Codex
observations with a reached-limit type into hits, and ranks the turns in the window by list-price cost for the same harness and provider.
The window differs by harness. Claude's 5-hour limit is a session block and its weekly limit resets at a fixed time, so a Claude hit's window
is reset-anchored: `[resets_at - window, hit]`. Codex windows are rolling and their reset time only slides, so a Codex hit's window is the
trailing one: `[hit - window, hit]`. A hit with no known window length or no reset time (Claude) has no ranking. Reset times are re-parsed into
canonical UTC text at use; anything unparseable is dropped. The shares are of what tokenatlas saw in the window, never of the limit: usage elsewhere counts toward the limit but is not in the logs.

## Storage decision

For the single-machine seminar MVP, SQLite from the Python standard library
provides transactions, a writer lock, deduplication and file checkpoints in one
local file. This intentionally implements a smaller slice than the earlier
immutable-chunk/multi-machine design. No sync, quota meter, heartbeat service,
credential fingerprint, or statusline migration is introduced.

`PRAGMA user_version=2` versions the database (observation dicts keep `v: 1`). Schema 2 stores typed
integer columns, one global string dictionary, a compact positional `raw_usage` array (rare shapes in
`extra`), and integer source links: a real 379,401-observation history takes 140 MB instead of 990 MB
(about 370 bytes per observation instead of about 2.6 KB); a version 1
database migrates in one transaction on first open (retained observations whose files are gone survive)
and is then vacuumed. Unknown database versions fail
closed; later migrations must be explicit and preserve original observations.
On POSIX systems, database files are 0600 and a newly created immediate state
directory is 0700. Native Windows uses the current user's filesystem ACLs because
POSIX ownership and mode checks are unavailable.
A random machine ID persists in the database and does not contain a hostname.
It identifies this local store, not physical hardware; cloned stores are not
independent machine identities. Cross-machine merging is outside v1 scope.

The four normalized token categories are non-overlapping. Codex fresh input
requires all of its input/cache counters; if a cache field is absent, the
remainder is unknown. Pi already stores reasoning as a subset of output. OpenCode
stores text output and reasoning separately, so its adapter adds reasoning to the
normalized output while retaining the reasoning subtotal. Reports therefore always
treat reasoning as a subset of normalized output and never add it again.
Unavailable fields remain null. Raw counters survive normalization;
nontrivial iterations and inconsistent token relationships are warnings.

`flags` is a sorted list of short labels the harness itself logged about a request, or null. The only flag is `interrupted`
(#96): the user stopped the request or its turn. Claude Code writes a `[Request interrupted by user` user row (the latest request in the same
file's current turn is flagged: in a subagent file that is the subagent's latest request, and the flag rolls up to the parent turn; main-thread
markers are rare in current transcripts), Codex a `turn_aborted` event (the last record of that turn),
Pi an assistant message with `stopReason: "aborted"` (that request, or the turn's last one if it carries no usage), OpenCode a message
error named `MessageAbortedError` (usually with no tokens, so the last retained request of the same turn is flagged). Nothing is inferred; a stop the harness did not log is not flagged. Merging two copies of an
observation keeps the union of their flags. The column is added within schema 2 and stored as a JSON string reference; the
harness revisions were bumped so existing histories are re-read once to fill it.

Only changed source files are reparsed (whole file, to retain session context).
OpenCode is read through a query-only SQLite connection; its database, WAL, and SHM
metadata form the checkpoint fingerprint. Other file fingerprints include inode,
size, modification and change times. A file modified less than 2 seconds before its read finished is not
checkpointed and is reread on the next refresh: a same-size rewrite within one filesystem timestamp tick would
keep every one of those fields, so its fingerprint cannot be trusted yet (like git's "racily clean" entries). Source
checkpoints and merged observations commit atomically under SQLite's writer
lock. The same provider/harness/request key is merged with maximum observed
counters, so streaming updates cannot regress. Codex events without an ordinal
use the session, timestamp, cumulative counters and reset segment, independent of
physical line numbers. Claude row UUIDs without a request/message ID and Codex
rows lacking enough identity metadata are explicitly ambiguous. Pi response IDs
are provider-scoped; copied fork entries retain the earliest source session.
OpenCode message IDs are the per-call identity and sessions are scoped by harness
in the HTML report. Synthetic identities are scoped
to this store's machine ID and have weaker guarantees. No data is deleted from
history because a source is rotated, truncated or removed. Errors and partial
tails are visible; incomplete tails are reparsed on the next refresh.

Imports are manual. Run `refresh` before source retention expires; an optional
scheduler can be configured separately. There is no background collector or
installation change in this ticket. Old daily aggregates cannot reconstruct
missing request, prompt or minute-level data and are not added to transcripts.

## Privacy and interpretation

Prompt content, tool inputs, credentials and environment configuration are
never persisted. User turn IDs are linked from observed metadata where possible;
sequential associations are marked derived. Unlinked turns/subagents remain
explicitly unknown. A prompt reference is not a copy of the prompt.

The local JSON report includes project identities and import root paths;
`--records` additionally includes individual source-file references;
keep it private. The default HTML report pseudonymizes direct identities but retains
exact times and model metadata, so it is not guaranteed anonymous. An event's timestamp
is an attribution time, not a measurement of continuous token generation during
that minute. Request duration is null when unavailable. Local-day/hour/minute
buckets use IANA timezone rules and distinguish repeated DST hours by UTC offset.
