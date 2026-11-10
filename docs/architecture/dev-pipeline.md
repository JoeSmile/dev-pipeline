# DevPipeline 状态机（骨架说明）

父 Workflow `DevPipelineWorkflow` 只管阶段、门禁、Jira 同步、预算与幂等。一条父执行对应一张 Story。写码 / RAG / 双 Review / 验证是 Activity 或 Child，不把 agent loop 写进 Workflow。

正式架构图按 `AGENTS.md` 使用 Archify 产出；下文描述 [DP-9](https://weiperfect007.atlassian.net/browse/DP-9) 已落地的骨架：合法转移表、Child 循环，以及已锁定的 `TestDeploying`。

## 父与 Child

- 父执行阶段：`Intake` → `ScopeLocked` → `SchemeDecided` →（可选 `Spike`）→ `TicketsReady` → 聚合 `Implementing` → `TestDeploying` → `Retro` → `Done`，或在门禁失败 / `abort` / 测试部署失败时进入 `Blocked`。
- Child（`TicketWorkflow`）循环：`Implementing` → `Reviewing` →（阻断或范式漂移时 `HumanGate`）→ `Verifying`。仅建议/信息可从 `Reviewing` 直达 `Verifying`。验证失败回到 `Implementing`。
- 只要仍有必做 Ticket 处于 `Implementing` / `Reviewing` / `HumanGate` / `Verifying`，或失败尚未处理，父执行不得进入 `TestDeploying` 或 `Retro`。
- 合法路径在全部必做 Ticket 验证过线后进入 `TestDeploying`。只有测试部署成功才允许 `Retro`。测试部署失败不得进入 `Retro`。不存在生产部署转移。

## 编排规则（DP-26 最小集）

- Ticket 按依赖拓扑执行：无依赖的 Ticket 可并行启动；依赖未过线的 Ticket 不得提前开工。
- 单张 Child 失败不自动取消同 Story 其他未受影响的 Ticket。
- 失败处理方式：对该 Ticket 重试、人工将其从必做清单中移除（`drop_ticket`），或对 Story `abort`。
- 幂等键：`ticket_id + execution_id`（同时用作 Child workflow id）。
- `reopen` 必须使用新的 `execution_id` 启动新的父执行，不能在已损坏的本次执行上恢复。

## 契约摘要

| 阶段 | 进入下一阶段的硬条件 |
|---|---|
| Intake | 规范化 Story 草稿，缺项标待证 |
| ScopeLocked | 要解决 / 不做什么已锁 |
| SchemeDecided | 取舍记录 + 假设分级（红线 / 局部 / 候选） |
| Spike | 高危假设有可证伪结果；失败则 Blocked 或补防护 |
| TicketsReady | 每张票有目标、验收、依赖、I/O 契约 |
| Implementing | 分支、diff、单测、回滚说明；幂等键 `ticket_id + execution_id`。父执行在此等待 Child |
| Reviewing | 双 Agent 并行输出阻断/建议/信息，并引用规则条文；无新证据即停 |
| HumanGate | 人工 Signal：放行 / 打回 / 否决 |
| Verifying | 至少一行「红线 ↔ 证据」，命令或现象可复现。全部必做过线后进入 TestDeploying |
| TestDeploying | 测试环境可达地址；失败则阻断，不得进入 Retro。禁止任何生产发布 |
| Retro | 写入 `docs/knowledge/daily/YYYY-MM-DD.md`，更新 candidates；全局草案待签批。当前为桩，不写文件 |

## 人工 Signal

`lock_scope`、`decide_scheme`、`approve_spike`、`review_override`、`promote_rule`、`abort`、`reopen`（重开必须新 execution）。`promote_rule` 在本骨架中只记录请求，自动晋升一律拒绝。

进度可见性（[DP-47](https://weiperfect007.atlassian.net/browse/DP-47)）目前是父执行上的查询快照：当前阶段、正在等待的 Signal、各 Ticket 状态、测试部署状态。完整心跳尚未实现。

## 配置根目录

启动 Worker 必须显式传入已存在的绝对路径 `--config-root`。缺失或无效必须可见地失败，见 `docs/dev-setup.md`。Workflow 模块本身不做文件系统访问；校验发生在 Worker 入口与 `validate_config_root` Activity。
