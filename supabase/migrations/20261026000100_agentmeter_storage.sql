-- The private Storage bucket for large session files (results over the inline limit, report.pdf,
-- manifest.json, features.csv, labels.csv, flows.json). Only the Edge Function reads and writes it
-- (service-role key); the GPU backend uploads through single-use signed URLs. 50 MB per file is the
-- Supabase free-plan limit. Skipped where there is no Storage schema (local tests).
DO $$
BEGIN
  IF to_regclass('storage.buckets') IS NOT NULL THEN
    INSERT INTO storage.buckets (id, name, public, file_size_limit)
    VALUES ('agentmeter', 'agentmeter', false, 52428800)
    ON CONFLICT (id) DO NOTHING;
  END IF;
END $$;
