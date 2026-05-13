# 07 · AI 与知识库

> MVP1 不实现，但所有数据模型 / 接口预留空位。

## 7.1 向量库：Milvus
- Collection：`kb_chunks`（dim 由 embedding 模型决定，默认 1536）
- Field：`id`, `team_id`, `kb_id`, `doc_id`, `chunk_ord`, `text`, `vector`, `meta_json`
- 检索：`team_id == ?` 必加，避免跨租户泄露

## 7.2 图库：Neo4j
节点：`User`, `Product`, `Feature`, `Price`, `Scene`, `Tag`
边：`ASKED_ABOUT`, `HAS_FEATURE`, `PRICED_AT`, `IN_STAGE`, `INTERESTED_IN`

## 7.3 Graph RAG
```
question
  → embed → milvus.top_k=20
  → 解析命中 chunk 关联的实体集 E
  → neo4j: 取 E 的 1~2 hop 邻居，剪枝按 score
  → context = milvus_chunks ⊕ subgraph_summary
  → LLM
```

## 7.4 Embedding / LLM 接入
- 抽象 `LLMProvider` / `EmbeddingProvider` 接口
- 内置实现：OpenAI / 通义 / 智谱 / Ollama
- 切换通过环境变量 `LLM_PROVIDER=...`，绝不硬编码

详细落地见 [modules/knowledge.md](modules/knowledge.md) 与 [modules/ai-workflow.md](modules/ai-workflow.md)。
