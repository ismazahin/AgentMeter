-- Sessions: free-text notes and tags (owner or admin edits; everyone reads and searches).
-- Separate columns, so a backend summary upload never overwrites them.
ALTER TABLE sessions ADD COLUMN notes TEXT;
ALTER TABLE sessions ADD COLUMN tags_json TEXT;
