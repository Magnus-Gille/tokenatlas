// Token format `tka_<scope>_<id>_<secret>` (design section 4). Only sha256(secret) is stored.
import { sha256Hex } from "./dedupe.ts";

export type Scope = "ingest" | "read";

export interface ParsedToken {
  scope: Scope;
  id: string;
  secret: string;
}

const TOKEN = /^tka_(ingest|read)_([a-z0-9]{8,32})_([A-Za-z0-9_-]{43})$/;

export function parseToken(text: string): ParsedToken | null {
  const m = TOKEN.exec(text);
  return m ? { scope: m[1] as Scope, id: m[2], secret: m[3] } : null;
}

const randomChars = (bytes: number, alphabet: string) =>
  [...crypto.getRandomValues(new Uint8Array(bytes))].map((b) => alphabet[b % alphabet.length]).join("");

/** Mint a token: the plaintext is shown once to the operator, the row stores the hash. */
export async function mintToken(scope: Scope): Promise<{ token: string; tokenId: string; secretSha256: string }> {
  const id = randomChars(12, "abcdefghijklmnopqrstuvwxyz0123456789");
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const secret = btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return { token: `tka_${scope}_${id}_${secret}`, tokenId: id, secretSha256: await sha256Hex(secret) };
}
