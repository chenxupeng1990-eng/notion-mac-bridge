# 故障恢复手册（实录版）

本文档来自真实生产故障：同一套系统在 2026-10 两周内经历两次"全断"，每次都在 30 分钟内恢复。按症状索引，每条给出**判据 → 定位 → 动作**，以及"不该做什么"。

---

## 症状 A：Notion 两个连接同时显示离线，妙答 run_task 正常

**判据**：①（mac本地）报连接失败；②（妙答）`ping`/`run_task` 正常。
**定位**：①的 cloudflared 挂了或域名变了；②独立于隧道所以照常。
**动作**：

```bash
pgrep -fl cloudflared                      # 进程在吗
tail -50 bridge/logs/tunnel-error.log      # launchd 渲染后的日志路径
curl -s http://127.0.0.1:20241/quicktunnel # metrics 端点给出当前域名
```

- 进程没了 → `launchctl kickstart -k gui/$(id -u)/<prefix>.notion-tunnel`，等新域名打印进日志，把 Notion 里 ①的 URL 换成新域名。
- 进程在但 `/quicktunnel` 为空/报错 → 见症状 B。
- **不要**：手工 `cloudflared tunnel --url …` 乱起（会和 launchd 托管的抢 429 配额）。

## 症状 B：隧道进程活着、反复重连、Notion 全 530/EOF

**判据**：`tunnel-error.log` 出现 `QUIC … no recent network activity`，或 `HTTP2 … TLS handshake with edge error: EOF`；直接 curl `https://<域名>/mcp` 也 530。
**定位思路**（这是最难的一次，按顺序做）：

1. 先排除 Cloudflare 自己出问题：`curl -sI https://www.cloudflare.com` 正常 → Cloudflare 可达，问题在隧道边缘连接。
2. 看 DNS 是否被污染：`dig +short region1.v2.argotunnel.com` 有正常 IP → 边缘可达。
3. **SNI 对比实验**（决定性一步）：用 Python `ssl` 对**同一个真实边缘 IP** 分别以不同 SNI 握手：

```python
import socket, ssl
for sni in ("region1.v2.argotunnel.com", "h2.cftunnel.com"):
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection(("198.41.192.227", 7844), 5) as s:
            with ctx.wrap_socket(s, server_hostname=sni) as t:
                print(sni, "OK", t.version())
    except Exception as e:
        print(sni, "FAIL", type(e).__name__, e)
```

4. 若像我们这次：`region1.v2.argotunnel.com` 成功而 `h2.cftunnel.com`/`quic.cftunnel.com` 立刻 EOF——说明**本机与 Cloudflare 边缘之间的某个中间盒按 SNI 掐断了特定域名**（我们环境里是代理软件的 TUN 模式路由）。这不是你的证书、不是 Cloudflare 故障、也不是域名问题。

**动作（按侵入性从低到高）**：

1. `run_tunnel.py` 的绕行参数组合（已内置，无需改代理/系统设置）：

```
cloudflared tunnel --no-autoupdate --protocol http2 \
  --edge 198.41.192.227:7844 \
  --edge-bind-address $(ipconfig getifaddr en0) \
  --edge-ip-version 4 --metrics 127.0.0.1:20241 --url http://127.0.0.1:8760
```

`--edge-bind-address` 绑定物理网卡源地址后，握手包不再走 TUN 的故障路径。**每次启动重新读 DHCP 地址**（脚本已做），别把它硬编码进 plist。
2. 若仍失败：临时关闭代理软件 TUN 验证归因（验证完记得恢复原设置）。
3. 彻底摆脱该类问题：迁 named tunnel（自有域名 + `cloudflared tunnel run`，走不同连接路径）。

**不要**：为了一个隧道改全局代理规则——影响面太大；先做 SNI 对比实验拿证据。

## 症状 C：launchd 反复拉起隧道但一直 429

**判据**：日志 `failed to run tunnel: ... 429` 或域名永远出不来。
**原因**：quick tunnel 创建频率有 IP 级限流；ThrottleInterval=120 之前我们踩过 15 秒自锁的坑。
**动作**：杀掉全部手工 cloudflared；确认 plist ThrottleInterval≥120；等 ≥5 分钟让限流窗口滑过。

## 症状 D：run_task 排队不执行

**判据**：②提交后 `task_status` 一直 queued。
**定位**：worker 没心跳。`tail worker/state/launchd-error.log`；看 worker 进程；`SELECT * FROM mcp_workers`（妙搭数据库控制台）看 last_seen。
**动作**：常见是 `MIAODA_WORKER_TOKEN` 与云端环境变量不一致（401 在 worker 日志里表现为持续失败）；或 `base_url` 写错路径（必须是 `<app-url>/mcp` 同级，worker 调用 `/worker/claim`）。worker 有 outbox，断网期间不会丢结果，恢复后自动重放。

## 症状 E：exec 返回 running、请求断开，或命令超时后仍有进程

先区分等待预算与命令超时：①本地 `exec(wait_seconds=45)` 最多等待75秒，返回 `running` 时命令继续；调用 `job_wait(job_id, seconds=45)` 续等，或 `job_status(job_id)` 立即查状态。请求取消也保留已启动的 job。日志尾部有界，只用于进度预览，完整输出查 `log_path`。

`timeout_seconds` 才是命令的运行上限，默认120秒；到期 `execution.py` 结束进程组并返回 `timed_out=true`。若仍看到进程，检查命令是否用 `nohup`/`disown` 等脱离原组；查看进程树和产物，不能仅凭工具连接断开判断进程已停止。

①本地 job ID 只在当前 bridge 进程内有效，重启后 `unknown job` 不说明命令没执行过；先检查已知 `log_path` 与产物。本地 `exec` 没有 `request_id`，重调会新建任务。②云端 `exec` 才有稳定 `request_id` 重试和 `/bridge/rpc` 一小时进程内幂等缓存；云端不自动重试，bridge 重启后先查证，不能将其 job ID 交给本地 `job_wait`。

## 症状 F：怀疑流量被代理误路由（国内变慢）

先别改规则。用只读探测对比"TUN 路径 vs 物理网卡直连"的耗时 + 查实际命中规则链（代理软件的 connections API 看 `chains` 是否 DIRECT）。我们的结论：绝大多数国内域名命中 GEOIP/DOMAIN-SUFFIX DIRECT，未见误路由；**GEOSITE 兜底缺失**是常见覆盖限制——具体站点慢再针对性加规则，别全局动刀。

## 症状 G：named tunnel 域名固定，但网络切换后一直无连接

可选 `bridge/scripts/run_fixed_tunnel.py` watchdog 的必填环境为 `NOTION_FIXED_TUNNEL_CONFIG`（已有 cloudflared 配置文件）和 `NOTION_FIXED_TUNNEL_ID`（隧道名称或 UUID）。它不读取 `bridge/.env`；通过 shell 或自备 supervisor 传入变量。配置、凭据引用和 ingress 由用户准备，完整变量表见 [教程 §2.3](TUTORIAL.md#23-接-notion)。

默认 readiness 地址为 `http://127.0.0.1:20242/ready`，与 quick tunnel 的20241分开。watchdog 每15秒检查 readiness，240秒无注册连接或子进程退出后重探；优先物理网卡路径，失败时回落默认路由。出口网络仍可能全部不可达。退出码1表示连续无健康连接的失败周期预算耗尽（默认10周期，每周期最多两个路径的子进程），2表示配置无效，0表示收到停止信号。先检查配置和日志，不要把无健康连接当作 MCP 工具错误。

现有 `render-plists.sh` 没有生成 named-watchdog plist；若用于②直通，需要 worker 显式 `bridge_url` 与云端主机白名单匹配，20241的 quick-tunnel 自动发现不能提供 named 域名。

## 症状 H：拿到了图片路径或元数据，但无法审查画面

①`view_image(mode="inline")` 提供原生 MCP 图片块，本地工具以 `structured_output=False` 避免额外结构化载荷。`info` 只有尺寸/格式/大小，`file` 只有保存的缩略图路径和SHA；这些证据不能证明 agent 看到了图片。

若 inline 结果被客户端溢出到会话文件，先确认客户端支持在云端 sandbox 读取该文件、解码图片块并用图片查看器打开。此流程依赖客户端，不能保证可用。不要反复缩小图片或把 base64 粘贴进对话来替代审查；未打开时明确标注尚未视觉验证。

---

## 恢复后的固定验收动作

```bash
# 从仓库根目录进入 bridge；脚本读取 bridge/.env 的 NOTION_BRIDGE_TOKEN
cd bridge
uv run python scripts/check_mcp_acceptance.py --url 'https://<本地桥域名>/mcp'
```

`--url` 必填。该脚本验证①本地工具发现和 `exec` 连续调用，不能用②的URL直接替换（鉴权和结果格式不同）。②在 Notion 中单独验证 `ping`、带唯一 `request_id` 的最小 `run_task` 和 `task_status`；需要直通时再查云端 `exec`/`read_file`。运行验收后报告实际结果，不以脚本存在或工具元数据推断已验证。
