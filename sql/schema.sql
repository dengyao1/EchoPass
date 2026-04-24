-- EchoPass · PostgreSQL 持久化表结构（与 echopass/engine.py 配套）
-- 使用：psql -U <user> -d <db> -f sql/schema.sql
--
-- 从旧版 speaker_rt_demo 升级请执行：
--   psql -U <user> -d <db> -f sql/migrations/001_rename_speaker_demo_enrollments.sql

CREATE TABLE IF NOT EXISTS echopass_speaker_enrollments (
    id BIGSERIAL PRIMARY KEY,
    speaker_name  TEXT        NOT NULL,
    model_id      TEXT        NOT NULL,
    embedding_dim SMALLINT    NOT NULL,
    embedding     BYTEA       NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_echopass_speaker_model_name UNIQUE (model_id, speaker_name)
);

CREATE INDEX IF NOT EXISTS idx_echopass_speaker_model_id
    ON echopass_speaker_enrollments (model_id);

COMMENT ON TABLE echopass_speaker_enrollments
    IS 'EchoPass: L2-normalized float32 speaker embeddings (little-endian).';
