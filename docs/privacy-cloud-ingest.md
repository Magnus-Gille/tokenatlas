# Privacy notice: TokenAtlas cloud ingest (draft, TOKEN-211)

Draft text for the cloud usage service described in `docs/architecture/cloud-ingest.md`. It is not legal advice: when the
service is offered to anyone besides its owner, have it reviewed against the owner's actual role (see "Roles").

## What is collected

Cloud coding sessions run in short-lived containers, so their token usage would otherwise be lost. A hook in the container
sends one small record per model request ("digest row") to the service:

- token counters (input, output, cache read, cache write, the cache time-to-live split, per advisor iteration),
- the model name, service tier and speed label, stop reason,
- timestamps, session, request, message, parent-session, agent and turn ids, the harness version, entrypoint, attribution agent and effort,
- thinking-token counts and the inference geography label,
- the working directory (`cwd`) and git branch name,
- stored by the service on receipt: the source label of the sending environment, the id of the token used and the time received.

Never sent: prompts, assistant text, tool inputs or outputs, file contents. The client drops everything outside a fixed allowlist
and the service validates it again.

**`cwd` and the branch name are personal data.** They can name a client, a project or a person. Treat them as such: they are kept
in the stored row and in the local copy. A client option to drop or hash them is planned (design section 9); until then, do not
use the service for sessions whose directory or branch names must stay secret.

## Why, and on what basis

To show the person who runs the sessions what they used and what it cost, across machines and containers. For the owner's own
use this is the owner's own data. Offered to others, the lawful basis and the role split below must be settled first.

## Roles

- **Owner's own use:** the owner is the controller of their own usage data. If the sessions contain work for an employer or
  client, the owner is a controller (or the employer is) and Cloudflare is the processor; the "purely personal" household
  exemption only applies to purely personal sessions.
- **As a service for others:** TokenAtlas would be a processor for its customers and needs a data processing agreement, a
  sub-processor list, a record of processing and this notice. Per-person usage in an employer context is employee monitoring:
  the customer must have a basis for it and inform their staff.

## Where it is processed

- Storage: a Cloudflare D1 database. Its location is fixed when the database is created (an EU jurisdiction restriction is
  preferred over a location hint; verify it applies to the account before relying on it).
- **Processing is not EU-only.** A Cloudflare Worker runs in the data centre nearest the caller, and cloud-session containers are
  likely hosted in the US, so data transits and is processed outside the EU even when it is stored in the EU.
- Sub-processor: Cloudflare, Inc. (its data processing terms apply). No other sub-processor.

## How long it is kept

- **Retention:** 90 days by default, per tenant (`retention_days`), measured from when the service received the row, not from the
  event time (a restarted transcript resends old rows). A daily job deletes older rows, in bounded batches: with a very large
  backlog it takes several days to catch up, so rows may outlive the period by that long.
- **Local copies are not deleted by that job.** `tokenatlas sync-cloud` copies the rows into the owner's own history database,
  which is the intended long-term store.
- A deleted row leaves a **tombstone** (a hash of its identity, no content) for 30 days so a queued retry cannot bring it back.
  After those 30 days a very old queued retry or a transcript reread can enter the service again as a new row, with a fresh
  retention period.

## Access, export, erasure

- **Access and export:** the pull API (`GET /v1/events`) returns every stored row for the tenant; that is also the export.
- **Erasure:** the operator erases by session, by token or by date (`cloud-ingest/admin/erase.ts`). Erased rows are tombstoned
  and removed; pulling clients are told to re-pull from the start.
- **Limits of erasure**, to be stated plainly to anyone who asks:
  - D1 Time Travel keeps the database restorable for a limited period (check the current window), so deleted data can persist
    in the provider's restore points until they expire.
  - Local copies and snapshots on the owner's machines (Mac, Pi) are not touched by a cloud erasure and must be deleted there.
  - Backups the owner made of exports are outside the service.

## Security

Tokens are random, stored only as hashes, scoped (`ingest` or `read`) and revocable; the plaintext is shown once at creation.
Logs record counts and event names, never bodies or tokens. Transport is HTTPS only; the client refuses plain HTTP (except
loopback) and redirects.

## Contact

The service owner (name and address to be filled in before the notice is shown to anyone else).
