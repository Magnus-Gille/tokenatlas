// Reference Db adapter on node:sqlite: used by the tests and by `contract/run.ts --local`. Not shipped to Workers.
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import type { Db, SqlValue, Stmt } from "./ports.ts";

export function openSqlite(path = ":memory:"): { db: Db; raw: DatabaseSync } {
  const raw = new DatabaseSync(path);
  raw.exec(readFileSync(new URL("../migrations/sqlite/0001_init.sql", import.meta.url), "utf8"));
  const db: Db = {
    async all<T>(sql: string, params: SqlValue[] = []) {
      await new Promise((r) => setImmediate(r)); // yield like a network call, so tests exercise interleaving
      return raw.prepare(sql).all(...params) as T[];
    },
    async batch(stmts: Stmt[]) {
      await new Promise((r) => setImmediate(r));
      const changes: number[] = [];
      raw.exec("BEGIN");
      try {
        for (const s of stmts) changes.push(Number(raw.prepare(s.sql).run(...s.params).changes));
        raw.exec("COMMIT");
      } catch (e) {
        raw.exec("ROLLBACK");
        throw e;
      }
      return changes;
    },
  };
  return { db, raw };
}
