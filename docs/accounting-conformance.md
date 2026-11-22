# Accounting conformance (TOKEN-29)

`test_conformance.py` turns the accounting hazards from the AgentMeasure audit
([audit report](https://github.com/roy-tong/AgentMeasure/blob/main/campaigns/audit-report-2026-09.md),
[casebook](https://github.com/roy-tong/AgentMeasure/blob/main/campaigns/measurement-casebook.md), MIT) into regression
tests against the four importers (Claude Code including subagents, Codex, OpenCode, Pi; there is no Cowork-specific case). The data is
synthetic. A passing test means TokenAtlas does not have that specific bug; it does not certify the numbers in general.

Convention used by every case: a call's total is fresh input + cache read + cache write + output. Reasoning is a subset
of output and is never added again.

| Hazard (source) | Harness | Status | Test |
| --- | --- | --- | --- |
| One line per content block, same message id and usage (audit class 1) | Claude | covered | `test_claude_one_line_per_content_block_is_one_call` |
| Streamed partial rows, first-wins undercounts (class 1 variant) | Claude | covered | `test_claude_streamed_partial_rows_keep_the_final_output` |
| Same response written twice, partial then final | Pi | covered | `test_pi_streamed_duplicate_entries_keep_the_final_counts` |
| `output_not_final` (a call with no stop reason is a lower bound) | Claude | covered | `test_claude_output_without_a_stop_reason_is_not_final` |
| Resume or fork replays the parent's rows (class 5) | Claude | covered | `test_claude_resumed_or_forked_session_copies_history_once` |
| Resume loses history before the resume (class 5) | Claude | covered | `test_claude_resume_does_not_drop_history_before_the_resume` |
| Fork replays the parent prefix | Pi | covered | `test_pi_forked_session_replaying_the_parent_counts_once` |
| Resumed rollout replays earlier events (class 5) | Codex | covered | `test_codex_resumed_rollout_replaying_the_prefix_counts_once` |
| Cumulative counter restarts after resume | Codex | covered | `test_codex_lower_counter_after_resume_adds_instead_of_subtracting` |
| Subagent transcript versus its parent | Claude | covered | `test_claude_subagent_transcript_is_separate_from_its_parent`, `test_claude_subagent_rows_copied_into_the_parent_count_once` |
| `token_count` re-emitted with identical totals (class 2) | Codex | covered | `test_codex_reemitted_token_count_with_identical_totals_counts_once` |
| Per-call deltas reconcile with the final cumulative total (class 2) | Codex | covered | `test_codex_per_call_usage_reconciles_with_the_final_cumulative_total` |
| Input excludes cache, cache classes separate (class 3) | Claude, Pi | covered | `test_claude_input_excludes_cache_and_cache_classes_are_separate`, `test_pi_cache_classes_are_separate_from_input` |
| Cached input is a subset of input (class 3) | Codex | covered | `test_codex_cached_input_is_a_subset_of_input` |
| 5-minute and 1-hour cache writes sum, not add (class 3) | Claude | covered | `test_claude_cache_write_ttl_split_sums_without_double_counting` |
| 1-hour cache write priced at 2x input, not 1.25x (class 3) | all | covered elsewhere | `test_pricing.py` (separate `cache_write_5m` and `cache_write_1h` prices; no split means `partial`, not a guess) |
| Reasoning tokens are a subset of output | Claude, Codex, OpenCode | covered | `test_claude_thinking_tokens_are_a_subset_of_output`, `test_codex_reasoning_is_a_subset_of_output`, `test_opencode_reasoning_is_reported_as_a_subset_of_output` |
| Retries and errors: a failed attempt with no usage is not a call | Claude, OpenCode, Pi | covered | `test_claude_failed_retry_without_usage_adds_nothing`, `test_opencode_failed_request_without_tokens_adds_nothing`, `test_pi_response_without_usage_adds_nothing` |
| A retried request repeating its id counts once | Claude | covered | `test_claude_retry_that_repeats_a_request_id_counts_once` |
| A zero-token event adds nothing | Codex | covered | `test_codex_event_with_zero_counters_adds_no_tokens` |
| Fork under a new session id replays the parent's `token_count` events (class 5) | Codex | **known deviation**, unverified | `test_codex_fork_under_a_new_session_id_counts_the_replayed_prefix_once` (expected failure) |
| History copied on migration counts once | OpenCode | covered | `test_opencode_message_present_in_both_generations_counts_once` |
| Pricing-table drift (class 4) | all | not applicable to the importers | prices live in `prices.json` with a source URL and retrieval date per model; see `docs/pricing.md`. Drift is a maintenance task, not a parser bug |
| Explicit-zero repricing (a provider-logged real price of 0.0 replaced by a list price) | all | not applicable | TokenAtlas never reads a provider-logged cost; every cost is a list-price estimate from tokens |
| Semantic field reuse (a flag that swallows a gap into a zero) | all | not applicable as a parser case | unknown price and unknown tokens stay `n/a` rather than 0 (`docs/pricing.md`, status values) |

## Provenance of the cases

Audit classes 1 to 5, reasoning-as-subset and semantic field reuse come from the AgentMeasure texts. The cases labelled
"TokenAtlas-derived" in the test docstrings (retries and errors, subagent transcripts versus their parent, history copied
on migration, `output_not_final`) are hazards we added ourselves; they are not in the AgentMeasure audit or casebook.

## Known deviations

- **Codex fork under a new session id.** A Codex call id is `<session id>:<ordinal or hash>:token_count`. If a fork writes
  a new session id and replays the parent's `token_count` events, the replayed prefix would be counted a second time.
  A resume that keeps the original session id is handled. Whether a real Codex fork replays events under a new id is not
  verified (no real rollout was available); the test is an expected failure until someone checks.
- Two different Codex calls that share a session id and a row `ordinal` across two files merge into one call.

## Limits worth knowing

- A row whose counters are all explicit zeros is dropped (Claude, Codex). A row with a missing or null counter is kept as a
  zero-token request, because the zero is not proven.
- OpenCode's reasoning is added to its `output` on the premise (from the importer's reading of OpenCode) that
  `tokens.output` excludes reasoning. This has not been re-checked against a real database for every provider.
- Where a harness writes no request id, TokenAtlas derives a deterministic one from the row; two independent identical
  rows then look like one call. Records carry `id_synthetic` so such cases can be told apart (a Codex id hashed from the
  timestamp and cumulative total counts as not synthetic).
