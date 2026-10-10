# Token analytics

The offline report starts with a token-focused analytics view. Choose a 7- or
30-day window, move through history, and compare models, agent roles, surfaces,
harnesses and effort levels. Empty days stay on the timeline. Session and weekly
details make it possible to trace an aggregate back to its recorded usage.

## Reading the numbers

- **Tokens** sum fresh input, cache creation, cache reads and output. Reasoning
  is already included in output and is never added again.
- **Tokens per request** describes recorded model calls, not user messages.
- **Tokens per linked turn** uses requests that can be assigned to a turn;
  requests without a link are reported separately.
- **Cache reuse** is cache reads divided by all input, including cache creation.
  Missing counters make this ratio unknown rather than zero.
- **Period changes** compare equally sized adjacent windows. A zero baseline
  cannot support a percentage change. Missing collection is not proof of zero
  real-world usage.

Lower token use does not establish better answers or successful work. Compare
similar tasks and review the outcome before changing model or effort choices.
Account plan percentages and credit equivalents retain their existing separate
definitions; token shares are not subscription-limit shares.

## Coverage

TokenAtlas reads local coding-agent usage logs. It does not read ordinary
ChatGPT or Claude chat, or reconstruct account-wide usage from those logs.
Surfaces and agent roles are recorded metadata and can be unknown.

Activity comes from the optional retained context of top turns. These are
aggregate shell, edit, web and subagent counts, not a complete event stream.
They cannot establish full plugin or skill usage, or attribute exact token
cost to an individual tool. Shared reports omit private turn context.

An observed turn start is the first usage observation assigned to that turn;
it is not necessarily the time the user sent its initiating message. Speed and
service-tier fields remain unknown when absent. Shared reports only expose
allowlisted public values.

All calculations run locally in the standalone HTML report. No chart service,
analytics endpoint or external script is required.

## Token-efficiency facts

The analytics view adds four compact investigation signals, with the same numeric
contract in the additive `token_efficiency` object of `insights --json` (schema
version 1). Existing cost facts keep their prior contract. The Python and offline
JavaScript implementations are checked against the same synthetic input and exact
JSON expectations. No model call or network request is needed.

- **Concentration:** sum the known tokens of the largest N linked work turns and
  divide by all linked work-turn tokens. Automatic review is separate; unlinked
  requests never become a fabricated turn. Child requests retain their inherited
  turn assignment from the full history, even when filters exclude their parent.
- **Context volume:** input is fresh input + cache read + cache write. Requests
  with any missing input counter are excluded from threshold membership and the
  context distribution, with their count disclosed. The threshold is a presentation
  choice, not a validated waste detector. The median uses complete-input requests;
  p90 uses nearest rank, `ceil(0.9 * n) - 1` in the sorted array.
- **Delegation:** main, subagent, automatic review and other requests are disjoint
  groups. Subagent token share excludes automatic review from its numerator and
  includes every identified request in the denominator. Rolled-up children are
  counted once, not added again as a parent total.
- **Change:** compare known token sums with an adjacent window of equal elapsed
  duration. Rank project and turn contributors by absolute change; project usage
  follows each observation's project, including distinct worktrees. Unknown project
  attribution is not inferred from a turn's first request. A zero baseline has no
  percentage change; incomplete history cannot establish a real consumption trend.

Bounds are inclusive start / exclusive end. Evidence states exact timestamps,
timezone, snapshot, partial windows, settings and coverage. The browser derives
boundaries from the displayed local dates; CLI ISO boundaries retain their offsets.
`--days` remains a rolling elapsed-time window. To reproduce a browser window in
the CLI, pass its exact start/end and timezone, N and threshold. For identical
results also use the same retained observations and selection; a later database
snapshot can contain additional data. All-history CLI evidence starts at its first
observation and ends one millisecond after its retained collection snapshot unless explicit bounds are supplied. The CLI snapshot is the latest retained collection attempt, successful import or observation time, excluding future timestamps (an empty history without collection metadata uses the Unix epoch). Re-exporting unchanged history with fixed bounds is deterministic. `--days` intentionally uses the current rolling window. Timestamps are normalized to millisecond precision on both surfaces.

Unknown counters remain unknown; their available values contribute known sums.
Incomplete sums are lower bounds, but shares are shares of known counts and are
not themselves lower bounds. Ambiguous synthetic identities are excluded and
counted separately. Reasoning is already part of output. Observed volume does not
prove waste, lack of progress, model suitability, or outcome quality.

### Agent evidence and scenarios

The export is bounded to a small contributor list and uses pseudonymous project
codes and report-local turn ordinals. It omits raw session IDs, paths, prompts,
titles and transcript content, including when exported from a private report.
Those references are local to the report/history selection, not durable global IDs.
Private display labels can help the user navigate; they do not belong in the
aggregate evidence export. Applied filters are disclosed without exporting private
search text or metadata values.

The scenario interaction starts without an assumed saving. Select a population
(`large_context_input` or `subagent_input`) and a percentage explicitly. The result
is `known input tokens in population * percentage / 100`; output is excluded.
These populations can overlap, so reductions must not be added. This arithmetic
does not predict achievable savings, quality, money, subscription quota or energy.
There are no automatic instruction edits, stop rules, model switches or compactions.
Action templates and rated-outcome analysis remain separate follow-up work.
