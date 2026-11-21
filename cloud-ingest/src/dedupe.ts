// Canonical JSON, hashing and the dedupe key (design section 6). Web Crypto only, so it runs in Workers, Node and Lambda.

export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const o = value as Record<string, unknown>;
    return `{${Object.keys(o).sort().filter((k) => o[k] !== undefined).map((k) => `${JSON.stringify(k)}:${canonicalJson(o[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** The line's `uuid`, else a hash of the canonical (validated, stripped) row. */
export async function lineId(row: { uuid?: string }): Promise<string> {
  return row.uuid ?? (await sha256Hex(canonicalJson(row)));
}

/**
 * sha256(tenant | source | session_id | rel_path | line_id). The parts are joined as a JSON array so no choice of
 * separator can make two different tuples collide. `source` comes from the token, never from the request.
 */
export async function dedupeKey(tenant: string, source: string, sessionId: string, relPath: string, line: string): Promise<string> {
  return sha256Hex(JSON.stringify([tenant, source, sessionId, relPath, line]));
}

/** Hash of the stored content, to tell a retry (`duplicate`) from a same-key different-content `conflict`. */
export async function contentHash(row: unknown): Promise<string> {
  return sha256Hex(canonicalJson(row));
}
