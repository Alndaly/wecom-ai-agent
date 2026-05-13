# 12 · 术语表

| 术语 | 含义 |
| --- | --- |
| 企微 | 企业微信客户端 |
| Robot / Android | 一台跑 RPA 的安卓设备，对应一个 `robot_id` |
| Contact | 企微通讯录里的一个客户（按 `(robot_id, wxid)` 唯一） |
| Conversation | 一台 Robot 与一个 Contact 的对话 |
| Message | 一条消息，方向 in / out |
| Task | 后端下发给 Android 的动作（发文 / 发图 / 加好友 …） |
| Operator | 人工客服 |
| Mode | 会话模式：AI / 人工 / 混合 |
| 接管锁 | Redis 上的 `lock:conversation:{id}`，避免 AI 与人工双发 |
| Trace ID | 一次 AI 决策的链路 ID，可在审计中检索 |
| Team / 租户 | 数据隔离的最小单位 |
