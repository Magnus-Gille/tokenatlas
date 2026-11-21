// Operator erasure (design section 9): prints SQL for `wrangler d1 execute`, like admin/tokens.ts. Erase by session, by token or by
// date, always inside one tenant. Rows are tombstoned first (so a queued retry or a transcript reread cannot bring them back),
// `deleted_through_seq` is raised (cursors behind it answer 410 and re-pull), then the rows are deleted. Export is the pull API
// itself (GET /v1/events). Erasure is not absolute: D1 Time Travel keeps deleted data restorable for a limited window, and
// local copies and snapshots on the Mac/Pi are untouched (privacy notice, docs/privacy-cloud-ingest.md).
//
//   node admin/erase.ts --tenant <id> --session <session_id>            > erase.sql
//   node admin/erase.ts --tenant <id> --token-id <id>                   > erase.sql
//   node admin/erase.ts --tenant <id> --before 2026-10-01               > erase.sql   (on received_at)
//   wrangler d1 execute <db> --remote --file erase.sql      # the last statement reports how many rows were erased
import { parseArgs } from "node:util";

const ID = /^[A-Za-z0-9_-]{1,64}$/;
const q = (s: string) => `'${s.replace(/'/g, "''")}'`;

export function run(argv: string[], now = new Date()): { sql: string } {
  const { values } = parseArgs({ args: argv, options: { tenant: { type: "string" }, session: { type: "string" }, "token-id": { type: "string" }, before: { type: "string" } } });
  const tenant = values.tenant;
  if (typeof tenant !== "string" || !ID.test(tenant)) throw new Error("--tenant is required (letters, digits, _ and -)");
  const chosen = [values.session, values["token-id"], values.before].filter((v) => v !== undefined);
  if (chosen.length !== 1) throw new Error("give exactly one of --session, --token-id or --before");
  let where: string;
  if (values.session !== undefined) {
    if (!ID.test(values.session)) throw new Error("--session has characters outside the session id grammar");
    where = `session_id = ${q(values.session)}`;
  } else if (values["token-id"] !== undefined) {
    if (!/^[a-z0-9]{8,32}$/.test(values["token-id"])) throw new Error("--token-id must be 8-32 lower-case letters or digits");
    where = `token_id = ${q(values["token-id"])}`;
  } else {
    const d = values.before as string;
    if (!/^\d{4}-\d\d-\d\d$/.test(d) || Number.isNaN(Date.parse(`${d}T00:00:00Z`))) throw new Error("--before must be a date like 2026-10-01");
    where = `received_at < ${q(`${d}T00:00:00.000Z`)}`;
  }
  const t = q(tenant);
  const stamp = q(now.toISOString());
  const scope = `tenant_id = ${t} AND ${where}`;
  // The rows are pinned by identity: statement 1 tombstones them under this run's timestamp (refreshing older tombstones), and
  // the later statements only touch events whose key carries that timestamp, so a row ingested while the file runs, even for the
  // same session or token, is never deleted without a tombstone.
  const mine = `dedupe_key IN (SELECT dedupe_key FROM tombstones WHERE tenant_id = ${t} AND deleted_at = ${stamp})`;
  const sql = [
    `INSERT INTO tombstones (tenant_id, dedupe_key, deleted_at) SELECT tenant_id, dedupe_key, ${stamp} FROM events WHERE ${scope} ON CONFLICT (tenant_id, dedupe_key) DO UPDATE SET deleted_at = excluded.deleted_at;`,
    `INSERT INTO tenant_state (tenant_id, generation, deleted_through_seq) SELECT ${t}, 1, COALESCE(MAX(seq), 0) FROM events WHERE tenant_id = ${t} AND ${mine} ON CONFLICT (tenant_id) DO UPDATE SET deleted_through_seq = MAX(deleted_through_seq, excluded.deleted_through_seq);`,
    `DELETE FROM events WHERE tenant_id = ${t} AND ${mine};`,
    `SELECT changes() AS erased_rows;`,
    "",
  ].join("\n");
  return { sql };
}

if (import.meta.main) {
  try {
    process.stdout.write(run(process.argv.slice(2)).sql);
  } catch (e) {
    process.stderr.write(`${(e as Error).message}\n`);
    process.exit(2);
  }
}
