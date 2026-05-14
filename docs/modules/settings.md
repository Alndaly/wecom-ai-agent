# 运行时配置（Web 可编辑 ✅）

## 定位
所有"会变的策略类参数"——LLM 选型 / Embedding 选型 / Prompt / 检索阈值——
**不写死在环境变量里**，由 Web `/settings` 页面在线编辑，保存即时生效。

环境变量只承担两类职责：
1. **基础设施**：Milvus / Neo4j 的 URI、用户名密码
2. **初始默认值**：第一次启动时,如果数据库里没保存,就用 env 兜底

## 数据
`team_settings(team_id, key, value_json, version)`  · 每个 `(team_id, key)` 唯一。

scope 清单：

| key | 字段 |
| --- | --- |
| `llm` | `provider` (mock/openai) · `model` · `api_key` · `base_url` · `temperature` |
| `embedding` | `provider` (mock/openai) · `model` · `api_key` · `base_url` · `dim` |
| `retrieval` | `top_k` · `min_score` |
| `ai` | `confidence_threshold` · `context_window` · `default_prompt` |

`version` 字段在每次保存时 +1,Provider 工厂以 `(team_id, version)` 作为缓存键,
保证保存后下一次请求一定拿到新实例,不需要重启后端。

## REST

```
GET  /settings                 → 当前 team 所有 scope（api_key 自动掩码为 ********）
PUT  /settings/llm             → 全量替换 llm scope（api_key 留空 = 保留旧值）
PUT  /settings/embedding
PUT  /settings/retrieval
PUT  /settings/ai
POST /settings/test/llm        → 一次性 ping(可带 body 试新配置,api_key=空时取已存值)
POST /settings/test/embedding
POST /settings/test/vector_store   → 探活 Milvus / Memory
POST /settings/test/graph_store    → 探活 Neo4j / Memory
```

## 安全
- `api_key` 列**写后只读为掩码**。前端拿不到真值,只能"留空 = 保留 / 填写 = 覆盖"。
- 仍然存在 DB 明文存储这一假设(MVP3),生产应叠加 KMS 包装层(MVP5 议题)。

## 工厂集成
```
DB(team_id, "llm")  ──merge──> env defaults
                            └─> build_provider(cfg) ──> 缓存 (team_id, version)
```
保存 → version 自增 → 旧缓存自动作废 → 新请求重建。

## 验收 ✅
- [x] 在 Web `/settings` 改 LLM model + base_url + 保存 → 下一次 AI 回复就用新模型
- [x] `api_key` 不会回流到前端
- [x] `POST /settings/test/llm` 能直接验证配置可达性,无需保存
- [x] 切回 `provider=mock` 立即停用真模型

实跑见 `tools/settings_smoke.py`。
