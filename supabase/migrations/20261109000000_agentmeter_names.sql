-- Sessions can be named, and data sets renamed or removed (the Sessions and Data sets pages).
-- A session's name lives on its row, next to its notes, and is never overwritten by a summary
-- upload. A data set has no row of its own (the Data sets page builds it from the sessions and the
-- GPU server's prepared sets), so this table only records what users changed: its name and whether
-- it was removed. Same access model as every other table: RLS on, no policies, no grants.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title TEXT;

CREATE TABLE IF NOT EXISTS datasets (
  key         TEXT PRIMARY KEY,   -- the set's fingerprint (sha256), or name:<kind>/<name> before one exists
  set_id      TEXT,               -- <kind>/<name> on the GPU server (matches a renamed set before and after its first session)
  label       TEXT,               -- the name users see ('' = the prepared set's own name)
  deleted_at  TEXT,               -- removed from Data sets (sessions that ran on it keep their results)
  deleted_by  TEXT,
  updated_at  TEXT NOT NULL,
  updated_by  TEXT
);

ALTER TABLE datasets ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON TABLE datasets FROM anon, authenticated;
  END IF;
END $$;
