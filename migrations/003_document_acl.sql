-- Group ACL per document. An empty array means every user in the tenant.
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS allowed_groups TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS documents_allowed_groups_gin
    ON documents USING gin (allowed_groups);
