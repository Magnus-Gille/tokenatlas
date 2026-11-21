// Ports: the only things the core needs from its host. The Cloudflare entrypoint (slice 2) implements them with a D1
// binding and Date; a Lambda/Postgres port would implement the same two interfaces.

export type SqlValue = string | number | null;

export interface Stmt {
  sql: string;
  params: SqlValue[];
}

export interface Db {
  /** Read rows. Statements use `?` placeholders and the SQL subset common to SQLite and Postgres. */
  all<T = Record<string, SqlValue>>(sql: string, params?: SqlValue[]): Promise<T[]>;
  /** Run statements atomically (D1 `batch`); returns `changes` per statement, in order. */
  batch(stmts: Stmt[]): Promise<number[]>;
}

export interface Clock {
  now(): Date;
}

export interface Deps {
  db: Db;
  clock: Clock;
  /** Optional diagnostics sink; receives an event name and small, non-sensitive fields only. */
  log?: (event: string, info: Record<string, string>) => void;
}

export interface HttpRequest {
  method: string;
  path: string; // without query string
  query: Record<string, string>;
  headers: Record<string, string>; // lower-case names
  body: string;
}

export interface HttpResponse {
  status: number;
  headers: Record<string, string>;
  body: string;
}
