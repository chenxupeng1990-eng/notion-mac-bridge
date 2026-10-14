import { Inject, Injectable } from '@nestjs/common';
import { DRIZZLE_DATABASE, type PostgresJsDatabase } from '@lark-apaas/fullstack-nestjs-core';
import { sql } from 'drizzle-orm';
import { randomUUID } from 'node:crypto';
import { posix as macPath } from 'node:path';
import { TaskInputError, numberArg, stringArg, validateBridgeUrl } from './tasks.service';

const transientStatus = (status: number) => [408, 425, 429, 500, 502, 503, 504].includes(status);
// Must match the bridge's NOTION_WORKSPACE_DIR / NOTION_BRIDGE_JOBS_DIR.
// read_file additionally accepts the task's recorded workdir (task_root),
// derived server-side from the database and never from client input.
const localReadRoots: string[] = (process.env.MIAODA_LOCAL_ROOTS ?? '')
  .split(',')
  .map((value) => value.trim())
  .filter(Boolean);
const containsPath = (root: string, target: string) => {
  const relative = macPath.relative(root, target);
  return relative !== '..' && !relative.startsWith('../') && !macPath.isAbsolute(relative);
};

/** Direct RPCs never enter the task queue or invoke a model. */
@Injectable()
export class DirectService {
  constructor(@Inject(DRIZZLE_DATABASE) private readonly db: PostgresJsDatabase) {}

  async exec(args: Record<string, unknown>): Promise<Record<string, unknown>> {
    const cmd = stringArg(args, 'cmd', 50000);
    const cwd = args.cwd ?? '';
    if (typeof cwd !== 'string' || cwd.length > 4096 || cwd.includes('\0')) throw new TaskInputError('cwd must be a directory string');
    const timeout = numberArg(args, 'timeout_seconds', 120, 1, 86400);
    const maxOutput = numberArg(args, 'max_output_chars', 4000, 1, 1000000);
    const requestId = args.request_id == null ? randomUUID() : stringArg(args, 'request_id', 100);
    return this.rpc('exec', { cmd, cwd, timeout_seconds: timeout, max_output_chars: maxOutput }, requestId, timeout + 15);
  }

  async readFile(args: Record<string, unknown>): Promise<Record<string, unknown>> {
    const path = stringArg(args, 'path', 4096);
    if (path.includes('\0') || path.split('/').includes('..')) throw new TaskInputError('path must not contain a null character or parent traversal');
    const offset = numberArg(args, 'offset', 0, 0, Number.MAX_SAFE_INTEGER);
    const limit = numberArg(args, 'limit', 4000, 1, 100000);
    const params: Record<string, unknown> = { path, offset, limit };
    if (macPath.isAbsolute(path) && !localReadRoots.some(root => containsPath(root, path))) {
      // Only recorded task workdirs can extend the Mac's read roots. Never forward a caller-provided root.
      const rows = await this.db.execute<{ workdir: string | null }>(sql`
        SELECT DISTINCT workdir FROM mcp_tasks WHERE workdir IS NOT NULL
      `);
      const roots = rows.map(row => row.workdir)
        .filter((root): root is string => typeof root === 'string' && root.length <= 500 && macPath.isAbsolute(root) &&
          !root.includes('\0') && !root.split('/').includes('..'))
        .map(root => macPath.normalize(root))
        .filter(root => containsPath(root, path))
        .sort((a, b) => b.length - a.length);
      if (roots[0]) params._task_root = roots[0];
    }
    // The bearer-authenticated local endpoint still resolves symlinks and enforces sensitive-path restrictions.
    return this.rpc('read_file', params, randomUUID(), 20);
  }

  private async rpc(method: 'exec' | 'read_file', params: Record<string, unknown>, requestId: string, timeout: number): Promise<Record<string, unknown>> {
    const token = process.env.MCP_WORKER_TOKEN?.trim();
    if (!token) throw new TaskInputError('Direct worker RPC is unavailable: MCP_WORKER_TOKEN is not configured');
    const rows = await this.db.execute<{ bridge_url: string }>(sql`
      SELECT bridge_url FROM mcp_workers
      WHERE last_seen > CURRENT_TIMESTAMP - INTERVAL '120 seconds' AND bridge_url IS NOT NULL
      ORDER BY last_seen DESC LIMIT 1
    `);
    if (!rows[0]?.bridge_url) {
      throw new TaskInputError('Mac worker is offline: no bridge registered in the last 120 seconds. Start the worker and quick tunnel, then wait for its next claim or heartbeat.');
    }
    const url = validateBridgeUrl(rows[0].bridge_url);
    const body = JSON.stringify({ method, params, request_id: requestId });
    const attempts = method === 'read_file' ? 2 : 1;
    for (let attempt = 0; attempt < attempts; attempt++) {
      let response: Response;
      try {
        response = await globalThis.fetch(url, {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
          body,
          // A registry entry cannot redirect the worker credential to another host.
          redirect: 'manual',
          signal: AbortSignal.timeout(timeout * 1000),
        });
      } catch {
        if (attempt + 1 < attempts) continue;
        throw new TaskInputError(method === 'exec'
          ? 'Mac exec connection failed or timed out; execution state is unknown. Inspect local output or retry with the same request_id; the cloud did not retry the command.'
          : 'Could not read the file from the Mac worker; check that its quick tunnel is online.');
      }
      if (!response.ok) {
        await response.body?.cancel();
        if (transientStatus(response.status) && attempt + 1 < attempts) continue;
        throw new TaskInputError(`Mac worker ${method} returned HTTP ${response.status}${method === 'exec' ? '; execution state may be unknown. The cloud did not retry the command' : ''}`);
      }
      let result: unknown;
      try {
        result = await response.json();
      } catch {
        if (attempt + 1 < attempts) continue;
        throw new TaskInputError(`Mac worker returned invalid ${method} JSON${method === 'exec' ? '; inspect local output before retrying with the same request_id' : ''}`);
      }
      if (!result || typeof result !== 'object' || Array.isArray(result)) throw new TaskInputError('Mac worker returned an invalid RPC result');
      return result as Record<string, unknown>;
    }
    throw new TaskInputError('Mac worker RPC unavailable');
  }
}
