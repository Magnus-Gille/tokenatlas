# TokenAtlas cloud ingest: portable core (slice 1)

Implements slice 1 of `docs/architecture/cloud-ingest.md` (TOKEN-207): validation, dedupe key, cursor, token format and the
request handler (`POST /v1/ingest`, `GET /v1/events`, `GET /v1/health`) behind two ports (`Db`, `Clock`) in `src/ports.ts`.
No dependencies; Node >= 22.18 runs the TypeScript directly. The Cloudflare entrypoint, D1 adapter and operator CLI are
slice 2 (TOKEN-208).

```
cd cloud-ingest
npm test                                              # unit tests + the contract suite against the reference implementation
node contract/run.ts --local                          # same suite, standalone
node contract/run.ts --url https://ingest.example --ingest-token tka_ingest_... --read-token tka_read_...
```

The contract suite (`contract/suite.ts`) is black-box HTTP and uses a fresh session id per run, so it can be pointed at any
deployment (Cloudflare now, AWS later) with a staging tenant's tokens. Fixtures in `fixtures/` are produced by the
production hook's own `digest_row()` (`python fixtures/generate.py`); CI fails if they drift.

Deviations from the design text, all in `migrations/sqlite/0001_init.sql`: `tokens.source` (the namespace bound into dedupe
keys) and `tokens.daily_row_cap`, `events.content_hash` (for `duplicate` vs `conflict`).

Behaviour worth knowing:
- Inserts are one statement per ~64 KB chunk (`json_each`), with the daily cap enforced in the statement and the counter
  charged in the same atomic batch. Not verified against real D1 limits yet (slice 2).
- Pull reads in 64-row slices and stops at 1 MB, so a page never loads much more than the cap.
- Unknown keys are stripped before the 16 KB row cap applies; only a raw line over 64 KB is `row_too_large`.

Open items for later slices: the hook (slice 3) must send `session`, keep iteration `type`/`model`, and truncate rather than
send `cwd` over 256 characters, more than 64 iterations or a model such as `<synthetic>` (the validator rejects those and
the client quarantines them); the host adapter (slice 2) must cap the body while reading it and pass a `log` sink; the
Postgres port needs a lock for tombstone-vs-insert and the `json_each` dialect change.
