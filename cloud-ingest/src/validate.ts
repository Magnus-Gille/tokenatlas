// Server-side validation of one wire row (design section 5). Unknown keys are stripped, never rejected; a failing known
// field rejects the row with a stable code. The returned `row` is what is stored (no `session`, no `_path`).

export const MAX_ROW_BYTES = 16 * 1024;
export const MAX_RAW_LINE_BYTES = 64 * 1024; // before stripping unknown keys; the stored row is capped at MAX_ROW_BYTES
export const MAX_ITERATIONS = 64;
const MAX_COUNTER = Number.MAX_SAFE_INTEGER;

const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const TAG = /^[A-Za-z0-9][A-Za-z0-9_.:\-\[\]]{0,63}$/; // model, version, effort, entrypoint, stop_reason, ...
const LABEL = /^[\w.\-]{1,32}$/;
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$/;
const REL_PATH = /^(subagents\/(workflows\/wf_[A-Za-z0-9_-]{1,64}\/)?agent-[A-Za-z0-9_-]{1,64}\.jsonl)?$/;
const PRINTABLE = /^[^\p{C}]*$/u; // no control, format or unassigned characters

const ROW_IDS = ["uuid", "sessionId", "requestId", "parentSessionId", "agentId", "turn_id", "turnId"] as const;
const ROW_TAGS = ["entrypoint", "version", "effort", "attributionAgent"] as const;
const ROW_FREE = ["cwd", "gitBranch"] as const;
const MSG_IDS = ["id", "turn_id", "turnId"] as const;
const MSG_TAGS = ["model", "role", "stop_reason"] as const;
const COUNTERS = ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"] as const;
const CACHE_SPLIT = ["ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"] as const;
const LABELS = ["service_tier", "speed", "inference_geo", "type"] as const;

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
export type Row = Record<string, Json>;

export interface Valid {
  ok: true;
  session: string;
  relPath: string;
  row: Row;
}
export interface Invalid {
  ok: false;
  code: string; // stable, machine-readable: bad_json | not_object | row_too_large | bad_session | bad_path | bad_field:<name>
}

class Reject extends Error {}
const fail = (code: string): never => {
  throw new Reject(code);
};

function isObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function timestamp(v: unknown): string {
  const t = typeof v === "string" && TIMESTAMP.test(v) ? Date.parse(v) : NaN;
  if (!(t >= Date.UTC(2020, 0, 1) && t <= Date.UTC(2100, 0, 1))) fail("bad_field:timestamp");
  return v as string;
}

function str(v: unknown, re: RegExp, name: string): string {
  if (typeof v !== "string" || !re.test(v)) fail(`bad_field:${name}`);
  return v as string;
}

function free(v: unknown, name: string): string {
  if (typeof v !== "string" || [...v].length > 256 || !PRINTABLE.test(v)) fail(`bad_field:${name}`);
  return v as string;
}

function counter(v: unknown, name: string): number {
  if (typeof v !== "number" || !Number.isInteger(v) || v < 0 || v > MAX_COUNTER) fail(`bad_field:${name}`);
  return v as number;
}

/** Copy the allowlisted counters present in `src` (nested objects use the same rule). */
function counters(src: Record<string, unknown>, keys: readonly string[], prefix: string): Record<string, Json> {
  const out: Record<string, Json> = {};
  for (const k of keys) if (src[k] !== undefined) out[k] = counter(src[k], `${prefix}.${k}`);
  return out;
}

function nested(v: unknown, keys: readonly string[], name: string): Record<string, Json> | undefined {
  if (v === undefined) return undefined;
  if (!isObject(v)) return fail(`bad_field:${name}`);
  const out = counters(v, keys, name);
  return Object.keys(out).length ? out : undefined;
}

export function validateUsage(u: unknown): Record<string, Json> {
  if (!isObject(u)) return fail("bad_field:message.usage");
  const out = counters(u, COUNTERS, "usage");
  const split = nested(u.cache_creation, CACHE_SPLIT, "usage.cache_creation");
  if (split) out.cache_creation = split;
  const details = nested(u.output_tokens_details, ["thinking_tokens"], "usage.output_tokens_details");
  if (details) out.output_tokens_details = details;
  for (const k of LABELS) if (u[k] !== undefined) out[k] = str(u[k], LABEL, `usage.${k}`);
  if (u.iterations !== undefined) {
    if (!Array.isArray(u.iterations) || u.iterations.length > MAX_ITERATIONS) fail("bad_field:usage.iterations");
    out.iterations = (u.iterations as unknown[]).map((it, i) => {
      if (!isObject(it)) return fail(`bad_field:usage.iterations[${i}]`);
      const o: Record<string, Json> = counters(it, COUNTERS, `usage.iterations[${i}]`);
      const split = nested(it.cache_creation, CACHE_SPLIT, `usage.iterations[${i}].cache_creation`);
      if (split) o.cache_creation = split;
      // advisor iterations carry a type and the advisor model; pricing and warnings need both
      if (it.type !== undefined) o.type = str(it.type, LABEL, `usage.iterations[${i}].type`);
      if (it.model !== undefined) o.model = str(it.model, TAG, `usage.iterations[${i}].model`);
      return o;
    });
  }
  return out;
}

export function validateWireRow(input: unknown): Valid | Invalid {
  try {
    if (!isObject(input)) return { ok: false, code: "not_object" };
    const session = typeof input.session === "string" && ID.test(input.session) ? input.session : fail("bad_session");
    const relPath = input._path === undefined ? "" : typeof input._path === "string" && input._path.length <= 200 && REL_PATH.test(input._path) ? input._path : fail("bad_path");
    const row: Row = {};
    for (const k of ROW_IDS) if (input[k] !== undefined) row[k] = str(input[k], ID, k);
    for (const k of ROW_TAGS) if (input[k] !== undefined) row[k] = str(input[k], TAG, k);
    for (const k of ROW_FREE) if (input[k] !== undefined) row[k] = free(input[k], k);
    if (input.isSidechain !== undefined) {
      if (typeof input.isSidechain !== "boolean") fail("bad_field:isSidechain");
      row.isSidechain = input.isSidechain as boolean;
    }
    row.timestamp = timestamp(input.timestamp);
    if (input.type !== "assistant" && input.type !== "user") fail("bad_field:type");
    row.type = input.type as string;
    const m = input.message;
    if (!isObject(m)) fail("bad_field:message");
    const msg: Record<string, Json> = {};
    for (const k of MSG_IDS) if ((m as any)[k] !== undefined) msg[k] = str((m as any)[k], ID, `message.${k}`);
    for (const k of MSG_TAGS) if ((m as any)[k] !== undefined) msg[k] = str((m as any)[k], TAG, `message.${k}`);
    if (row.type === "assistant") msg.usage = validateUsage((m as any).usage);
    row.message = msg;
    if (new TextEncoder().encode(JSON.stringify(row)).length > MAX_ROW_BYTES) return { ok: false, code: "row_too_large" };
    return { ok: true, session: session as string, relPath, row };
  } catch (e) {
    if (e instanceof Reject) return { ok: false, code: e.message };
    throw e;
  }
}

/** Parse and validate one NDJSON line; the raw size check comes first so a huge line is never parsed. */
export function validateLine(line: string): Valid | Invalid {
  if (new TextEncoder().encode(line).length > MAX_RAW_LINE_BYTES) return { ok: false, code: "row_too_large" };
  let parsed: unknown;
  try {
    parsed = JSON.parse(line);
  } catch {
    return { ok: false, code: "bad_json" };
  }
  return validateWireRow(parsed);
}
