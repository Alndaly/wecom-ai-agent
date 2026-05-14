# 13 · 接入真实模型与基础设施

> 默认全 mock 跑得通；这篇写"切到真东西"。

## 13.1 LLM / Embedding（一定走 Web 配置）

不要在 env 里硬编码 API key。所有 LLM / Embedding 参数在 Web `/settings`
页面里改,保存即生效。

兼容的 chat-completions / embeddings 端点都能直接用：

| 厂商 | LLM `base_url` | Embedding 模型 |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `text-embedding-3-small` (1536) |
| DeepSeek | `https://api.deepseek.com/v1` | OpenAI 端 |
| 通义/DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `text-embedding-v3` (1024) |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | OpenAI 端 |
| Ollama 本地 | `http://localhost:11434/v1` | `nomic-embed-text` (768) |

操作步骤：
1. 打开 Web → 模型配置 → LLM 卡片
2. 用预设填,改 model 名,粘 api_key → **测试** → **保存**
3. Embedding 卡片同样,注意 `dim` 要与所选模型一致
4. 切真 embedding 后,把检索 `min_score` 从默认 `0.05` 改为 `0.5` 起步
   (mock 的字符 bigram 上限就 0.2,真 embedding 会高得多)

> 切换 embedding 模型 = 把维度也换了 → 旧向量库失效。**建议为每个新模型新建一个 KB**,
> 旧 KB 留作历史归档；或者重新跑 ingest pipeline 把旧文档重灌一遍。

## 13.2 Milvus

启用：

```bash
docker compose --profile rag up etcd minio milvus
# 然后 backend 端：
export VECTOR_STORE=milvus
export MILVUS_URI=http://localhost:19530
export MILVUS_COLLECTION=kb_chunks
export EMBEDDING_DIM=1536        # 必须与所选 embedding 模型一致
pip install '.[milvus]'          # 安装 pymilvus
uvicorn app.main:app --port 8000
```

后端启动时会自动建集合(HNSW + COSINE),已存在则跳过。

注意：
- `VECTOR_STORE=milvus` 但连不通时,启动直接报错而非降级到内存(避免线上误用)
- 在 `/settings` 页面的「基础设施」卡片可以看到当前激活的 store 名称

## 13.3 Neo4j

```bash
docker compose --profile rag up neo4j
export GRAPH_STORE=neo4j
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=neo4jtest
pip install '.[neo4j]'
uvicorn app.main:app --port 8000
```

打开 <http://localhost:7474> 用同样的账号密码登 Neo4j Browser,可以可视化查
`MATCH (n:Entity {team_id:1}) RETURN n LIMIT 50` 看产生的实体关系。

Schema 极简：`(:Entity {team_id, label, name})` + 任意关系类型(自动 sanitise 成
`A-Za-z0-9_`)。

## 13.4 一键拉起（含 RAG profile）

```bash
docker compose --profile rag up
# 等 milvus / neo4j healthcheck 通过(约 30-60s)
```

backend 自动读 docker-compose 设的 `VECTOR_STORE=memory` (默认)或你显式设的 milvus。
要让 backend 走 milvus 改一行 env：

```bash
VECTOR_STORE=milvus GRAPH_STORE=neo4j docker compose --profile rag up
```

## 13.5 验证

```bash
# 在 backend 已起 + LLM/Embedding 通过 /settings 配置好之后
export REAL_LLM_API_KEY=sk-...
export REAL_LLM_MODEL=gpt-4o-mini
export REAL_KB_MIN_SCORE=0.5
backend/.venv/bin/python tools/real_smoke.py
```

这个脚本会：
1. 用真 key 写 /settings
2. 探活 LLM + embedding
3. 入库一份文档
4. 触发一条入向消息
5. **检查 AI 回复中是否引用了知识库里的事实**(团购折扣等关键词)
6. 检查 `ai_reply_logs.model` 不再是 `mock`

不设 `REAL_LLM_API_KEY` 时脚本自动跳过 — CI 安全。

## 13.6 已知坑

- **Embedding dim 改了忘了换 KB** → 检索零命中。看 `/settings` 卡片的「基础设施」
  提醒,Milvus 的集合维度建好后不能改,要么删 collection 重建,要么开新 collection。
- **Milvus 在 macOS 上 etcd 启动慢** → 第一次 healthcheck 可能要 30s+,耐心等。
- **Neo4j 默认密码** → docker-compose 里设的是 `neo4jtest`,改前请改 compose。
- **租户隔离** 通过 `team_id == ?` 做强过滤,但 Milvus/Neo4j 的容量按集群计,不是按租户。
  真要做 SaaS 多租,要么按 team 分集合 / 分 db,要么继续用 filter 但写好审计。
