# 项目最小上下文入口

## 当前权威入口

1. 阅读根目录 `AGENTS.md`、[README.md](../README.md) 和 [docs/CHARTER.md](../docs/CHARTER.md)。
2. 从本项目 Jira（Key 待立项确认）定位当前 Ticket，读取需求、状态、直接阻塞依赖和链接的方案。
3. 只读当前 Ticket 涉及的代码、spec/plan、测试入口；发生验收失败时再定向读取历史证据。

任务状态以 Jira 为准，不维护本地 TASKS 镜像。`plans/superpowers/` 只保存方案与计划，不另存状态。

## 项目边界

- 本仓库：Temporal 为编排内核，调度写码 / Review / 验证 / Retro 等 Worker，支撑公司内部 AI Native 开发流程。
- `agent-platform`（`D:\projects\common-worker`）：通用 Agent Session 平台。后期对接，当前只允许引用对接原则，禁止拷贝其业务实现。
- 人只出现在范围锁定、方案取舍、阻断项/范式漂移、L0 规则升级这些门禁点。

## Jira 协作

- Jira 是需求、状态、直接依赖和验收的唯一任务入口。方案完成不等于功能完成；完成必须有真实测试/验收证据。
- 执行下一个任务：先定向查候选票，再核对其直接阻塞依赖；未完成依赖不在实现中绕过。
- 使用已连接的 Atlassian API；不可用时使用已保存的 Jira 凭据调用 REST API。更新后回读，不输出 token。
- 模块需求先入 Jira，经用户确认后记录 Superpowers 方案与小任务计划，再实现和立即 review。

## 验收与运行

- 单任务先运行相关单测、相关集成测试和该任务验收；跨模块、核心状态机或阶段发布时扩大范围。
- 区分代码回归、环境/夹具故障和历史基线失败；保留命令、版本、环境、摘要、首个失败与证据路径。
- 不把 dry-run、历史证据或进程 Running 冒充本次业务验收 PASS。

## 文档读取边界

先读本文件、`AGENTS.md`、`docs/CHARTER.md` 和当前 Ticket 链接的 plan。不默认全文扫描历史计划或日报。
