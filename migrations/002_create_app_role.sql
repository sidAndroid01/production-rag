-- The owner bootstraps the schema; the application connects as rag_app.
-- No password lives in Git: migrate.py sets it from RAG_APP_DB_PASSWORD.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
        CREATE ROLE rag_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO rag_app', current_database());
END $$;

GRANT USAGE ON SCHEMA public TO rag_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON documents, chunks TO rag_app;
