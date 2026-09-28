-- Versioned documents, page-aware chunks, and a durable ingestion queue.

-- Re-uploading a source creates a new version; older versions are kept but
-- marked superseded and excluded from retrieval.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_type TEXT NOT NULL DEFAULT 'text/plain';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'documents_status_check'
    ) THEN
        ALTER TABLE documents ADD CONSTRAINT documents_status_check
            CHECK (status IN ('ready', 'superseded'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS documents_tenant_source_status
    ON documents (tenant_id, source, status);

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS page INTEGER CHECK (page IS NULL OR page >= 1);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    payload JSONB NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    error TEXT,
    result JSONB,
    run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ingestion_jobs_claimable
    ON ingestion_jobs (run_after, created_at) WHERE status = 'queued';

-- The API role reads and writes jobs only for its current tenant. RLS is not
-- forced, so the owner-defined claim function below can see every tenant.
ALTER TABLE ingestion_jobs ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'ingestion_jobs_tenant_policy') THEN
        CREATE POLICY ingestion_jobs_tenant_policy ON ingestion_jobs
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
GRANT SELECT, INSERT, UPDATE ON ingestion_jobs TO rag_app;

-- Workers claim the oldest runnable job across tenants. Jobs left running by a
-- crashed worker for 10 minutes become claimable again. SKIP LOCKED lets many
-- workers poll concurrently without claiming the same job.
CREATE OR REPLACE FUNCTION claim_ingestion_job()
RETURNS TABLE (id UUID, tenant_id TEXT, payload JSONB, attempts INTEGER, max_attempts INTEGER)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    UPDATE ingestion_jobs AS job
    SET status = 'running', attempts = job.attempts + 1,
        locked_at = now(), updated_at = now()
    WHERE job.id = (
        SELECT candidate.id FROM ingestion_jobs AS candidate
        WHERE (candidate.status = 'queued' AND candidate.run_after <= now())
           OR (candidate.status = 'running'
               AND candidate.locked_at < now() - interval '10 minutes')
        ORDER BY candidate.run_after, candidate.created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING job.id, job.tenant_id, job.payload, job.attempts, job.max_attempts;
$$;
REVOKE ALL ON FUNCTION claim_ingestion_job() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_ingestion_job() TO rag_app;
