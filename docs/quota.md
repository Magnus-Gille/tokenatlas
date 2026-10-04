# Subscription quota

On a subscription, list-price cost is not what you pay. What limits you is the vendor's usage limit, a percentage of a
rolling window. This page says what tokenatlas can observe about those limits, how it attributes them to turns, and
how its quota numbers are worded. Research for #94, retrieved 2026-10-03.

## What each agent exposes

| | Claude Code | Codex |
|---|---|---|
| Where | Statusline payload `rate_limits` (v2.1.80+, Pro/Max), live only; transcripts record `quotaLimits` only on a **rejected** request | Every `token_count` event in the rollout carries `rate_limits` |
| Windows | `five_hour` and `seven_day`, each `{used_percentage, resets_at}`; a window can be absent | `primary` and `secondary`, each `{used_percent, window_minutes, resets_at}`; the length is `window_minutes`, not the slot name |
| Resolution | Number 0–100, may be fractional (precision undocumented) | Whole percent in practice (the backend type is an integer) |
| Plan | Not in the payload | `plan_type` (`pro`, `prolite`, `team`, …) |
| Limit reached | Rejected request: `error: rate_limit`, `quotaLimits.rateLimitType` `five_hour` / `seven_day`, `resetsAt` | `rate_limit_reached_type` (`rate_limit_reached`, `workspace_*_credits_depleted`, `workspace_*_usage_limit_reached`) |
| Written to disk by the agent | Only the rejection rows | Yes, in every rollout |

Both are **account-wide**. Claude's limit is shared by claude.ai, Claude Desktop, Claude Code and Cowork. Codex local
messages and cloud tasks share one allowance. Usage on other devices, or in chat, moves the percentage too, and
tokenatlas does not see it.

No vendor publishes a conversion from tokens or messages to a percentage. Model, context length, reasoning effort,
tool use and caching all change how fast a limit is used.

Sources: support.claude.com articles 11647753 and 11145838; code.claude.com/docs/en/statusline, /costs, /errors;
the anthropics/claude-code CHANGELOG (v2.1.80 added `rate_limits`); learn.chatgpt.com/docs/pricing;
openai/codex `codex-rs/protocol` (`RateLimitSnapshot`, `RateLimitWindow`, `RateLimitReachedType`).

## Claude snapshots (on by default)

Claude Code's payload is only live, so `tokenatlas statusline` records it by default (since 1.14; opt out with
`--no-record-quota` or `TOKENATLAS_NO_QUOTA=1`; `--record-quota` is accepted and does nothing): it appends
`{ts, session, five_hour: {used_percent, resets_at}, seven_day: {...}}` to `claude-quota.jsonl` next to the history, when a
value changed. Privacy: local only, nothing is sent; the file is 0600 (symlinks refused), holds only values Claude Code already
shows in its statusline plus the timestamp and session id, and is pruned to the last 60 days above 5 MB; a failure never
changes the status line. `tokenatlas doctor` reports whether recording is active. A reading is the counter
after its session's latest request, so the join places it at that request (within 5 minutes; an idle reading belongs to no turn)
and the shares then follow the same rules as for Codex. Coverage:

- Snapshots exist only while a Claude Code UI session is open and its statusline refreshes. `claude -p`, SDK runs and
  claude.ai chat are not recorded.
- The values are account-wide: use on claude.ai, Desktop, Cowork or another machine moves them, and is not in the logs.
- A window can be absent from the payload, and a value can be stale after a reset while the session is idle.
- A turn with no reading after its last request is an estimate, not observed.

## How well does list price predict the percentage?

This was measured on one owner's Codex history (January to October 2026: 379,546 deduplicated `token_count` events
and 76 weekly windows that moved at least 5 percentage points). List-price cost per 1% of a weekly window:

| Plan | Windows | Median USD per 1% | Interquartile range |
|---|---|---|---|
| pro | 28 | 9.38 | 5.75–14.70 |
| team | 34 | 0.57 | 0.36–0.92 |
| prolite | 5 | 0.35 | 0.08–2.46 |

Within one plan, the dollars per percent vary by a factor of 2–3 between weeks. Across 12-hour bins, list-price cost
explains under 10% of the variance in percentage movement (R² 0.09; output tokens 0.11). Usage that tokenatlas cannot
see (other machines, cloud tasks) and unpriced models (13% of events) account for part of this.

**Consequences:**

1. A turn's share is primarily **observed**: the percentage before the turn compared with the percentage at its last
   request, in the same window.
2. When concurrent turns share a step, tokenatlas spreads the movement it **observed in that window** over those turns
   by list-price cost. It never multiplies a turn's cost by a fixed dollars-per-percent rate. This is an
   **estimate**, labeled as one.
3. A user calibration (#93, `tokenatlas quota`) is shown with its spread, and is valid only for the plan and model mix it was
   made on. A reading is the used percentage the user copied from `/usage` or the Codex limits display, with the list price
   tokenatlas saw in that window; budget = cost seen / used, and several readings give the median and the min-max range. A
   turn without an observed or estimated share is then labeled **calibrated**: "≈ 4% of your weekly Claude limit (your
   calibration, 2026-10-03)", a turn's list price over the derived budget, in whole percent. It never replaces an observed or
   estimated share. Usage outside the logs (chat, other machines, cloud tasks) is in the percentage read but not in the cost seen, so the budget comes
   out too small and shares too large; a change of model mix drifts it. A turn with unpriced, incomplete or ambiguous requests shows a
   floor ("≥ 3%"), never "< 1%". Shared reports carry no calibrated shares.

## Wording rules

- Say "~3% of the weekly Codex limit (observed)" (Claude: "~6% of the 5-hour Claude limit"; sv "5-timmarsgränsen för Claude", "veckogränsen för Claude") or "≈ 3% (estimate)". Never show decimals for a whole-percent counter.
  Show "< 1%" when the counter did not move.
- A calibrated share is always "≈", says "your calibration" and its date, and is never called exact or observed.
- Name a window by its length (`window_minutes`, or Claude's `five_hour` / `seven_day`), never by the `primary` /
  `secondary` slot.
- "Share of what tokenatlas saw" is not "share of your limit". When a section adds up costs inside a window, say so.
- Do not state token or message caps per plan, peak-hour rules, or model weightings: none are published.
- A rate-limit rejection is a fact ("hit the 5-hour limit at 14:02, resets 19:00"). What filled the window is a
  ranking of what tokenatlas saw in it.
