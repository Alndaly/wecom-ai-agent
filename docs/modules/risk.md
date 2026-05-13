# 风控（MVP5）

## 监控指标
- 单设备 / 单会话发消息频率
- 相似话术比例（避免明显机器人）
- 深夜发送（23:00 - 7:00 默认禁用）
- 连续营销消息（连续 N 条带链接 / 价格）
- 高频加好友（默认每设备每日 ≤ 20）
- 群发频率
- 被删率（最近 N 天）

## 模型
- `risk_rules(id, team_id, type, threshold_json, action, enabled)`
  - `action`：`block` / `warn` / `escalate`
- `risk_events(id, team_id, rule_id, robot_id?, conversation_id?, severity, payload_json, created_at)`

## 介入点
- 任务调度 dispatch 前
- AI Workflow `risk_check` 节点

## 验收
- [ ] 阈值命中能 block 任务并落 event
- [ ] 控制台能查看 / 处理 event
