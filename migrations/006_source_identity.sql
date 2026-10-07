-- Identify documents by where they come from, not by their filename.
--
-- (source_system, source_id) is the logical document: "upload"/"handbook" or,
-- later, "gdrive"/<file id>. Versions of one logical document share it;
-- `source` stays as the display name, so a rename is a metadata change.

ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_system TEXT NOT NULL DEFAULT 'upload';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_id TEXT;
-- Earlier phases versioned by filename; keep those documents' history intact.
UPDATE documents SET source_id = source WHERE source_id IS NULL;
ALTER TABLE documents ALTER COLUMN source_id SET NOT NULL;

-- The source's own version marker (ETag, revision ID) and timestamps for freshness.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_version TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_modified_at TIMESTAMPTZ;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Identical bytes may legitimately exist as two different documents (two
-- folders, two ACLs); content is unique only within one logical document.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_tenant_id_content_sha256_key;
CREATE UNIQUE INDEX IF NOT EXISTS documents_identity_content
    ON documents (tenant_id, source_system, source_id, content_sha256);

DROP INDEX IF EXISTS documents_tenant_source_status;
CREATE INDEX IF NOT EXISTS documents_identity_status
    ON documents (tenant_id, source_system, source_id, status);
