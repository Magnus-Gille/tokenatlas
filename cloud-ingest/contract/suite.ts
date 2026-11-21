// Contract suite: black-box HTTP tests of the ingest API, runnable against any deployment (local reference, Cloudflare,
// AWS later). It only needs an `ingest` token and a `read` token for the same tenant. Every run uses a fresh session id, so
// it can be repeated against a live deployment; it never deletes anything.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

export interface ContractConfig {
  baseUrl: string;
  ingestToken: string;
  readToken: string;
  /** Daily row cap of the ingest token, when known: enables the quota test (the token must be fresh). */
  dailyCap?: number;
}

const fixture = (name: string) => readFileSync(new URL(`../fixtures/${name}`, import.meta.url), "utf8").trim().split("\n").map((l) => JSON.parse(l));

export function runContract(cfg: ContractConfig): void {
  const base = cfg.baseUrl.replace(/\/$/, "");
  const call = (path: string, init: { method?: string; token?: string; body?: string; client?: string | null } = {}) =>
    fetch(base + path, {
      method: init.method ?? "GET",
      redirect: "manual",
      headers: {
        "user-agent": "tokenatlas-contract/1",
        ...(init.token ? { authorization: `Bearer ${init.token}` } : {}),
        ...(init.client === null ? {} : { "x-tokenatlas-client": init.client ?? "2" }),
        ...(init.body !== undefined ? { "content-type": "application/x-ndjson" } : {}),
      },
      body: init.body,
    });
  const post = (rows: unknown[], token = cfg.ingestToken, extra: { client?: string | null } = {}) =>
    call("/v1/ingest", { method: "POST", token, body: rows.map((r) => (typeof r === "string" ? r : JSON.stringify(r))).join("\n") + "\n", ...extra });
  const run = Math.random().toString(36).slice(2, 10);
  const session = `ct${run}`;
  const rows = () => fixture("digest-valid.ndjson").map((r) => ({ ...r, session }));
  const pullAll = async (extra = "") => {
    const out: any[] = [];
    let cursor = "";
    for (let i = 0; i < 200; i++) {
      const res = await call(`/v1/events?limit=100${cursor ? `&after=${cursor}` : ""}${extra}`, { token: cfg.readToken });
      assert.equal(res.status, 200);
      const text = await res.text();
      const page = text.split("\n").filter(Boolean).map((l) => JSON.parse(l));
      out.push(...page);
      const next = res.headers.get("x-next-cursor")!;
      assert.ok(next, "X-Next-Cursor present");
      if (!page.length) return out;
      cursor = next;
    }
    throw new Error("pull did not terminate");
  };
  const mine = (events: any[]) => events.filter((e) => e.session_id === session);

  describe("health and headers", () => {
    it("GET /v1/health needs no auth and reports api 1", async () => {
      const res = await call("/v1/health");
      assert.equal(res.status, 200);
      assert.equal(res.headers.get("x-tokenatlas-api"), "1");
      assert.deepEqual(await res.json(), { ok: true, api: 1 });
    });
    it("errors use the {error:{code,message}} shape", async () => {
      const res = await call("/v1/nope");
      assert.equal(res.status, 404);
      const body: any = await res.json();
      assert.equal(typeof body.error.code, "string");
      assert.equal(typeof body.error.message, "string");
    });
  });

  describe("auth", () => {
    it("rejects a missing or malformed token with 401", async () => {
      assert.equal((await post(rows(), "")).status, 401);
      assert.equal((await post(rows(), "tka_ingest_x_y")).status, 401);
    });
    it("a read token cannot ingest and an ingest token cannot read (403)", async () => {
      assert.equal((await post(rows(), cfg.readToken)).status, 403);
      assert.equal((await call("/v1/events", { token: cfg.ingestToken })).status, 403);
    });
    it("an old client (no X-TokenAtlas-Client: 2) gets 426 for the whole request", async () => {
      assert.equal((await post(rows(), cfg.ingestToken, { client: null })).status, 426);
      assert.equal((await post(rows(), cfg.ingestToken, { client: "1" })).status, 426);
      assert.equal(mine(await pullAll()).length, 0, "nothing stored from a 426");
    });
  });

  describe("ingest", () => {
    it("accepts the real-digest fixture, then reports every resend as duplicate", async () => {
      const first = await post(rows());
      assert.equal(first.status, 200);
      const a: any = await first.json();
      assert.deepEqual(a.results.map((r: any) => r.status), rows().map(() => "accepted"));
      assert.deepEqual(a.results.map((r: any) => r.line), rows().map((_, i) => i + 1));
      const again: any = await (await post(rows())).json();
      assert.deepEqual(again.results.map((r: any) => r.status), rows().map(() => "duplicate"));
    });
    it("same uuid with different content is a conflict; the first row is kept", async () => {
      const changed = rows()[1];
      changed.message.usage.output_tokens += 1;
      const res: any = await (await post([changed])).json();
      assert.equal(res.results[0].status, "conflict");
      const stored = mine(await pullAll()).find((e) => e.row.uuid === "a1");
      assert.equal(stored.row.message.usage.output_tokens, 412);
    });
    it("rejects bad rows per line with a code and still accepts the good ones", async () => {
      const good = { ...rows()[1], uuid: "good1" };
      const batch = [
        "not json",
        { ...good, uuid: "x1", session: "../etc" },
        { ...good, uuid: "x2", _path: "subagents/../../x.jsonl" },
        { ...good, uuid: "x3", timestamp: "yesterday" },
        { ...good, uuid: "x4", message: { ...good.message, usage: { ...good.message.usage, output_tokens: -1 } } },
        { ...good, uuid: "x5", cwd: "a\u0000b" },
        { ...good, uuid: "x6", cwd: "x".repeat(300) },
        { ...good, uuid: "x7", message: { ...good.message, usage: { ...good.message.usage, output_tokens: 2 ** 53 } } },
        good,
      ];
      const res: any = await (await post(batch)).json();
      assert.deepEqual(res.results.map((r: any) => r.status), [...batch.slice(0, -1).map(() => "rejected"), "accepted"]);
      for (const r of res.results.slice(0, -1)) assert.equal(typeof r.code, "string");
    });
    it("an oversized line is row_too_large; unknown-key padding is stripped, not rejected", async () => {
      const huge: any = await (await post([{ ...rows()[1], uuid: "big1", padding: "x".repeat(70000) }])).json();
      assert.equal(huge.results[0].status, "row_too_large");
      const padded: any = await (await post([{ ...rows()[1], uuid: "big2", padding: "x".repeat(20000) }])).json();
      assert.equal(padded.results[0].status, "accepted");
    });
    it("limits: more than 200 rows is 413, an empty body is 400", async () => {
      const many = Array.from({ length: 201 }, (_, i) => ({ ...rows()[1], uuid: `many${i}` }));
      assert.equal((await post(many)).status, 413);
      assert.equal((await call("/v1/ingest", { method: "POST", token: cfg.ingestToken, body: "" })).status, 400);
    });
  });

  describe("pull", () => {
    it("returns the stored rows as envelopes, usage and layout intact", async () => {
      const events = mine(await pullAll());
      const sent = rows();
      const bySeq = events.map((e) => e.seq);
      assert.deepEqual(bySeq, [...bySeq].sort((a, b) => a - b), "ascending seq");
      assert.equal(events.filter((e) => ["u0", "a1", "a2", "s1", "w1", "a3"].includes(e.row.uuid)).length, sent.length);
      for (const e of events) {
        assert.equal(typeof e.dedupe_key, "string");
        assert.equal(e.dedupe_key.length, 64);
        assert.ok(e.received_at && e.token_id && e.source);
      }
      const sub = events.find((e) => e.row.uuid === "s1");
      assert.equal(sub.rel_path, "subagents/agent-a1b2c3d4.jsonl");
      assert.equal(sub.row.sessionId, "agent-session", "a subagent row keeps its own sessionId; the enclosing session is session_id");
      assert.equal(events.find((e) => e.row.uuid === "w1").rel_path, "subagents/workflows/wf_r9x/agent-e5f6.jsonl");
      assert.equal(events.find((e) => e.row.uuid === "a1").rel_path, "");
    });
    it("a POST/GET round trip keeps the usage object identical, so pricing cannot change", async () => {
      const sent = rows().find((r) => r.uuid === "a3");
      const got = mine(await pullAll()).find((e) => e.row.uuid === "a3");
      assert.deepEqual(got.row, (({ session: _s, _path: _p, ...rest }) => rest)(sent));
    });
    it("strips unknown keys instead of rejecting the row", async () => {
      const res: any = await (await post([{ ...rows()[1], uuid: "extra1", brand_new_key: "x", message: { ...rows()[1].message, content: "SECRET" } }])).json();
      assert.equal(res.results[0].status, "accepted");
      const stored = mine(await pullAll()).find((e) => e.row.uuid === "extra1");
      assert.equal(stored.row.brand_new_key, undefined);
      assert.equal(stored.row.message.content, undefined);
    });
    it("pages with an opaque cursor and never repeats or skips a row", async () => {
      const total = (await pullAll()).length;
      const seen = new Set<number>();
      let cursor = "";
      for (;;) {
        const res = await call(`/v1/events?limit=2${cursor ? `&after=${cursor}` : ""}`, { token: cfg.readToken });
        const page = (await res.text()).split("\n").filter(Boolean).map((l) => JSON.parse(l));
        for (const e of page) {
          assert.ok(!seen.has(e.seq), "no repeats");
          seen.add(e.seq);
        }
        cursor = res.headers.get("x-next-cursor")!;
        if (!page.length) break;
      }
      assert.equal(seen.size, total);
    });
    it("an exhausted cursor stays valid and later rows appear after it", async () => {
      const res = await call("/v1/events?limit=1000", { token: cfg.readToken });
      let cursor = res.headers.get("x-next-cursor")!;
      for (let i = 0; i < 50; i++) { // drain
        const r = await call(`/v1/events?limit=1000&after=${cursor}`, { token: cfg.readToken });
        const t = await r.text();
        cursor = r.headers.get("x-next-cursor")!;
        if (!t) break;
      }
      await post([{ ...rows()[1], uuid: "late1" }]);
      const next = await (await call(`/v1/events?after=${cursor}`, { token: cfg.readToken })).text();
      assert.deepEqual(next.split("\n").filter(Boolean).map((l) => JSON.parse(l).row.uuid), ["late1"]);
    });
    it("a malformed cursor is 400 and a bad limit is 400", async () => {
      assert.equal((await call("/v1/events?after=!!!", { token: cfg.readToken })).status, 400);
      assert.equal((await call("/v1/events?limit=0", { token: cfg.readToken })).status, 400);
    });
  });

  if (cfg.dailyCap !== undefined) {
    describe("quota", () => {
      it("charges only new rows and cuts the batch at the cap with status quota", async () => {
        const cap = cfg.dailyCap!;
        const batch = Array.from({ length: Math.min(200, cap + 5) }, (_, i) => ({ ...rows()[1], uuid: `q${i}` }));
        const res: any = await (await post(batch)).json();
        const accepted = res.results.filter((r: any) => r.status === "accepted").length;
        assert.ok(accepted <= cap);
        if (batch.length > cap) assert.ok(res.results.some((r: any) => r.status === "quota"));
        const retry: any = await (await post(batch.slice(0, 2))).json();
        assert.ok(retry.results.every((r: any) => ["duplicate", "quota"].includes(r.status)));
      });
    });
  }
}
