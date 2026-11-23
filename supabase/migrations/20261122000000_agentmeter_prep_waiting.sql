-- Preparation on the GPU machine, with a waiting list (docs/superpowers/specs/2026-11-22-prep-on-gpu-machine-design.md).
-- The preparation service runs next to the GPU server and never while a benchmark runs, so an
-- upload can wait. prep_jobs.status gains: waiting (stored, not started), cancelled, expired.
--   used_at     when a benchmark was authorised on the job's prepared set: each upload starts one
--   attempts    how many times the service picked the job up (a machine that went away re-queues it)
--   claimed_at  when the service last picked it up
--   queued_at   when it joined the waiting list (milliseconds: the order of the list)
ALTER TABLE prep_jobs ADD COLUMN IF NOT EXISTS used_at TEXT;
ALTER TABLE prep_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE prep_jobs ADD COLUMN IF NOT EXISTS claimed_at TEXT;
ALTER TABLE prep_jobs ADD COLUMN IF NOT EXISTS queued_at TEXT;
CREATE INDEX IF NOT EXISTS idx_prep_jobs_status ON prep_jobs(status, queued_at);
