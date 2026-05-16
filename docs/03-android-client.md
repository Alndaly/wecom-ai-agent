# 03 · Android 执行端

## 3.1 职责

Android 是系统对企业微信的设备侧入口，负责采集真实消息、暴露页面观察能力、执行后端下发的通用 UI 原语。

| 类型 | 能力 |
| --- | --- |
| 入向 | 通知监听、聊天页 harvest、消息页扫描、系统消息过滤前置 |
| 出向 | 执行点击、输入、返回、打开企微、截图、UI dump 等原语 |
| 自治 | 心跳上报、自动重连、前台服务保活、页面状态上报 |
| 观测 | 截图、节点树、命令回执、执行日志 |

## 3.2 技术构成

| 模块 | 技术 |
| --- | --- |
| 消息监听 | `AccessibilityService` + `NotificationListenerService` + `MessageListScanner` |
| 后端通信 | OkHttp WebSocket（主链路）+ Retrofit（事件和上传兜底） |
| 保活 | `ForegroundService` + 系统省电白名单引导 |
| 页面观察 | AccessibilityNodeInfo dump + API 30+ 截图 |
| 任务执行 | 后端队列串行；Android 只执行 `device.command` 原语 |

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

页面状态只作为观测提示。真正发送时，后端 ReAct agent 每一步都会重新请求 UI dump / 截图，并根据当前节点树决定下一步，不依赖固定坐标或固定输入框 id。

## 3.4 与后端的协议

详见 [10-api-contracts.md](10-api-contracts.md#102-wsandroidwsandroid)。当前主链路事件：

| 方向 | event | 用途 |
| --- | --- | --- |
| Android → 后端 | `device.hello` | 注册 / 重连 |
| Android → 后端 | `device.heartbeat` | 30s 一次 |
| Android → 后端 | `message.received` | 上报客户新消息 |
| Android → 后端 | `device.command_ack` | 命令已接收 |
| Android → 后端 | `device.command_result` | 通用原语执行结果 |
| Android → 后端 | `device.ui_dump` | UI 节点树与屏幕尺寸 |
| Android → 后端 | `device.screen_frame` | 实时屏幕帧 |
| 后端 → Android | `device.command` | 打开企微、点击、输入、滑动、截图、dump 等 |

`task.completed` / `task.failed` 只保留给旧任务回执和本地测试兼容；`send_text` 的真实执行已经由后端 ReAct + `device.command_result` 驱动。

## 3.5 采集规则

- 通知监听忽略企微聚合通知里的“未读消息”摘要，避免把系统摘要当客户消息。
- 聊天页 harvest 采用 baseline-then-diff，进入历史会话时不会重放旧消息。
- 消息页扫描只在用户已经位于消息 tab 时滑动，且滑动区域限定在列表 bounds 内。
- 所有入站最终由后端再次去重和过滤系统消息；设备侧过滤是减少噪声，不是唯一防线。

## 3.6 验收标准

代码层：
- [x] WS 连接 + 心跳 + 自动重连
- [x] NotificationListener 解析 WeCom 通知并上报 `message.received`
- [x] AccessibilityService 在 CHAT 页主动 harvest 新气泡
- [x] MessageListScanner 分层扫描消息页未读预览
- [x] `device.command` 原语回 `device.command_result`
- [x] 失败自动 dump 节点树到 logcat + 上传 `device.ui_dump`
- [x] dry-run 开关 + 校准 / 测试按钮

真机层：
- [ ] 抓取通知准确率 ≥ 95%
- [ ] 后端 ReAct `send_text` 在企微聊天里完成发送
- [ ] 异常页面 30s 内能自动回到 HOME 或上报可诊断状态

完整真机部署 + UI 校准步骤见 [14-android-real-device.md](14-android-real-device.md)。
