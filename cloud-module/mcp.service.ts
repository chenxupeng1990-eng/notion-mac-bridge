// 无状态 Streamable HTTP MCP。Notion经 /mcp 提交任务，Mac经 /worker 主动拉取。
// 两端分别鉴权，Postgres协调跨实例的任务状态；短任务同步等待，长任务返回task_id。
import { Injectable, Logger } from '@nestjs/common';
import type { Request } from 'express';
import { TasksService, TaskInputError, numberArg, stringArg } from './tasks.service';
import { DirectService } from './direct.service';

const SERVER_NAME = 'notion-mcp-test';
const SERVER_VERSION = '1.2.0';
// 客户端没带 protocolVersion 时的兜底版本
const DEFAULT_PROTOCOL_VERSION = '2025-06-18';

const startedAt = new Date().toISOString();

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === 'object') {
    Object.values(value).forEach(deepFreeze);
    Object.freeze(value);
  }
  return value;
}

interface JsonRpcRequest {
  jsonrpc?: string;
  id?: string | number | null;
  method?: string;
  params?: Record<string, unknown>;
}

@Injectable()
export class McpService {
  private readonly logger = new Logger(McpService.name);
  // Discovery is static across requests and instances; clone/freeze once instead of rebuilding schemas on every call.
  private static readonly tools = deepFreeze(structuredClone(McpService.createToolDefs()));
  constructor(private readonly tasks: TasksService, private readonly direct: DirectService) {}

  /** 按 JSON-RPC 2.0 处理单个请求；notification（无 id）返回 null 表示 202 空响应。 */
  async handle(req: Request, body: JsonRpcRequest, disconnected: () => boolean = () => false): Promise<Record<string, unknown> | null> {
    const { id, method, params } = body ?? ({} as JsonRpcRequest);

    // notification：没有 id 就返回 202 空响应体
    if (id === undefined || id === null) {
      this.logger.log(`notification: ${method}`);
      return null;
    }

    switch (method) {
      case 'initialize':
        return this.ok(id, {
          protocolVersion: this.negotiate(params?.protocolVersion),
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
          instructions:
            '通过 run_task 驱动用户 Mac 上的 Kimi CLI，默认 engine=kimi，模型走magpie的group/notion-cheap路由组。你负责规划与审查，本地 agent 负责执行。' +
            'Notion专用工作区为worker配置的default_workdir。省略workdir时每条任务在tasks/<task_id>/独立执行；长期项目放projects/，交付物放artifacts/。' +
            '每次新任务提供稳定且唯一的 request_id，重试同一任务复用它，避免重复执行。' +
            'run_task 最多同步等20秒；未完成则用 task_status(wait_seconds=120) 等待，默认20秒，完成即返回，尊重 next_check_seconds，' +
            '不要空闲轮询，也不要因为尚在queued/running就重复提交。用户授予执行进程完整本机权限，workdir仅指定当前目录。' +
            '已知命令可直接用 exec，不排队也不调用模型；exec/read_file需要本地bridge与quick tunnel在线，run_task队列独立于该隧道。' +
            '需要大量输出时将完整内容写入文件，只返回路径和简短总结，再用 read_file 按字符分页读取。',
        });

      case 'tools/list':
        return this.ok(id, { tools: McpService.tools });

      case 'tools/call': {
        const name = params?.name as string | undefined;
        const args = (params?.arguments ?? {}) as Record<string, unknown>;
        try {
          return this.ok(id, await this.callTool(name, args, req, disconnected));
        } catch (e) {
          const message = e instanceof TaskInputError ? e.message : 'Tool execution failed; inspect service diagnostics';
          if (!(e instanceof TaskInputError)) this.logger.error('Tool execution failed');
          // 不把数据库异常中的 SQL 或参数回传到 MCP。
          return this.ok(id, { content: [{ type: 'text', text: message }], isError: true });
        }
      }

      case 'ping':
        return this.ok(id, {});

      default:
        return {
          jsonrpc: '2.0',
          id,
          error: { code: -32601, message: `Method not found: ${method}` },
        };
    }
  }

  /** 优先回客户端请求的版本（标准做法：服务端接受客户端版本即可）。 */
  private negotiate(requested: unknown): string {
    return typeof requested === 'string' && requested.length > 0
      ? requested
      : DEFAULT_PROTOCOL_VERSION;
  }

  private ok(id: string | number | null, result: Record<string, unknown>) {
    return { jsonrpc: '2.0', id, result };
  }

  private static createToolDefs() {
    return [
      {
        name: 'exec',
        description: '直接在用户Mac上执行已知shell命令，不排队也不调用模型；需要本地bridge与quick tunnel在线，run_task队列独立于该隧道。cwd只指定当前目录，执行进程拥有用户授予的本机权限。默认超时120秒；max_output_chars限制内联输出，完整输出保存在log_path。大量输出写入文件，只返回路径和简短总结，再用read_file按字符分页读取。可提供稳定request_id用于相同命令的幂等重试；执行状态不明时先检查本地输出。',
        inputSchema: {
          type: 'object', additionalProperties: false,
          properties: {
            cmd: { type: 'string', minLength: 1, maxLength: 50000 },
            cwd: { type: 'string', maxLength: 4096, default: '' },
            timeout_seconds: { type: 'integer', minimum: 1, maximum: 86400, default: 120 },
            max_output_chars: { type: 'integer', minimum: 1, maximum: 1000000, default: 4000 },
            request_id: { type: 'string', minLength: 1, maxLength: 100, description: '同一次执行的稳定ID，最多100字符；省略时由服务端生成UUID' },
          },
          required: ['cmd'],
        },
        annotations: { readOnlyHint: false, destructiveHint: true, openWorldHint: true },
      },
      {
        name: 'read_file',
        description: '直接读取Mac工作区或实际任务目录中的UTF-8文本文件，不排队也不调用模型；需要本地bridge与quick tunnel在线。offset与limit按Unicode字符分页，默认offset=0、limit=4000，最多100000字符；返回content、total_chars、next_offset、eof和sha256。优先读取exec的log_path或task_status的result_path。',
        inputSchema: {
          type: 'object', additionalProperties: false,
          properties: {
            path: { type: 'string', minLength: 1, maxLength: 4096 },
            offset: { type: 'integer', minimum: 0, maximum: Number.MAX_SAFE_INTEGER, default: 0 },
            limit: { type: 'integer', minimum: 1, maximum: 100000, default: 4000 },
          },
          required: ['path'],
        },
        annotations: { readOnlyHint: true, idempotentHint: true },
      },
      {
        name: 'run_task',
        description: '让本地 Mac 的 Kimi CLI 执行任务，模型走magpie的group/notion-cheap路由组。短任务直接返回结果，长任务返回 task_id。每个任务使用唯一request_id，重试时复用；不要重复提交正在排队/运行的任务。workdir只是当前目录，执行进程拥有本机用户权限。大量输出必须写入文件，返回文件路径与简短总结；完整结果可用read_file分页读取。',
        inputSchema: {
          type: 'object', additionalProperties: false,
          properties: {
            prompt: { type: 'string', minLength: 1, maxLength: 50000, description: '执行指令、验收条件和结果汇报要求' },
            request_id: { type: 'string', maxLength: 40, description: '每个任务唯一的幂等ID，例如UUID，重试时保持不变' },
            engine: { type: 'string', enum: ['kimi', 'claude'], default: 'kimi', description: '默认Kimi CLI配合group/notion-cheap模型组；claude使用本地网关额度' },
            workdir: { type: 'string', maxLength: 500, description: '已有绝对目录；省略则自动使用工作区 tasks/<task_id>。长期项目使用该工作区projects下的目录。' },
            wait_seconds: { type: 'integer', minimum: 0, maximum: 20, default: 20 },
            timeout_seconds: { type: 'integer', minimum: 10, maximum: 1800, default: 600 },
          },
          required: ['prompt', 'request_id'],
        },
        annotations: { readOnlyHint: false, destructiveHint: true, openWorldHint: true },
      },
      {
        name: 'task_status',
        description: '查询任务进度或结果，默认等待20秒，最多120秒，任务完成立即返回。仅在已有未完成任务时调用，遵守返回的next_check_seconds；完成后停止查询。result_path/result_chars/result_sha256为完整结果文件元数据，使用read_file按字符分页读取。',
        inputSchema: {
          type: 'object', additionalProperties: false,
          properties: { task_id: { type: 'string' }, wait_seconds: { type: 'integer', minimum: 0, maximum: 120, default: 20 } },
          required: ['task_id'],
        },
        annotations: { readOnlyHint: true },
      },
      {
        name: 'list_tasks', description: '查看最近的任务记录，用于恢复上下文；不用于空闲轮询。',
        inputSchema: { type: 'object', properties: { limit: { type: 'integer', minimum: 1, maximum: 30, default: 10 } }, additionalProperties: false },
        annotations: { readOnlyHint: true },
      },
      {
        name: 'cancel_task', description: '取消排队或运行中的任务。worker下次心跳会终止进程，已发生的文件写入不会回滚。',
        inputSchema: { type: 'object', properties: { task_id: { type: 'string' } }, required: ['task_id'], additionalProperties: false },
        annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
      },
      {
        name: 'ping',
        title: 'Ping',
        description:
          '连通性测试。返回 pong、服务端启动时间和当前调用时间。用户想确认这个 MCP 是否可用、或需要验证 Notion 到飞书妙搭的链路是否打通时调用。',
        inputSchema: { type: 'object', properties: {}, additionalProperties: false },
        annotations: { readOnlyHint: true },
      },
      {
        name: 'echo',
        title: 'Echo',
        description: '回显传入的文本，用于验证工具参数传递是否正确。',
        inputSchema: {
          type: 'object',
          properties: {
            text: { type: 'string', description: '要回显的文本' },
          },
          required: ['text'],
          additionalProperties: false,
        },
        annotations: { readOnlyHint: true },
      },
      {
        name: 'add',
        title: 'Add',
        description: '计算两个数字之和，用于验证数值类型参数。',
        inputSchema: {
          type: 'object',
          properties: {
            a: { type: 'number', description: '第一个加数' },
            b: { type: 'number', description: '第二个加数' },
          },
          required: ['a', 'b'],
          additionalProperties: false,
        },
        annotations: { readOnlyHint: true },
      },
      {
        name: 'server_info',
        title: 'Server Info',
        description:
          '返回服务端部署信息（启动时间、版本、实例）和本次调用的请求头/身份信息，用于排查网关与鉴权是否正常。',
        inputSchema: { type: 'object', properties: {}, additionalProperties: false },
        annotations: { readOnlyHint: true },
      },
    ];
  }

  private text(value: unknown, structured?: Record<string, unknown>) {
    return structured
      ? { content: [{ type: 'text', text: JSON.stringify(structured, null, 2) }], structuredContent: structured }
      : { content: [{ type: 'text', text: String(value) }] };
  }

  private async callTool(
    name: string | undefined,
    args: Record<string, unknown>,
    req: Request,
    disconnected: () => boolean,
  ): Promise<Record<string, unknown>> {
    const now = new Date().toISOString();

    switch (name) {
      case 'exec': {
        const result = await this.direct.exec(args);
        return { ...this.text('', result), isError: result.timed_out === true || (typeof result.code === 'number' && result.code !== 0) };
      }
      case 'read_file':
        return this.text('', await this.direct.readFile(args));
      case 'run_task': {
        stringArg(args, 'request_id', 40);
        const task = await this.tasks.submit(args, disconnected);
        return { ...this.text('', task), isError: task.status === 'failed' };
      }
      case 'task_status': {
        const task = await this.tasks.waitForTask(stringArg(args, 'task_id', 36), numberArg(args, 'wait_seconds', 20, 0, 120), disconnected);
        return { ...this.text('', task), isError: task.status === 'failed' };
      }
      case 'list_tasks':
        return this.text('', { tasks: await this.tasks.list(numberArg(args, 'limit', 10, 1, 30)) });
      case 'cancel_task':
        return this.text('', await this.tasks.cancel(stringArg(args, 'task_id', 36)));
      case 'ping':
        return this.text('pong', {
          pong: true,
          server: SERVER_NAME,
          version: SERVER_VERSION,
          startedAt,
          now,
        });

      case 'echo': {
        if (typeof args.text !== 'string') {
          throw new Error('参数错误：echo 需要字符串参数 text');
        }
        return this.text(args.text, { echo: args.text, now });
      }

      case 'add': {
        const { a, b } = args;
        if (typeof a !== 'number' || typeof b !== 'number') {
          throw new Error('参数错误：add 需要两个数字参数 a 和 b');
        }
        return this.text(String(a + b), { a, b, sum: a + b });
      }

      case 'server_info':
        return this.text('ok', {
          server: SERVER_NAME,
          version: SERVER_VERSION,
          startedAt,
          now,
          instance: process.env.VERF_FUNC_ID ?? process.env._FAAS_FUNC_ID ?? 'unknown',
          // 网关注入的用户身份：有就说明网关鉴权链路通了
          user: (req as Request & { userContext?: unknown }).userContext ?? null,
          method: req.method,
          path: req.path,
          headers: {
            'content-type': req.headers['content-type'] ?? null,
            accept: req.headers.accept ?? null,
            'user-agent': req.headers['user-agent'] ?? null,
            'x-forwarded-proto': req.headers['x-forwarded-proto'] ?? null,
          },
        });

      default:
        throw new Error(`未知工具：${name ?? '(未提供)'}`);
    }
  }
}