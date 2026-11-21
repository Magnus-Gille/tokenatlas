// Opaque pull cursor: (tenant, generation, seq) as base64url JSON (design sections 3 and 8).

export interface Cursor {
  tenant: string;
  generation: number;
  seq: number;
  /** The tenant's `deleted_through_seq` when the cursor was issued: rows deleted after that are a gap, rows deleted before it are not. */
  deleted?: number;
}

const b64 = (s: string) => btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const unb64 = (s: string) => atob(s.replace(/-/g, "+").replace(/_/g, "/"));

export function encodeCursor(c: Cursor): string {
  return b64(JSON.stringify({ t: c.tenant, g: c.generation, s: c.seq, d: c.deleted ?? 0 }));
}

/** Returns null for anything that is not a well-formed cursor. */
export function decodeCursor(text: string): Cursor | null {
  try {
    if (!/^[A-Za-z0-9_-]{1,512}$/.test(text)) return null;
    const o = JSON.parse(unb64(text));
    if (typeof o?.t !== "string" || !Number.isSafeInteger(o.g) || !Number.isSafeInteger(o.s) || o.g < 1 || o.s < 0) return null;
    const d = o.d === undefined ? 0 : o.d;
    if (!Number.isSafeInteger(d) || d < 0) return null;
    return { tenant: o.t, generation: o.g, seq: o.s, deleted: d };
  } catch {
    return null;
  }
}
