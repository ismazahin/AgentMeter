-- ============================================================================
-- Add the real weight_preset -> session foreign key to a MIGRATED app DB
-- ----------------------------------------------------------------------------
-- WHY: on app DBs created before `preset_id` existed, the column was added with
-- ALTER TABLE, which cannot attach a foreign-key constraint in SQLite. So the FK
-- lives in the code's schema but not in the file, and DBeaver draws no line.
-- This rebuilds ONLY the `session` table with the real FK, preserving all data.
--
-- RUN THIS ONLY on the APP DB (agentmeter_app.db) — NEVER on the study DB.
-- SAFETY: close other connections to the file and make a backup copy first
--   (just copy agentmeter_app.db to agentmeter_app.backup.db).
-- Then run this whole script in a DBeaver SQL editor on the app connection,
-- and Refresh (F5) the connection afterwards — the session->weight_preset line
-- will appear.
-- Fresh app DBs created by the current code already have this FK; this is only
-- needed for older, migrated files.
-- ============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE session_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    source_filename TEXT,
    summary         TEXT,
    analysis_ref    TEXT,
    analysis_json   TEXT NOT NULL,
    source_run_id   TEXT,
    preset_id       INTEGER,
    FOREIGN KEY (preset_id) REFERENCES weight_preset(id) ON DELETE SET NULL
);

INSERT INTO session_new (id, name, created_at, updated_at, source_filename,
                         summary, analysis_ref, analysis_json, source_run_id, preset_id)
SELECT id, name, created_at, updated_at, source_filename,
       summary, analysis_ref, analysis_json, source_run_id, preset_id
FROM session;

DROP TABLE session;
ALTER TABLE session_new RENAME TO session;

COMMIT;

PRAGMA foreign_keys = ON;

-- Verify: this should list one FK (preset_id -> weight_preset.id) ...
PRAGMA foreign_key_list(session);
-- ... and this should return NO rows (all references valid):
PRAGMA foreign_key_check;
