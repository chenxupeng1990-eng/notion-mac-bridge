BEGIN;
ALTER TABLE mcp_tasks ADD COLUMN IF NOT EXISTS result_path text;
ALTER TABLE mcp_tasks ADD COLUMN IF NOT EXISTS result_chars integer;
ALTER TABLE mcp_tasks ADD COLUMN IF NOT EXISTS result_sha256 varchar(64);

CREATE TABLE IF NOT EXISTS mcp_workers (
  id varchar(100) PRIMARY KEY,
  bridge_url text,
  last_seen TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  _created_at TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  _created_by user_profile,
  _updated_at TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  _updated_by user_profile
);
ALTER TABLE mcp_workers ENABLE ROW LEVEL SECURITY;
CREATE POLICY service_role_bypass_policy ON mcp_workers TO service_role USING (true);
CREATE POLICY "修改全部数据" ON mcp_workers AS PERMISSIVE FOR ALL TO authenticated USING (true);
CREATE POLICY "查看全部数据" ON mcp_workers AS PERMISSIVE FOR SELECT TO authenticated, anon USING (true);
CREATE POLICY "修改本人数据" ON mcp_workers AS PERMISSIVE FOR ALL TO authenticated USING (
  current_setting('app.user_id', TRUE) = ((_created_by).user_id)::text
);
DROP POLICY "修改全部数据" ON mcp_workers;
DROP POLICY "查看全部数据" ON mcp_workers;
DROP POLICY "修改本人数据" ON mcp_workers;
COMMIT;
