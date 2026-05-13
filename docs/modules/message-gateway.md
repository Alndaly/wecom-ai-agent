# 消息网关

## 职责
- 接收 Android 上报的客户消息事件
- 去重（基于 `external_msg_id` 或 `(robot_id, contact_external_id, sent_at, content_hash)`）
- 持久化 `messages`
- 触发会话路由（找/建 conversation → 决定 AI / 人工）

## 入口
- WS 事件：`message.received`
- 等价 REST（用于回放 / 调试）：`POST /android/events/message`

## 事件载荷
```json
{
  "event": "message.received",
  "robot_id": "robot_001",
  "contact": { "external_id": "wxid_xxx", "nickname": "张三", "avatar": null },
  "external_msg_id": "msg_xxx",
  "type": "text",
  "content": "在吗",
  "sent_at": "2026-05-13T12:00:00Z"
}
```

支持的 `type`：`text` / `image` / `file` / `voice` / `video` / `system`（MVP1 只做 text）。

## 处理流程
```
收到事件
  → 校验 robot 合法性
  → 去重（命中则丢弃）
  → upsert contact
  → 找 / 建 conversation
  → 落 messages（direction=in）
  → 广播到 Web (/ws/web)
  → if conversation.mode in (AI, MIXED): 触发 AI Workflow (MVP2)
```

## 验收
- [ ] 同一 `external_msg_id` 重复上报 → 只入库一次
- [ ] 客户首条消息 → 自动创建 conversation
- [ ] Web 工作台能在 ≤ 1s 看到这条消息
