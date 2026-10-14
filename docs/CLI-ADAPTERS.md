# 接入你自己的 CLI

这套方法的核心是：云端 agent 提供任务与验收条件，常驻 worker 在本机执行任务，再把结果和产物路径交回云端审查。Kimi、Claude Code、Magpie 和 `group/notion-cheap` 是本仓库实例使用的执行器、网关与模型配置，读者可以换成自己已有的 CLI 和模型服务。

CLI 适配解决的是执行器接口；Windows 的进程管理、文件安全和常驻服务仍需单独移植。目前本仓库在 macOS 上验收。

## 当前实现提供哪些接口

`worker/worker.py` 内置两种协议，云端 `run_task.engine` 目前只接受 `kimi` 和 `claude`。可以配置可执行文件与模型，也可以把其中一个可执行文件指向你的适配脚本。改路径不会自动转换 CLI 参数。

| 协议 | 实际调用 | prompt 输入 | 成功结果读取 |
|---|---|---|---|
| `kimi` | `binary -p <prompt> -m <model> --output-format text` | 命令行参数；stdin 关闭 | stdout 作为 UTF-8 文本，退出码必须为 0 |
| `claude` | `binary -p --output-format json --permission-mode bypassPermissions [--model <model>]` | UTF-8 stdin，写入后关闭 | stdout 为 JSON 对象，其 `result` 为文本；`is_error=true` 或非零退出码判失败。也兼容含 `type=result` 对象的 JSON 数组 |

`kimi` 分支没有附加 `--auto`/`--yolo`；原实例用 CLI 本身的配置处理权限。`claude` 分支会传 `bypassPermissions`。接自己的 CLI 时，要明确它的无人值守方式与权限设置，避免 worker 等待交互确认。

## 方法一：沿用内置 CLI

在 `worker/worker-config.json` 配置已有 CLI 的绝对路径。以下是 Claude Code 的配置示例；`base_url` 填你部署的云端应用地址，`default_workdir` 填本机已有工作区：

```json
{
  "worker_id": "my-machine",
  "base_url": "https://example.feishuapp.com/app/app_example",
  "default_workdir": "/absolute/path/to/notion-workspace",
  "binaries": {
    "claude": "/absolute/path/to/claude"
  }
}
```

Notion 调用 `run_task` 时明确设置 `engine="claude"`。省略 `models.claude` 时，使用该 CLI 自己的模型配置；需要覆盖时，可加 `"models": {"claude": "<该 CLI 支持的模型标识>"}`。

Kimi 的模型配置使用 `models.kimi` 或 `kimi_model`。仓库默认值是示例网关别名 `magpie/group/notion-cheap`，使用其他服务时需替换成该 CLI 已配置、可调用的模型标识。

CLI 的登录凭证和网关配置按它自己的方式保存在本机。仓库样例不包含任何真实凭证。

## 方法二：用适配脚本接其他 CLI

可以把 `binaries.kimi` 指向一个接受上表参数的可执行脚本，继续使用 `engine="kimi"` 作为传输协议名称：

```json
{
  "worker_id": "my-machine",
  "base_url": "https://example.feishuapp.com/app/app_example",
  "default_workdir": "/absolute/path/to/notion-workspace",
  "binaries": {
    "kimi": "/absolute/path/to/my-cli-adapter"
  },
  "models": {
    "kimi": "<你的 CLI 支持的模型标识>"
  }
}
```

适配脚本完成三个转换即可：

1. 接收 `-p <prompt>`、`-m <model>`、`--output-format text`，转换为目标 CLI 的参数或 stdin。目标 CLI 自行选模型时，可在适配层忽略 `-m`。
2. 以任务的当前工作目录启动目标 CLI，运行一次完整任务并退出。将结果文本写到 stdout，将诊断信息写到 stderr；目标 CLI 返回 JSON/JSONL 时，先提取最终结果文本。
3. 成功退出码为 0，失败保留非零退出码；不要脱离 worker 的进程组启动后台任务，确保取消与超时能终止执行。

转换逻辑示意：

```text
worker: adapter -p <prompt> -m <model> --output-format text
                  ↓
adapter: 解析参数 → 按目标 CLI 文档构造 argv/stdin → 执行
                  ↓
         提取最终文本到 stdout → 传递退出码给 worker
```

目标 CLI 名称、无人值守参数、模型参数和输出格式以其实际文档为准。Codex、Gemini CLI 或自建 agent 均可按这个接口接入，本仓库没有附带或验收它们的现成适配器。

若希望云端直接使用新的 `engine` 名称，需要同步修改 MCP schema、云端及 worker 的任务校验，以及 worker 的命令构造、stdin 和结果解析；也应核对数据库字段与后续迁移是否兼容新值。使用上述适配脚本可保留当前工具契约。

## 与本地 bridge 的关系

- `exec`、文件读写、`view_image` 等基础工具直接运行本地代码，不要求安装 Kimi 或 Claude CLI。
- `kimi_exec` 是 bridge 自己的 Kimi 协议入口，通过 `bridge/config.json` 的 `kimi_bin`、`kimi_model` 设置，和队列 worker 的配置相互独立。实际调用还包含 `--add-dir <cwd>`，适配脚本需兼容该参数；stdout/stderr 合并为文本返回。
- `delegate` 直接请求网关的 Anthropic Messages 接口，使用 `magpie_base` 与 `delegate_model`；它没有通过 worker CLI 执行。替换网关时也需兼容此 API。

只需要直接执行已知命令时，可使用 `exec`。需要本地 agent 自主读文件、实现代码、测试时，才需要接入 CLI 执行器。

## 接入验收

先在任务工作目录单独运行目标 CLI，确认无人值守执行、结果输出、退出码与模型配置正常，再接 worker。

通过 `run_task` 提交一个新 `request_id`：要求在独立任务目录写入 `cli-check.txt`，读取回验并汇报路径、内容和验收结果。完成后确认云端收到 `succeeded` 与真实产物；失败任务应收到 `failed`，取消或超时应结束执行进程。适配器的验收应覆盖这几种行为。
