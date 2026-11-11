# 本地开发环境（Windows 优先）

本文说明如何在本机跑 `dev-pipeline` 的骨架 Worker 与测试。[DP-29](https://weiperfect007.atlassian.net/browse/DP-29) 要求配置根目录为显式必填参数：必须是已经存在的绝对路径。不要省略它，程序也不会回退到当前目录、用户主目录或环境变量。

远程仓库：<https://github.com/JoeSmile/dev-pipeline>。L0 见 `docs/CHARTER.md`（已于 2026-11-10 冻结）。

## 1. 预备

- 安装 [Python 3.12](https://www.python.org/downloads/)（不要用 3.13+）。
- 安装 Git，并克隆仓库：

```powershell
git clone https://github.com/JoeSmile/dev-pipeline.git
cd dev-pipeline
```

- 准备一个已经存在的配置根目录，例如：

```powershell
New-Item -ItemType Directory -Force -Path D:\pipeline-config
```

后面的 `CURSOR_API_KEY` 等密钥会放在这个目录里，本骨架还不读取它们，但路径本身必须先合法存在。

该目录还必须包含 `budget.toml`（可从仓库 `docs/examples/budget.toml` 复制）。文件里的限额数字均为待证占位，须经负责人签批后才视为正式限额。缺少该文件或内容非法时，Worker 与 Workflow 都会以中文错误退出，不会使用隐藏默认限额继续执行。

```powershell
Copy-Item .\docs\examples\budget.toml D:\pipeline-config\budget.toml
```

## 2. 安装依赖

在仓库根目录：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m pip install pytest pytest-asyncio ruff mypy
```

## 3. 跑测试（不需要 Temporal 服务）

测试使用 `temporalio.testing.WorkflowEnvironment`，不必先启动 Temporal。

```powershell
python -m pytest
ruff check src tests
mypy
```

## 4. 用 Docker 跑测试（不必在宿主机装 Python）

仓库提供 `Dockerfile` 与 `compose.yaml`。镜像使用 Python 3.12（不要用 3.13+）。配置根目录规则不变：需要时必须传入已存在的绝对路径，镜像和脚本都不会给隐藏默认值。本仓库的 pytest 使用 `WorkflowEnvironment`，默认这条命令**不需要**已启动的 Temporal 服务。

在仓库根目录：

```powershell
docker compose build test
docker compose run --rm test
```

这会在容器内依次执行 `python -m pytest`、`ruff check src tests` 和 `mypy`。 测试镜像会从官方 `temporalio/temporal:1.5.1` 带入 CLI，容器内的 Workflow 测试使用本地开发服务，不必再访问 `temporal.download`。也可以直接运行：

```powershell
.\scripts\test-docker.ps1
```

非 Windows 环境可以使用 `scripts/test-docker.sh`。

### 真实 Temporal History 验收

可选 profile `temporal` 会启动官方开发服务镜像 `temporalio/temporal:1.5.1`（`server start-dev`），并把 gRPC 端口绑定到宿主机 `localhost:7233`。`test-history` 服务设置 `TEMPORAL_TARGET=temporal:7233`，从而执行 `tests/test_signal_history_live.py`。不需要在界面上点击任何按钮。

```powershell
.\scripts\test-docker.ps1 -WithTemporal
```

等价的 Compose 命令：

```powershell
docker compose --profile temporal run --rm test-history
docker compose --profile temporal down
```

如果 7233 已被占用（例如本机已有 Temporal 服务），`temporal` profile 会因端口冲突无法启动。可以先释放该端口，或把已有服务当作 History 目标（不必再起一份）：

```powershell
docker compose run --rm -e TEMPORAL_TARGET=host.docker.internal:7233 test python -m pytest tests/test_signal_history_live.py -vv
```


## 5. 本地 Temporal（只在要跑真实 Worker 时）

默认地址是 `localhost:7233`。任选一种方式：

- 官方 CLI：`temporal server start-dev --port 7233`
- 或使用本仓库 `compose.yaml` 的 `temporal` profile：`docker compose --profile temporal up temporal`（监听 `localhost:7233`）

确认服务在 `localhost:7233` 后再启动 Worker。

## 6. 启动 Worker

配置根目录是必填的，脚本和 Python 入口都不会给默认值。

```powershell
.\scripts\start-worker.ps1 -ConfigRoot D:\pipeline-config
```

等价的 Python 入口：

```powershell
python -m dev_pipeline.worker --config-root D:\pipeline-config
```

可选参数：

- `--temporal-target` / `-TemporalTarget`，默认 `localhost:7233`
- `--task-queue` / `-TaskQueue`，默认 `dev-pipeline`

若省略 `--config-root`、路径不存在、不是绝对路径，或指向一个文件而不是目录，进程会以中文错误退出，而不会继续执行。

Worker 启动成功与配置根目录错误都会通过 structlog 记录，不再使用 `print`。CLI 参数没有变化：`--config-root` 仍是必填且必须已经存在的绝对目录。

非 Windows 环境可以使用 `scripts/start-worker.sh --config-root /absolute/path`，约定与上面相同。

## 7. 本骨架尚未包含的内容

- 真实 Coding / Review LLM Worker
- Cursor CLI 运行时
- 与 `agent-platform` 的对接
- 真实测试环境发布（目前只有拒绝生产目标的桩 Activity）
- 进度心跳的完整实现（目前是可查询的阶段 / Ticket / 测试部署 / 预算花费快照）
