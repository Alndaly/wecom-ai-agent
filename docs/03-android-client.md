# 03 · Android 执行端

## 3.1 职责

模拟真人操作企业微信客户端，作为整个系统对企微的唯一"出入口"。

| 类型 | 能力 |
| --- | --- |
| 入向 | 监听新消息、监听通知、读取聊天页文本、识别页面状态 |
| 出向 | 发送文本 / 图片 / 文件 / 朋友圈、加好友、改备注 |
| 自治 | 心跳上报、设备状态、页面异常自动恢复 |
| 观测 | 截图上传、日志上报、任务执行回执 |

## 3.2 技术构成

| 模块 | 技术 |
| --- | --- |
| 消息监听 | `AccessibilityService`（聊天页 DOM 抓取）+ `NotificationListenerService`（兜底） |
| 后端通信 | OkHttp WebSocket（长连）+ Retrofit（事件上报、文件上传） |
| 保活 | `ForegroundService` + 系统省电白名单引导 |
| 文本识别 | 优先 AccessibilityNodeInfo，OCR（ML Kit）作为兜底 |
| 任务执行 | 队列化（`TaskExecutor`），同设备串行避免页面竞态 |

## 3.3 页面状态机

```
        ┌──────┐
        │ HOME │
        └──┬───┘
   ┌───────┼────────┬────────┬─────────┐
   ▼       ▼        ▼        ▼         ▼
SEARCH  CHAT    CONTACT  MOMENTS    UNKNOWN
                                     │
                              恢复策略 → HOME
```

- 每个 task 执行前 MUST 确认当前页面，不在目标页 → 走"导航子任务"。
- `UNKNOWN` 出现 N 次 / M 秒 → 上报 `device.page_lost`，由后端降级（暂停下发新任务，等人工介入）。

## 3.4 与后端的协议

详见 [10-api-contracts.md](10-api-contracts.md#android-ws)。MVP1 必须实现的事件：

| 方向 | event | 用途 |
| --- | --- | --- |
| Android → 后端 | `device.hello` | 注册 / 重连 |
| Android → 后端 | `device.heartbeat` | 30s 一次 |
| Android → 后端 | `message.received` | 上报客户新消息 |
| Android → 后端 | `task.completed` / `task.failed` | 任务执行回执 |
| 后端 → Android | `task.dispatch` | 下发任务（含 `taskId`、`type`、`payload`） |
| 后端 → Android | `device.command` | 软指令（重启监听、清缓存、截图） |

## 3.5 验收标准（MVP1）

- [ ] 设备启动后能向后端注册并保持心跳 ≥ 1 小时不掉线。
- [ ] 在企微聊天页能稳定捕获文本消息并上报（实测准确率 ≥ 95%）。
- [ ] 收到 `send_text` 任务能在 ≤ 5s 内完成发送并回执。
- [ ] 异常页面 30s 内能自动回到 HOME。
