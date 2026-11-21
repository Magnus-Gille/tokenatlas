// Db port on Cloudflare D1. Minimal structural types, so the core keeps zero dependencies and tests can pass a fake.
import type { Db, SqlValue, Stmt } from "./ports.ts";

export interface D1Statement {
  bind(...values: SqlValue[]): D1Statement;
  all<T = unknown>(): Promise<{ results?: T[] }>;
}
export interface D1Binding {
  prepare(sql: string): D1Statement;
  batch(statements: D1Statement[]): Promise<{ meta?: { changes?: number } }[]>;
}

export function d1Db(binding: D1Binding): Db {
  return {
    async all<T>(sql: string, params: SqlValue[] = []) {
      const out = await binding.prepare(sql).bind(...params).all<T>();
      return (out.results ?? []) as T[];
    },
    async batch(stmts: Stmt[]) {
      // D1 runs a batch as one implicit transaction: all statements apply or none do.
      const results = await binding.batch(stmts.map((s) => binding.prepare(s.sql).bind(...s.params)));
      return results.map((r) => Number(r.meta?.changes ?? 0));
    },
  };
}
