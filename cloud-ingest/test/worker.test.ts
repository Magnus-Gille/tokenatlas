// The Worker entrypoint and the D1 adapter, against a fake D1 (node:sqlite), plus the black-box contract suite through them.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { after, describe, it } from "node:test";
import { run as admin } from "../admin/tokens.ts";
import { runContract } from "../contract/suite.ts";
import { type D1Binding, type D1Statement } from "../src/d1-adapter.ts";
import { openSqlite } from "../src/sqlite-adapter.ts";
import worker from "../worker/index.ts";

function fakeD1(raw: ReturnType<typeof openSqlite>["raw"]): D1Binding {
  const stmt = (sql: string, params: any[] = []): D1Statement & { _run(): any } => ({
    bind: (...v: any[]) => stmt(sql, v),
    all: async () => ({ results: raw.prepare(sql).all(...params) as any[] }),
    _run: () => ({ meta: { changes: Number(raw.prepare(sql).run(...params).changes) } }),
  });
  return {
    prepare: (sql) => stmt(sql),
    batch: async (list: any[]) => {
      raw.exec("BEGIN");
      try {
        const out = list.map((s) => s._run());
        raw.exec("COMMIT");
        return out;
      } catch (e) {
        raw.exec("ROLLBACK");
        throw e;
      }
    },
  };
}

async function setup(cap = 50) {
  const { raw } = openSqlite();
  const exec = (sql: string) => raw.exec(sql);
  exec((await admin(["create-tenant", "--id", "t1", "--name", "test"])).sql);
  const ingest = await admin(["mint", "--tenant", "t1", "--scope", "ingest", "--source", "cloud-env-a", "--label", "cloud env a", "--cap", String(cap)]);
  const read = await admin(["mint", "--tenant", "t1", "--scope", "read", "--source", "mac", "--label", "mac reader"]);
  exec(ingest.sql); exec(read.sql);
  const env = { DB: fakeD1(raw) };
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    const init: RequestInit = { method: req.method, headers: req.headers as any };
    if (req.method === "POST") init.body = Buffer.concat(chunks);
    const out = await worker.fetch(new Request(`http://localhost${req.url}`, init), env);
    res.writeHead(out.status, Object.fromEntries(out.headers)).end(Buffer.from(await out.arrayBuffer()));
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  return { raw, env, ingest: ingest.secret!, read: read.secret!, url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, close: () => new Promise<void>((r) => server.close(() => r())) };
}

const s = await setup(50);
after(() => s.close());
runContract({ baseUrl: s.url, ingestToken: s.ingest, readToken: s.read, dailyCap: 50 });

describe("worker entrypoint", () => {
  it("rejects an oversized body by content-length without reading it", async () => {
    const res = await worker.fetch(new Request("http://x/v1/ingest", { method: "POST", headers: { "content-length": String(300 * 1024), authorization: `Bearer ${s.ingest}`, "x-tokenatlas-client": "2" }, body: "x" }), s.env);
    assert.equal(res.status, 413);
  });
  it("rejects an oversized streamed body without a content-length", async () => {
    const big = new ReadableStream({ start(c) { for (let i = 0; i < 40; i++) c.enqueue(new Uint8Array(8192)); c.close(); } });
    const res = await worker.fetch(new Request("http://x/v1/ingest", { method: "POST", body: big, duplex: "half" } as any), s.env);
    assert.equal(res.status, 413);
  });
  it("does not buffer a body for paths that never need one, and a failing body read is a JSON 400", async () => {
    const res = await worker.fetch(new Request("http://x/v1/health", { method: "POST", headers: { "content-length": String(300 * 1024) }, body: "x" }), s.env);
    assert.equal(res.status, 405);
    const broken = new ReadableStream({ start(c) { c.error(new Error("client went away")); } });
    const bad = await worker.fetch(new Request("http://x/v1/ingest", { method: "POST", body: broken, duplex: "half" } as any), s.env);
    assert.equal(bad.status, 400);
    assert.equal(bad.headers.get("x-tokenatlas-api"), "1");
  });
  it("answers health and unknown paths with the API header", async () => {
    const ok = await worker.fetch(new Request("http://x/v1/health"), s.env);
    assert.equal(ok.headers.get("x-tokenatlas-api"), "1");
    assert.equal((await worker.fetch(new Request("http://x/nope"), s.env)).status, 404);
  });
});

describe("token admin CLI", () => {
  it("emits SQL that the schema accepts, a secret only on stderr, and revocation works", async () => {
    const { raw } = openSqlite();
    raw.exec((await admin(["create-tenant", "--id", "t9", "--name", "Acme Co", "--retention-days", "30"])).sql);
    const minted = await admin(["mint", "--tenant", "t9", "--scope", "read", "--source", "laptop", "--label", "o'brien laptop".replace("'", "")]);
    assert.ok(minted.secret?.startsWith("tka_read_"));
    assert.ok(!minted.sql.includes(minted.secret!.split("_").pop()!), "SQL holds the hash, never the secret");
    raw.exec(minted.sql);
    const id = (raw.prepare("SELECT token_id FROM tokens").get() as any).token_id;
    raw.exec((await admin(["revoke", "--token-id", id])).sql);
    assert.ok((raw.prepare("SELECT revoked_at FROM tokens").get() as any).revoked_at);
    const missing = (await admin(["revoke", "--token-id", "nosuchtoken1"])).sql;
    assert.match(missing, /NO TOKEN REVOKED/);
  });
  it("refuses values that could break out of the SQL", async () => {
    for (const bad of [["create-tenant", "--id", "x'; DROP TABLE tokens;--", "--name", "n"], ["mint", "--tenant", "t1", "--scope", "admin", "--source", "s", "--label", "l"],
      ["mint", "--tenant", "t1", "--scope", "read", "--source", "s", "--label", "a'b"], ["revoke", "--token-id", "1; DELETE"], ["mint", "--tenant", "t1", "--scope", "read", "--source", "s", "--label", "l", "--cap", "-1"], ["nope"]]) {
      await assert.rejects(admin(bad), undefined, bad.join(" "));
    }
  });
});
