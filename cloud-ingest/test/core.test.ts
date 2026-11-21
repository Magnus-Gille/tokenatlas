import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { canonicalJson, contentHash, dedupeKey, lineId } from "../src/dedupe.ts";
import { decodeCursor, encodeCursor } from "../src/cursor.ts";
import { mintToken, parseToken } from "../src/tokens.ts";
import { validateLine, validateWireRow } from "../src/validate.ts";
import { handle } from "../src/handler.ts";
import { openSqlite } from "../src/sqlite-adapter.ts";
import { readFileSync } from "node:fs";

const rows = readFileSync(new URL("../fixtures/digest-valid.ndjson", import.meta.url), "utf8").trim().split("\n");

describe("validation", () => {
  it("accepts every real-digest fixture row and strips session/_path from the stored row", () => {
    for (const line of rows) {
      const v = validateLine(line);
      assert.ok(v.ok, JSON.stringify(v));
      assert.equal((v.row as any).session, undefined);
      assert.equal((v.row as any)._path, undefined);
    }
  });
  it("accepts a subagent row without a usable sessionId and the workflow layout", () => {
    const base = JSON.parse(rows[1]);
    delete base.sessionId;
    for (const _path of ["", "subagents/agent-ab12.jsonl", "subagents/workflows/wf_x1/agent-ab12.jsonl"]) assert.ok(validateWireRow({ ...base, _path }).ok, _path);
  });
  it("rejects path traversal and overlong components", () => {
    const base = JSON.parse(rows[1]);
    for (const _path of ["../x", "subagents/../agent-a.jsonl", "subagents/agent-a.jsonl/x", "/subagents/agent-a.jsonl", `subagents/agent-${"a".repeat(65)}.jsonl`, "subagents/workflows/wf_a/b/agent-a.jsonl"]) {
      const v = validateWireRow({ ...base, _path });
      assert.ok(!v.ok && v.code === "bad_path", _path);
    }
  });
  it("session and ids use the narrow grammar (no dots, separators or overlength)", () => {
    const base = JSON.parse(rows[1]);
    for (const s of ["a.b", "a/b", "", "-a", "a".repeat(65), 5]) assert.ok(!validateWireRow({ ...base, session: s }).ok, String(s));
    assert.ok(!validateWireRow({ ...base, requestId: "a b" }).ok);
  });
  it("counters are integers in [0, 2^53-1], also nested", () => {
    const base = JSON.parse(rows[1]);
    const withUsage = (u: any) => validateWireRow({ ...base, message: { ...base.message, usage: { ...base.message.usage, ...u } } });
    assert.ok(withUsage({ output_tokens: 2 ** 53 - 1 }).ok);
    for (const bad of [-1, 1.5, "7", 2 ** 53, NaN, null]) assert.ok(!withUsage({ output_tokens: bad }).ok, String(bad));
    assert.ok(!withUsage({ cache_creation: { ephemeral_1h_input_tokens: -1 } }).ok);
    assert.ok(!withUsage({ iterations: [{ input_tokens: 1.5 }] }).ok);
    assert.ok(!withUsage({ iterations: Array.from({ length: 65 }, () => ({})) }).ok);
  });
  it("keeps iteration type and model (advisor warnings) and the speed label (pricing)", () => {
    const v = validateWireRow({ ...JSON.parse(rows[1]), message: { ...JSON.parse(rows[1]).message, usage: { speed: "fast", iterations: [{ output_tokens: 1, type: "advisor_message", model: "claude-fable-5-2" }] } } });
    assert.ok(v.ok);
    assert.deepEqual((v.row as any).message.usage, { speed: "fast", iterations: [{ output_tokens: 1, type: "advisor_message", model: "claude-fable-5-2" }] });
  });
  it("cwd and gitBranch: at most 256 printable characters", () => {
    const base = JSON.parse(rows[1]);
    assert.ok(validateWireRow({ ...base, cwd: "é".repeat(256) }).ok);
    assert.ok(!validateWireRow({ ...base, cwd: "x".repeat(257) }).ok);
    for (const c of ["\n", "\u0000", "‮", "​"]) assert.ok(!validateWireRow({ ...base, gitBranch: `a${c}b` }).ok, JSON.stringify(c));
  });
  it("unknown keys are stripped, not rejected", () => {
    const v = validateWireRow({ ...JSON.parse(rows[1]), hello: "x", message: { ...JSON.parse(rows[1]).message, content: "SECRET", usage: { ...JSON.parse(rows[1]).message.usage, server_tool_use: { a: 1 } } } });
    assert.ok(v.ok);
    assert.ok(!JSON.stringify(v.row).includes("SECRET"));
    assert.ok(!("hello" in v.row) && !("server_tool_use" in (v.row as any).message.usage));
  });
  it("prototype-pollution style keys are ignored", () => {
    const v = validateLine(`{"__proto__":{"x":1},"constructor":{},${rows[1].slice(1)}`);
    assert.ok(v.ok);
    assert.equal(({} as any).x, undefined);
  });
  it("a line over 64 KB is row_too_large before it is parsed; smaller padding is stripped", () => {
    assert.deepEqual(validateLine(JSON.stringify({ ...JSON.parse(rows[1]), pad: "x".repeat(70000) })), { ok: false, code: "row_too_large" });
    assert.ok(validateLine(JSON.stringify({ ...JSON.parse(rows[1]), pad: "x".repeat(20000) })).ok);
  });
  it("timestamps must be real dates in a sane range", () => {
    const base = JSON.parse(rows[1]);
    for (const t of ["9999-99-99T99:99:99Z", "1999-01-01T00:00:00Z", "2101-01-01T00:00:00Z", "2026-11-21"]) assert.ok(!validateWireRow({ ...base, timestamp: t }).ok, t);
    assert.ok(validateWireRow({ ...base, timestamp: "2026-11-21T10:00:00.123+02:00" }).ok);
  });
  it("assistant rows need usage, user rows need none", () => {
    const a = JSON.parse(rows[1]);
    assert.ok(!validateWireRow({ ...a, message: { role: "assistant" } }).ok);
    assert.ok(validateLine(rows[0]).ok);
  });
});

describe("dedupe key", () => {
  it("canonical JSON ignores key order", async () => {
    assert.equal(canonicalJson({ b: 1, a: { d: [2, { z: 1, y: 2 }], c: 1 } }), canonicalJson({ a: { c: 1, d: [2, { y: 2, z: 1 }] }, b: 1 }));
    assert.equal(await contentHash({ a: 1, b: 2 }), await contentHash({ b: 2, a: 1 }));
  });
  it("line id is the uuid, else a hash of the canonical row", async () => {
    assert.equal(await lineId({ uuid: "u1" }), "u1");
    const a = await lineId({ x: 1, y: 2 } as any);
    assert.match(a, /^[0-9a-f]{64}$/);
    assert.equal(a, await lineId({ y: 2, x: 1 } as any));
  });
  it("every tuple member changes the key and separators cannot collide", async () => {
    const k = await dedupeKey("t", "s", "sess", "", "u");
    assert.match(k, /^[0-9a-f]{64}$/);
    const others = await Promise.all([dedupeKey("t2", "s", "sess", "", "u"), dedupeKey("t", "s2", "sess", "", "u"), dedupeKey("t", "s", "sess2", "", "u"), dedupeKey("t", "s", "sess", "subagents/agent-a.jsonl", "u"), dedupeKey("t", "s", "sess", "", "u2"), dedupeKey("t", "s|sess", "", "", "u")]);
    assert.equal(new Set([k, ...others]).size, 7);
  });
});

describe("cursor", () => {
  it("round-trips and rejects garbage", () => {
    const c = { tenant: "t1", generation: 2, seq: 42 };
    assert.deepEqual(decodeCursor(encodeCursor(c)), c);
    for (const bad of ["", "!!", "e30", btoa("not json"), "A".repeat(600), encodeCursor({ tenant: "t", generation: 0, seq: 1 }), encodeCursor({ tenant: "t", generation: 1, seq: -1 })]) assert.equal(decodeCursor(bad), null, bad);
  });
});

describe("tokens", () => {
  it("mint and parse", async () => {
    const t = await mintToken("ingest");
    const p = parseToken(t.token)!;
    assert.equal(p.scope, "ingest");
    assert.equal(p.id, t.tokenId);
    assert.match(t.secretSha256, /^[0-9a-f]{64}$/);
    assert.equal(parseToken("tka_admin_aaaaaaaa_" + "a".repeat(43)), null);
    assert.equal(parseToken(t.token + "x"), null);
  });
});

describe("handler behaviours that need operator access", () => {
  const setup = async () => {
    const { db, raw } = openSqlite();
    let now = new Date("2026-11-21T22:00:00Z");
    const clock = { now: () => now };
    raw.prepare("INSERT INTO tenants VALUES ('t1','x','2026-11-21T00:00:00Z',90)").run();
    raw.prepare("INSERT INTO tenants VALUES ('t2','y','2026-11-21T00:00:00Z',90)").run();
    const mint = async (scope: "ingest" | "read", tenant = "t1", source = "env-a", cap = 5000) => {
      const t = await mintToken(scope);
      raw.prepare("INSERT INTO tokens (token_id, tenant_id, secret_sha256, scope, source, label, daily_row_cap, created_at) VALUES (?,?,?,?,?,'l',?,'2026-11-21T00:00:00Z')").run(t.tokenId, tenant, t.secretSha256, scope, source, cap);
      return t;
    };
    const call = (method: string, path: string, token: string, body = "", query: Record<string, string> = {}) =>
      handle({ db, clock }, { method, path, query, headers: { authorization: `Bearer ${token}`, "x-tokenatlas-client": "2" }, body });
    return { raw, mint, call, setNow: (d: string) => (now = new Date(d)) };
  };
  const body = (...uuids: string[]) => uuids.map((u) => JSON.stringify({ ...JSON.parse(rows[1]), uuid: u })).join("\n") + "\n";

  it("a revoked token is 401", async () => {
    const { raw, mint, call } = await setup();
    const t = await mint("ingest");
    assert.equal((await call("POST", "/v1/ingest", t.token, body("a"))).status, 200);
    raw.prepare("UPDATE tokens SET revoked_at = '2026-11-21T22:00:00Z'").run();
    assert.equal((await call("POST", "/v1/ingest", t.token, body("b"))).status, 401);
  });
  it("tenants are isolated: another tenant's reader sees nothing and its cursor is refused", async () => {
    const { mint, call } = await setup();
    const i1 = await mint("ingest"), r1 = await mint("read"), r2 = await mint("read", "t2", "env-b");
    await call("POST", "/v1/ingest", i1.token, body("a", "b"));
    const own = await call("GET", "/v1/events", r1.token);
    assert.equal(own.body.trim().split("\n").length, 2);
    assert.equal((await call("GET", "/v1/events", r2.token)).body, "");
    const foreign = await call("GET", "/v1/events", r2.token, "", { after: own.headers["x-next-cursor"] });
    assert.equal(foreign.status, 400);
  });
  it("the same row from another source (environment) is a different key, never a duplicate", async () => {
    const { mint, call } = await setup();
    const a = await mint("ingest", "t1", "env-a"), b = await mint("ingest", "t1", "env-b");
    assert.equal(JSON.parse((await call("POST", "/v1/ingest", a.token, body("x"))).body).results[0].status, "accepted");
    assert.equal(JSON.parse((await call("POST", "/v1/ingest", b.token, body("x"))).body).results[0].status, "accepted");
  });
  it("a tombstoned key is suppressed and cannot be resurrected", async () => {
    const { raw, mint, call } = await setup();
    const i = await mint("ingest"), r = await mint("read");
    await call("POST", "/v1/ingest", i.token, body("gone"));
    const ev = raw.prepare("SELECT dedupe_key FROM events").get() as any;
    raw.prepare("DELETE FROM events").run();
    raw.prepare("INSERT INTO tombstones VALUES ('t1', ?, '2026-11-21T22:00:00Z')").run(ev.dedupe_key);
    assert.equal(JSON.parse((await call("POST", "/v1/ingest", i.token, body("gone"))).body).results[0].status, "suppressed");
    assert.equal((await call("GET", "/v1/events", r.token)).body, "");
  });
  it("cursor expiry and reset are 410, never an empty page; no cursor restarts from zero", async () => {
    const { raw, mint, call } = await setup();
    const i = await mint("ingest"), r = await mint("read");
    await call("POST", "/v1/ingest", i.token, body("a", "b", "c"));
    const cursor = (await call("GET", "/v1/events", r.token, "", { limit: "1" })).headers["x-next-cursor"];
    raw.prepare("INSERT INTO tenant_state VALUES ('t1', 1, 2)").run();
    const expired = await call("GET", "/v1/events", r.token, "", { after: cursor });
    assert.equal(expired.status, 410);
    assert.equal(JSON.parse(expired.body).error.code, "cursor_expired");
    raw.prepare("UPDATE tenant_state SET generation = 2").run();
    const reset = await call("GET", "/v1/events", r.token, "", { after: cursor });
    assert.equal(reset.status, 410);
    assert.equal(JSON.parse(reset.body).error.code, "cursor_reset");
    assert.equal((await call("GET", "/v1/events", r.token)).body.trim().split("\n").length, 3);
  });
  it("quota is per token and per UTC day; retries are free", async () => {
    const { mint, call, setNow } = await setup();
    const i = await mint("ingest", "t1", "env-a", 3);
    const statuses = async (...u: string[]) => JSON.parse((await call("POST", "/v1/ingest", i.token, body(...u))).body).results.map((r: any) => r.status);
    assert.deepEqual(await statuses("a", "b", "c", "d"), ["accepted", "accepted", "accepted", "quota"]);
    assert.deepEqual(await statuses("a", "d"), ["duplicate", "quota"]);
    setNow("2026-11-22T00:00:01Z");
    assert.deepEqual(await statuses("d"), ["accepted"]);
  });
  it("duplicates inside one request collapse to one row", async () => {
    const { mint, call } = await setup();
    const i = await mint("ingest");
    const res = JSON.parse((await call("POST", "/v1/ingest", i.token, body("a", "a"))).body);
    assert.deepEqual(res.results.map((r: any) => r.status), ["accepted", "duplicate"]);
  });
  it("the byte cap pages a large pull and the next page continues without gaps", async () => {
    const { mint, call } = await setup();
    const i = await mint("ingest"), r = await mint("read");
    for (let n = 0; n < 12; n++) await call("POST", "/v1/ingest", i.token, Array.from({ length: 100 }, (_, k) => JSON.stringify({ ...JSON.parse(rows[1]), uuid: `u${n}-${k}`, cwd: "/" + "x".repeat(250) })).join("\n") + "\n");
    const seen: number[] = [];
    let after = "";
    for (let p = 0; p < 20; p++) {
      const res = await call("GET", "/v1/events", r.token, "", after ? { after } : {});
      if (!res.body) break;
      seen.push(...res.body.trim().split("\n").map((l) => JSON.parse(l).seq));
      after = res.headers["x-next-cursor"];
    }
    assert.equal(seen.length, 1200);
    assert.deepEqual(seen, [...seen].sort((a, b) => a - b));
    assert.equal(new Set(seen).size, 1200);
  });
  it("parallel requests cannot overshoot the daily cap (db calls yield like D1)", async () => {
    const { raw, mint, call } = await setup();
    const t = await mint("ingest", "t1", "env-a", 10);
    const bodies = Array.from({ length: 5 }, (_, n) => body(...Array.from({ length: 10 }, (_, k) => `p${n}-${k}`)));
    const res = await Promise.all(bodies.map((b) => call("POST", "/v1/ingest", t.token, b)));
    assert.ok(res.every((r) => r.status === 200));
    const stored = (raw.prepare("SELECT COUNT(*) c FROM events").get() as any).c;
    const charged = (raw.prepare("SELECT rows FROM quota_day").get() as any).rows;
    assert.equal(stored, 10);
    assert.equal(charged, 10);
  });
  it("a 200-row request costs a handful of statements, not one per row", async () => {
    const { db, raw } = openSqlite();
    raw.prepare("INSERT INTO tenants VALUES ('t1','x','2026-11-21T00:00:00Z',90)").run();
    const t = await mintToken("ingest");
    raw.prepare("INSERT INTO tokens (token_id, tenant_id, secret_sha256, scope, source, label, created_at) VALUES (?, 't1', ?, 'ingest', 's', 'l', 'x')").run(t.tokenId, t.secretSha256);
    let calls = 0;
    const counting = { all: (s: string, p?: any) => (calls++, db.all(s, p)), batch: (s: any) => (calls++, db.batch(s)) };
    const lines = Array.from({ length: 200 }, (_, k) => JSON.stringify({ ...JSON.parse(rows[1]), uuid: `m${k}` })).join("\n") + "\n";
    const res = await handle({ db: counting as any, clock: { now: () => new Date("2026-11-21T22:00:00Z") } }, { method: "POST", path: "/v1/ingest", query: {}, headers: { authorization: `Bearer ${t.token}`, "x-tokenatlas-client": "2" }, body: lines });
    assert.equal(JSON.parse(res.body).counts.accepted, 200);
    assert.ok(calls <= 20, `db calls: ${calls}`);
  });
  it("internal failures never leak details", async () => {
    const { mint } = await setup();
    const t = await mint("ingest");
    const res = await handle({ db: { all: async () => { throw new Error("SECRET sql detail"); }, batch: async () => [] }, clock: { now: () => new Date() } }, { method: "POST", path: "/v1/ingest", query: {}, headers: { authorization: `Bearer ${t.token}`, "x-tokenatlas-client": "2" }, body: body("a") });
    assert.equal(res.status, 500);
    assert.ok(!res.body.includes("SECRET"));
  });
});
