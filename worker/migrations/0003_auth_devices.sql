-- Profile page: which devices are logged in, and whether each login was "Remember me".
-- remember = 0: shorter refresh lifetime and logged out after session_idle_minutes of no activity.
ALTER TABLE auth_sessions ADD COLUMN remember INTEGER NOT NULL DEFAULT 1;
ALTER TABLE auth_sessions ADD COLUMN user_agent TEXT;
