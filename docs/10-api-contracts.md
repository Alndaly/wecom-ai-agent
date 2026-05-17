# 10 · API 与 WS 协议

约定：
- 所有时间戳 ISO 8601 UTC（`2026-05-13T12:00:00Z`）。
- 所有 ID 在 Web API 中按数字返回；前端类型需要避免 JS 大整数精度问题。
- 错误用 `{ "detail": "..." }`，HTTP 状态码即语义。

## 10.1 REST

### Auth
```
POST /auth/login      body: {email, password}    → {access_token, token_type}
GET  /auth/me         → {id, email, display_name, team_id}
```

### Robots
```
GET    /robots                                      → Robot[]
POST   /robots                  {name}              → {robot, token}
GET    /robots/{id}                                  → Robot
DELETE /robots/{id}                                  → 204
GET    /robots/{id}/queue                            → RobotQueueSnapshot
POST   /robots/{id}/tasks/{task_id}/cancel           → RobotCommandOut
POST   /robots/{id}/agent/run     {goal, ...}        → RobotTask
```

`/queue` 返回当前运行任务和等待队列，等待项包含 `task_id`、`kind`、`priority`、`waited_ms`、`cancellable` 等字段。取消接口只取消等待中的任务；运行中的 ReAct 任务正在控制真实设备 UI，当前不做跨进程中断。

### Conversations
```
GET   /conversations?robot_id=&unread_only=&q=        → Conversation[]
GET   /conversations/{id}                             → Conversation
PATCH /conversations/{id}       {mode}                → Conversation
GET   /conversations/{id}/messages?before=&limit=50   → Message[]
POST  /conversations/{id}/messages {type, content}    → {message, task}
```

人工发送会把当前会话内仍处于 `pending/processing` 的客户入站消息标为 `queued`，并把这些 message id 写入出站任务的 `feedback_message_ids`，发送成功后统一标为 `replied`。

### Android 事件（兜底 REST，正常走 WS）
```
POST /android/events           Authorization: Bearer <robot_token>
body: 见 10.2 事件 schema
```

## 10.2 WS：Android（`/ws/android`）

**URL**：`ws://.../ws/android?robot_id=<rid>&token=<robot_token>`

**通用包格式**：
```json
{ "event": "<name>", "ts": "...", "payload": { ... } }
```

### Android → 后端
| event | payload |
| --- | --- |
| `device.hello` | `{version, model, android_version}` |
| `device.heartbeat` | `{current_page, battery, ...}` |
| `message.received` | 见 [message-gateway.md](modules/message-gateway.md) |
| `device.command_ack` | `{request_id}` |
| `device.command_result` | `{request_id, ok, message?, data?}` |
| `device.ui_dump` | `{request_id?, page, tree, nodes, screen_size}` |
| `device.screen_frame` | `{mime, width, height, data}` |
| `task.completed` | `{task_id, result?}`，旧任务 / 本地测试兼容 |
| `task.failed` | `{task_id, error, retryable}`，旧任务 / 本地测试兼容 |
| `device.page_lost` | `{snapshot_url?}` |

### 后端 → Android
| event | payload |
| --- | --- |
| `device.command` | `{request_id, command, args}` |

常用 `command` 包括 `open_wecom`、`tap_text`、`tap_xy`、`swipe`、`input_text`、`press_back`、`press_home`、`dump_ui`、`screenshot`。发送文本不是固定脚本；后端 ReAct agent 会连续发送这些原语。

## 10.3 WS：Web（`/ws/web`）

**URL**：`ws://.../ws/web?token=<JWT>`

客户端目前只发 `{op: "ping"}`。后端主要推送：

| event | payload |
| --- | --- |
| `message.new` | `MessageOut` |
| `message.updated` | `MessageOut`，含 `feedback_status/feedback_trace_id/feedback_at/feedback_reply_task_ids` |
| `message.deleted` | `{id, conversation_id}` |
| `conversation.updated` | `ConversationOut` |
| `conversation.deleted` | `{id}` |
| `task.updated` | `RobotTaskOut` |
| `task.log` | `RobotTaskLogOut` |
| `ai.suggestion` | `{conversation_id, trace_id, text, confidence, reason}` |
| `kb.hits` | `{conversation_id, trace_id, hits}` |
| `device.status` | `RobotOut` |
| `device.ui_dump` | `{robot_id, ...}` |
| `device.screen_frame` | `{robot_id, ...}` |

## 10.4 错误码
| 状态 | 含义 |
| --- | --- |
| 400 | 参数错误 |
| 401 | 未鉴权 / Token 无效 |
| 403 | 无权限 / 跨租户 |
| 404 | 资源不存在 |
| 409 | 状态冲突（如任务不可取消、设备 ID 已存在） |
| 422 | Pydantic 校验失败 |
| 429 | 频控（后续风控） |
| 5xx | 服务异常 |
