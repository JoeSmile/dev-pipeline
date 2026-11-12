# 本地开发环境（Windows 优先）

本文说明如何在本机跑 `dev-pipeline` 的骨架 Worker 与测试。[DP-29](https://weiperfect007.atlassian.net/browse/DP-29) 要求配置根目录为显式必填参数：必须是已经存在的绝对路径。不要省略它，程序也不会回退到当前目录、用户主目录或环境变量。

本仓库 Worker 与现场 History 测试默认连接**已经在跑的官方 Temporal**：`127.0.0.1:7233`（容器 `agent-platform-temporal-1`，compose 项目名 `agent-platform`，官方仓库 `D:\projects\kpl agents\agent-platform`）。不要在本仓库再起一份 Temporal，也不要把 `test-docker.ps1 -WithTemporal` 或 `docker compose --profile temporal` 当作默认启动方式。

远程仓库：<https://github.com/JoeSmile/dev-pipeline>。L0 见 `docs/CHARTER.md`（已于 2026-11-10 冻结）。本期不要求对接 `agent-platform` 的 worker / app / init；只用它已经提供的 Temporal 服务。不要尝试修复其 schema / init 失败。

## 1. 预备

- 期望 Python **3.12**（不要用 3.13+）。请用 [python.org](https://www.python.org/downloads/) 的安装包，不要用 Microsoft Store 里的 `python` 占位程序。在这台 Windows 开发机上，系统里的 `python` 经常只是商店占位，不能用来跑本仓库的测试或 Worker。
- **推荐用 Docker 跑测试**，不必在宿主机安装可用的 Python。本仓库验证过的 Docker 路径是 `docker compose run --build --rm test`（务必加 `--build`，以免用到过期镜像）。本机 Docker 版本示例：Docker 29.8.0，Compose v5.5.1。
- 安装 Git，并用 SSH 克隆仓库（HTTPS 经常失败）：

```powershell
git clone git@github.com:JoeSmile/dev-pipeline.git
cd dev-pipeline
```

- 准备一个已经存在的配置根目录，例如：

```powershell
New-Item -ItemType Directory -Force -Path D:\pipeline-config
```

后面的 `CURSOR_API_KEY` 等密钥会放在这个目录里（文件名就是 `CURSOR_API_KEY`）。完整 Cursor CLI 运行时见 [DP-46](https://weiperfect007.atlassian.net/browse/DP-46)；本阶段若构建写码命令，只把密钥从该文件注入环境变量，禁止 `--api-key` 进命令行，也不得写入 Temporal History、证据文件或日志。文档示例只用占位，不要写入真实密钥。本票不调用现场 Cursor CLI。

该目录还必须包含 `budget.toml`（可从仓库 `docs/examples/budget.toml` 复制）。文件里的限额数字均为待证占位，须经负责人签批后才视为正式限额。缺少该文件或内容非法时，Worker 与 Workflow 都会以中文错误退出，不会使用隐藏默认限额继续执行。

该目录还可选包含 `spike.toml`（可从仓库 `docs/examples/spike.toml` 复制），用于配置可选 Spike 的可证伪时限（`timeout_seconds`，大于 0 的整数）。缺少该文件时使用待证占位默认 4 小时，不是已签批的正式 SLA。文件存在但内容非法时，Spike 活动会以中文错误失败，不会假装已有正式时限。

该目录还可选包含 `workspace.toml`（可从仓库 `docs/examples/workspace.toml` 复制），用于声明写码沙箱的授权工作区根目录 `authorized_root`（必须是已存在的绝对目录）。每张票的授权 worktree 为 `<authorized_root>/<ticket_id>/<execution_id>`。缺少该文件时默认未授权，沙箱拒绝任何写入。文件存在但内容非法时，工作区声明活动会以中文错误失败，不会偷偷写到未授权路径。Worker 与 CLI 默认不对外部或远端仓库开放写权限；未确认 L0 前不默认向目标 Story 远程仓库 push 或开 PR（见 [DP-20](https://weiperfect007.atlassian.net/browse/DP-20)、[DP-31](https://weiperfect007.atlassian.net/browse/DP-31)，默认未授权 / 待证）。不要把 `D:\projects\dev-pipeline` 本身当成目标 Story 的 worktree。

```powershell
Copy-Item .\docs\examples\budget.toml D:\pipeline-config\budget.toml
```

如需把父执行阶段镜像回 Jira，再准备 `jira.toml`（可从 `docs/examples/jira.toml` 复制）以及配置根目录下的令牌文件（由 `api_token_file` 指向文件名）。Worker 启动不强制要求该文件；缺失或非法时镜像 Activity 会以中文错误失败，并在快照上留下「镜像滞后」，不会让 Workflow 把同步当成成功。可选的 `stage_field`（自定义字段名 / ID）仍为待证。不要把令牌写进 `jira.toml`、命令行或证据日志。

## 2. 安装依赖（仅在要用宿主机跑 Worker 时）

若只跑测试，可跳过本节，直接看第 3 节的 Docker 命令。若要在宿主机启动本仓库 Worker，先确认已安装真实的 Python 3.12，再在仓库根目录创建虚拟环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m pip install pytest pytest-asyncio ruff mypy
```

`scripts/start-worker.ps1` 会优先使用 `.venv\Scripts\python.exe`，其次 `py -3.12`，并拒绝 Windows 商店占位程序。

## 3. 跑状态机桩测试（推荐 Docker，不需要 Temporal 服务）

测试使用 `temporalio.testing.WorkflowEnvironment`，不必先启动 Temporal，也不必在宿主机安装 Python。在仓库根目录：

```powershell
docker compose run --build --rm test
```

等价脚本：

```powershell
.\scripts\test-docker.ps1
```

这会重建测试镜像，并在容器内依次执行 `python -m pytest`、`ruff check src tests` 和 `mypy`。务必加 `--build`，不要用可能过期的旧镜像。非 Windows 环境可以使用 `scripts/test-docker.sh`。

只有在已经用官方安装包建好 `.venv` 时，才可以直接在宿主机跑：

```powershell
python -m pytest
ruff check src tests
mypy
```

不要用 Windows 商店里的 `python` 跑上述命令。

## 4. 官方本地 Temporal（只在要跑真实 Worker 或现场 History 时）

默认地址是 `127.0.0.1:7233`。这是已经在跑的官方 `agent-platform` Temporal，不是本仓库附带的临时服务。

先确认端口已在监听，例如：

```powershell
docker ps --filter "name=agent-platform-temporal-1"
```

若容器存在但已停止，先启动它，不要另起一份：

```powershell
docker start agent-platform-temporal-1
```

若官方栈尚未运行，到官方仓库启动，不要在本仓库再起 Temporal：

```powershell
cd "D:\projects\kpl agents\agent-platform"
.\scripts\agent_platform_start.ps1
```

只需确认 Temporal 在 `127.0.0.1:7233` 监听。本期不要求 `agent-platform` 的 worker / app / init 健康；init 若因 schema（例如 `phase1-clean-20261103`）失败可以忽略，不要在本仓库修复它，也不要使用：

- `temporal server start-dev --port 7233`
- `docker compose --profile temporal up temporal`（本仓库）
- `.\scripts\test-docker.ps1 -WithTemporal`

确认 `127.0.0.1:7233` 可达后再启动本仓库 Worker。

## 5. 启动 Worker

配置根目录是必填的，脚本和 Python 入口都不会给默认值。Worker 默认指向官方 Temporal `127.0.0.1:7233`，脚本不会启动第二份 Temporal。

```powershell
.\scripts\start-worker.ps1 -ConfigRoot D:\pipeline-config
```

等价的 Python 入口（需已按第 2 节准备好真实的 Python 3.12 / `.venv`）：

```powershell
python -m dev_pipeline.worker --config-root D:\pipeline-config
```

可选参数：

- `--temporal-target` / `-TemporalTarget`，默认 `127.0.0.1:7233`
- `--task-queue` / `-TaskQueue`，默认 `dev-pipeline`

若省略 `--config-root` / `-ConfigRoot`、路径不存在、不是绝对路径，或指向一个文件而不是目录，进程会以中文错误退出（退出码 2），而不会继续执行，也不会把失败说成「可能已存在」。

若官方 Temporal 未在 `127.0.0.1:7233` 监听，`start-worker.ps1` 会提示到 `agent-platform` 启动官方栈，并以退出码 1 失败；它不会改为启动本仓库的 Temporal profile。

Worker 启动成功与配置根目录错误都会通过 structlog 记录，不再使用 `print`。CLI 参数没有变化：`--config-root` 仍是必填且必须已经存在的绝对目录。

非 Windows 环境可以使用 `scripts/start-worker.sh --config-root /absolute/path`，约定与上面相同。

## 6. 现场 History 测试（官方 Temporal，不是临时服务）

现场 History 测试必须打在已运行的官方 Temporal 上。从测试容器访问宿主机上的 `127.0.0.1:7233` 时，使用 `TEMPORAL_TARGET=host.docker.internal:7233`：

```powershell
docker compose run --build --rm -e TEMPORAL_TARGET=host.docker.internal:7233 test python -m pytest -m temporal_server -vv
```

等价脚本：

```powershell
.\scripts\test-docker.ps1 -LiveHistory
```

若 7233 没有服务，先按第 4 节启动官方栈，不要用 `-WithTemporal` 或 `docker compose --profile temporal` 再起一份。这些测试使用临时目录和假 Jira，不写真实 Jira 票，不调用现场 Cursor CLI，也不把真实密钥打进输出。

## 7. 用 CLI 启动、发 Signal、查状态

Worker 已连接后，用 CLI 根据 Jira key 显式启动一条父执行，再用人信号完成门禁，并用 `status` / `--follow` 查看进度。Jira 不是编排器；本入口只校验 key 形态，不拉取远端 Issue，也不监听标签。可复制的验收命令（Windows 第一本地环境）：

```powershell
dev-pipeline start --jira-key DP-13 --config-root D:\pipeline-config
```

等价入口：

```powershell
python -m dev_pipeline.cli start --jira-key DP-13 --config-root D:\pipeline-config
```

可选参数：

- `--execution-id`，省略则生成 UUID 并打印
- `--temporal-target`，默认 `127.0.0.1:7233`
- `--task-queue`，默认 `dev-pipeline`

成功时标准输出会打印 `workflow_id`、`execution_id` 和 Jira key，并写明已进入 Intake。然后可抄以下命令完成一次人工门禁并查看状态：

```powershell
dev-pipeline signal lock_scope --config-root D:\pipeline-config --workflow-id story-DP-13-<execution_id> --in-scope "登录校验" --out-of-scope "支付" --locked-by "Joe"
dev-pipeline status --workflow-id story-DP-13-<execution_id> --config-root D:\pipeline-config
dev-pipeline status --follow --workflow-id story-DP-13-<execution_id> --config-root D:\pipeline-config
```

`status` 打印当前步骤、最近进度行和最近心跳；等候门禁显示「正在等候你确认××」，心跳过期只标「可能无响应」。`--follow` 持续刷新，默认间隔 2 秒（待证），只读、不改状态。退出码：0 成功；2 用法或配置错误（缺 `--config-root`、路径非法、payload 缺字段、未知 Signal 等）；1 Temporal 不可达、找不到执行或 Signal 被拒绝（阶段不变）；130 为 Ctrl+C 结束跟随。若省略 `--config-root`、路径不存在、不是绝对路径、`start` 时目录缺少合法 `budget.toml`、Jira key 非法、Temporal 不可达，或目标 workflow id 已存在，进程会以中文错误非零退出。已存在的执行不会被当成启动成功。契约见 README 与 `plans/superpowers/DP-28.md`。

## 8. 本骨架尚未包含的内容

- 真实 Coding / Review LLM Worker
- Cursor CLI 运行时
- 与 `agent-platform` 的产品对接（本仓库只使用其已经在跑的 Temporal 服务）
- 真实测试环境发布（目前只有拒绝生产目标的桩 Activity）
- MCP / BA 视图与现场 Cursor stream-json 运行时（[DP-38](https://weiperfect007.atlassian.net/browse/DP-38)、[DP-42](https://weiperfect007.atlassian.net/browse/DP-42)、[DP-46](https://weiperfect007.atlassian.net/browse/DP-46)）；操作 CLI 的 start / signal / status --follow 已由 [DP-28](https://weiperfect007.atlassian.net/browse/DP-28) 提供
