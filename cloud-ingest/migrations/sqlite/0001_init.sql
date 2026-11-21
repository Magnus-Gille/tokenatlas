-- Schema from docs/architecture/cloud-ingest.md section 5 (SQLite dialect; D1 runs this unchanged).
-- Differences from the design text: tokens carry `source` (the namespace bound into dedupe keys, kept stable across token
-- rotation) and `daily_row_cap` (per-token quota); events carry `content_hash` (conflict detection).
CREATE TABLE tenants (
  tenant_id   TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  retention_days INTEGER NOT NULL DEFAULT 90
);
CREATE TABLE tokens (
  token_id    TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL REFERENCES tenants(tenant_id),
  secret_sha256 TEXT NOT NULL UNIQUE,
  scope       TEXT NOT NULL CHECK (scope IN ('ingest','read')),
  source      TEXT NOT NULL,
  label       TEXT NOT NULL,
  daily_row_cap INTEGER NOT NULL DEFAULT 5000,
  created_at  TEXT NOT NULL, last_used_at TEXT, revoked_at TEXT
);
CREATE TABLE events (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id   TEXT NOT NULL,
  dedupe_key  TEXT NOT NULL,
  source      TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  rel_path    TEXT NOT NULL DEFAULT '',
  line_id     TEXT NOT NULL,
  request_id  TEXT,
  ts          TEXT NOT NULL,
  received_at TEXT NOT NULL,
  token_id    TEXT NOT NULL,
  schema_ver  INTEGER NOT NULL DEFAULT 1,
  content_hash TEXT NOT NULL,
  row_json    TEXT NOT NULL,
  UNIQUE (tenant_id, dedupe_key)
);
CREATE INDEX events_pull ON events (tenant_id, seq);
CREATE INDEX events_session ON events (tenant_id, session_id);
CREATE INDEX events_retention ON events (tenant_id, received_at);
CREATE TABLE quota_day (tenant_id TEXT, token_id TEXT, day TEXT, rows INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (token_id, day));
CREATE TABLE tombstones (tenant_id TEXT, dedupe_key TEXT, deleted_at TEXT, PRIMARY KEY (tenant_id, dedupe_key));
CREATE TABLE tenant_state (tenant_id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 1, deleted_through_seq INTEGER NOT NULL DEFAULT 0);
