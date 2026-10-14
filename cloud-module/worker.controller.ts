import { Body, Controller, HttpCode, Post, Req, Res } from '@nestjs/common';
import type { Request, Response } from 'express';
import { isAuthorized } from './auth';
import { TasksService, numberArg, stringArg } from './tasks.service';

@Controller('worker')
export class WorkerController {
  constructor(private readonly tasks: TasksService) {}

  @Post('claim')
  @HttpCode(200)
  async claim(@Body() body: Record<string, unknown>, @Req() req: Request, @Res() res: Response) {
    await this.respond(req, res, async () => this.tasks.claim(
      stringArg(body, 'worker_id', 100), numberArg(body, 'wait_seconds', 15, 0, 15), () => res.destroyed, body.bridge_url,
    ));
  }

  @Post('heartbeat')
  @HttpCode(200)
  async heartbeat(@Body() body: Record<string, unknown>, @Req() req: Request, @Res() res: Response) {
    await this.respond(req, res, async () => this.tasks.heartbeat(
      stringArg(body, 'worker_id', 100), stringArg(body, 'task_id', 36), body.bridge_url,
    ));
  }

  @Post('result')
  @HttpCode(200)
  async result(@Body() body: Record<string, unknown>, @Req() req: Request, @Res() res: Response) {
    await this.respond(req, res, async () => this.tasks.report(stringArg(body, 'worker_id', 100), body));
  }

  private async respond(req: Request, res: Response, action: () => Promise<unknown>) {
    if (!isAuthorized(req, 'MCP_WORKER_TOKEN')) {
      res.status(401).json({ error: 'Invalid worker token' });
      return;
    }
    try {
      res.setHeader('Cache-Control', 'no-store');
      res.json(await action());
    } catch {
      // 不回显 SQL、提示词或密钥。
      res.status(400).json({ error: 'Invalid worker request or task state' });
    }
  }
}
