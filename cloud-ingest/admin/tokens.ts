// Operator CLI (design sections 3 and 4): no HTTP admin API in v1. It prints SQL for `wrangler d1 execute`; a minted token's
// plaintext goes to stderr once and is never stored. Every value is validated against a strict pattern before it is placed in
// SQL, so the generated statements cannot be injected into.
//
//   node admin/tokens.ts create-tenant --id <ulid> --name <name> [--retention-days 90]
//   node admin/tokens.ts mint --tenant <id> --scope ingest|read --source <env-label> --label <text> [--cap 5000] > mint.sql
//   node admin/tokens.ts revoke --token-id <id>
//   wrangler d1 execute <db> --remote --file mint.sql
import { parseArgs } from "node:util";
import { mintToken, type Scope } from "../src/tokens.ts";

const ID = /^[A-Za-z0-9_-]{1,64}$/;
const TEXT = /^[A-Za-z0-9 _.:@\-]{1,80}$/;

function need(values: Record<string, unknown>, key: string, re: RegExp): string {
  const v = values[key];
  if (typeof v !== "string" || !re.test(v)) throw new Error(`--${key} is missing or has characters outside ${re}`);
  return v;
}
const q = (s: string) => `'${s.replace(/'/g, "''")}'`;

export async function run(argv: string[], now = new Date()): Promise<{ sql: string; secret?: string }> {
  const { positionals, values } = parseArgs({
    args: argv, allowPositionals: true,
    options: { id: { type: "string" }, name: { type: "string" }, "retention-days": { type: "string" }, tenant: { type: "string" }, scope: { type: "string" },
      source: { type: "string" }, label: { type: "string" }, cap: { type: "string" }, "token-id": { type: "string" } },
  });
  const ts = now.toISOString();
  switch (positionals[0]) {
    case "create-tenant": {
      const days = Number(values["retention-days"] ?? 90);
      if (!Number.isInteger(days) || days < 1 || days > 3650) throw new Error("--retention-days must be 1..3650");
      const id = need(values, "id", ID);
      return { sql: `INSERT INTO tenants (tenant_id, name, created_at, retention_days) VALUES (${q(id)}, ${q(need(values, "name", TEXT))}, ${q(ts)}, ${days});\nINSERT INTO tenant_state (tenant_id) VALUES (${q(id)}) ON CONFLICT (tenant_id) DO NOTHING;\n` };
    }
    case "mint": {
      const scope = need(values, "scope", /^(ingest|read)$/) as Scope;
      const cap = Number(values.cap ?? 5000);
      if (!Number.isInteger(cap) || cap < 1 || cap > 1_000_000) throw new Error("--cap must be 1..1000000");
      const t = await mintToken(scope);
      const sql = `INSERT INTO tokens (token_id, tenant_id, secret_sha256, scope, source, label, daily_row_cap, created_at) VALUES (${q(t.tokenId)}, ${q(need(values, "tenant", ID))}, ${q(t.secretSha256)}, ${q(scope)}, ${q(need(values, "source", ID))}, ${q(need(values, "label", TEXT))}, ${cap}, ${q(ts)});\n`;
      return { sql, secret: t.token };
    }
    case "revoke":
      return { sql: `UPDATE tokens SET revoked_at = ${q(ts)} WHERE token_id = ${q(need(values, "token-id", /^[a-z0-9]{8,32}$/))} AND revoked_at IS NULL;\n` };
    default:
      throw new Error("usage: tokens.ts create-tenant|mint|revoke (see the header of this file)");
  }
}

if (import.meta.main) {
  try {
    const out = await run(process.argv.slice(2));
    process.stdout.write(out.sql);
    if (out.secret) process.stderr.write(`token (shown once, store it in the secret manager): ${out.secret}\n`);
  } catch (e) {
    process.stderr.write(`${(e as Error).message}\n`);
    process.exit(2);
  }
}
