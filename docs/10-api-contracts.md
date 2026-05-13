# 10 · API 与 WS 协议

约定：
- 所有时间戳 ISO 8601 UTC（`2026-05-13T12:00:00Z`）。
- 所有 ID 用字符串（避免 JS 大整数精度问题）。
- 错误用 `{ "detail": "..." }`，HTTP 状态码即语义。

## 10.1 REST（MVP1）

### Auth
```
POST /auth/login      body: {email, password}    → {access_token, token_type}
GET  /auth/me         → {id, email, display_name, team_id}
```

### Robots
```
GET    /robots                      → Robot[]
POST   /robots         {name}       → {robot, token}    (token 只返回这一次！)
GET    /robots/{id}                 → Robot
DELETE /robots/{id}                 → 204
```

### Conversations
```
GET  /conversations?robot_id=&unread_only=&q=         → Conversation[]
GET  /conversations/{id}                              → Conversation
PATCH /conversations/{id}   {mode}                    → Conversation
GET  /conversations/{id}/messages?before=&limit=50    → Message[]
POST /conversations/{id}/messages   {type, content}   → {message, task}
```

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
| `task.completed` | `{task_id, result?}` |
| `task.failed` | `{task_id, error, retryable}` |
| `device.page_lost` | `{snapshot_url?}` |

### 后端 → Android
| event | payload |
| --- | --- |
| `task.dispatch` | `{task_id, type, payload}` |
| `device.command` | `{command: "screenshot"|"restart"|"clear_cache"}` |

## 10.3 WS：Web（`/ws/web`）

**URL**：`ws://.../ws/web?token=<JWT>`

后端 → Web 推送事件见 [realtime.md](modules/realtime.md)。客户端目前只发 `{op: "ping"}`。

## 10.4 错误码
| 状态 | 含义 |
| --- | --- |
| 400 | 参数错误 |
| 401 | 未鉴权 / Token 无效 |
| 403 | 无权限 / 跨租户 |
| 404 | 资源不存在 |
| 409 | 状态冲突（如设备 ID 已存在） |
| 422 | Pydantic 校验失败 |
| 429 | 频控（MVP5 风控） |
| 5xx | 服务异常 |
