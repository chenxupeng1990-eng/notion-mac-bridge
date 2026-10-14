import { Module } from '@nestjs/common';

import { McpController } from './mcp.controller';
import { McpService } from './mcp.service';
import { TasksService } from './tasks.service';
import { WorkerController } from './worker.controller';
import { DirectService } from './direct.service';

@Module({
  controllers: [McpController, WorkerController],
  providers: [McpService, TasksService, DirectService],
  exports: [McpService],
})
export class McpModule {}