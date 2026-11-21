// HTTP-agnostic request handler: routing, auth, ingest, pull (design sections 3-6, 8). A host adapter turns its native
// request into HttpRequest and the HttpResponse back; nothing here knows Cloudflare.
import { contentHash, dedupeKey, lineId } from "./dedupe.ts";
import { decodeCursor, encodeCursor } from "./cursor.ts";
import type { Deps, HttpRequest, HttpResponse, SqlValue, Stmt } from "./ports.ts";
import { parseToken, type Scope } from "./tokens.ts";
import { sha256Hex } from "./dedupe.ts";
import { validateLine } from "./validate.ts";

export const API_VERSION = "1";
export const CLIENT_VERSION = "2";
export const MAX_ROWS_PER_REQUEST = 200;
export const MAX_BODY_BYTES = 256 * 1024;
export const MAX_PULL_ROWS = 1000;
export const MAX_PULL_BYTES = 1024 * 1024;
const PULL_SLICE = 64;
const LAST_USED_GRANULARITY_MS = 3_600_000;

export type Status = "accepted" | "duplicate" | "conflict" | "rejected" | "row_too_large" | "suppressed" | "quota";

interface Principal {
  tokenId: string;
  tenant: string;
  source: string;
  scope: Scope;
  dailyCap: number;
}

const baseHeaders = (extra: Record<string, string> = {}) => ({ "x-tokenatlas-api": API_VERSION, ...extra });

function json(status: number, body: unknown, extra: Record<string, string> = {}): HttpResponse {
  return { status, headers: baseHeaders({ "content-type": "application/json", ...extra }), body: JSON.stringify(body) };
}

export const errorResponse = (status: number, code: string, message: string, extra: Record<string, string> = {}) =>
  json(status, { error: { code, message } }, extra);

async function authenticate(deps: Deps, header: string | undefined, scope: Scope): Promise<Principal | HttpResponse> {
  const m = /^Bearer (\S+)$/.exec(header ?? "");
  const token = m ? parseToken(m[1]) : null;
  if (!token) return errorResponse(401, "unauthorized", "missing or malformed bearer token");
  const rows = await deps.db.all<{ token_id: string; tenant_id: string; source: string; scope: Scope; revoked_at: string | null; last_used_at: string | null; daily_row_cap: number }>(
    "SELECT token_id, tenant_id, source, scope, revoked_at, last_used_at, daily_row_cap FROM tokens WHERE secret_sha256 = ? AND token_id = ?",
    [await sha256Hex(token.secret), token.id],
  );
  const t = rows[0];
  if (!t || t.revoked_at) return errorResponse(401, "unauthorized", "unknown or revoked token");
  if (t.scope !== scope) return errorResponse(403, "forbidden", `this endpoint needs a ${scope} token`);
  const now = deps.clock.now();
  if (!t.last_used_at || now.getTime() - Date.parse(t.last_used_at) >= LAST_USED_GRANULARITY_MS) {
    await deps.db.batch([{ sql: "UPDATE tokens SET last_used_at = ? WHERE token_id = ?", params: [now.toISOString(), t.token_id] }]);
  }
  return { tokenId: t.token_id, tenant: t.tenant_id, source: t.source, scope: t.scope, dailyCap: t.daily_row_cap };
}

interface Existing {
  dedupe_key: string;
  content_hash: string;
}

async function lookup(deps: Deps, tenant: string, keys: string[]): Promise<{ events: Map<string, string>; tombstones: Set<string> }> {
  const events = new Map<string, string>();
  const tombstones = new Set<string>();
  for (let i = 0; i < keys.length; i += 50) {
    const chunk = keys.slice(i, i + 50);
    const marks = chunk.map(() => "?").join(",");
    for (const r of await deps.db.all<Existing>(`SELECT dedupe_key, content_hash FROM events WHERE tenant_id = ? AND dedupe_key IN (${marks})`, [tenant, ...chunk])) events.set(r.dedupe_key, r.content_hash);
    for (const r of await deps.db.all<{ dedupe_key: string }>(`SELECT dedupe_key FROM tombstones WHERE tenant_id = ? AND dedupe_key IN (${marks})`, [tenant, ...chunk])) tombstones.add(r.dedupe_key);
  }
  return { events, tombstones };
}

interface Candidate {
  index: number;
  key: string;
  hash: string;
  item: { k: string; s: string; p: string; l: string; r: string | null; t: string; h: string; j: string };
}

const INSERT_CHUNK_BYTES = 64 * 1024; // keeps each statement's bound JSON small (D1 limits SQL text to 100 KB and about 2 MB per bound value)

// One statement per chunk of rows (not per row). The daily cap is enforced inside the statement (LIMIT from the counter)
// and the counter is charged in the same atomic batch, so parallel requests cannot overshoot the cap; a tombstone makes
// the row vanish from the SELECT, so an erased key cannot be resurrected. Dialect note: json_each/json_extract are the
// SQLite form (D1); the Postgres port uses jsonb_array_elements / ->>.
const INSERT_SQL =
  "INSERT INTO events (tenant_id, dedupe_key, source, session_id, rel_path, line_id, request_id, ts, received_at, token_id, schema_ver, content_hash, row_json) " +
  "SELECT ?, json_extract(j.value, '$.k'), ?, json_extract(j.value, '$.s'), json_extract(j.value, '$.p'), json_extract(j.value, '$.l'), json_extract(j.value, '$.r'), json_extract(j.value, '$.t'), ?, ?, 1, json_extract(j.value, '$.h'), json_extract(j.value, '$.j') " +
  "FROM json_each(?) AS j " +
  "WHERE NOT EXISTS (SELECT 1 FROM tombstones WHERE tenant_id = ? AND dedupe_key = json_extract(j.value, '$.k')) " +
  "ORDER BY j.key " +
  "LIMIT (SELECT CASE WHEN ? - COALESCE((SELECT rows FROM quota_day WHERE token_id = ? AND day = ?), 0) > 0 THEN ? - COALESCE((SELECT rows FROM quota_day WHERE token_id = ? AND day = ?), 0) ELSE 0 END) " +
  "ON CONFLICT (tenant_id, dedupe_key) DO NOTHING";

const CHARGE_SQL =
  "UPDATE quota_day SET rows = rows + (SELECT COUNT(*) FROM events WHERE tenant_id = ? AND token_id = ? AND received_at = ? AND dedupe_key IN (SELECT json_extract(j.value, '$.k') FROM json_each(?) AS j)) " +
  "WHERE token_id = ? AND day = ?";

function chunks(cands: Candidate[]): Candidate[][] {
  const out: Candidate[][] = [];
  let cur: Candidate[] = [], bytes = 0;
  for (const c of cands) {
    const size = c.item.j.length * 2 + 400;
    if (cur.length && bytes + size > INSERT_CHUNK_BYTES) { out.push(cur); cur = []; bytes = 0; }
    cur.push(c);
    bytes += size;
  }
  if (cur.length) out.push(cur);
  return out;
}

async function ingest(deps: Deps, req: HttpRequest): Promise<HttpResponse> {
  if (req.method !== "POST") return errorResponse(405, "method_not_allowed", "use POST");
  if (req.headers["x-tokenatlas-client"] !== CLIENT_VERSION) {
    return errorResponse(426, "upgrade_required", `send header X-TokenAtlas-Client: ${CLIENT_VERSION}`);
  }
  const who = await authenticate(deps, req.headers.authorization, "ingest");
  if ("status" in who) return who;
  // note: a host adapter must also cap the body while reading it; this check runs after buffering
  if (new TextEncoder().encode(req.body).length > MAX_BODY_BYTES) return errorResponse(413, "payload_too_large", "request over 256 KB");
  const lines = req.body.split("\n").map((l) => l.replace(/\r$/, ""));
  while (lines.length && lines[lines.length - 1] === "") lines.pop();
  if (!lines.length) return errorResponse(400, "empty", "no rows");
  if (lines.length > MAX_ROWS_PER_REQUEST) return errorResponse(413, "too_many_rows", `at most ${MAX_ROWS_PER_REQUEST} rows per request`);

  const received = deps.clock.now().toISOString();
  const day = received.slice(0, 10);
  const results: { line: number; status: Status; code?: string }[] = lines.map((_, i) => ({ line: i + 1, status: "rejected" as Status }));
  const seenInBatch = new Map<string, string>(); // key -> content hash of the first occurrence
  const toCheck: Candidate[] = [];

  for (const [i, line] of lines.entries()) {
    const v = validateLine(line);
    if (!v.ok) {
      results[i] = { line: i + 1, status: v.code === "row_too_large" ? "row_too_large" : "rejected", code: v.code };
      continue;
    }
    const lid = await lineId(v.row as { uuid?: string });
    const key = await dedupeKey(who.tenant, who.source, v.session, v.relPath, lid);
    const hash = await contentHash(v.row);
    const prior = seenInBatch.get(key);
    if (prior !== undefined) {
      results[i] = { line: i + 1, status: prior === hash ? "duplicate" : "conflict" };
      continue;
    }
    seenInBatch.set(key, hash);
    toCheck.push({ index: i, key, hash, item: { k: key, s: v.session, p: v.relPath, l: lid, r: typeof v.row.requestId === "string" ? v.row.requestId : null, t: v.row.timestamp as string, h: hash, j: JSON.stringify(v.row) } });
  }

  const found = await lookup(deps, who.tenant, toCheck.map((c) => c.key));
  const fresh: Candidate[] = [];
  for (const c of toCheck) {
    const stored = found.events.get(c.key);
    if (found.tombstones.has(c.key)) results[c.index] = { line: c.index + 1, status: "suppressed" };
    else if (stored !== undefined) results[c.index] = { line: c.index + 1, status: stored === c.hash ? "duplicate" : "conflict" };
    else fresh.push(c);
  }

  if (fresh.length) {
    const stmts: Stmt[] = [{ sql: "INSERT INTO quota_day (tenant_id, token_id, day, rows) VALUES (?, ?, ?, 0) ON CONFLICT (token_id, day) DO NOTHING", params: [who.tenant, who.tokenId, day] }];
    for (const group of chunks(fresh)) {
      const json = JSON.stringify(group.map((c) => c.item));
      const cap = who.dailyCap;
      stmts.push({ sql: INSERT_SQL, params: [who.tenant, who.source, received, who.tokenId, json, who.tenant, cap, who.tokenId, day, cap, who.tokenId, day] });
      // retries and duplicates are free: only rows this request really inserted are charged
      stmts.push({ sql: CHARGE_SQL, params: [who.tenant, who.tokenId, received, JSON.stringify(group.map((c) => ({ k: c.key }))), who.tokenId, day] });
    }
    await deps.db.batch(stmts);
    // which rows landed? (our token, this request's timestamp, our keys)
    const landed = new Set<string>();
    for (let i = 0; i < fresh.length; i += 50) {
      const part = fresh.slice(i, i + 50);
      const rows = await deps.db.all<{ dedupe_key: string; content_hash: string }>(
        `SELECT dedupe_key, content_hash FROM events WHERE tenant_id = ? AND token_id = ? AND received_at = ? AND dedupe_key IN (${part.map(() => "?").join(",")})`,
        [who.tenant, who.tokenId, received, ...part.map((c) => c.key)]);
      for (const r of rows) if (r.content_hash) landed.add(r.dedupe_key);
    }
    const rest = fresh.filter((c) => !landed.has(c.key));
    for (const c of fresh) if (landed.has(c.key)) results[c.index] = { line: c.index + 1, status: "accepted" };
    if (rest.length) {
      // not inserted: cut by the cap, erased in between, or a concurrent writer got there first
      const again = await lookup(deps, who.tenant, rest.map((c) => c.key));
      for (const c of rest) {
        const stored = again.events.get(c.key);
        results[c.index] = { line: c.index + 1, status: again.tombstones.has(c.key) ? "suppressed" : stored === undefined ? "quota" : stored === c.hash ? "duplicate" : "conflict" };
      }
    }
  }

  const counts: Record<string, number> = {};
  for (const r of results) counts[r.status] = (counts[r.status] ?? 0) + 1;
  return json(200, { results, counts });
}

async function tenantState(deps: Deps, tenant: string): Promise<{ generation: number; deleted: number }> {
  const r = (await deps.db.all<{ generation: number; deleted_through_seq: number }>("SELECT generation, deleted_through_seq FROM tenant_state WHERE tenant_id = ?", [tenant]))[0];
  return { generation: Number(r?.generation ?? 1), deleted: Number(r?.deleted_through_seq ?? 0) };
}

async function pull(deps: Deps, req: HttpRequest): Promise<HttpResponse> {
  if (req.method !== "GET") return errorResponse(405, "method_not_allowed", "use GET");
  const who = await authenticate(deps, req.headers.authorization, "read");
  if ("status" in who) return who;
  const state = await tenantState(deps, who.tenant);
  let after = 0;
  if (req.query.after) {
    const c = decodeCursor(req.query.after);
    if (!c || c.tenant !== who.tenant) return errorResponse(400, "bad_cursor", "malformed cursor");
    if (c.generation !== Number(state.generation)) return errorResponse(410, "cursor_reset", "storage was reset: drop the cursor and pull from the start");
    // A gap exists only when rows were deleted after this cursor was issued (state.deleted moved past what it knew) and beyond its position:
    // a re-pull from zero issues cursors that already carry the current marker, so it can page through live rows below it.
    if (c.seq < state.deleted && state.deleted > (c.deleted ?? 0)) return errorResponse(410, "cursor_expired", "rows behind this cursor were deleted: drop the cursor and pull from the start");
    after = c.seq;
  }
  const requested = req.query.limit === undefined ? MAX_PULL_ROWS : Number(req.query.limit);
  if (!Number.isInteger(requested) || requested < 1) return errorResponse(400, "bad_limit", "limit must be a positive integer");
  const limit = Math.min(requested, MAX_PULL_ROWS);
  // Read in small slices so a page never loads much more than the byte cap (rows can be up to 16 KB).
  const out: string[] = [];
  let bytes = 0;
  let last = after;
  let full = false;
  while (!full && out.length < limit) {
    const rows = await deps.db.all<Record<string, SqlValue>>(
      "SELECT seq, dedupe_key, source, token_id, session_id, rel_path, received_at, row_json FROM events WHERE tenant_id = ? AND seq > ? ORDER BY seq LIMIT ?",
      [who.tenant, last, Math.min(PULL_SLICE, limit - out.length)],
    );
    if (!rows.length) break;
    for (const r of rows) {
      const line = JSON.stringify({ seq: Number(r.seq), dedupe_key: r.dedupe_key, source: r.source, token_id: r.token_id, session_id: r.session_id, rel_path: r.rel_path, received_at: r.received_at, row: JSON.parse(String(r.row_json)) });
      const size = new TextEncoder().encode(line).length + 1;
      if (out.length && bytes + size > MAX_PULL_BYTES) { full = true; break; } // the next page continues from `last`
      out.push(line);
      bytes += size;
      last = Number(r.seq);
    }
  }
  return {
    status: 200,
    headers: baseHeaders({ "content-type": "application/x-ndjson", "x-next-cursor": encodeCursor({ tenant: who.tenant, generation: state.generation, seq: last, deleted: state.deleted }) }),
    body: out.map((l) => l + "\n").join(""),
  };
}

export async function handle(deps: Deps, req: HttpRequest): Promise<HttpResponse> {
  try {
    if (req.path === "/v1/health") return req.method === "GET" ? json(200, { ok: true, api: 1 }) : errorResponse(405, "method_not_allowed", "use GET");
    if (req.path === "/v1/ingest") return await ingest(deps, req);
    if (req.path === "/v1/events") return await pull(deps, req);
    return errorResponse(404, "not_found", "unknown path");
  } catch (e) {
    deps.log?.("handler_error", { name: e instanceof Error ? e.name : "unknown", path: req.path }); // never bodies, tokens or messages
    return errorResponse(500, "internal", "internal error"); // never echo details; bodies and tokens must not reach logs
  }
}
