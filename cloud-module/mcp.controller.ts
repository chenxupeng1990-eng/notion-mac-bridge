import { Body, Controller, Get, HttpCode, Options, Post, Req, Res } from '@nestjs/common';
import type { Request, Response } from 'express';

import { McpService } from './mcp.service';
import { isAuthorized } from './auth';

/**
 * 极简 MCP Streamable HTTP 端点：POST /mcp
 *
 * 为什么挂在 /mcp 而不是 /openapi/mcp：
 * 妙搭网关只对 /openapi/** 前缀强制 API Key 鉴权，且该前缀下的 OPTIONS 预检
 * 也要求携带 Authorization 头——而浏览器的 CORS 预检不会带自定义头，
 * Notion 这类浏览器内 MCP 客户端会直接被 403 挡掉。普通路径由网关透传到本服务，
 * 所以鉴权在这里自己做（见 ./auth.ts，token 走环境变量 MCP_AUTH_TOKEN）。
 *
 * - 无状态（不分配 session id），每个请求独立处理，标准 MCP 客户端可直接连。
 * - 普通请求回 application/json；长工具调用以每请求SSE保活并返回同一JSON-RPC结果。
 * - 通知类请求（无 JSON-RPC id）回 202 空体。
 */
@Controller('mcp')
export class McpController {
  constructor(private readonly mcpService: McpService) {}

  @Post()
  @HttpCode(200)
  async handle(@Body() body: unknown, @Req() req: Request, @Res() res: Response): Promise<void> {
    this.setCors(res, req);

    if (!isAuthorized(req)) {
      res.setHeader('WWW-Authenticate', 'Bearer realm="notion-mcp-test"');
      res.status(401).json({
        jsonrpc: '2.0',
        id: (body as { id?: string | number | null })?.id ?? null,
        error: { code: -32001, message: 'Unauthorized: missing or invalid bearer token' },
      });
      return;
    }

    const request = body as Record<string, unknown>;
    if (!this.needsStream(request)) {
      const result = await this.mcpService.handle(req, request, () => res.destroyed);
      if (res.destroyed) return;
      if (!result) {
        res.status(202).end();
        return;
      }
      res.status(200).json(result);
      return;
    }

    res.status(200);
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache, no-transform');
    res.setHeader('X-Accel-Buffering', 'no');
    res.flushHeaders();
    const write = (chunk: string) => {
      if (res.destroyed || res.writableEnded) return;
      res.write(chunk);
      // Compression middleware exposes flush(); headers alone do not flush its buffered body.
      (res as Response & { flush?: () => void }).flush?.();
    };
    write(': connected\n\n');
    const keepAlive = setInterval(() => write(': keep-alive\n\n'), 15000);
    keepAlive.unref();
    const cleanup = () => clearInterval(keepAlive);
    res.once('close', cleanup);
    try {
      const result = await this.mcpService.handle(req, request, () => res.destroyed);
      if (result && !res.destroyed) write(`event: message\ndata: ${JSON.stringify(result)}\n\n`);
    } finally {
      cleanup();
      res.off('close', cleanup);
      if (!res.destroyed && !res.writableEnded) res.end();
    }
  }

  /** GET 不提供 SSE 流（无状态模式），按规范回 405 并声明 Allow。 */
  @Get()
  @HttpCode(405)
  async notSupported(@Req() req: Request, @Res() res: Response): Promise<void> {
    this.setCors(res, req);
    res.setHeader('Allow', 'POST');
    res.status(405).json({
      jsonrpc: '2.0',
      error: { code: -32000, message: 'Method Not Allowed: this MCP endpoint is POST-only' },
    });
  }

  /** 浏览器内 MCP 客户端的跨域预检：不校验 token（预检本来就不带 Authorization） */
  @Options()
  @HttpCode(204)
  preflight(@Req() req: Request, @Res() res: Response): void {
    this.setCors(res, req);
    res.setHeader('Access-Control-Max-Age', '86400');
    res.status(204).end();
  }

  private needsStream(body: Record<string, unknown> | null | undefined): boolean {
    if (body?.method !== 'tools/call' || body.id == null) return false;
    const params = body.params as { name?: unknown; arguments?: Record<string, unknown> } | undefined;
    const args = params?.arguments;
    const wait = args?.wait_seconds ?? 20;
    const timeout = args?.timeout_seconds ?? 120;
    return (params?.name === 'task_status' && typeof wait === 'number' && wait > 20) ||
      (params?.name === 'exec' && typeof timeout === 'number' && timeout > 20);
  }

  private setCors(res: Response, req: Request) {
    res.setHeader('Access-Control-Allow-Origin', req.headers.origin ?? '*');
    res.setHeader('Vary', 'Origin');
    res.setHeader('Access-Control-Allow-Methods', 'POST, GET, OPTIONS');
    res.setHeader(
      'Access-Control-Allow-Headers',
      'Content-Type, Authorization, mcp-session-id, mcp-protocol-version, last-event-id',
    );
    res.setHeader('Access-Control-Expose-Headers', 'mcp-session-id, mcp-protocol-version');
  }
}