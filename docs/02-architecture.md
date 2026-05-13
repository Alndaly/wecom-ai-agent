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
        PostgreSQL              Redis             Celery       Milvus / Neo4j    LLM API
        (业务主库)          (缓存+pubsub)       (异步任务)     (RAG / KG)        (回复生成)
```

## 2.2 数据流

### 2.2.1 入向（客户消息 → 系统）

1. Android `AccessibilityService` 捕获企微聊天页面新消息。
2. 通过 WebSocket 上报 `message.received` 事件到后端。
3. 后端消息网关去重 → 落 `messages` 表 → 触发会话路由。
4. 会话路由按 `conversation.mode` 决定走 AI Workflow 或推 Web 客服。

### 2.2.2 出向（系统 → 客户消息）

1. AI Workflow 或人工客服生成回复。
2. 后端创建 `robot_task`（如 `send_text`）入队。
3. 任务调度器经 WebSocket 推送给目标 Android。
4. Android 执行（拉起企微 → 找到会话 → 输入 → 发送）。
5. Android 回调 `task.completed` / `task.failed`，后端更新任务并广播到 Web。

## 2.3 技术栈

| 层 | 选型 | 备注 |
| --- | --- | --- |
| Android | Kotlin + AccessibilityService + NotificationListenerService + WebSocket(OkHttp) | RPA 主体 |
| 后端 | FastAPI + Pydantic v2 + SQLAlchemy 2.x + Alembic | Python 3.11+ |
| 主库 | PostgreSQL 15+ | MVP1 允许用 SQLite 走通闭环 |
| 缓存 | Redis 7+ | 会话锁、WS pubsub、短期上下文 |
| 异步任务 | Celery + Redis broker | 文档解析、SOP、定时任务 |
| AI Workflow | LangGraph | 状态机式编排 |
| 向量库 | Milvus | RAG 召回 |
| 图库 | Neo4j | 实体关系 + Graph RAG |
| Web | Next.js 14 (App Router) + TailwindCSS | 服务端组件 + WS 客户端 |
| LLM | 通过抽象 `LLMProvider` 接入（OpenAI / 通义 / 智谱 / 自部署） | 不绑定单一厂商 |

## 2.4 部署形态

- 开发：`docker-compose up` 起 Postgres + Redis + 后端 + 前端，Android 走真机或模拟 client。
- 生产：后端容器化 + 独立 Postgres / Redis / Milvus / Neo4j；Android 物理机群（每台机绑一个 `robotId`）。

详见 [ADR-0001：客户端 RPA 而非官方 API](adr/0001-rpa-over-official-api.md)。
