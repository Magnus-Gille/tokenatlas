// Cloudflare Worker entrypoint: the only Cloudflare-specific code besides src/d1-adapter.ts. It turns a Request into the
// core's HttpRequest (capping the body while it reads it), runs the core, and turns the HttpResponse back.
import { d1Db, type D1Binding } from "../src/d1-adapter.ts";
import { MAX_BODY_BYTES, errorResponse, handle } from "../src/handler.ts";
import { purgeExpired } from "../src/retention.ts";

export interface Env {
  DB: D1Binding;
}

async function readCapped(request: Request, limit: number): Promise<string | null> {
  const declared = Number(request.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > limit) return null;
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const all = new Uint8Array(total);
  let at = 0;
  for (const c of chunks) {
    all.set(c, at);
    at += c.byteLength;
  }
  return new TextDecoder().decode(all);
}

export async function fetchHandler(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  let body = "";
  if (request.method === "POST" && url.pathname === "/v1/ingest") { // other paths never need a body: do not buffer it
    let read: string | null;
    try {
      read = await readCapped(request, MAX_BODY_BYTES);
    } catch {
      const r = errorResponse(400, "bad_body", "request body could not be read");
      return new Response(r.body, { status: r.status, headers: r.headers });
    }
    if (read === null) {
      const r = errorResponse(413, "payload_too_large", "request over 256 KB");
      return new Response(r.body, { status: r.status, headers: r.headers });
    }
    body = read;
  }
  const out = await handle(
    { db: d1Db(env.DB), clock: { now: () => new Date() }, log: (event, info) => console.log(JSON.stringify({ event, ...info })) },
    {
      method: request.method,
      path: url.pathname,
      query: Object.fromEntries(url.searchParams),
      headers: Object.fromEntries([...request.headers].map(([k, v]) => [k.toLowerCase(), v])),
      body,
    },
  );
  return new Response(out.body, { status: out.status, headers: out.headers });
}

/** Daily cron (wrangler.toml [triggers]): retention. Counts only go to the log. */
export async function scheduledHandler(env: Env): Promise<void> {
  await purgeExpired({ db: d1Db(env.DB), clock: { now: () => new Date() }, log: (event, info) => console.log(JSON.stringify({ event, ...info })) });
}

export default {
  fetch: fetchHandler,
  scheduled: (_controller: unknown, env: Env) => scheduledHandler(env),
};
