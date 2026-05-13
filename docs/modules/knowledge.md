# 知识库（MVP3 ✅）

## 状态
**MVP3 已落地**：文档上传 → 解析 → 切分 → Embedding → VectorStore + GraphStore。
默认全部用内存实现，可平滑切到 Milvus / Neo4j。

## 流水线

```
upload (REST)
  → KnowledgeDocument(status=pending)
  → BackgroundTasks → pipeline.ingest_document
        → parsers.parse   (txt/md/pdf)
        → chunker.chunk   (size + overlap)
        → embedder.embed
        → vector_store.upsert
        → entities.extract → graph_store.upsert_edge
        → status=ready
```

> 升级到 Celery：把 `ingest_document(...)` 包成 `@task`，把 `BackgroundTasks.add_task` 换成 `delay()` 即可，节点逻辑不变。

## 抽象层

| 抽象 | 默认实现 | 升级路径 |
| --- | --- | --- |
| `EmbeddingProvider` | `MockEmbedding`（字符 bigram 哈希,dim=256） | `OpenAIEmbedding`（`text-embedding-3-small/large` 或兼容端） |
| `VectorStore`       | `MemoryVectorStore`（线性扫,cosine） | `MilvusVectorStore`（`pymilvus`,COSINE,auto_id） |
| `GraphStore`        | `MemoryGraphStore`（dict-of-adj） | `Neo4jGraphStore`（`neo4j` 异步驱动） |

切换通过 `EMBEDDING_PROVIDER` / `VECTOR_STORE` / `GRAPH_STORE` 环境变量。

## 解析器

| 类型 | 实现 |
| --- | --- |
| txt / md | 直接 decode（utf-8 / gbk / 兜底） |
| pdf | `pypdf`（按需安装：`pip install pypdf`） |
| word / excel | MVP3 不在范围，MVP4 视需求加 `python-docx` / `openpyxl` |

## Chunker

字符级滑窗，优先在句号 / 问号 / 换行处切。超长片段硬切；保留 `overlap` 字符兜底跨片段语义。

## 实体抽取

MVP3 用规则：
- KB 描述里逗号分隔的种子词 → `Product`
- `¥xxx 元 / RMB / USD` → `Price`
- `【…】` / `[…]` → `Feature`

升级路径：插一个 LLM-NER 节点（同一接口），其余流程不变。

## 检索（Hybrid RAG + Graph RAG）

`retrieve(team_id, query, top_k=5)`：
1. embed 问题
2. 向量库 `top_k` 召回（强制按 `team_id` 隔离）
3. score 低于阈值的剔除（`KB_MIN_SCORE`）
4. 取首条 hit 的实体集合 → 1-hop 图扩展 → `graph_facts`
5. `to_context()` 拼成可注入 LLM 的文本块

## AI Workflow 集成
`workflow.py` 在 `generate` 之前插入：
- `_load_memory(contact_id)` → `UserProfile.summary` 注入 system
- `_retrieve(team_id, query)` → 知识片段 + 图谱事实注入 system

命中后通过 WS 推送 `kb.hits` 给前端，右栏自动加载 chunks 内容。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/kb` | 列表 |
| POST | `/kb` | 新建 |
| GET / DELETE | `/kb/{id}` | 详情 / 删除 |
| GET | `/kb/{id}/docs` | 文档列表 |
| POST | `/kb/{id}/docs` | multipart 上传文件 |
| POST | `/kb/{id}/docs/paste` | 表单粘贴文本（demo / 测试） |
| GET | `/kb/{id}/docs/{doc_id}` | 文档详情 / 状态 |
| POST | `/kb/search` | `{query, top_k}` |
| GET | `/kb/chunks/by-ids?ids=1,2,3` | 工作台右栏溯源 |

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EMBEDDING_PROVIDER` | `mock` | `mock` / `openai` |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | provider=openai 时生效 |
| `EMBEDDING_DIM` | `256` | mock 默认；openai 1536/3072 |
| `EMBEDDING_API_KEY` | `""` | 不填则回退 `LLM_API_KEY` |
| `VECTOR_STORE` | `memory` | `memory` / `milvus` |
| `MILVUS_URI` | `http://localhost:19530` | |
| `MILVUS_COLLECTION` | `kb_chunks` | |
| `GRAPH_STORE` | `memory` | `memory` / `neo4j` |
| `NEO4J_URI / USER / PASSWORD` | bolt://localhost:7687 / neo4j / neo4j | |
| `KB_TOP_K` | `5` | |
| `KB_MIN_SCORE` | `0.05` | mock embedding 数值较低；OpenAI 推荐 0.5+ |
| `KB_CHUNK_SIZE` | `400` | |
| `KB_CHUNK_OVERLAP` | `60` | |

## 验收 ✅

- [x] 上传文档后状态变 `ready`
- [x] 搜索能返回片段并定位原文（含 score）
- [x] 客户消息 → AI 触发 `kb.hits` 推送 → 前端右栏渲染
- [x] 跨租户搜索隔离

实跑见：`tools/kb_smoke.py`。
