# Android 执行端

MVP1 阶段先搭出 Kotlin 工程骨架与 WebSocket 通信、监听服务、任务执行框架。AccessibilityService 的 UI 解析与企微页面状态机在 MVP1b 完善（需要真机调试）。

## 模块

| 包 | 说明 |
| --- | --- |
| `net` | OkHttp 长连、事件编解码、重连 |
| `service.WeComAccessibilityService` | 抓取聊天页 + 模拟点击 |
| `service.MessageNotificationListener` | 兜底监听通知栏新消息 |
| `service.AgentForegroundService` | 保活前台服务，承载长连接 |
| `service.TaskExecutor` | 串行执行后端下发的任务 |
| `model` | 事件 / 任务数据类 |
| `ui.MainActivity` | 配置后端地址 / robot_id / token，查看运行状态 |

## 运行前置

1. 在 Web 管理台 `设备` 页创建设备，复制 `robot_id` 与 `token`。
2. APP 启动后填写：后端地址（`ws://<ip>:8000`）、`robot_id`、`token`。
3. 系统设置中开启"无障碍服务"与"通知使用权限"。
4. 加入电池白名单，保持后台运行。

## 调试

- 真机不便时用 `tools/mock_android.py`（项目根目录）模拟同一协议。
- 通信协议详见 [../docs/10-api-contracts.md](../docs/10-api-contracts.md)。
