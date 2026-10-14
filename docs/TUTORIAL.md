# 教程：Notion 云端 Claude ↔ 本地 Mac 双通道 MCP（方法论与部署）

本文是这套系统从零到日常可用的完整记录：每一步**为什么这么做**、**怎么验证**、**踩过什么坑**。代码在仓库三个目录里各自可独立运行；按顺序读下去即可复现。

- 目标读者：有一台常开 Mac、用 Notion（含其订阅内 Claude agent 额度）、想让 AI 真正操作自己电脑的人。
- 阅读前提：会用终端；了解 HTTP 与 bearer token 概念即可，不需要后端经验。

---

## 0. 全景图

```
┌──────────────── Notion 云端（规划/审查，用订阅额度） ────────────────┐
│   Claude agent，两个自定义 MCP 连接：                                │
│   ①  "mac本地"   → https://<随机域名>.trycloudflare.com/mcp          │
│   ②  "妙答"      → https://<你的app>.feishuapp.com/app/<id>/mcp      │
└──────────┬───────────────────────────────────────┬──────────────────┘
           │ ① MCP Streamable HTTP                  │ ② MCP（固定公网 URL）
           ▼                                        ▼
   cloudflared quick tunnel                 飞书妙搭 NestJS 应用
   （重启换域名！）                          Postgres 任务队列 + SSE 长轮询
           │                                        │ HTTP 转发 exec/read_file
           ▼                                        │ （经 ①的 /bridge/rpc）
   bridge/（127.0.0.1:8760） ←── worker 发现隧道地址 ──┤
   12 工具：exec/fs_*/view_image/render/…             │
                                                       ▼
                                            worker/（Mac 常驻 launchd）
                                            拉任务 → 跑 kimi/claude CLI
                                            → 廉价模型网关 → 回报结果
```

**两条通道的分工是整套设计的心脏：**

- ① 延迟低（一次网络往返）、工具全（能直接把图片字节回给 Notion），但 quick tunnel 域名每次重启都换，且依赖 cloudflared 活着。
- ② 域名固定（妙搭托管）、任务进队列由 Mac 主动拉取，**隧道全挂时 run_task 依然可用**；代价是队列任务的输出只能写文件再分页读，回不了图片字节。

日常用法：能走 ① 走 ①（快、全）；① 断了或要长跑的任务走 ②（稳、可排队）。

---

## 1. 方法论：七个关键决策

### 1.1 云端管"想"，本地管"做"，模型管"省"

Notion 的 agent 额度按消息数算、且无法触达你的机器；本地跑执行类模型走自建网关的 `group/notion-cheap` 路由组（多个国产廉价模型互备）。规划审查（少量高价值 token）放云端，执行（海量低价值 token）放本地廉价路由——**同一件事的花费差一个数量级**。

落地位置：`worker/` 的 `kimi_model` 指向你的网关路由组；bridge 的 `delegate_model` 同理。没有网关？把模型名换成任何 CLI 支持的模型即可，架构不变。

### 1.2 工具契约向后兼容，永不破坏

所有增强只做三件事：**加可选参数、加返回字段、改默认值（保持旧行为可复现）**。工具名和已有参数签名从第一天起没动过。收益：Notion 端配置一次就不需要再动；云端 agent 的提示词里教过的用法永远有效。

### 1.3 上下文是稀缺资源 → 输出治理三件套

LLM 窗口塞满垃圾输出会让整个系统变笨。对策（全部在 `bridge/src/notion_bridge/execution.py`）：

1. **head+tail 截断**：预览保留开头和结尾，中间替换为中文标记 `[... 截断 N 字符，完整输出见 <路径> ...]`；
2. **完整日志落盘**：stdout+stderr 全量写 `tasks/exec-logs/<job_id>.log`（0600、O_EXCL 防覆盖），agent 需要细节时用 `read_file` 分页读；
3. **大结果自动归档**：worker 任务结果 >8000 字符时写 `result.md` 并把路径/字符数/sha256 放进元数据，队列里只回预览。

### 1.4 拉模型优于推模型

家庭网络（NAT + 运营商无公网 IPv4 + 各种代理中间盒）做不好"外面推进来"。worker 每 15 秒主动出站 claim/heartbeat，任何入站穿透问题都不存在；隧道只服务于①这条低延迟通道，挂了也不影响任务执行。

### 1.5 安全 = 边界清晰，而非假装沙箱

`exec` 以用户权限跑，这是事实，文档里明说。真正要守住的边界用代码守：

- 三条通道三套独立 bearer token，只存 `.env`；
- 文件读写限制在工作区内，拒绝 `..`、逐段 `O_NOFOLLOW` 防符号链接逃逸、`.env*`/`.git`/`audit.log` 双保险（resolve 前后各查一次）；
- 云端→本地 RPC：worker 每个请求都要带独立 token；`bridge_url` 严格 HTTPS + 主机后缀白名单 + **永不跟随重定向**（登记信息被篡改也偷不走凭证）；
- 结果回报前先做 token 脱敏替换；
- 超时杀**整个进程组**（`start_new_session` + `killpg`），不留孤儿。

### 1.6 每个环节可独立验收

不要"部署完点一下试试"。每层都有可重复的验收脚本与判据（见 §5），出问题能立刻定位到"传输层断了 / 工具层断了 / 模型层断了"。

### 1.7 常驻靠 launchd，故障靠手册

三个 plist 模板在 `deploy/`：RunAtLoad + KeepAlive，日志固定位置，崩溃自拉。隧道服务 ThrottleInterval=120——Cloudflare 对 quick tunnel 创建有 IP 级限流（429），15 秒重试会自锁，2 分钟刚好。排障步骤写在 [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md)，是两次真实故障的实录。

---

## 2. 部署第一步：本地桥（通道①）

### 2.1 安装

```bash
brew install uv cloudflared
git clone <本仓库> && cd notion-mac-bridge/bridge
cp config.json.example config.json
```

`config.json` 要点：

| 键 | 说明 |
|---|---|
| `allowed_roots` / `workspace_dir` | 允许 agent 读写的工作区。建议单独目录，别指到家目录。 |
| `magpie_base` / `delegate_model` / `kimi_model` | 你的廉价模型网关地址与路由组名；没有就删掉 delegate/kimi_exec 相关配置（工具仍在，只是调不通）。 |
| `port` | 默认 8760，只绑 127.0.0.1。 |

### 2.2 Token 与启动

```bash
printf 'NOTION_BRIDGE_TOKEN=%s\n' "$(openssl rand -hex 32)" > .env
uv sync
uv run python scripts/run.py --tunnel
```

脚本会起桥、起 cloudflared，并打印一个 `https://<随机>.trycloudflare.com/mcp` 地址。看到它即代表传输层活着。

### 2.3 接 Notion

Notion → Settings → Connections → 新建 MCP：URL 填上面地址，鉴权选 Bearer Token 填 `.env` 里的值。连上后让 agent 调 `echo` 或 `exec(date)` 验证全链路。

> **注意**：这个域名每次重启都会变（§6.1）。变一次就得去 Notion 改一次——这正是我们做通道②的原因。想要固定域名可自建 named tunnel（`cloudflared tunnel` 官方流程，教程略）。

### 2.4 工具一览（12 个，签名见源码）

`fs_list` / `fs_read` / `fs_search`（只读三件套）· `fs_write` / `write_binary`（工作区内写，返回字节数+sha256）· `exec`（前台/后台/超时/截断）· `job_status` · `view_image`（inline/info/file 三模式，见 §4.4）· `fetch`（URL 下载到工作区）· `render`（headless Chrome 出图/出片）· `delegate` / `kimi_exec`（本地廉价模型干重活）。

---

## 3. 部署第二步：云端队列（通道②）

通道②需要一个**带固定公网 URL 的常驻后端 + Postgres**。我用飞书妙搭（低代码平台，能托管 NestJS 应用、内置 PG、给 `*.feishuapp.com` 固定域名，免费额度够个人用）。**换成任何能跑 Node + 连 Postgres 的小服务器（VPS、NAS、Cloudflare Workers+D1…）也一样**——`cloud-module/` 的 NestJS 代码除两行 `@lark-apaas` 导入（平台注入的 DRIZZLE_DATABASE provider）外都是标准 NestJS，可自行替换 provider 后移植。

### 3.1 妙搭侧步骤

1. 妙搭新建应用（全栈模板），clone 平台生成的仓库；
2. 把 `cloud-module/` 复制为 `server/modules/mcp/`，跑 `migrations/` 里 4 个 SQL（平台数据库控制台执行）；
3. `app.module.ts` 的 imports 加 `McpModule`；`main.ts` 里 `await app.listen(...)` **之前**加 `app.use(machineIdentity)`（从 `./modules/mcp/auth` 导入）——这一步不做，鉴权根本不会生效；
4. 环境变量：`MCP_AUTH_TOKEN`（Notion 用）、`MCP_WORKER_TOKEN`（worker 用）、`SUDA_APP_ID`、`CLIENT_BASE_PATH`、`MIAODA_LOCAL_ROOTS`（= 本地工作区路径，逗号分隔，用于 read_file 的 task_root 校验）、可选 `MIAODA_BRIDGE_HOST_SUFFIXES`；
5. 发布。MCP 端点 = `https://<app>.feishuapp.com/app/<id>/mcp`。

### 3.2 两个平台级深坑（都花了我半天）

**坑一：`/openapi/**` 前缀不能用作 MCP 路径。** 妙搭网关对该前缀强制 API-Key 鉴权，且 OPTIONS 预检也要求携带 Authorization——浏览器里的 Notion 发 CORS 预检时**不会带自定义头**，直接被 403。解法：挂到普通路径 `/mcp`（网关透传），鉴权自己做（`auth.ts` 的 timing-safe bearer 比较）。

**坑二：SSE 必须每请求独立、响应头立刻 flush。** 平台前置的缓冲层会攒包；`task_status(wait_seconds)` 长等待若不 flush headers 并每 15 秒写 `: keepalive\n\n` 注释行，Notion 侧会超时断连。`mcp.controller.ts` 里这两点是逐行验证过的实现。

### 3.3 worker 侧

```bash
cd ../worker
cp worker-config.json.example worker-config.json   # 改 base_url / default_workdir / 模型
printf 'MIAODA_WORKER_TOKEN=%s\n' "$(openssl rand -hex 24)" > ../.env
# MIAODA_MCP_TOKEN 也写进同一个 ../.env（云端 MCP_AUTH_TOKEN 的同值副本，测试脚本用）
# 同时把 MIAODA_WORKER_TOKEN 同值写入 bridge/.env —— 通道②的 exec/read_file
# 直通要经 /bridge/rpc，bridge 端要用它验签（env 优先，文件兜底）
python3 worker.py                    # 先前台跑一轮，看 claim/heartbeat 日志正常再上 launchd
```

worker 纯标准库，不需要 venv。它做四件事：claim（长轮询 15s）→ 跑 `kimi -p "<prompt>"` 或 claude CLI（工作目录 = 任务 workdir 或 `tasks/<task_id>/`）→ 心跳续命 → 回报（脱敏 + 大结果归档）。断网时任务写进 `state/` outbox 重放，不丢。

配置了 `bridge_url`（或跑着带 `--metrics 127.0.0.1:20241` 的 quick tunnel 时自动发现）后，worker 会把该地址登记给云端，云端于是能把 ②的 `exec`/`read_file` **直通转发**到 ① 的 `/bridge/rpc`——**不排队、不调模型、秒级返回**，且同一 `request_id` 重复提交只执行一次（幂等缓存 1 小时）。

### 3.4 接 Notion

再建一个 MCP 连接，URL=②的地址，Bearer=`MCP_AUTH_TOKEN`。至此 10 个云端工具可用：`run_task`(prompt/engine/workdir/timeout_seconds/request_id) · `task_status`(wait_seconds≤120) · `list_tasks` · `cancel_task` · `exec` · `read_file` · `ping` · `echo` · `add` · `server_info`。

把 `docs/notion-playbook.md` 的内容贴进 Notion 的 agent 指令里（教它两通道怎么选、request_id 怎么复用、大输出怎么读）。

---

## 4. 部署第三步：常驻化与验收

### 4.1 launchd 三件套

```bash
cd deploy
# 编辑三个 plist：把 __REPO__ 换成你的路径前缀，__LABELPREFIX__ 换成你的域名反写（如 com.example）
sh ./render-plists.sh
# 按 render-plists.sh 末尾打印的 launchctl bootstrap 命令加载
```

三个服务：bridge、tunnel（走 `scripts/run_tunnel.py`，见 §6.2 的绕行参数）、worker。验证：

```bash
launchctl print gui/$(id -u)/com.example.notion-bridge | head
tail -f ../../bridge/logs/bridge.log   # 路径按你的渲染结果
```

### 4.2 验收脚本（每条都有明确判据）

```bash
# 50 次随机间隔连续调用，验证会话/传输稳定性（判据：全程无错误、无重连）
NOTION_BRIDGE_URL=https://<你的隧道>/mcp uv run python scripts/check_mcp_acceptance.py
# 功能矩阵：截断/日志/写入/二进制/路径拒绝/图片三模式/超时/后台/环境
uv run --with numpy python scripts/check_features.py   # 需要 NOTION_BRIDGE_URL
# worker 单元 42 项 + bridge 单元 191 项
python3 -B -m unittest discover -s worker
uv run --with pytest python -m pytest bridge/tests
```

---

## 5. 日常用法速记

- **小命令快速验证** → ①`exec`（head+tail 预览 + log_path）
- **长任务 / 隧道断了** → ②`run_task` + `task_status(120)`，完成后读 `result.md`
- **要 agent 看大输出** → ①`exec` 或 ②`exec` 拿 log_path → `read_file` 分页
- **看图**：Notion 界面内联显示图片很贵（token）也常显示不出来。用 ①`view_image(mode="info")` 只要尺寸格式元数据，`mode="file")` 把 ≤300KB 缩略存到工作区再看文件；`mode="inline"` 保留旧行为。
- **重复提交防御**：②的所有调用带稳定 `request_id`；重试复用，别新生成。

---

## 6. 故障手册摘要（全文见 TROUBLESHOOTING.md）

### 6.1 quick tunnel 重启换域名

无解，特性不是 bug。对策：②通道不受影响（worker 重新登记新地址即可，自动）；①去 Notion 改 URL；彻底解决用 named tunnel（需自备域名）。

### 6.2 隧道活着但全 530 / TLS EOF（真实事故）

症状：Notion 两个连接同时断，`cloudflared` 日志 QUIC "no recent network activity"，换 `--protocol http2` 仍 `TLS handshake with edge error: EOF`。

排查到根因的过程在 TROUBLESHOOTING.md，一句话版本：**本机代理软件（Clash Verge/Mihomo）TUN 模式下，Cloudflare 隧道边缘域名（`*.cftunnel.com`）的 TLS 握手被中间链路按 SNI 掐断，而普通域名正常**——用 Python 对同一 IP 不同 SNI 的握手对比定位。

绕过办法（已固化进 `run_tunnel.py`）：`--protocol http2 --edge <真实边缘IP>:7844 --edge-bind-address <en0当前IPv4> --edge-ip-version 4`。源地址绑定让握手不走 TUN 的故障路径。**没有**改系统代理、没有动代理规则。

### 6.3 Cloudflare 429（创建太频）

launchd 快速重启会把自己打进限流。ThrottleInterval=120 + 手动重启间隔 ≥2 分钟。

### 6.4 "妙答连不上但 run_task 正常"

`exec`/`read_file`（直通）依赖①的隧道，`run_task`（队列）不依赖。隧道断了只有直通 530——不是 bug，见 §3.3 的架构分工。

### 6.5 worker 拉着任务但结果不回报

看 `state/` outbox：回报失败会重放；如果任务结果含明文 token，Redactor 会先替换成 `[REDACTED]`——这是设计，不是丢数据。

---

## 7. 成本与安全清单

成本结构：Notion 订阅（规划）+ 本地电费（执行）+ 廉价网关（token）。三个 bearer 按月轮换；公网 URL 定期当秘密看待（quick tunnel 天然每次重启即换，named tunnel 要自己克制分享）。**任何时候都不要把 `.env`、`audit.log`、`state/tasks/` 提交进 git**——本仓库 `.gitignore` 已兜底，fork 后别改。

## 8. 已知限制

- ① 的可用性天花板 = cloudflared + 你的出口网络；对公网暴露 exec 请始终假定"知道 URL 和 token 的人 = 拿到你的 Mac"。
- ② 依赖妙搭平台（或你移植的目标平台）的请求超时上限，长 `task_status` 等待靠 SSE 保活绕开，移植时别丢这段。
- worker 单任务单进程，没有并发池——刻意保持简单。
- `render` 需要本机装 Google Chrome；缺了该工具会明确报错，不影响其余工具。
