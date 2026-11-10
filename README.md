# dev-pipeline

公司内部 AI Native 开发流程的编排仓库：以 Temporal 为内核，调度写码 Worker、Code Review Worker、验证与 Retro，并与 Jira、RAG 及其他 Agents 配合。

本仓库独立于 `agent-platform`。后期再对接，当前不拷贝、不实现 Agent Session 运行时。

Jira 项目：[dev-pipeline（DP）](https://weiperfect007.atlassian.net/jira/software/projects/DP/boards/34)。票只写在 Jira，本仓库不维护 `TASKS.md`。Jira 只镜像流程状态，不当编排器。

## 当前阶段

立项中。协作约定与代码规范已从 `common-worker` / `agent-platform` 迁入并适配，包名 `dev_pipeline`。Jira 项目 Key 已定为 `DP`。`docs/CHARTER.md` 中的 L0 已于 2026-11-10 冻结。远程仓库待定。尚未实现 Workflow 或 Worker。

开工先读 `plans/AI-CONTEXT.md`、`AGENTS.md`、`docs/CHARTER.md`。状态机与契约的详细底稿见 `docs/architecture/dev-pipeline.md`。

## 推荐形态

父 Workflow `DevPipelineWorkflow` 只管阶段、门禁、Jira 同步、预算与幂等。写码、RAG、双 Review、验证是 Activity 或 Child，不把 agent loop 写进 Workflow。Coding Worker 只碰授权 worktree；Review Worker 默认只读 diff。互辩 / RAG / 多模型的预算闸门挂在父 Workflow：超限即停并记一条 Bad。

## 流程状态机

```mermaid
stateDiagram-v2
    [*] --> Intake: Jira Signal / 人工开单
    Intake --> ScopeLocked: 范围+非目标已锁
    ScopeLocked --> SchemeDecided: 方案取舍已拍
    SchemeDecided --> Spike: 假设可能击穿 L0
    SchemeDecided --> TicketsReady: 无高危假设
    Spike --> TicketsReady: 最小验证过
    Spike --> Blocked: 击穿红线且无防护
    TicketsReady --> Implementing: 派 Coding Worker
    Implementing --> Reviewing: diff 提交
    Reviewing --> Implementing: 阻断项打回
    Reviewing --> HumanGate: 阻断/范式漂移
    Reviewing --> Verifying: 仅建议/信息
    HumanGate --> Implementing: 人改完续跑
    HumanGate --> Verifying: 人放行
    HumanGate --> Blocked: 人否决
    Verifying --> Retro: 过线证据齐
    Verifying --> Implementing: 验证未守住红线
    Retro --> Done: daily+candidate 已写
    Blocked --> [*]
    Done --> [*]
```

可进入 `Blocked`：红线被击穿且无防护，或人工否决。`Reviewing` 若仅有建议/信息，可直接 `Verifying`；有阻断项或范式漂移必须 `HumanGate`。验证未守住红线则回到 `Implementing`。测试环境受控发布是默认不自动 push 的例外，阶段名见 [DP-43](https://weiperfect007.atlassian.net/browse/DP-43)，尚未锁进上图。

## 阶段契约

| 阶段 | 输入 | 输出（进下一阶段的硬条件） |
|---|---|---|
| Intake | Jira key、reporter、原始诉求 | 规范化 Story 草稿；缺项标 `待证` |
| ScopeLocked | L0 Rule/Skill + Story 草稿 | 要解决 / 不做什么；禁止未锁范围就开编码 |
| SchemeDecided | 范围 + RAG 规范/历史事故 | 取舍记录；假设清单（红线级 / 局部）；全局 vs 局部 vs 候选 |
| Spike（可选） | 红线级假设 | 半小时～半天可证伪结果；失败则 Blocked 或补防护 |
| TicketsReady | 已拍方案 | Ticket 清单：目标、验收、依赖、I/O 契约 |
| Implementing | 单张 Ticket + worktree 权限 | 分支、diff、单测、回滚说明；幂等键 `ticket_id + execution_id` |
| Reviewing | diff + L0 + 本 Story 局部约束 | 双 Agent 并行：阻断 / 建议 / 信息 + 引用规则条文；无新证据就停 |
| HumanGate | 仅阻断项或跨模块/范式漂移 | 放行 / 打回 / 否决（Signal） |
| Verifying | 验收步骤 + L0 红线 | 「红线 ↔ 证据」至少一行；命令/现象可复现 |
| Retro | 当日证据（git、Jira、Codex session、评审） | `docs/knowledge/daily/YYYY-MM-DD.md`；candidate 更新；全局草案等签批 |

## 人工 Signal

人只出现在这些点：

- `lock_scope` / `decide_scheme`：锁范围、拍方案
- `approve_spike`：高危假设是否接受防护后继续
- `review_override`：阻断项或范式漂移放行/打回/否决
- `promote_rule`：候选升 L0 / 全局 Skill（默认禁止自动升）
- `abort` / `reopen`：中止或重开同一 Ticket（必须开新 execution，不复用已乱掉的一次）

## 硬约束（L0，2026-11-10 冻结）

完整条文与一期工作默认见 `docs/CHARTER.md`。

1. Workflow 只做确定性编排；LLM 调用、写代码、检索、测试、外部 IO 全部落在 Activity / Child Worker。
2. 禁止静默升级全局 Rule / Skill / 本宪法；晋升必须人工 Signal。
3. 写码 Worker 只能改授权 repo/worktree；Review Worker 默认只读 diff。
4. 同一 Ticket 重跑必须幂等（`ticket_id` + `execution_id`），不得重复开 PR 或改乱分支。
5. Jira 只镜像状态，不当编排器；任务对错以可复现证据为准，不把 dry-run 或历史记录当成 PASS。
6. 互辩、RAG、多模型必须有预算闸门；无新证据即停。
7. 任何生产发布禁止。测试环境受控发布须另有明确授权。
8. 未确认 `lock_scope` / `decide_scheme` 前不得编码。
9. 单文件超过 2000 行视为 Review 阻断。
10. 执行过程必须可见：当前步骤提示与定期心跳，避免被当成挂死。进度不是催办；完整思考与密钥不进 History。

优先级：数据 / 任务不丢失 > 可观测与可回放 > 人工门禁清楚 > 迭代速度 > 极致性能。

## 技术栈（拟定）

- Python 3.12
- Temporal Python SDK
- Ruff（行宽 100）+ mypy strict + pytest

## 本地准备

Jira 项目 Key 已定为 `DP`。L0 已冻结。待确认远程仓库后，再初始化依赖与开发环境。不要在未确认远程仓库前开通对外部仓库的写权限。
