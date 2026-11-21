// Retention (design section 9): delete events older than the tenant's `retention_days`, measured on `received_at` (not the
// event's own `ts`: a transcript restart resends old rows). Runs from the daily scheduled Worker; bounded per invocation so it
// stays inside the platform's subrequest limits (MAX_DB_CALLS across all tenants), and it simply continues the next day if there
// was more than one run could do.
//
// Each batch is one atomic unit: tombstones for the deleted keys, the `deleted_through_seq` marker that makes cursors behind
// it answer 410 instead of silently skipping, and the delete. A tombstone keeps a queued retry or a transcript reread from
// resurrecting an expired row with a fresh retention; tombstones themselves expire after TOMBSTONE_DAYS (the replay window).
import type { Deps } from "./ports.ts";

export const BATCH_ROWS = 500;
export const MAX_BATCHES_PER_TENANT = 20;
export const TOMBSTONE_DAYS = 30;
export const QUOTA_DAYS = 14;
export const MAX_DB_CALLS = 36; // one invocation's budget (a free-plan Worker allows about 50 subrequests); the next day continues
const MAX_RETENTION_DAYS = 3650;
const DAY_MS = 86_400_000;

export interface PurgeResult {
  tenants: number;
  eventsDeleted: number;
  tombstonesDeleted: number;
  moreToDo: boolean;
}

/** SQL for one retention batch: all three statements act on one explicit list of seq values. (admin/erase.ts pins its rows through tombstones instead.) */
export const BY_SEQS = "tenant_id = ? AND seq IN (SELECT value FROM json_each(?))";
export const TOMBSTONE_SQL = `INSERT INTO tombstones (tenant_id, dedupe_key, deleted_at) SELECT tenant_id, dedupe_key, ? FROM events WHERE ${BY_SEQS} ON CONFLICT (tenant_id, dedupe_key) DO NOTHING`;
export const MARK_SQL = "INSERT INTO tenant_state (tenant_id, generation, deleted_through_seq) VALUES (?, 1, ?) ON CONFLICT (tenant_id) DO UPDATE SET deleted_through_seq = MAX(deleted_through_seq, excluded.deleted_through_seq)";
export const DELETE_SQL = `DELETE FROM events WHERE ${BY_SEQS}`;

export async function purgeExpired(deps: Deps): Promise<PurgeResult> {
  const now = deps.clock.now();
  const result: PurgeResult = { tenants: 0, eventsDeleted: 0, tombstonesDeleted: 0, moreToDo: false };
  // Bookkeeping first (two calls), so a tenant with a large backlog can never starve it.
  const tombstoneCutoff = new Date(now.getTime() - TOMBSTONE_DAYS * DAY_MS).toISOString();
  const [gone] = await deps.db.batch([{ sql: "DELETE FROM tombstones WHERE rowid IN (SELECT rowid FROM tombstones WHERE deleted_at < ? LIMIT 5000)", params: [tombstoneCutoff] }]);
  result.tombstonesDeleted = gone;
  if (gone >= 5000) result.moreToDo = true;
  await deps.db.batch([{ sql: "DELETE FROM quota_day WHERE day < ?", params: [new Date(now.getTime() - QUOTA_DAYS * DAY_MS).toISOString().slice(0, 10)] }]);
  let calls = 3;
  const tenants = await deps.db.all<{ tenant_id: string; retention_days: number }>("SELECT tenant_id, retention_days FROM tenants");
  for (const t of tenants) {
    result.tenants++;
    const days = Number(t.retention_days);
    if (!Number.isInteger(days) || days < 1 || days > MAX_RETENTION_DAYS) continue; // a corrupt setting must never delete everything
    const cutoff = new Date(now.getTime() - days * DAY_MS).toISOString();
    for (let batch = 0; ; batch++) {
      if (batch >= MAX_BATCHES_PER_TENANT || calls + 2 > MAX_DB_CALLS) {
        result.moreToDo = true;
        break;
      }
      calls += 2;
      const seqs = (await deps.db.all<{ seq: number }>("SELECT seq FROM events WHERE tenant_id = ? AND received_at < ? ORDER BY seq LIMIT ?", [t.tenant_id, cutoff, BATCH_ROWS])).map((r) => Number(r.seq));
      if (!seqs.length) break;
      const list = JSON.stringify(seqs);
      const changes = await deps.db.batch([
        { sql: TOMBSTONE_SQL, params: [now.toISOString(), t.tenant_id, list] },
        { sql: MARK_SQL, params: [t.tenant_id, Math.max(...seqs)] },
        { sql: DELETE_SQL, params: [t.tenant_id, list] },
      ]);
      result.eventsDeleted += changes[2];
      if (seqs.length < BATCH_ROWS) break;
    }
  }
  deps.log?.("retention", { tenants: String(result.tenants), events_deleted: String(result.eventsDeleted), tombstones_deleted: String(result.tombstonesDeleted), more: String(result.moreToDo) });
  return result;
}
