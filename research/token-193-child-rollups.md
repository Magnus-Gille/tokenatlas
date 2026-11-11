# TOKEN-193: child-session rollups and long-lived turns

Status: research note, 2026-11-11

## Confirmed mechanism

`tokenatlas.prompts.assign_prompts` binds a subagent thread to the latest observed
parent-turn start at or before the thread's first observed row. It follows at most
eight parent-session links; a cycle prevents binding. `top_prompts` then aggregates
bound child observations into that parent prompt. A child with no resolvable parent
turn can remain an `own_subagent` prompt; an orphan without its own turn is
unattributed. The aggregate retains request and child-request counts, first/last
timestamps, duration, and token/cost totals. Its subagent count is a proxy based on
distinct session/agent pairs and may undercount source files. There is currently no
warning that a prompt includes a multi-day child span.

The collectors derive turn candidates from logged structure. Claude uses genuine
user rows after excluding metadata, injected-only rows, and tool-result-only rows.
Codex uses explicit turn identifiers when present and otherwise derives a turn from
user-message events. These predicates identify a user-like event in a transcript;
they do not prove that a human typed it or that the event represents an independent
top-level turn.

## Local audit evidence

A local read-only investigation found that one long aggregate draws from many
sidechain-marked transcript files even though the stored session/agent pairs report
only a small number of child identities. Each inspected file contained a single
user-like start row. Some files spanned multiple calendar dates. A user-like row
alone does not establish human authorship or an independent turn, and a multi-day
span does not prove continuous work. Source files, events, and stored agent identity
are different dimensions; their counts must not be treated as interchangeable.

This note deliberately excludes raw paths, identifiers, prompts, transcript text,
and user-specific usage figures.

## Interpretation options

1. Improve child identity first. Preserve source provenance and parent links in a
   bounded, privacy-safe form, then distinguish source files, sessions, agents, and
   user-like events. Do not infer identity from a basename alone.
2. Add transparent long-span labeling. Keep the accounting rollup, but display that
   it includes child work over a long time range, including child request count and
   span. This is the safest product change while identity evidence is incomplete.
3. Split child turns only on trustworthy evidence. A child event should become an
   independent turn only when the format explicitly marks a user-originated turn (or
   an equivalent reliable boundary). A generic user-like row, agent file, or elapsed
   time is insufficient.

## Tests needed

- Synthetic parent plus child sessions with explicit parent links and multiple child
  user-like events; assert current rollup and any proposed split separately.
- A child transcript containing injected, tool-result, and user-like rows; verify the
  classifier does not equate user-like with human-authored.
- Nested children, orphan children, copied/resumed sessions, and repeated source
  files; assert no double counting and stable parent assignment.
- A multi-day synthetic span; verify the report labels duration and child share
  without changing totals.
- Privacy tests for shared/redacted reports so source metadata cannot expose paths,
  prompts, or stable private IDs.

The existing `test_top_prompts` suite covers parent binding, nested children,
orphans, cross-turn attribution, and filtering. It does not prove the classification
of a real child session's user input, so that question remains open pending a
redacted transcript or an upstream format contract.
