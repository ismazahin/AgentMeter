-- AgentMeter control plane, D1 schema v1. One row per session — never one row per flow.
CREATE TABLE IF NOT EXISTS users (
  id             TEXT PRIMARY KEY,
  username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
  role           TEXT NOT NULL CHECK (role IN ('admin', 'user')),
  pw_hash        TEXT NOT NULL,          -- base64url PBKDF2-SHA256(HMAC(pepper, password), salt, iter)
  pw_salt        TEXT NOT NULL,
  pw_iter        INTEGER NOT NULL,
  disabled       INTEGER NOT NULL DEFAULT 0,
  must_change_pw INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
  id            TEXT PRIMARY KEY,          -- "sid" inside access tokens
  user_id       TEXT NOT NULL REFERENCES users(id),
  refresh_hash  TEXT NOT NULL,             -- sha256 of the current refresh token
  prev_hash     TEXT,                      -- the rotated-out one (reuse => revoke)
  created_at    TEXT NOT NULL,
  last_used_at  TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  revoked_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_user ON auth_sessions(user_id);

CREATE TABLE IF NOT EXISTS settings (
  user_id    TEXT PRIMARY KEY REFERENCES users(id),
  json       TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_by TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backends (
  id                TEXT PRIMARY KEY,      -- 'default' (one GPU backend)
  url               TEXT,
  mode              TEXT,
  gpu_json          TEXT,
  creds_json        TEXT,                  -- presence booleans only, never values
  version           TEXT,
  registered_at     TEXT,
  last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS run_grants (
  jti           TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL REFERENCES users(id),
  kind          TEXT NOT NULL,             -- prepare | import | benchmark | resume
  after_prepare TEXT,
  prepare_jti   TEXT,                      -- the free benchmark of a wizard session points here
  counted       INTEGER NOT NULL,          -- 1 = counts towards sessions per hour
  free_used     INTEGER NOT NULL DEFAULT 0,
  job_id        TEXT,
  issued_at     TEXT NOT NULL,
  expires_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grants_user_time ON run_grants(user_id, issued_at);

CREATE TABLE IF NOT EXISTS sessions (
  id                  TEXT PRIMARY KEY,    -- the backend job id
  owner_id            TEXT,
  owner_username      TEXT,
  status              TEXT NOT NULL,       -- Running | Done | Demo | Failed | Interrupted
  created_at          TEXT NOT NULL,
  finished_at         TEXT,
  input_type          TEXT,
  n_flows             INTEGER,
  gpu                 TEXT,
  provider            TEXT,
  models_json         TEXT,
  more_efficient      TEXT,
  prepared_set        TEXT,
  prepared_set_sha256 TEXT,
  settings_fingerprint TEXT,
  weights_json        TEXT,
  summary_json        TEXT,                -- compact: metrics, identity, constraint facts (~2 KB)
  results_json        TEXT,                -- session_results.json, stored unparsed (NULL when in R2)
  results_r2_key      TEXT,
  results_bytes       INTEGER,
  files_json          TEXT,                -- R2 objects: {"report.pdf": key, ...}
  updated_at          TEXT NOT NULL,
  deleted_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at);

CREATE TABLE IF NOT EXISTS rate_limits (
  bucket       TEXT NOT NULL,
  key          TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  count        INTEGER NOT NULL,
  PRIMARY KEY (bucket, key, window_start)
);
