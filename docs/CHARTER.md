# L0 项目宪法

冻结于 2026-11-10（Joe 确认，见 [DP-8](https://weiperfect007.atlassian.net/browse/DP-8)）。本文件只写原则，后续全链路继承。改动必须经人工签批（`promote_rule` 或同等确认），禁止静默修改。

## 1. 核心业务目标

用 Temporal 编排公司内部 AI Native 开发流程：写码 Worker、Code Review Worker、验证与 Retro 回流，并与 Jira、RAG、其他 Agents 配合。后期再与 `agent-platform` 对接，不在本仓库重做一套 Agent Session 运行时。

## 2. 硬红线（一票否决）

1. Workflow 只做确定性编排；LLM 调用、写代码、检索、测试、外部 IO 全部落在 Activity / Child Worker。
2. 禁止静默升级全局 Rule / Skill / 本宪法；晋升必须人工 Signal。
3. 写码 Worker 只能改授权 repo/worktree；Review Worker 默认只读 diff。
4. 同一 Ticket 重跑必须幂等（`ticket_id` + `execution_id`），不得重复开 PR 或改乱分支。
5. Jira 只镜像状态，不当编排器；任务对错以可复现证据为准，不把 dry-run 或历史记录当成 PASS。
6. 互辩、RAG、多模型必须有预算闸门；无新证据即停。
7. 任何生产发布禁止。测试环境受控发布须另有明确授权，见 [DP-43](https://weiperfect007.atlassian.net/browse/DP-43)。
8. 未确认 `lock_scope` / `decide_scheme` 前不得编码。
9. 单文件超过 2000 行视为 Review 阻断；更严软上限未拍前，不以软上限冒充已定。
10. 执行过程必须可见：当前步骤提示与定期心跳，避免阶段长时间不变时被当成挂死。进度不是催办：不催人去点 Signal，不把逐步进度刷到 Jira 评论，不把完整思考或密钥写入 History。细则见 [DP-47](https://weiperfect007.atlassian.net/browse/DP-47)。

## 3. 全局优先级

数据 / 任务不丢失 > 可观测与可回放 > 人工门禁清楚 > 迭代速度 > 极致性能

## 4. 故障爆炸半径与可容忍损失

- 单次执行失败不得破坏其他 Ticket 的工作区或已合并主线。
- 默认不自动 push、不自动合并、不自动改全局规则。
- 预算超限时停止并记录，不得无感继续计费。
- `abort` 必须留下可回放记录，并回收本 execution 创建的未合并分支 / worktree；已存在的远程 PR 不得偷偷关闭或合并，只标记并等待人工处理（关闭或保留的具体动作仍待证）。`reopen` 必须开新 `execution_id`，可继承只读产物，不得复用已乱掉的工作区。

## 5. 技术范式与模块边界

- 父 Workflow：`DevPipelineWorkflow` 管阶段、门禁、Jira 同步、预算、幂等。一条父 Workflow 对应一张 Story；每张实现 Ticket 用 Child。
- Child / Activity：Coding、双 Review、验证、RAG、Retro。
- 与 `agent-platform` 的对接面另开契约，本期不做实现。
- 包名 `dev_pipeline`；代码规范见 `.cursor/rules/` 与 `pyproject.toml`（Ruff 行宽 100，mypy strict）。

## 6. 多角色需求分析方法

立项与每张 Story 顶层构思使用：交付编排、产品/业务、架构、实现、真实用户、UX。角色是分析方法，不是自动呼叫其他 Bot 的授权。

## 7. 一期工作默认（2026-11-10 一并冻结）

改期须再拍，不得静默扩大或缩窄。

1. 一条父 Workflow 对应一张 Story；每张实现 Ticket 用 Child。多 Ticket 默认按依赖拓扑执行；无依赖可并行；单张失败不自动取消同 Story 其他未受影响的 Ticket，但 Story 不能进 Retro，直到失败被处理或人工 `abort`。
2. 本期无人产品界面。人通过本仓库 CLI 或 MCP（Codex / Grok 对话）发 Signal、查状态；Temporal UI 只对照 History。Jira 只镜像，不当操作台。带约定 label 的票可由监听触发启动，但不由 Jira 驱动阶段。
3. 默认不自动 `push`、不自动合并、不自动改全局规则。开 PR 必须另有明确授权；未授权则只在授权 worktree 产生产物。测试环境受控发布是例外，见 [DP-43](https://weiperfect007.atlassian.net/browse/DP-43)；任何生产发布禁止。
4. `abort` 必须留下可回放记录，并回收本 execution 创建的未合并分支 / worktree；已存在的远程 PR 不得偷偷关闭或合并，只标记并等待人工处理（具体动作待证）。`reopen` 开新 `execution_id`，可继承只读产物（范围、方案、假设、已关闭的 Ticket 证据），不得复用已乱掉的工作区。
5. 对话 / CLI / 监听（及后期会议预留）落到同一 Intake，见 [DP-37](https://weiperfect007.atlassian.net/browse/DP-37)。
6. Intake 后做规模分流，见 [DP-41](https://weiperfect007.atlassian.net/browse/DP-41)。未确认 `lock_scope` / `decide_scheme` 前不得编码；大需求必须停在门禁等人。
7. 单文件超过 2000 行视为 Review 阻断，见 [DP-44](https://weiperfect007.atlassian.net/browse/DP-44)。更严软上限未拍前，不以软上限冒充已定。
8. 不会看代码的 BA 用白话验收面完成范围确认与测试环境业务验收，见 [DP-42](https://weiperfect007.atlassian.net/browse/DP-42)。
9. 一期过线以 [DP-34](https://weiperfect007.atlassian.net/browse/DP-34) / [DP-45](https://weiperfect007.atlassian.net/browse/DP-45) 为准。会议分析 [DP-35](https://weiperfect007.atlassian.net/browse/DP-35) 与对照 `stock-equity-note` 抽 template [DP-36](https://weiperfect007.atlassian.net/browse/DP-36) 不在一期，本期不抽代码。
10. 一期审需求、写码、双 Review 的 LLM 运行时统一为 Cursor CLI（`agent`）+ 配置根目录中的 `CURSOR_API_KEY`。不自建 agent 循环、不自建模型网关、本期不对接 agent-platform。细则见 [DP-46](https://weiperfect007.atlassian.net/browse/DP-46)。
11. 执行过程必须可见，避免阶段长时间不变时被当成挂死。Activity 持续发出当前步骤提示（类似 Cursor 正在执行的步骤）并定期心跳；CLI / MCP / Query 能看到当前步骤、最近进度行和最近心跳。进度不是催办：仍不催人去点 Signal，不把逐步进度刷到 Jira 评论，不把完整思考或密钥写入 History。细则见 [DP-47](https://weiperfect007.atlassian.net/browse/DP-47)。
