# 项目协作约定

- 日常开工先读 [最小上下文入口](plans/AI-CONTEXT.md)，按 Jira Ticket 定向读取需求、直接依赖和关联 spec/plan；不默认全文读取历史计划、CHANGELOG、日报或 evidence。任务状态以 Jira 为准，不维护本地 TASKS 镜像。
- 统一使用 Superpowers：模块级纯需求先记入 Jira，经用户确认后按 `brainstorming` 定方案，按 `writing-plans` 形成独立可验收、可 review 的小任务。方案与计划按 Ticket 链接保存在 `plans/superpowers/`；实现前记录关键决定，不在实现中静默扩大范围。
- Cursor 与 Codex 按需求确认、记录方案、实现、测试、立即 code review 的顺序工作。Important（改变方案、验收、数据或权限边界）交用户决定，其余确认缺陷直接修。Epic 表示业务能力，Story 表示模块需求，技术 Task 关联需求票；阶段使用标签，真正前置条件使用阻塞链接。
- 任务状态、直接依赖、验收和执行顺序以 Jira 为准。方案文档不另存状态，不把设计完成记作功能完成。
- 本仓库是独立项目：用 Temporal 编排公司内部 AI Native 开发流程（写码 Worker、Code Review Worker、验证与 Retro），后期再与 `agent-platform` 对接。在对接合同写清之前，禁止把 `agent-platform` / `common-worker` 的业务代码拷进来，也不在本仓库实现 Agent Session 运行时。
- Python 包名为 `dev_pipeline`。管理入口后续放在 `src/dev_pipeline/`，不沿用 `src/agent_platform/`。
- 用户已确定的技术方案优先于模型此前的建议；变更方案时同步对应规格和 Task，不能在实现中静默换方案。
- 画架构图、流程图、时序图、数据流、生命周期或状态图时，使用 Archify 技能并按其当前说明出图，不手写 SVG、Mermaid 或 HTML 代替。立项讨论可用文字；需要正式图时再出 Archify。
- 分析、设计或重检需求时使用多角色视角，按实际风险选择深度，结论和验收同步到 Jira/规格；不自动授权子代理，不把未来架构纳入当前实现，也不在未要求时去呼叫其他 Bot：
  - 交付编排：阶段、门禁、范围与验收边界
  - 产品/业务（PM/BA）：范围、非目标、用户故事与验收口径
  - 架构：模块边界、Temporal 确定性、契约与爆炸半径
  - 实现：代码、测试、PR
  - 真实用户：真实操作与失败恢复
  - UX：入口、状态、反馈是否可理解
- Jira 操作固定走项目凭据和 API：先检查已配置的 Atlassian 连接器；连接器不可用时使用已保存的 `JIRA_URL`、`JIRA_USERNAME`、`JIRA_API_TOKEN` 调 REST API。不得改走浏览器或重复索要已保存凭据。创建/更新后回读结果，不再同步本地 TASKS；token 不输出、不进仓库或命令日志。Jira 项目 Key 在立项确认前不得擅自沿用 CW。
