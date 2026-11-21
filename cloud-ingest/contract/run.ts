// Usage:
//   node contract/run.ts --local                          reference implementation (node:sqlite), no network
//   node contract/run.ts --url https://ingest.example --ingest-token tka_ingest_... --read-token tka_read_...
// Tokens may also come from TOKENATLAS_CONTRACT_INGEST_TOKEN / _READ_TOKEN. The suite writes test rows (a random session
// id per run) to the target; point it at a staging tenant, not at production data you care about.
import { parseArgs } from "node:util";
import { runContract } from "./suite.ts";

const { values } = parseArgs({ options: { url: { type: "string" }, local: { type: "boolean" }, "ingest-token": { type: "string" }, "read-token": { type: "string" }, "daily-cap": { type: "string" } } });

if (values.local) {
  const { startLocal } = await import("./local-server.ts");
  const s = await startLocal();
  runContract({ baseUrl: s.url, ingestToken: s.ingestToken, readToken: s.readToken, dailyCap: s.dailyCap });
} else {
  const ingestToken = values["ingest-token"] ?? process.env.TOKENATLAS_CONTRACT_INGEST_TOKEN;
  const readToken = values["read-token"] ?? process.env.TOKENATLAS_CONTRACT_READ_TOKEN;
  if (!values.url || !ingestToken || !readToken) {
    console.error("need --url, --ingest-token and --read-token (or --local)");
    process.exit(2);
  }
  runContract({ baseUrl: values.url, ingestToken, readToken, dailyCap: values["daily-cap"] ? Number(values["daily-cap"]) : undefined });
}
