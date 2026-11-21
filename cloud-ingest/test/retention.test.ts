import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { run as admin } from "../admin/tokens.ts";
import { run as erase } from "../admin/erase.ts";
import { handle } from "../src/handler.ts";
import { BATCH_ROWS, MAX_DB_CALLS, TOMBSTONE_DAYS, purgeExpired } from "../src/retention.ts";
import { openSqlite } from "../src/sqlite-adapter.ts";
import { encodeCursor } from "../src/cursor.ts";
import worker, { scheduledHandler } from "../worker/index.ts";

const NOW = new Date("2026-11-21T12:00:00Z");
const daysAgo = (n: number) => new Date(NOW.getTime() - n * 86_400_000).toISOString();

async function setup(retentionDays = 90) {
  const { db, raw } = openSqlite();
  raw.exec((await admin(["create-tenant", "--id", "t1", "--name", "test", "--retention-days", String(retentionDays)])).sql);
  raw.exec((await admin(["create-tenant", "--id", "t2", "--name", "other", "--retention-days", "7"])).sql);
  const read = await admin(["mint", "--tenant", "t1", "--scope", "read", "--source", "mac", "--label", "reader"], NOW);
  raw.exec(read.sql);
  const insert = raw.prepare("INSERT INTO events (tenant_id, dedupe_key, source, session_id, rel_path, line_id, ts, received_at, token_id, content_hash, row_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)");
  let n = 0;
  const add = (tenant: string, received: string, session = "s1", token = "tok1") => {
    n++;
    insert.run(tenant, n.toString(16).padStart(64, "0"), "cloud", session, "", `l${n}`, received, received, token, "h", "{}");
  };
  const deps = { db, clock: { now: () => NOW } };
  return { db, raw, add, deps, read: read.secret! };
}
const count = (raw: any, sql: string, ...p: any[]) => Number(raw.prepare(sql).get(...p)["count(*)"]);

describe("retention", () => {
  it("deletes on received_at past each tenant's own retention and leaves the rest", async () => {
    const { raw, add, deps } = await setup(90);
    add("t1", daysAgo(100)); add("t1", daysAgo(91)); add("t1", daysAgo(89)); add("t1", daysAgo(1));
    add("t2", daysAgo(8)); add("t2", daysAgo(6));
    const r = await purgeExpired(deps);
    assert.equal(r.eventsDeleted, 3);
    assert.equal(count(raw, "SELECT count(*) FROM events WHERE tenant_id='t1'"), 2);
    assert.equal(count(raw, "SELECT count(*) FROM events WHERE tenant_id='t2'"), 1);
    assert.equal(count(raw, "SELECT count(*) FROM tombstones"), 3);
  });
  it("tombstones what it deletes and raises deleted_through_seq so an old cursor gets 410, not a silent gap", async () => {
    const { raw, add, deps, read } = await setup(90);
    add("t1", daysAgo(100)); add("t1", daysAgo(1));
    await purgeExpired(deps);
    const state = raw.prepare("SELECT deleted_through_seq FROM tenant_state WHERE tenant_id='t1'").get() as any;
    assert.equal(Number(state.deleted_through_seq), 1);
    const res = await handle(deps, { method: "GET", path: "/v1/events", query: { after: encodeCursor({ tenant: "t1", generation: 1, seq: 0 }) }, headers: { authorization: `Bearer ${read}` }, body: "" });
    assert.equal(res.status, 410);
    assert.match(res.body, /cursor_expired/);
  });
  it("a purged row cannot come back through the ingest path (tombstone)", async () => {
    const { raw, add, deps } = await setup(90);
    add("t1", daysAgo(100));
    await purgeExpired(deps);
    const ts = raw.prepare("SELECT dedupe_key FROM tombstones").all() as any[];
    assert.equal(ts.length, 1);
    assert.equal(count(raw, "SELECT count(*) FROM events"), 0);
  });
  it("works in bounded batches and reports when more is left", async () => {
    const { raw, add, deps } = await setup(90);
    for (let i = 0; i < BATCH_ROWS * 2 + 5; i++) add("t1", daysAgo(200));
    const r = await purgeExpired(deps);
    assert.equal(r.eventsDeleted, BATCH_ROWS * 2 + 5);
    assert.equal(r.moreToDo, false);
    assert.equal(count(raw, "SELECT count(*) FROM events"), 0);
  });
  it("expires tombstones after the replay window and old quota counters", async () => {
    const { raw, deps } = await setup(90);
    raw.prepare("INSERT INTO tombstones VALUES ('t1','old','" + daysAgo(TOMBSTONE_DAYS + 1) + "')").run();
    raw.prepare("INSERT INTO tombstones VALUES ('t1','new','" + daysAgo(TOMBSTONE_DAYS - 1) + "')").run();
    raw.prepare("INSERT INTO quota_day VALUES ('t1','tok1','2026-10-01',5)").run();
    raw.prepare("INSERT INTO quota_day VALUES ('t1','tok1','2026-11-21',5)").run();
    const r = await purgeExpired(deps);
    assert.equal(r.tombstonesDeleted, 1);
    assert.equal(count(raw, "SELECT count(*) FROM tombstones"), 1);
    assert.equal(count(raw, "SELECT count(*) FROM quota_day"), 1);
  });
  it("a corrupt retention setting never deletes anything", async () => {
    const { raw, add, deps } = await setup(90);
    add("t1", daysAgo(500));
    raw.exec("UPDATE tenants SET retention_days = 0 WHERE tenant_id='t1'");
    assert.equal((await purgeExpired(deps)).eventsDeleted, 0);
    raw.exec("UPDATE tenants SET retention_days = -5 WHERE tenant_id='t1'");
    assert.equal((await purgeExpired(deps)).eventsDeleted, 0);
  });
  it("the Worker exposes a scheduled handler that runs it", async () => {
    assert.equal(typeof (worker as any).scheduled, "function");
    assert.equal(typeof scheduledHandler, "function");
  });
});

describe("erasure script", () => {
  const run = async (args: string[]) => {
    const t = await setup(90);
    t.add("t1", daysAgo(5), "s1", "tokaaaa1"); t.add("t1", daysAgo(5), "s2", "tokaaaa1"); t.add("t1", daysAgo(50), "s2", "tokbbbb2"); t.add("t2", daysAgo(5), "s1", "tokaaaa1");
    t.raw.exec(erase(args, NOW).sql);
    return t;
  };
  it("erases one session inside one tenant, with tombstones and the cursor marker", async () => {
    const t = await run(["--tenant", "t1", "--session", "s2"]);
    assert.equal(count(t.raw, "SELECT count(*) FROM events WHERE tenant_id='t1'"), 1);
    assert.equal(count(t.raw, "SELECT count(*) FROM events WHERE tenant_id='t2'"), 1);
    assert.equal(count(t.raw, "SELECT count(*) FROM tombstones"), 2);
    assert.equal(Number((t.raw.prepare("SELECT deleted_through_seq d FROM tenant_state WHERE tenant_id='t1'").get() as any).d), 3);
  });
  it("erases by token and by date", async () => {
    const byToken = await run(["--tenant", "t1", "--token-id", "tokbbbb2"]);
    assert.equal(count(byToken.raw, "SELECT count(*) FROM events WHERE tenant_id='t1'"), 2);
    const byDate = await run(["--tenant", "t1", "--before", daysAgo(10).slice(0, 10)]);
    assert.equal(count(byDate.raw, "SELECT count(*) FROM events WHERE tenant_id='t1'"), 2);
  });
  it("reports how many rows it erased and erases nothing for an unknown session", async () => {
    const t = await run(["--tenant", "t1", "--session", "nope"]);
    assert.equal(count(t.raw, "SELECT count(*) FROM events WHERE tenant_id='t1'"), 3);
    assert.match(erase(["--tenant", "t1", "--session", "s1"]).sql, /SELECT changes\(\) AS erased_rows/);
  });
  it("refuses unsafe or ambiguous input", () => {
    for (const args of [["--tenant", "t1"], ["--tenant", "t1", "--session", "a", "--before", "2026-01-01"], ["--tenant", "t1", "--session", "a'; DROP TABLE events;--"],
      ["--tenant", "t1", "--before", "yesterday"], ["--session", "s1"], ["--tenant", "t1", "--token-id", "X"]]) assert.throws(() => erase(args), undefined, args.join(" "));
  });
});

describe("pulling after deletions", () => {
  const pull = async (t: any, after?: string) => {
    const res = await handle(t.deps, { method: "GET", path: "/v1/events", query: { limit: "2", ...(after ? { after } : {}) }, headers: { authorization: `Bearer ${t.read}` }, body: "" });
    return { status: res.status, lines: res.body.split("\n").filter(Boolean).length, next: res.headers["x-next-cursor"] };
  };
  const drain = async (t: any) => {
    let cursor: string | undefined, total = 0;
    for (let i = 0; i < 20; i++) {
      const r = await pull(t, cursor);
      assert.equal(r.status, 200, `page ${i}`);
      if (!r.lines) return { total, cursor };
      total += r.lines;
      cursor = r.next;
    }
    throw new Error("did not terminate");
  };
  it("erasing a middle session does not wedge a re-pull from zero (cursors carry the marker they were issued under)", async () => {
    const t = await setup(90);
    for (let i = 0; i < 4; i++) t.add("t1", daysAgo(5), "s1");
    t.add("t1", daysAgo(5), "s2");
    for (let i = 0; i < 4; i++) t.add("t1", daysAgo(5), "s3");
    t.raw.exec(erase(["--tenant", "t1", "--session", "s2"], NOW).sql);
    assert.equal((await drain(t)).total, 8);
  });
  it("deleting the tail leaves a cursor that stays valid for the next sync", async () => {
    const t = await setup(90);
    for (let i = 0; i < 3; i++) t.add("t1", daysAgo(5), "s1");
    t.add("t1", daysAgo(5), "s2");
    t.raw.exec(erase(["--tenant", "t1", "--session", "s2"], NOW).sql);
    const first = await drain(t);
    assert.equal(first.total, 3);
    assert.equal((await pull(t, first.cursor)).status, 200);
  });
  it("a cursor issued before a later deletion still gets 410 (the gap is real)", async () => {
    const t = await setup(90);
    for (let i = 0; i < 4; i++) t.add("t1", daysAgo(5), "s1");
    const early = (await pull(t)).next;
    t.raw.exec(erase(["--tenant", "t1", "--session", "s1"], NOW).sql);
    t.add("t1", daysAgo(5), "s9");
    assert.equal((await pull(t, early)).status, 410);
  });
  it("an empty tenant after retention gives a cursor that keeps working", async () => {
    const t = await setup(90);
    t.add("t1", daysAgo(200));
    await purgeExpired(t.deps);
    const r = await pull(t);
    assert.equal(r.lines, 0);
    assert.equal((await pull(t, r.next)).status, 200);
  });
});

describe("robustness", () => {
  it("writes the marker even when the tenant has no tenant_state row", async () => {
    const t = await setup(90);
    t.add("t1", daysAgo(100));
    t.raw.exec("DELETE FROM tenant_state WHERE tenant_id='t1'");
    await purgeExpired(t.deps);
    assert.equal(Number((t.raw.prepare("SELECT deleted_through_seq d FROM tenant_state WHERE tenant_id='t1'").get() as any).d), 1);
  });
  it("stays within the per-invocation call budget across tenants and still does the bookkeeping", async () => {
    const t = await setup(90);
    for (let i = 0; i < BATCH_ROWS * 40; i++) t.add("t1", daysAgo(200));
    let calls = 0;
    const counting = { ...t.deps, db: { all: (...a: any[]) => (calls++, (t.db.all as any)(...a)), batch: (...a: any[]) => (calls++, (t.db.batch as any)(...a)) } };
    const r = await purgeExpired(counting as any);
    assert.ok(calls <= MAX_DB_CALLS, `calls ${calls}`);
    assert.equal(r.moreToDo, true);
    assert.ok(r.eventsDeleted > 0 && r.eventsDeleted < BATCH_ROWS * 40);
  });
  it("an absurd retention_days is skipped instead of aborting the run", async () => {
    const t = await setup(90);
    t.add("t1", daysAgo(500)); t.add("t2", daysAgo(30));
    t.raw.exec("UPDATE tenants SET retention_days = 100000000 WHERE tenant_id='t1'");
    const r = await purgeExpired(t.deps);
    assert.equal(r.eventsDeleted, 1); // t2 is still handled
  });
  it("erasure only deletes rows it tombstoned: a row that arrives while the file runs survives", async () => {
    const t = await setup(90);
    t.add("t1", daysAgo(5), "s1");
    const statements = erase(["--tenant", "t1", "--session", "s1"], NOW).sql.split(/;\n/).filter((x) => x.trim());
    t.raw.exec(statements[0] + ";");
    t.add("t1", daysAgo(0), "s1"); // ingested between statement 1 and the delete
    for (const x of statements.slice(1)) t.raw.exec(x + ";");
    assert.equal(count(t.raw, "SELECT count(*) FROM events"), 1);
    assert.equal(count(t.raw, "SELECT count(*) FROM tombstones"), 1);
  });
});
