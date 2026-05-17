# WeCom AI Agent

AI + 人工协同的企微私域运营智能体平台。

> 完整产品文档见 [docs/README.md](docs/README.md)。

## 目录结构

```
backend/   FastAPI 服务（REST + WebSocket，SQLite/Postgres）
web/       Next.js 管理台（工作台 / 设备 / …）
android/   Kotlin RPA 客户端骨架
tools/     mock_android.py / e2e_smoke.py
docs/      产品文档
```

## 快速开始（MVP1）

### 1. 后端

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e .
uvicorn app.main:app --reload --port 8000
```

启动后会播种默认账号：`admin@example.com / admin123`。
OpenAPI: <http://localhost:8000/docs>

任务执行依赖 Celery + Redis。Docker Compose 会同时启动 `redis` 和 `celery`；
本地分开跑时另开一个终端：

```bash
cd backend
celery -A app.worker.celery_app worker -Q device_tasks --loglevel=INFO --concurrency=8 --prefetch-multiplier=1
```

### 2. Web

```bash
cd web
cp .env.local.example .env.local
npm install
npm run dev
```

打开 <http://localhost:3000> 登录。

### 3. 模拟 Android（无真机）

新建设备拿到 `robot_id` 与 `token` 后：

```bash
python tools/mock_android.py \
  --base ws://localhost:8000 \
  --robot-id robot_xxxx \
  --token <token> \
  --send "你好,在吗?"
```

之后即可在 Web 工作台看到消息、回复客户，mock 端会自动 ack 任务下发。

### 4. 一键 E2E 冒烟测试

```bash
backend/.venv/bin/python tools/e2e_smoke.py
```

覆盖：登录 → 建设备 → Android WS → 入向消息 → 去重 → 客服回复 → 任务下发 → ack → 消息状态变为 sent。

### 5. MVP2 — AI 自动回复

零配置（用内置 mock provider）：直接重启后端，把会话切到 `AI` 或 `混合`，发 "在吗" 即可看到 AI 自动回复。

接真实 LLM：

```bash
export LLM_PROVIDER=openai
export LLM_API_KEY=sk-...
export LLM_MODEL=gpt-4o-mini
# 兼容端：export LLM_BASE_URL=https://api.deepseek.com/v1
```

MVP2 端到端验证：

```bash
backend/.venv/bin/python tools/ai_smoke.py
```

### 6. MVP3 — 知识库 + 长期记忆 + 模型在线配置

零配置可跑（内存向量库 + 图谱 + mock embedding）：

```bash
backend/.venv/bin/python tools/kb_smoke.py
```

**模型配置走 Web `/settings` 页面**（不要再用环境变量）。LLM / Embedding 都用 OpenAI 兼容协议,内置预设：OpenAI / DeepSeek / 通义 / 智谱 / Ollama。改完点「测试」探活,「保存」即时生效,无需重启后端。

接 Milvus / Neo4j：

```bash
# 一键起 RAG 全栈
docker compose --profile rag up
# 然后让后端连过去
export VECTOR_STORE=milvus
export GRAPH_STORE=neo4j
export NEO4J_PASSWORD=neo4jtest
pip install '.[real]'   # pymilvus + neo4j + pypdf
uvicorn app.main:app --port 8000
```

完整步骤见 [docs/13-real-providers.md](docs/13-real-providers.md)。

### 7. Android 真机（不再是 stub）

`AccessibilityService` + `NotificationListener` 已经接到企微：
- 通知监听 → 实时上报 `message.received`
- 任务下发 → `WeComAutomator` 真去打开企微 → 搜索 → 输入 → 点发送
- 失败自动 dump UI 树到后端 `var/ui_dumps/`
- 干跑（dry-run）开关 + 校准按钮 + 测试发送一应俱全

完整上手手册：[docs/14-android-real-device.md](docs/14-android-real-device.md)。
我没真机所以代码里的 UI 节点策略只能靠 dump 来调；第一次发不成功时把 `var/ui_dumps/` 里的 dump 给我看一眼就能改对。

### 8. 真模型端到端验证

```bash
export REAL_LLM_API_KEY=sk-...
export REAL_LLM_MODEL=gpt-4o-mini      # 或 deepseek-chat / qwen-plus / glm-4
export REAL_KB_MIN_SCORE=0.5
backend/.venv/bin/python tools/real_smoke.py
```

会写真配置 → 探活 → 入库 → 触发 AI 回复 → 断言回复确实引用了知识库事实 → 检查 `ai_reply_logs.model` 不再是 `mock`。

### 9. 切到 Postgres（生产前提）

后端启动时 **自动跑 Alembic 迁移**（`alembic upgrade head`），无需手动建表。本地、Docker、生产共用同一份迁移。

```bash
# Docker compose 自动用 Postgres：
docker compose up postgres backend

# 本地连一个独立 Postgres：
export DATABASE_URL="postgresql+asyncpg://wecom:wecom@localhost:5432/wecom"
cd backend && . .venv/bin/activate && uvicorn app.main:app --reload

# 端到端针对真 Postgres 跑一遍：
export PG_TEST_URL="postgresql+asyncpg://<user>:<pwd>@<host>:5432/wecom_ai_test"
backend/.venv/bin/python tools/postgres_smoke.py
```

模型改动 → 生成新迁移：

```bash
cd backend && alembic revision --autogenerate -m "add foo column"
# 审一遍生成的文件，重命名为 00NN_xxx.py，提交
```

完整说明见 [docs/15-database-migrations.md](docs/15-database-migrations.md)。

## 里程碑

见 [docs/11-milestones.md](docs/11-milestones.md)。当前完成度：**MVP1 ✅ · MVP2 ✅ · MVP3 ✅**。
