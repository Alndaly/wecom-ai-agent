# 任务调度

## 职责
统一封装所有后端驱动设备的动作，提供：
- 任务创建与审计日志
- 按 `robot_id` 路由
- 每设备串行执行，避免页面竞态
- 任务队列可视化
- 等待中任务取消
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
- `robot_tasks(id, robot_id, type, payload_json, status, priority, queue_seq, attempts, max_attempts, last_error, conversation_id?, message_id?, created_at, updated_at)`
- `robot_task_logs(id, task_id?, robot_id, level, message, payload_json, created_at)`
- 状态：`pending` / `dispatched` / `queued` / `running` → `completed` / `failed` / `timeout` / `cancelled`

## 关键流程
```
send_orchestrator.create_and_dispatch_send_text(...)
  → 写 Message(status=pending)
  → 写 RobotTask(status=dispatched)
  → task_queue.enqueue(robot_id, kind=send_text, priority=...)
  → 写 RobotTask(status=queued, priority, queue_seq)
  → Celery drain_robot_queue(robot_id) 唤醒 backend
  → backend 按 robot_id claim 下一个任务并标 running
  → runner 通过 ReAct + device.command 驱动 Android
  → 写 task.log / task.updated / message.updated
  → 若 payload 有 feedback_message_ids，同步入站 feedback_status
```

后端不再把完整 `send_text` 脚本作为 `task.dispatch` 丢给 Android。Android 只执行 `device.command` 原语，发送策略由后端 ReAct 根据实时 UI dump 决策。

Celery worker 不直接驱动手机。Android WebSocket 仍由 FastAPI 进程持有，所以
Celery 只调用内部接口 `/internal/tasks/drain/{robot_id}`。真正的设备串行锁在
backend 内部和数据库 claim 上：
- 同一个 `robot_id` 同一时间只能有一个 drain 在执行。
- 不同 `robot_id` 可以被多个 Celery worker 并行唤醒。
- 同一设备内按 `priority ASC, queue_seq ASC, id ASC` 执行。
- `agent_goal` 优先级 0，会排在自动回复 `send_text/send_media` 优先级 50 前面。

## 可视化与取消
- `GET /robots/{id}/queue` 返回当前运行任务和等待队列。
- `POST /robots/{id}/tasks/{task_id}/cancel` 取消等待中任务。运行中任务已经在控制真实手机 UI，Celery 不能安全地跨进程中断 coroutine，因此当前返回不可取消。
- 队列写 `[queue] waiting / starting / cancelled` 等日志，Web 设备页可以实时展示。

## 恢复
FastAPI 启动时 `recover_pending_tasks()` 会把遗留在 `dispatched/queued/running` 的任务改回 `queued` 并重新唤醒 Celery。已经 `completed/failed/cancelled/timeout` 的任务不会恢复。发送 runner 开始前会检查关联 outbound message 是否已经 `sent`；如果已经发送成功，只把 task 收敛成 `completed`，不会再次操作手机。

## 验收
- [ ] 创建 `send_text` 任务能进入指定设备队列
- [ ] 队列同一时间只运行一个设备任务
- [ ] 等待中任务可以取消
- [ ] Android 原语执行结果能推动任务状态更新
