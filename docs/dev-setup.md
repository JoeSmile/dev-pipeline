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

## 4. 本地 Temporal（只在要跑真实 Worker 时）

默认地址是 `localhost:7233`。任选一种方式：

- 官方 CLI：`temporal server start-dev --port 7233`
- 或按 Temporal 文档使用 Docker 启动开发服务

确认服务在 `localhost:7233` 后再启动 Worker。

## 5. 启动 Worker

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

## 6. 本骨架尚未包含的内容

- 真实 Coding / Review LLM Worker
- Cursor CLI 运行时
- 与 `agent-platform` 的对接
- 真实测试环境发布（目前只有拒绝生产目标的桩 Activity）
- 进度心跳的完整实现（目前是可查询的阶段 / Ticket / 测试部署快照）
