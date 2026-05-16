# 12 · 术语表

| 术语 | 含义 |
| --- | --- |
| 企微 | 企业微信客户端 |
| Robot / Android | 一台跑 RPA 的安卓设备，对应一个 `robot_id` |
| Contact | 企微通讯录里的一个客户（按 `(robot_id, wxid)` 唯一） |
| Conversation | 一台 Robot 与一个 Contact 的对话 |
| Message | 一条消息，方向 in / out |
| Task | 后端创建并进入设备队列的动作（发文 / 语义目标 / 后续发图等） |
| Operator | 人工客服 |
| Mode | 会话模式：AI / 人工 / 混合 |
| Feedback Status | 客户入站消息是否已被 AI / 人工处理的状态 |
| Queue | 每个 Robot 独立的后端任务队列，同设备串行且可取消 |
| Trace ID | 一次 AI 决策的链路 ID，可在审计中检索 |
| Team / 租户 | 数据隔离的最小单位 |
