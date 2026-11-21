// Reference deployment for the contract suite: the portable core on node:http + node:sqlite, with one tenant and two tokens.
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { handle } from "../src/handler.ts";
import type { Clock } from "../src/ports.ts";
import { openSqlite } from "../src/sqlite-adapter.ts";
import { mintToken, type Scope } from "../src/tokens.ts";

export async function startLocal(opts: { dailyCap?: number; clock?: Clock } = {}) {
  const { db, raw } = openSqlite();
  const clock = opts.clock ?? { now: () => new Date() };
  const dailyCap = opts.dailyCap ?? 5000;
  raw.prepare("INSERT INTO tenants (tenant_id, name, created_at) VALUES ('t1', 'test', ?)").run(clock.now().toISOString());
  const mint = async (scope: Scope, source: string) => {
    const t = await mintToken(scope);
    raw.prepare("INSERT INTO tokens (token_id, tenant_id, secret_sha256, scope, source, label, daily_row_cap, created_at) VALUES (?, 't1', ?, ?, ?, 'test', ?, ?)")
      .run(t.tokenId, t.secretSha256, scope, source, dailyCap, clock.now().toISOString());
    return t;
  };
  const ingest = await mint("ingest", "cloud-env-a");
  const read = await mint("read", "reader");
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    const url = new URL(req.url ?? "/", "http://localhost");
    const headers = Object.fromEntries(Object.entries(req.headers).map(([k, v]) => [k, String(v)]));
    const out = await handle({ db, clock }, { method: req.method ?? "GET", path: url.pathname, query: Object.fromEntries(url.searchParams), headers, body: Buffer.concat(chunks).toString("utf8") });
    res.writeHead(out.status, out.headers).end(out.body);
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  return {
    url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
    ingestToken: ingest.token, readToken: read.token, dailyCap, db, raw, mint,
    close: () => new Promise<void>((r) => server.close(() => r())),
  };
}
