# 会话管理

## 职责
- 维护 (`robot_id`, `contact_id`) → `conversation` 一一映射
- 存储消息流、未读数、最后消息预览
- 维护会话模式（AI / 人工 / 混合）
- 为每条真实客户入站消息记录反馈状态
- 人工发送时把待反馈客户消息和出站任务关联起来

## 关键模型
- `contacts(id, team_id, robot_id, external_id, nickname, avatar, tags_json, stage, ...)`
- `conversations(id, robot_id, contact_id, mode, operator_id, unread_count, last_message_at, last_message_preview)`
- `messages(id, conversation_id, direction, sender_type, sender_id, type, content, status, external_msg_id, feedback_status, feedback_trace_id, feedback_at, feedback_reply_task_ids, created_at)`
  - `direction`：`in` / `out`
  - `sender_type`：`customer` / `ai` / `human` / `system`
  - `status`：出站发送状态，`pending` / `sending` / `sent` / `failed` / `cancelled`
  - `feedback_status`：入站客户消息的处理状态，见 [09-data-model.md](../09-data-model.md#feedback_status)

## 入站处理

`conversation.ingest_inbound_message` 做最小闭环：

1. 按 `external_msg_id` 去重。
2. 找到或创建 contact / conversation。
3. 过滤机器人回声、时间分隔线、企微系统消息。
4. 真客户消息落库为 `feedback_status=pending`，并广播 `message.new` / `conversation.updated`。
5. `mode=ai|mixed` 时唤醒 `auto_reply_scheduler`；`mode=human` 时只留待人工处理。

系统消息不落库，也不进入 `pending`，因此不会影响未读客户消息是否被反馈。

## 反馈状态

每条真实客户入站消息必须被记录是否得到反馈。AI 可以把多条未读合并总结成少量回复，所以反馈状态记录在入站消息上，而不是强制要求一条入站对应一条出站。

人工发送时，服务端会把该会话内 `pending/processing` 的客户消息标为 `queued`，并把它们的 id 放进 `send_text` 任务 payload。发送成功后统一标为 `replied`。

## 接口
- `GET /conversations` 列表（支持 `robot_id`、`unread_only`、`q` 搜索）
- `GET /conversations/{id}` 详情
- `GET /conversations/{id}/messages?before=...&limit=50` 历史
- `POST /conversations/{id}/messages` 人工发送（body: `type`, `content`）
- `PATCH /conversations/{id}` 改模式 `{mode: ai|human|mixed}`

## 验收
- [ ] 客户消息能正确进会话并标为 `feedback_status=pending`
- [ ] 系统消息不会产生待反馈记录
- [ ] 客服能从工作台发送文本，消息状态会从 pending → sent
- [ ] 人工发送能覆盖同会话待反馈客户消息
- [ ] 切模式立即生效
