# 任务调度

## 职责
统一封装所有后端驱动设备的动作，提供：
- 任务创建与审计日志
- 按 `robot_id` 路由
- 每设备串行执行，避免页面竞态
- 任务队列可视化
- 等待中 / 运行中任务取消
- 启动时恢复未完成任务

## 任务类型
| type | payload | 当前状态 |
| --- | --- | --- |
| `send_text` | `{contact_external_id, text, feedback_message_ids?}` | 已实现 |
| `agent_goal` | `{goal, max_steps?, force_llm?}` | 已实现 |
| `send_image` | `{conversation_external_id, image_url}` | 规划中 |
| `send_file` | `{conversation_external_id, file_url, filename}` | 规划中 |
| `add_friend` | `{search_keyword, hello_text}` | 规划中 |
| `post_moments` | `{text, image_urls[]}` | 规划中 |

## 模型
- `robot_tasks(id, robot_id, type, payload_json, status, attempts, max_attempts, last_error, conversation_id?, message_id?, created_at, updated_at)`
- `robot_task_logs(id, task_id?, robot_id, level, message, payload_json, created_at)`
- 状态：`pending` / `dispatched` / `queued` / `running` → `completed` / `failed` / `timeout` / `cancelled`

## 关键流程
```
send_orchestrator.create_and_dispatch_send_text(...)
  → 写 Message(status=pending)
  → 写 RobotTask(status=dispatched)
  → task_queue.enqueue(robot_id, kind=send_text, priority=...)
  → queue consumer 标 running 并调用 runner
  → runner 通过 ReAct + device.command 驱动 Android
  → 写 task.log / task.updated / message.updated
  → 若 payload 有 feedback_message_ids，同步入站 feedback_status
```

后端不再把完整 `send_text` 脚本作为 `task.dispatch` 丢给 Android。Android 只执行 `device.command` 原语，发送策略由后端 ReAct 根据实时 UI dump 决策。

## 可视化与取消
- `GET /robots/{id}/queue` 返回当前运行任务和等待队列。
- `POST /robots/{id}/tasks/{task_id}/cancel` 取消等待中任务，或取消当前运行 coroutine。
- 队列写 `[queue] waiting / starting / cancelled` 等日志，Web 设备页可以实时展示。

## 恢复
FastAPI 启动时 `recover_pending_tasks()` 会把遗留在 `dispatched/queued` 的任务重新入队。已经 `completed/failed/cancelled/timeout` 的任务不会恢复。

## 验收
- [ ] 创建 `send_text` 任务能进入指定设备队列
- [ ] 队列同一时间只运行一个设备任务
- [ ] 等待中任务可以取消
- [ ] 运行中任务可以中断并写 `cancelled`
- [ ] Android 原语执行结果能推动任务状态更新
