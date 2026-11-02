-- Audit log: who changed what, and when — admin actions (users, roles, passwords, the model
-- catalog, system limits), session deletions and password changes. Append-only from the Edge
-- Function; read by admins (GET /api/admin/audit). Same access model as every other table:
-- RLS on, no policies, no grants (only the Edge Function's own database role reads or writes it).
CREATE TABLE IF NOT EXISTS audit_log (
  id             BIGSERIAL PRIMARY KEY,
  at             TEXT NOT NULL,
  actor_id       TEXT,
  actor_username TEXT,
  action         TEXT NOT NULL,           -- e.g. user.create, user.update, session.delete, limits.update
  target         TEXT,                    -- the user, session or setting acted on
  detail_json    TEXT                     -- what changed (never a password or token)
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON TABLE audit_log FROM anon, authenticated;
    REVOKE ALL ON SEQUENCE audit_log_id_seq FROM anon, authenticated;
  END IF;
END $$;
