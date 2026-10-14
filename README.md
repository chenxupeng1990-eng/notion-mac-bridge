# notion-mac-bridge

> Let **Notion's cloud Claude agent** (using your Notion subscription quota, not API tokens) plan, review and drive **your own Mac** through two MCP channels — a direct local bridge and a durable cloud queue — while heavy execution runs on **cheap local models behind your own gateway**.

让 Notion 云端 Claude（用 Notion 自己的额度）作为规划者/审查者，通过两条 MCP 通道操作你自己的 Mac；本地执行交给你选择的 CLI 和模型服务。方法论见 [docs/TUTORIAL.md](docs/TUTORIAL.md)。

**Kimi 和 Magpie 是实例配置。** 可以沿用 Claude Code，也可以写适配脚本接自己的 CLI；当前 worker 内置 `kimi`/`claude` 两种调用协议，替换其他 CLI 时需转换参数、prompt 输入和结果输出。具体见 [CLI 适配指南](docs/CLI-ADAPTERS.md)。代码及部署脚本目前在 macOS 上验收，Windows 需另行移植。

```
 English TL;DR
 ─────────────────────────────────────────────────────────────
 You have:  Notion (cloud Claude agent, fixed subscription)
            a Mac you control, a home network, maybe a cheap-model gateway
 Problem:   cloud agents can't touch your machine; local agents eat API quota
 Answer:    two MCP endpoints + a pull-worker:
            ① "mac local"  – cloudflared tunnel → MCP server on 127.0.0.1
                             12 tools: exec/fs_*/view_image/render/delegate…
            ② "cloud queue" – fixed public URL (e.g. Feishu Miaoda NestJS)
                             Postgres job queue + resident Mac worker
                             survives every tunnel restart
 Cost:      planning on Notion quota, execution on cheap local routes
 Security:  bearer tokens + path allow-lists + no redirects + launchd
 Status:    runs daily since 2026-10. See docs/TUTORIAL.md for the full recipe.
```

## 为什么是这个架构（方法论摘要）

| 决策 | 原因 |
|---|---|
| **云端只做规划/审查，本地做执行** | 规划与审查使用 Notion agent 额度；本地 CLI 按读者已有的登录、订阅或 API/网关配置执行。廉价模型路由是可选的成本策略。 |
| **两条 MCP 通道并存** | ①本地桥接（cloudflared quick tunnel）延迟低、工具全，但重启换域名；②云端队列（妙搭固定 URL + Mac 常驻 worker 拉取）永远可用，隧道挂了也能排队执行。 |
| **worker 主动外连拉任务** | 家庭网络没有公网入口；出站长连接穿过 NAT/代理，比任何入站穿透都稳。 |
| **输出治理：head+tail 截断 + 完整日志落盘 + 分页读** | agent 上下文是稀缺资源。exec 预览只留首尾，全量在 `tasks/exec-logs/<job>.log`，用 `read_file` 按需分页；>8000 字符任务结果自动写 `result.md`。 |
| **向后兼容演进** | 工具名和已有参数永不改动，只做加参数、加返回字段的增强——Notion 端已配置的连接不会因升级而断。 |
| **launchd 常驻 + 恢复手册** | 三个服务（bridge / tunnel / worker）开机自启、崩溃自拉；quick tunnel 429 时 ThrottleInterval=120。 |

## 仓库结构

```
bridge/          ①本地 MCP 桥（Python 3.12, mcp 2.3 MCPServer, uvicorn）
                 12 个工具 + /bridge/rpc 免队列直通 RPC（供②的 exec/read_file 用）
worker/          Mac 常驻拉取器（纯标准库 Python）：claim/heartbeat/result、
                 大结果落盘、tokens 脱敏、cloudflared quick-tunnel 自发现
cloud-module/    ②妙搭(NestJS)云端 MCP 服务源码模块 + SQL 迁移：
                 固定公网 URL、Postgres 任务队列、SSE 保活、双 token 鉴权
deploy/          launchd plist 模板、.env 样例
docs/            教程(方法论)、故障恢复手册、Notion 端提示词
```

依赖概览（完整清单见各目录与教程）：

- **bridge**: `uv` · python≥3.12 · `mcp[cli]≥2.3`（含 mcp-types/starlette/uvicorn）· `pillow` · `httpx`
- **worker**: Python≥3.10 标准库 · 本地 CLI 执行器（内置 Kimi/Claude Code 协议，其他 CLI 通过适配脚本接入；只使用 bridge 的基础工具时无需 CLI）
- **cloud-module**: Node≥20 · NestJS 10 · `@lark-apaas/fullstack-nestjs-core`（飞书妙搭平台模板自带）· Postgres（平台内置）
- **通道**: `cloudflared`（brew）· 可选 Google Chrome（render 工具）· 可选 ffmpeg-full（含 drawtext）

## 快速上手

```bash
# 1) 本地桥（Mac）
cd bridge
cp config.json.example config.json                 # 改 allowed_roots / workspace_dir
printf 'NOTION_BRIDGE_TOKEN=%s\n' "$(openssl rand -hex 32)" > .env
uv sync

# 2) 一条命令起桥+隧道（或只跑 `python -m notion_bridge.server`，隧道交给 launchd）
uv run python scripts/run.py --tunnel              # 打印 https://<随机>.trycloudflare.com/mcp

# 3) 接 Notion
# Notion → Settings → Connections → 新建 MCP：
#   URL    = https://<随机>.trycloudflare.com/mcp
#   Header = Authorization: Bearer <NOTION_BRIDGE_TOKEN>
```

云端队列、launchd 常驻、worker、验收脚本、以及**两次隧道故障的完整排查记录**，都在 **[docs/TUTORIAL.md](docs/TUTORIAL.md)**。

## 安全边界（先读这个）

这不是沙箱。`exec`/`run_task` 以**你本人权限**运行；唯一的闸门是 bearer token 与隧道：

- 每条通道独立 token（bridge / MCP / worker 三套），只存 `.env`，永不进 git；本仓库所有 `.env*`、日志、任务状态目录都在 `.gitignore`。
- 文件写入限工作区内：拒绝 `..`、拒绝符号链接逃逸、`.env`/`.git`/`audit.log` 一律不可读不可写。
- 云端→本地 RPC 校验 `bridge_url` host 白名单且**永不跟随重定向**；worker 上报的结果先做 token 脱敏。
- 公网 URL 本身就是秘密的一部分：quick tunnel 域名重启即换，等于定期失效。

## 许可与免责

MIT（见 [LICENSE](LICENSE)）。本项目按"我给自己搭的工作流"开源，未做任何面向公众的安全加固；请把它当作方法论参考，按自己的威胁模型审查后再用。
