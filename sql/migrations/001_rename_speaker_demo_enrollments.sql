-- EchoPass 迁移 001：speaker_demo_enrollments -> echopass_speaker_enrollments
--
-- 适用场景：之前跑过 demo/speaker_rt_demo 的老部署，已经有声纹注册数据。
-- 新代码默认读写 echopass_speaker_enrollments，这里做"改名 + 约束重建"。
--
-- 如果老表不存在（全新部署），脚本会跳过所有操作，不会报错。
-- 如果新表已存在且老表也在（极端情况），ALTER TABLE 会失败，请手工处理。
--
-- 使用：psql -U <user> -d <db> -f sql/migrations/001_rename_speaker_demo_enrollments.sql

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'speaker_demo_enrollments')
       AND NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_name = 'echopass_speaker_enrollments') THEN

        ALTER TABLE speaker_demo_enrollments
            RENAME TO echopass_speaker_enrollments;

        -- 旧唯一约束名
        BEGIN
            ALTER TABLE echopass_speaker_enrollments
                RENAME CONSTRAINT uq_speaker_demo_model_name
                TO uq_echopass_speaker_model_name;
        EXCEPTION WHEN undefined_object THEN
            NULL; -- 约束名以前可能不同，忽略
        END;

        -- 旧索引名
        BEGIN
            ALTER INDEX idx_speaker_demo_model_id
                RENAME TO idx_echopass_speaker_model_id;
        EXCEPTION WHEN undefined_object THEN
            NULL;
        END;

        COMMENT ON TABLE echopass_speaker_enrollments
            IS 'EchoPass: L2-normalized float32 speaker embeddings (little-endian).';

        RAISE NOTICE 'Renamed speaker_demo_enrollments -> echopass_speaker_enrollments';
    ELSE
        RAISE NOTICE 'No rename needed (old table missing or new table already exists).';
    END IF;
END $$;
