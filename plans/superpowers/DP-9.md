# DP-9 骨架决策记录

本记录固化 [DP-9](https://weiperfect007.atlassian.net/browse/DP-9) 父 Workflow 骨架在 Code Review 之后采用的主流做法。不扩展产品范围：写码 / Review 仍是桩，不接 Cursor CLI，不实现真实部署器。

## 测试环境发布必须另有授权

全部未移除 Ticket 验证过线后，父执行停在聚合 `Implementing`，查询快照提示「正在等候你确认测试环境发布」。只有人工 Signal `authorize_test_deploy` 之后，才进入 `TestDeploying` 并调用 `publish_to_test_env`。没有该 Signal 不得发布，也不得进入 Retro。这落实 L0「测试环境受控发布须另有明确授权」。

## 启动清单不接受可延后 Ticket

`TicketSpec` 没有 `required` 或等价的启动参数。清单中的每张票都是必做的。唯一的范围内移除方式是启动后的 `drop_ticket`；被移除的票不再阻挡 TestDeploying / Retro。任何未移除且仍在飞行阶段或失败未处理的票都会阻挡。

## 依赖门禁在 Child 上真实生效

`TicketWorkflow` 从 `Pending` 开始。`dependencies_verified` 守卫不再被当成永远为真。父编排只在依赖过线后启动 Child，并发送 `dependencies_satisfied`。若 Child 被直接启动且声明了未过线依赖，则停在 `Pending`，不得跳到 `Implementing`。

## abort 必须停掉 Child

Story `abort` 不只依赖 `ParentClosePolicy.ABANDON`。父执行会向仍在运行的 Child 发送 `abort` 并取消它们，等待结束后再以可回放结果进入 `Blocked`。关闭策略使用 `REQUEST_CANCEL` 作为安全网。

## 状态机分成两张图

README 与架构文档分别给出父、Child 两张 mermaid 图，不再把两台机器画在一起。父执行进入 TestDeploying 的边是 `Implementing → TestDeploying`，不是 `Verifying --> TestDeploying`。

## 模块拆分

`DevPipelineWorkflow` 仍是 Temporal 入口；快照与状态在 `workflows/parent_state.py`，Signal 在 `workflows/parent_signals.py`，编排步骤在 `workflows/parent_orchestrate.py`。`TicketWorkflow` 的阶段步骤在 `workflows/ticket_steps.py`。测试用的转移表 oracle 放在 `tests/transition_oracles.py`，不进生产 `transitions.py`。
