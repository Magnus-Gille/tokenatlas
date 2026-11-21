// Runs the black-box contract suite against the reference implementation.
import { runContract } from "../contract/suite.ts";
import { startLocal } from "../contract/local-server.ts";
import { after } from "node:test";

const s = await startLocal({ dailyCap: 50 });
after(() => s.close());
runContract({ baseUrl: s.url, ingestToken: s.ingestToken, readToken: s.readToken, dailyCap: s.dailyCap });
