# Agent CLI usability evaluation

## Reproduce the isolated fixture and smoke baseline

```sh
python3 scripts/agent_eval.py <output-directory> --baseline
```

Use a **fresh disposable directory** outside deliverables (for this repository,
`build/agent-checks/` is ignored). No network, browser, model, installer, or
scheduler is invoked. The fixture uses `scripts/demo.py`'s seeded synthetic
HOME, quota data and orchestration outcomes, imported through the real CLI.
It refuses to overwrite an existing HOME/state tree. Omitting `--baseline`
prepares the fixture and tasks only.

Outputs (all runtime artifacts, **do not commit**):

- `home/`, `state/`: isolated logs, history and existing report.
- `environment.json`: complete synthetic subprocess environment. Use it instead
  of inheriting provider path overrides from a real HOME.
- `tasks.json`: ten natural-language tasks with a fixed evaluation clock
  (`2026-09-30T12:00:00Z`), UTC timezone, and snapshot destination.
- `baseline.json`: known-command smoke score, explicitly marked **not a model eval**.
- `answers.json`, `transcript.json`: baseline answer references and command receipts.
- `tickets.json`: one ticket record per failed smoke task, with task ID and evidence.

Tasks cover costliest turn yesterday, attributed project costs last week, Codex
diagnosis, thread listing and rating, existing report location, day comparison,
session totals, snapshot/import, overhead, and range counts. The project-cost
answer explicitly excludes unattributed usage; coverage is never called complete.

## Independent small-model run (still required for acceptance)

This script deliberately does **not** spawn an agent or call a model API. In a
suitable evaluation environment:

1. Record model identifier/revision, provider, sampling parameters (temperature 0
   if supported), token limit, actual token usage/cost, TokenAtlas commit and seed.
   Choose a small, inexpensive coding-capable model; enforce a fixed budget.
2. Prepare a fresh fixture. Expose a terminal tool running with `environment.json`
   and the checkout's CLI available as `tokenatlas` (or a launcher for
   `python3 -m tokenatlas`). No network/browser and no personal HOME.
3. Give the model only the instruction **“use the tokenatlas CLI”**, plus one task
   and its clock/timezone/destination context from `tasks.json`. Do not give it
   baseline commands, answers, source, this protocol, or a prewritten skill.
   It may discover `tokenatlas --help` and command help itself. Start each task
   with fresh conversation context; reset the fixture for mutation tasks.
4. Capture every tool call, stdout, stderr, exit status, final answer and latency.
   Help/discovery calls are allowed. Record retries caused by a rejected option,
   unparseable output, misleading help, missing fields, unexpected prompts or
   browser side effects separately from reasoning mistakes. Do not silently retry.
5. Grade the facts against a separate baseline fixture with the same seed. Compare
   identities/counts exactly and numeric costs within floating-point tolerance;
   ignore timestamps, report age and machine/path prefixes. Check the outcome
   file actually contains the requested rating, snapshot exists, report wasn't
   rebuilt for the location task, and no browser was launched. Partial/unpriced
   costs must be qualified. A correct answer with a CLI-caused retry is not a pass.
6. Target **10/10, zero CLI-caused retries**. For each failure record a ticket:
   task ID, model/revision, prompt, failing command, output/exit, expected behavior,
   CLI vs model attribution, and minimal reproduction. Keep transcripts and
   generated tickets in the output directory; summarize actionable issues in docs.

This protocol requires a model-capable conductor; it has **not** been run in the
network-blocked worker environment. No 10/10 model result is claimed.

## Worker verification and outstanding tickets

- Deterministic smoke: **10/10**, no smoke failure tickets. Also covered by
  `python3 -m unittest -v test_agent_eval`.
- Contract coverage: `test_agent_cli` exercises every command's help/examples,
  versioned reporting JSON, error envelopes/codes, date inputs, missing entities,
  outcome recording, collection receipts, and browser suppression. Upgrade
  discovery is mocked: no network or installation is performed.
- **EVAL-1 (open acceptance):** run the independent small-model protocol above;
  report the model/cost, per-task results and any CLI-caused retries. The smoke
  runner knows the commands and cannot establish discoverability or model success.
- Full unittest discovery is run separately in an isolated Docker environment,
  with browser suppression unset so default-behavior tests remain meaningful.
  See the PR verification record for the exact source revision and result.
- The Harness²/Pi pilot uses the owner's requested `gpt-6.1-sol` at `high`
  through an OpenAI subscription. This is a deliberate model-size deviation
  from the small-model protocol above. Its implementation attempts and this
  independent usability protocol are different measurements.
- The pilot is currently incomplete: OpenAI returned a Subscription Sharing
  usage-limit error. No API-key fallback was used. A 10/10 model result and a
  Harness²-versus-ordinary-Pi comparison are not claimed.

## Verification commands

```sh
python3 -m unittest discover -v
python3 scripts/agent_eval.py <output-directory> --baseline
git diff --check
```

Run these in an isolated environment with a synthetic HOME and no production
credentials. Browser behavior tests replace browser launching with mocks; leave
`TOKENATLAS_NO_BROWSER` unset for the full suite. Headless contract tests set it
explicitly where required. Installation and remote-sync tests use test doubles;
no production installation or remote host is needed.
