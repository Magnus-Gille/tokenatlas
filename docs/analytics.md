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
