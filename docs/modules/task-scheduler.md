# 任务调度

## 职责
统一封装所有「下发给 Android 的动作」，提供：
- 任务创建
- 设备路由（按 `robot_id`）
- WS 下发
- 回执处理
- 重试 / 超时
- 同设备串行（避免页面竞态）

## 任务类型
| type | payload | MVP |
| --- | --- | --- |
| `send_text` | `{conversation_external_id, text}` | 1 |
| `send_image` | `{conversation_external_id, image_url}` | 2 |
| `send_file` | `{conversation_external_id, file_url, filename}` | 2 |
| `add_friend` | `{search_keyword, hello_text}` | 4 |
| `update_remark` | `{contact_external_id, remark, tags[]}` | 4 |
| `post_moments` | `{text, image_urls[]}` | 4 |

## 模型
- `robot_tasks(id, robot_id, type, payload_json, status, attempts, max_attempts, last_error, conversation_id?, message_id?, created_at, updated_at)`
- 状态：`pending` → `dispatched` → `running` → `completed` / `failed` / `timeout`

## 关键流程
```
service.create_task(...)
  → 写库 status=pending
  → enqueue(robot_id)
  → 若 Android 在线 → WS dispatch & status=dispatched
  → Android 回 task.completed/failed → 落库 + 广播 task.updated
  → message_id 关联的 messages.status 同步更新
```

## 超时与重试
- WS 下发后 60s 无回执 → 标 `timeout`
- 失败可重试 ≤ `max_attempts`（默认 2）
- 重试间隔指数退避（5s → 20s → 60s）

## 验收
- [ ] 创建 `send_text` 任务能下发到指定设备
- [ ] Android 回执后任务状态正确更新
- [ ] 设备离线时任务停在 `pending`，上线后自动续发
