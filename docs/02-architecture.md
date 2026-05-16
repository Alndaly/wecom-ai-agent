# 02 · 系统架构

## 2.1 三端拓扑

```
┌────────────────────┐        WebSocket (android)        ┌──────────────────────┐
│  Android 执行端    │ ◀────────────────────────────────▶│                      │
│  (Kotlin RPA)      │        HTTP (events/upload)        │                      │
└────────────────────┘                                    │     后端服务         │
                                                          │   (FastAPI)          │
┌────────────────────┐        WebSocket (web)             │                      │
│  Web 管理台        │ ◀────────────────────────────────▶│                      │
│  (Next.js)         │        HTTP (REST + JSON)          │                      │
└────────────────────┘                                    └──────────┬───────────┘
                                                                     │
              ┌──────────────────────┬──────────────────┬────────────┼──────────────┐
              ▼                      ▼                  ▼            ▼              ▼
        PostgreSQL / SQLite     In-process WS Hub   Robot Queue   Milvus / Neo4j   LLM API
        (业务主库)              (单进程广播)         (每设备串行)  (RAG / KG)       (回复生成)
```

当前实现是单 FastAPI 进程内编排：WebSocket Hub、设备命令 request future、每设备任务队列、自动回复调度器都在进程内存中。横向扩展时需要补 Redis pub/sub、分布式锁和共享队列。

## 2.2 数据流

### 2.2.1 入向（客户消息 → 系统）

1. Android 通过通知监听、聊天页无障碍 harvest、消息页扫描发现企微消息。
2. Android 通过 `/ws/android` 上报 `message.received`。
3. 后端 `conversation.ingest_inbound_message` 去重、过滤系统消息和机器人回声。
4. 真客户入站消息落 `messages`，并写 `feedback_status=pending`。
5. 会话更新通过 WebSocket 推给 Web；如果会话模式是 `ai` 或 `mixed`，唤醒 `auto_reply_scheduler`。

系统消息不会落库，也不会进入未读反馈判断，避免和客户消息的公平调度互相污染。

### 2.2.2 自动回复（待反馈消息 → 决策）

1. `auto_reply_scheduler` 按 robot 运行，读取 `pending/processing` 的客户入站消息。
2. 同一轮只处理一个会话，最多取 20 条待反馈消息交给 AI，AI 可以总结多条未读后生成少量回复。
3. 同一个会话最多连续处理 2 轮；如果还有其他会话待反馈，必须回到待处理池切换会话。
4. `workflow.handle_inbound` 生成 `reply/suggest/skip`，并写 `ai_reply_logs`。
5. `reply` 最多创建 2 个出站 `send_text` 任务，并把本批入站消息标为 `queued`；`suggest/skip/failed` 会记录对应反馈状态。

### 2.2.3 出向（系统 → 客户消息）

1. 人工发送或 AI 回复调用 `send_orchestrator.create_and_dispatch_send_text`。
2. 后端创建出站 `Message` 与 `RobotTask`，任务状态进入 `dispatched`。
3. `task_queue` 按机器人维护优先级队列，同一台设备一次只跑一个任务。
4. 队列 runner 调用后端 ReAct 设备 agent；agent 通过 `device.command` 请求 Android 执行通用 UI 原语。
5. 每一步写 `robot_task_logs` 并广播 `task.log`；完成后广播 `task.updated` 和 `message.updated`。
6. 如果任务关联了入站反馈消息，成功标 `replied`，失败或取消标 `failed`。

后端不再通过 `task.dispatch` 下发完整 send_text 脚本；Android 只暴露观察与操作原语，具体策略由后端 ReAct 循环根据实时 UI dump 决策。

## 2.3 技术栈

| 层 | 选型 | 备注 |
| --- | --- | --- |
| Android | Kotlin + AccessibilityService + NotificationListenerService + OkHttp WebSocket | 消息采集与设备原语执行 |
| 后端 | FastAPI + Pydantic v2 + SQLAlchemy 2.x + Alembic | Python 3.11+ |
| 主库 | PostgreSQL 15+ / SQLite | SQLite 用于本地闭环，生产建议 PostgreSQL |
| 实时通信 | 进程内 WS Hub | 多 worker 需要 Redis pub/sub |
| 任务队列 | `services/task_queue.py` 进程内优先级队列 | 每机器人串行、可取消、可恢复 `dispatched/queued` 任务 |
| 自动回复 | `services/auto_reply_scheduler.py` | 每机器人公平轮转待反馈会话 |
| AI Workflow | 手写状态机 + 可选会话 ReAct agent | 可后续迁移 LangGraph |
| 设备自动化 | 后端 ReAct agent + Android 通用命令原语 | 避免设备尺寸和页面结构写死 |
| 向量库 | Milvus / memory | RAG 召回 |
| 图库 | Neo4j / memory | 实体关系 + Graph RAG |
| Web | Next.js App Router + TailwindCSS | 服务端组件 + WS 客户端 |
| LLM | `LLMProvider` 抽象 | OpenAI-compatible / mock |

## 2.4 部署形态

- 开发：后端 + 前端本地启动，数据库可用 SQLite 或 Postgres，Android 走真机。
- 生产：后端单 worker、独立 Postgres、可选 Milvus / Neo4j、Android 物理机群，每台设备绑定一个 `robot_id`。
- 多 worker / HA：需要先把 WS 广播、任务队列、自动回复调度状态和设备命令 future 外置。

详见 [ADR-0001：客户端 RPA 而非官方 API](adr/0001-rpa-over-official-api.md)。
