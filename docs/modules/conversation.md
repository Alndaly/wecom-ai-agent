# 会话管理

## 职责
- 维护 (`robot_id`, `contact_id`) → `conversation` 一一映射
- 存储消息流
- 维护会话模式（AI / 人工 / 混合）与接管锁
- 维护未读数、最后消息预览

## 关键模型
- `contacts(id, team_id, robot_id, external_id, nickname, avatar, tags_json, stage, ...)`
- `conversations(id, robot_id, contact_id, mode, operator_id, ai_enabled, unread_count, last_message_at, last_message_preview)`
- `messages(id, conversation_id, direction, sender_type, sender_id, type, content, status, external_msg_id, created_at)`
  - `direction`：`in` / `out`
  - `sender_type`：`customer` / `ai` / `human`
  - `status`（仅 out）：`pending` / `sending` / `sent` / `failed`

## 接管锁
Redis：`lock:conversation:{id}` = `{operator_id, expire_at}`，TTL 默认 600s。

| 动作 | 行为 |
| --- | --- |
| 客服开始输入 / 接管 | SET 锁，模式临时切到 `human` |
| 客服发完且 5 分钟无动作 | 锁过期 → 模式回到原值（如 `mixed`） |
| AI 想发消息但发现锁存在 | 跳过本轮 |

## 接口（MVP1）
- `GET /conversations` 列表（支持 `robot_id`、`unread_only`、`q` 搜索）
- `GET /conversations/{id}` 详情
- `GET /conversations/{id}/messages?before=...&limit=50` 历史
- `POST /conversations/{id}/messages` 人工发送（body: `type`, `content`）
  - 服务端创建 `messages(direction=out, status=pending)` + 创建 `send_text` task
- `PATCH /conversations/{id}` 改模式 `{mode: ai|human|mixed}`

## 验收
- [ ] 客户消息能正确进会话
- [ ] 客服能从工作台发送文本，消息状态会从 pending → sent
- [ ] 切模式立即生效
