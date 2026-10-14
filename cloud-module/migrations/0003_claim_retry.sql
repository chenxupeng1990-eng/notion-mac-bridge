CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_tasks_worker_running
  ON mcp_tasks (claimed_by) WHERE status = 'running' AND claimed_by IS NOT NULL;
