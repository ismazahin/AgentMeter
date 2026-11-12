-- Each user sees only their own work (an admin sees everyone's). Data set names and removals become
-- per user: two users who uploaded the same flows have one data set key, and one renaming or removing
-- it must not change it for the other. The old shared rows are kept as the starting point for the
-- user who last changed them. Same access model as every other table: RLS on, no policies, no grants.
CREATE TABLE IF NOT EXISTS dataset_views (
  user_id     TEXT NOT NULL,
  key         TEXT NOT NULL,     -- as datasets.key: the set's fingerprint, or name:<kind>/<name>
  set_id      TEXT,
  label       TEXT,              -- '' = the prepared set's own name
  deleted_at  TEXT,              -- removed from this user's Data sets
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);

INSERT INTO dataset_views (user_id, key, set_id, label, deleted_at, updated_at)
SELECT u.id, d.key, d.set_id, d.label, d.deleted_at, d.updated_at
  FROM datasets d JOIN users u ON u.username = d.updated_by
ON CONFLICT (user_id, key) DO NOTHING;

CREATE INDEX IF NOT EXISTS sessions_owner_idx ON sessions (owner_id, created_at);

ALTER TABLE dataset_views ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON TABLE dataset_views FROM anon, authenticated;
  END IF;
END $$;
