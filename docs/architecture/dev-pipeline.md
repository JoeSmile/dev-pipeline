# DevPipeline 状态机（立项草图）

父 Workflow `DevPipelineWorkflow` 只管阶段、门禁、Jira 同步、预算与幂等。写码 / RAG / 双 Review / 验证是 Activity 或 Child，不把 agent loop 写进 Workflow。

正式架构图按 `AGENTS.md` 使用 Archify 产出；下文保留状态与契约，便于立项讨论。

## 阶段

Intake → ScopeLocked → SchemeDecided →（可选 Spike）→ TicketsReady → Implementing → Reviewing →（必要时 HumanGate）→ Verifying → Retro → Done

可进入 Blocked：红线被击穿且无防护，或人工否决。

Reviewing 若仅有建议/信息，可直接 Verifying；有阻断项或范式漂移必须 HumanGate。验证未守住红线则回到 Implementing。

## 契约摘要

| 阶段 | 进入下一阶段的硬条件 |
|---|---|
| Intake | 规范化 Story 草稿，缺项标待证 |
| ScopeLocked | 要解决 / 不做什么已锁 |
| SchemeDecided | 取舍记录 + 假设分级（红线 / 局部 / 候选） |
| Spike | 高危假设有可证伪结果；失败则 Blocked 或补防护 |
| TicketsReady | 每张票有目标、验收、依赖、I/O 契约 |
| Implementing | 分支、diff、单测、回滚说明；幂等键 `ticket_id + execution_id` |
| Reviewing | 双 Agent 并行输出阻断/建议/信息，并引用规则条文；无新证据即停 |
| HumanGate | 人工 Signal：放行 / 打回 / 否决 |
| Verifying | 至少一行「红线 ↔ 证据」，命令或现象可复现 |
| Retro | 写入 `docs/knowledge/daily/YYYY-MM-DD.md`，更新 candidates；全局草案待签批 |

## 人工 Signal

`lock_scope`、`decide_scheme`、`approve_spike`、`review_override`、`promote_rule`、`abort`、`reopen`（重开必须新 execution）。
