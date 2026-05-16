# 05 · 后端服务

## 5.1 定位

后端是整个系统的 **业务中枢**——既服务 Web 管理台，又调度 Android 机群，又承载 AI 决策。

## 5.2 模块清单

| 模块 | 文档 | MVP |
| --- | --- | --- |
| 认证与权限 | [auth.md](modules/auth.md) | 1 |
| 设备管理 | [device.md](modules/device.md) | 1 |
| 消息网关 | [message-gateway.md](modules/message-gateway.md) | 1 |
| 会话管理 | [conversation.md](modules/conversation.md) | 1 |
| Web 实时通信 | [realtime.md](modules/realtime.md) | 1 |
| 任务调度 | [task-scheduler.md](modules/task-scheduler.md) | 1 |
| AI Workflow | [ai-workflow.md](modules/ai-workflow.md) | 2 |
| 知识库处理 | [knowledge.md](modules/knowledge.md) | 3 |
| 长期记忆 | [memory.md](modules/memory.md) | 3 |
| 风控 | [risk.md](modules/risk.md) | 5 |
| 数据分析 | [analytics.md](modules/analytics.md) | 5 |

## 5.3 目录结构

```
backend/
  app/
    main.py                # FastAPI 入口
    core/
      config.py            # 配置（环境变量）
      db.py                # SQLAlchemy session
      security.py          # 密码 / JWT
      ws_manager.py        # WebSocket 连接池
    models.py              # SQLAlchemy 模型
    schemas.py             # Pydantic 模型
    deps.py                # FastAPI Depends
    routers/
      auth.py
      robots.py
      conversations.py
      messages.py
      tasks.py
    ws/
      web.py               # /ws/web  Web 客户端
      android.py           # /ws/android  Android 客户端
    services/
      conversation.py
      auto_reply_scheduler.py
      task_queue.py
      send_orchestrator.py
      retention.py
      settings_service.py
  alembic/                 # 迁移（MVP2 引入）
  pyproject.toml
  README.md
```

## 5.4 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./dev.db` | MVP1 用 SQLite，生产切 Postgres |
| `JWT_SECRET` | `dev-secret-change-me` | 必须改 |
| `JWT_EXPIRE_MIN` | `1440` | Token 有效期（分钟） |
| `CORS_ORIGINS` | `http://localhost:3000` | 逗号分隔 |
| `LOG_LEVEL` | `INFO` | |
| `LLM_PROVIDER` | `mock` | `mock` \| `openai`（含兼容端） |
| `LLM_MODEL` | `gpt-4o-mini` | |
| `LLM_API_KEY` | `""` | provider=openai 时必填 |
| `LLM_BASE_URL` | `""` | 例如 DeepSeek `https://api.deepseek.com/v1` |
| `AI_CONFIDENCE_THRESHOLD` | `0.55` | 低于此值且 mode=mixed → 转 suggest |
| `AI_CONTEXT_WINDOW` | `10` | AI 喂入的近 N 条消息 |

## 5.5 启动

```bash
cd backend
pip install -e .
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

启动后：
- REST：`http://localhost:8000/docs`
- Web WS：`ws://localhost:8000/ws/web?token=...`
- Android WS：`ws://localhost:8000/ws/android?robot_id=...&token=...`
