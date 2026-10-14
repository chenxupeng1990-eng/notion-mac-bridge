CREATE TABLE IF NOT EXISTS mcp_tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_no varchar(40) NOT NULL UNIQUE,
  prompt text NOT NULL,
  engine varchar(20) NOT NULL DEFAULT 'kimi',
  workdir varchar(500),
  status varchar(20) NOT NULL DEFAULT 'queued',
  claimed_by varchar(100),
  claimed_at TIMESTAMP(3) WITH TIME ZONE,
  heartbeat_at TIMESTAMP(3) WITH TIME ZONE,
  result text,
  error text,
  exit_code integer,
  duration_ms integer,
  submitted_by varchar(64),
  _created_at TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  _created_by user_profile DEFAULT (
    CASE
      WHEN current_setting('app.user_id', TRUE) = '' THEN NULL
      ELSE concat('(', current_setting('app.user_id', TRUE), ')')::user_profile
    END
  ),
  _updated_at TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  _updated_by user_profile DEFAULT (
    CASE
      WHEN current_setting('app.user_id', TRUE) = '' THEN NULL
      ELSE concat('(', current_setting('app.user_id', TRUE), ')')::user_profile
    END
  )
);

CREATE INDEX IF NOT EXISTS idx_mcp_tasks_status_created ON mcp_tasks (status, _created_at);

ALTER TABLE mcp_tasks ENABLE ROW LEVEL SECURITY;

CREATE POLICY service_role_bypass_policy ON mcp_tasks
  TO service_role USING (true);

CREATE POLICY "修改全部数据" ON mcp_tasks
  AS PERMISSIVE FOR ALL TO authenticated USING (true);

CREATE POLICY "查看全部数据" ON mcp_tasks
  AS PERMISSIVE FOR SELECT TO authenticated, anon USING (true);

CREATE POLICY "修改本人数据" ON mcp_tasks
  AS PERMISSIVE FOR ALL TO authenticated USING (
    (current_setting('app.user_id'::text) = ANY (ARRAY[]::text[]))
    AND (current_setting('app.user_id'::text) = ((_created_by).user_id)::text)
  );
