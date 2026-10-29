-- Cloud data preparation (Phase: prepare in the cloud while the GPU benchmarks). One row per job
-- the control plane hands to the cloud preparation service (cloudprep/, Google Cloud Run). The
-- input and the prepared files live in Storage under prep/<id>/; the row holds the job's state
-- and its progress log. Same access model as every other table: RLS on, no policies, no grants
-- (only the Edge Function's own database role reads or writes it).
CREATE TABLE IF NOT EXISTS prep_jobs (
  id           TEXT PRIMARY KEY,
  owner_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status       TEXT NOT NULL,              -- uploading | running | done | failed
  source_kind  TEXT NOT NULL,              -- file | url
  filename     TEXT,
  url          TEXT,
  size_bytes   BIGINT,
  options_json TEXT NOT NULL,              -- {max_flows, other_attack, limits}
  phase        TEXT,
  message      TEXT,
  log_json     TEXT NOT NULL DEFAULT '[]', -- the latest progress events (capped)
  set_name     TEXT,                       -- "csv_runs/<name>" | "pcap_runs/<name>" when done
  files_json   TEXT,                       -- ["manifest.json", "features.csv", "labels.csv"]
  error        TEXT,
  error_code   TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_prep_jobs_owner ON prep_jobs(owner_id, created_at);

ALTER TABLE prep_jobs ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON TABLE prep_jobs FROM anon, authenticated;
  END IF;
END $$;
