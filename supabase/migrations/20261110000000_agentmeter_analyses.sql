-- The traffic analysis without a GPU (docs/superpowers/specs/2026-11-09-gpu-free-analyse-traffic-design.md).
-- An analysis IS a cloud preparation job whose service also scored the candidate pool with the IDS:
-- no new table. When the job finishes, the control plane records the prepared set's fingerprint
-- (manifest prepared_set.content_sha256, the Data sets key) and the analysis summary (ids_pool.json),
-- so an analysis nobody has benchmarked yet is listed and has an owner (the person who uploaded it).
ALTER TABLE prep_jobs ADD COLUMN IF NOT EXISTS prepared_sha256 TEXT;
ALTER TABLE prep_jobs ADD COLUMN IF NOT EXISTS ids_json TEXT;
CREATE INDEX IF NOT EXISTS idx_prep_jobs_set ON prep_jobs(set_name);
CREATE INDEX IF NOT EXISTS idx_prep_jobs_sha ON prep_jobs(prepared_sha256);
