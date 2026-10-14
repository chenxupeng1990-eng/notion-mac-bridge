import { Inject, Injectable } from '@nestjs/common';
import { DRIZZLE_DATABASE, type PostgresJsDatabase } from '@lark-apaas/fullstack-nestjs-core';
import { sql } from 'drizzle-orm';
import { randomUUID } from 'node:crypto';
import { posix as path } from 'node:path';

export class TaskInputError extends Error {}

/** A worker may advertise only its HTTPS RPC endpoint on an allow-listed domain. Never follow redirects.
 * MIAODA_BRIDGE_HOST_SUFFIXES: comma-separated, e.g. "trycloudflare.com, Tunnel on your domain" —
 * quick tunnels (*.trycloudflare.com) need nothing; a named tunnel on your own domain works too. */
export function validateBridgeUrl(value: unknown): string {
  const suffixes = (process.env.MIAODA_BRIDGE_HOST_SUFFIXES ?? 'trycloudflare.com')
    .split(',').map((s) => s.trim().toLowerCase()).filter(Boolean);
  if (typeof value !== 'string' || value.length > 2048 || /\s/.test(value)) {
    throw new TaskInputError('bridge_url must be a plain HTTPS URL');
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new TaskInputError('Invalid bridge_url');
  }
  if (url.protocol !== 'https:' || url.username || url.password || url.port || url.search || url.hash || url.pathname !== '/bridge/rpc') {
    throw new TaskInputError('Invalid bridge_url');
  }
  const host = url.hostname.toLowerCase();
  const ok = suffixes.some((suffix) => host === suffix || host.endsWith('.' + suffix));
  if (!ok) {
    throw new TaskInputError(`bridge_url host must be under one of: ${suffixes.join(', ')}`);
  }
  return url.href;
}

export type TaskStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
export interface TaskRow extends Record<string, unknown> {
  id: string;
  task_no: string;
  prompt: string;
  engine: 'kimi' | 'claude';
  workdir: string | null;
  timeout_seconds: number;
  status: TaskStatus;
  claimed_by: string | null;
  result: string | null;
  result_path: string | null;
  result_chars: number | null;
  result_sha256: string | null;
  error: string | null;
  exit_code: number | null;
  duration_ms: number | null;
  _created_at: string | Date;
  _updated_at: string | Date;
}
const terminal = (status: TaskStatus) => ['succeeded', 'failed', 'cancelled'].includes(status);
const pause = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export function stringArg(args: Record<string, unknown>, key: string, max: number): string {
  const value = args[key];
  if (typeof value !== 'string' || !value.trim() || value.length > max) {
    throw new TaskInputError(`${key} must be a nonempty string with at most ${max} characters`);
  }
  return value;
}
export function numberArg(args: Record<string, unknown>, key: string, fallback: number, min: number, max: number): number {
  const value = args[key] ?? fallback;
  if (typeof value !== 'number' || !Number.isInteger(value) || value < min || value > max) {
    throw new TaskInputError(`${key} must be an integer between ${min} and ${max}`);
  }
  return value;
}
function taskId(value: string) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) {
    throw new TaskInputError('Invalid task_id');
  }
  return value;
}

@Injectable()
export class TasksService {
  constructor(@Inject(DRIZZLE_DATABASE) private readonly db: PostgresJsDatabase) {}

  publicTask(row: TaskRow) {
    return {
      task_id: row.id, request_id: row.task_no, engine: row.engine, workdir: row.workdir,
      timeout_seconds: row.timeout_seconds, status: row.status,
      result: row.result, error: row.error, exit_code: row.exit_code, duration_ms: row.duration_ms,
      result_path: row.result_path ?? null, result_chars: row.result_chars ?? null, result_sha256: row.result_sha256 ?? null,
      created_at: row._created_at, updated_at: row._updated_at,
      ...(terminal(row.status) ? {} : { next_check_seconds: row.status === 'queued' ? 45 : 20 }),
    };
  }

  // 失联任务标记失败，不重入队列：未知副作用的命令不能自动执行第二遍。
  private async expireLostTasks() {
    await this.db.execute(sql`
      UPDATE mcp_tasks SET status = 'failed', error = 'Worker heartbeat lost; execution state unknown. Inspect local artifacts before resubmitting.',
        _updated_at = CURRENT_TIMESTAMP
      WHERE status = 'running' AND heartbeat_at < CURRENT_TIMESTAMP - INTERVAL '90 seconds'
    `);
  }

  async submit(args: Record<string, unknown>, disconnected: () => boolean = () => false) {
    const prompt = stringArg(args, 'prompt', 50000);
    const engine = args.engine ?? 'kimi';
    if (engine !== 'kimi' && engine !== 'claude') throw new TaskInputError('engine must be kimi or claude');
    const workdir = args.workdir == null ? null : stringArg(args, 'workdir', 500);
    if (workdir && (!workdir.startsWith('/') || workdir.includes('\0'))) throw new TaskInputError('workdir must be an absolute Mac directory');
    const timeout = numberArg(args, 'timeout_seconds', 600, 10, 1800);
    const requestId = args.request_id == null ? randomUUID() : stringArg(args, 'request_id', 40);
    const wait = numberArg(args, 'wait_seconds', 20, 0, 20);
    const id = randomUUID();
    await this.db.execute(sql`
      INSERT INTO mcp_tasks (id, task_no, prompt, engine, workdir, timeout_seconds, submitted_by)
      VALUES (${id}::uuid, ${requestId}, ${prompt}, ${engine}, ${workdir}, ${timeout}, 'notion-mcp')
      ON CONFLICT (task_no) DO NOTHING
    `);
    const rows = await this.db.execute<TaskRow>(sql`SELECT * FROM mcp_tasks WHERE task_no = ${requestId}`);
    const row = rows[0];
    if (!row) throw new TaskInputError('Task could not be created');
    if (row.prompt !== prompt || row.engine !== engine || row.workdir !== workdir || row.timeout_seconds !== timeout) {
      throw new TaskInputError('request_id already belongs to a different task; reuse it only for identical retries');
    }
    return this.waitForTask(row.id, wait, disconnected);
  }

  async waitForTask(id: string, seconds: number, disconnected: () => boolean = () => false) {
    taskId(id);
    const until = Date.now() + seconds * 1000;
    const checkConnection = () => {
      if (disconnected()) throw new TaskInputError('Request disconnected; the task continues running');
    };
    checkConnection();
    await this.expireLostTasks();
    for (;;) {
      checkConnection();
      const rows = await this.db.execute<TaskRow>(sql`SELECT * FROM mcp_tasks WHERE id = ${id}::uuid`);
      const row = rows[0];
      if (!row) throw new TaskInputError('Task not found');
      if (terminal(row.status) || Date.now() >= until) return this.publicTask(row);
      await pause(Math.min(1200, Math.max(0, until - Date.now())));
    }
  }

  async list(limit: number) {
    await this.expireLostTasks();
    const rows = await this.db.execute<TaskRow>(sql`SELECT * FROM mcp_tasks ORDER BY _created_at DESC LIMIT ${limit}`);
    return rows.map((row) => ({ ...this.publicTask(row), result: row.result ? '[use task_status to read result]' : null }));
  }

  async cancel(id: string) {
    await this.db.execute(sql`
      UPDATE mcp_tasks SET status = 'cancelled', _updated_at = CURRENT_TIMESTAMP
      WHERE id = ${taskId(id)}::uuid AND status IN ('queued', 'running')
    `);
    return this.waitForTask(id, 0);
  }

  async registerWorker(workerId: string, bridgeUrl?: unknown) {
    stringArg({ worker_id: workerId }, 'worker_id', 100);
    const url = bridgeUrl == null ? null : validateBridgeUrl(bridgeUrl);
    await this.db.execute(sql`
      INSERT INTO mcp_workers (id, bridge_url, last_seen)
      VALUES (${workerId}, ${url}, CURRENT_TIMESTAMP)
      ON CONFLICT (id) DO UPDATE SET
        bridge_url = COALESCE(EXCLUDED.bridge_url, mcp_workers.bridge_url),
        last_seen = CURRENT_TIMESTAMP, _updated_at = CURRENT_TIMESTAMP
    `);
  }

  async claim(workerId: string, seconds: number, disconnected: () => boolean, bridgeUrl?: unknown) {
    const until = Date.now() + seconds * 1000;
    await this.registerWorker(workerId, bridgeUrl);
    await this.expireLostTasks();
    do {
      if (disconnected()) return { task: null };
      // 领取响应丢失时返回同一条已领取任务，不能顺手领取第二条。
      let rows = await this.db.execute<TaskRow>(sql`
        SELECT * FROM mcp_tasks WHERE status = 'running' AND claimed_by = ${workerId} LIMIT 1
      `);
      if (!rows[0]) {
        // 行锁 + SKIP LOCKED + UPDATE 在同一个事务语句，跨实例也只领取一次。
        rows = await this.db.execute<TaskRow>(sql`
          WITH candidate AS (
            SELECT id FROM mcp_tasks WHERE status = 'queued'
            ORDER BY _created_at, id FOR UPDATE SKIP LOCKED LIMIT 1
          )
          UPDATE mcp_tasks AS task SET status = 'running', claimed_by = ${workerId},
            claimed_at = CURRENT_TIMESTAMP, heartbeat_at = CURRENT_TIMESTAMP, _updated_at = CURRENT_TIMESTAMP
          FROM candidate WHERE task.id = candidate.id AND task.status = 'queued'
          RETURNING task.*
        `);
      }
      if (rows[0]) {
        const row = rows[0];
        return { task: {
          task_id: row.id, prompt: row.prompt, engine: row.engine, workdir: row.workdir,
          timeout_seconds: row.timeout_seconds,
        } };
      }
      if (Date.now() >= until) break;
      await pause(Math.min(2000, Math.max(0, until - Date.now())));
    } while (Date.now() < until);
    return { task: null };
  }

  async heartbeat(workerId: string, id: string, bridgeUrl?: unknown) {
    taskId(id);
    await this.registerWorker(workerId, bridgeUrl);
    const rows = await this.db.execute<TaskRow>(sql`
      UPDATE mcp_tasks SET heartbeat_at = CURRENT_TIMESTAMP, _updated_at = CURRENT_TIMESTAMP
      WHERE id = ${taskId(id)}::uuid AND claimed_by = ${workerId} RETURNING *
    `);
    if (!rows[0]) throw new TaskInputError('Task not assigned to this worker');
    return { task_id: id, status: rows[0].status };
  }

  async report(workerId: string, args: Record<string, unknown>) {
    const id = taskId(stringArg(args, 'task_id', 36));
    const status = args.status;
    if (status !== 'succeeded' && status !== 'failed') throw new TaskInputError('Invalid completion status');
    const result = args.result ?? '';
    const error = args.error ?? null;
    if (typeof result !== 'string' || Buffer.byteLength(result, 'utf8') > 180000) throw new TaskInputError('Result too large');
    if (error !== null && (typeof error !== 'string' || error.length > 10000)) throw new TaskInputError('Invalid error');
    const exitCode = numberArg(args, 'exit_code', -1, -1000, 255);
    const duration = numberArg(args, 'duration_ms', 0, 0, 86400000);
    const resultChars = args.result_chars == null ? null : numberArg(args, 'result_chars', 0, 0, 10000000);
    const resultSha256 = args.result_sha256 == null ? null : args.result_sha256;
    if (resultSha256 !== null && (typeof resultSha256 !== 'string' || resultSha256.length !== 64 || !/^[a-f0-9]{64}$/i.test(resultSha256))) {
      throw new TaskInputError('result_sha256 must be a 64-character hexadecimal SHA-256');
    }
    let resultPath: string | null = null;
    if (args.result_path != null) {
      resultPath = stringArg(args, 'result_path', 4096);
      if (!path.isAbsolute(resultPath) || resultPath.includes('\0') || resultPath.split('/').includes('..')) {
        throw new TaskInputError('result_path must be an absolute Mac path without null characters or parent traversal');
      }
      resultPath = path.normalize(resultPath);
      const assigned = await this.db.execute<TaskRow>(sql`SELECT * FROM mcp_tasks WHERE id = ${id}::uuid AND claimed_by = ${workerId}`);
      if (!assigned[0]) throw new TaskInputError('Task not assigned to this worker');
      const roots = [...((process.env.MIAODA_LOCAL_ROOTS ?? '').split(',').map((v) => v.trim()).filter(Boolean)), assigned[0].workdir]
        .filter((root): root is string => typeof root === 'string' && path.isAbsolute(root) && !root.includes('\0'));
      if (!roots.some((root) => {
        const relative = path.relative(path.normalize(root), resultPath!);
        return relative !== '' && relative !== '..' && !relative.startsWith('../') && !path.isAbsolute(relative);
      })) throw new TaskInputError('result_path must be inside the Notion workspace or the task workdir');
    }
    // New workers persist full output on the Mac and report only a short summary.
    // Legacy workers can still send the original <=180 KB inline result; do not invent an artifact for it.
    if ((resultPath !== null || resultChars !== null || resultSha256 !== null) && Array.from(result).length > 8000) {
      throw new TaskInputError('Artifact reports must keep result to at most 8000 characters; return the saved file path and a summary');
    }
    await this.db.execute(sql`
      UPDATE mcp_tasks SET status = ${status}, result = ${result}, error = ${error}, exit_code = ${exitCode},
        duration_ms = ${duration}, result_path = ${resultPath}, result_chars = ${resultChars}, result_sha256 = ${resultSha256},
        heartbeat_at = CURRENT_TIMESTAMP, _updated_at = CURRENT_TIMESTAMP
      WHERE id = ${id}::uuid AND claimed_by = ${workerId} AND status = 'running'
    `);
    const rows = await this.db.execute<TaskRow>(sql`SELECT * FROM mcp_tasks WHERE id = ${id}::uuid AND claimed_by = ${workerId}`);
    if (!rows[0]) throw new TaskInputError('Task not assigned to this worker');
    // 重试报告不会覆盖终态，取消任务也不会被晚到的成功报告改回成功。
    return { task_id: id, status: rows[0].status };
  }
}
