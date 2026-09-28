-- Durable, tenant-isolated chat history so conversations survive restarts and
-- work across replicas.
CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_session
    ON chat_messages (tenant_id, user_id, session_id, id);

ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_messages FORCE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'chat_messages_tenant_policy') THEN
        CREATE POLICY chat_messages_tenant_policy ON chat_messages
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
GRANT SELECT, INSERT, DELETE ON chat_messages TO rag_app;
GRANT USAGE ON SEQUENCE chat_messages_id_seq TO rag_app;
