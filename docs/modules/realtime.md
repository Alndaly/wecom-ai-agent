# Web 实时通信

## 职责
为 Web 工作台提供单一 WS 通道，推送：

| event | payload | 触发时机 |
| --- | --- | --- |
| `message.new` | `{conversation_id, message}` | 客户消息入库 / 出向消息状态变化 |
| `conversation.updated` | `{conversation_id, ...}` | 模式切换、接管、未读数变化 |
| `robot.status` | `{robot_id, status, current_page}` | 设备上下线 / 心跳 |
| `task.updated` | `{task_id, status, error?}` | Android 回执 |
| `ai.suggestion` | `{conversation_id, suggestions[]}` | MVP2 AI 候选回复 |

## 协议
- URL：`ws://.../ws/web?token=<JWT>`
- 客户端 → 服务端：`{op: "subscribe", topics: ["robot:*", "conv:123"]}`（MVP1 简化，全订阅）
- 心跳：客户端每 30s 发 `{op: "ping"}`，服务端回 `{op: "pong"}`

## 实现
- `WsManager`：`team_id → set[WebSocket]`；广播按 team 隔离
- 跨进程：MVP1 单进程内存版；多 worker → Redis pub/sub（MVP5）

## 验收
- [ ] Web 登录后能建立 WS 连接
- [ ] Android 发来消息 ≤ 1s 出现在前端
- [ ] 断网 → 自动重连
