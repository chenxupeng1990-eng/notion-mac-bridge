BEGIN;
ALTER TABLE mcp_tasks ADD COLUMN IF NOT EXISTS timeout_seconds integer NOT NULL DEFAULT 600;
ALTER TABLE mcp_tasks ADD CONSTRAINT mcp_tasks_status_check CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'));
ALTER TABLE mcp_tasks ADD CONSTRAINT mcp_tasks_engine_check CHECK (engine IN ('kimi', 'claude'));
ALTER TABLE mcp_tasks ADD CONSTRAINT mcp_tasks_timeout_check CHECK (timeout_seconds BETWEEN 10 AND 1800);

-- 任务包含本地输出，只通过验证 Bearer 后的服务身份读写。
DROP POLICY "修改全部数据" ON mcp_tasks;
DROP POLICY "查看全部数据" ON mcp_tasks;
DROP POLICY "修改本人数据" ON mcp_tasks;
COMMIT;
