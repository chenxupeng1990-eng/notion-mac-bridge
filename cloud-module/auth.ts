import { timingSafeEqual } from 'node:crypto';
import type { Request, Response, NextFunction } from 'express';

function safeEqual(a: string, b: string): boolean {
  const left = Buffer.from(a, 'utf8');
  const right = Buffer.from(b, 'utf8');
  return left.length === right.length && timingSafeEqual(left, right);
}

export function isAuthorized(req: Request, key = 'MCP_AUTH_TOKEN'): boolean {
  const expected = (process.env[key] ?? '').trim();
  const header = req.headers.authorization;
  if (!expected || typeof header !== 'string') return false;
  const match = /^Bearer\s+(.+)$/i.exec(header.trim());
  return !!match && safeEqual(match[1].trim(), expected);
}

// 在平台 SQL 上下文中间件前验密钥，再绑定服务身份。
// 浏览器 MCP 与 worker 使用独立 token；未认证者不会获得数据库角色。
export function machineIdentity(req: Request, res: Response, next: NextFunction): void {
  const prefix = (process.env.CLIENT_BASE_PATH ?? '').replace(/\/+$/, '').toLowerCase();
  const pathname = req.path.toLowerCase();
  const path = (prefix && pathname.startsWith(prefix + '/') ? pathname.slice(prefix.length) : pathname).replace(/\/+$/, '');
  const key = path === '/mcp' ? 'MCP_AUTH_TOKEN' : path.startsWith('/worker/') ? 'MCP_WORKER_TOKEN' : null;
  if (!key || req.method === 'OPTIONS') return next();
  if (!isAuthorized(req, key)) {
    if (path === '/mcp') {
      res.setHeader('Access-Control-Allow-Origin', '*');
      res.setHeader('Access-Control-Expose-Headers', 'WWW-Authenticate');
    }
    res.setHeader('WWW-Authenticate', 'Bearer realm="notion-mcp"');
    res.status(401).json({ error: 'Missing or invalid bearer token' });
    return;
  }
  // UserContextMiddleware 读这个可信身份，再由 SqlExecutionContextMiddleware 使用。
  req.headers['x-larkgw-suda-webuser'] = JSON.stringify({
    user_id: '-1', is_system_account: true, app_id: process.env.SUDA_APP_ID,
    env: 'runtime',
  });
  next();
}
