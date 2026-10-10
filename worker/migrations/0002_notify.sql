-- Phase 47: Telegram notification sent for a session's final status (once per session).
ALTER TABLE sessions ADD COLUMN notified_status TEXT;
