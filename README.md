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

### 6. MVP3 — 知识库 + 长期记忆

零配置可跑（内存向量库 + 图谱 + mock embedding）：

```bash
backend/.venv/bin/python tools/kb_smoke.py
```

接 Milvus / Neo4j / 真 embedding：

```bash
export EMBEDDING_PROVIDER=openai
export EMBEDDING_API_KEY=sk-...
export VECTOR_STORE=milvus   MILVUS_URI=http://localhost:19530
export GRAPH_STORE=neo4j     NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=...
export KB_MIN_SCORE=0.5
```

Web 上 `/knowledge` 新建库 + 上传 / 粘贴文档 + 内置「检索测试」。工作台右栏在 AI 触发后会自动加载 `kb.hits` 命中卡片。

## 里程碑

见 [docs/11-milestones.md](docs/11-milestones.md)。当前完成度：**MVP1 ✅ · MVP2 ✅ · MVP3 ✅**。
