# 长期记忆（MVP3 ✅）

## 状态
基础长期记忆已落地。后续可换更复杂的策略（多粒度摘要、偏好结构化抽取等）。

## 三层

| 层 | 存储 | 用途 |
| --- | --- | --- |
| 短期上下文 | 后续 Redis | 最近 N 条原文，TTL 24h（MVP3 直接读 `messages` 表） |
| 结构化画像 | Postgres `user_profiles` | 摘要 / 阶段 / 偏好 |
| 语义记忆 | `user_memories` + VectorStore | 摘要 / 关键事件，可用于 RAG-style 召回 |
| 关系记忆 | GraphStore (Memory/Neo4j) | 实体关系（KB 路径复用） |

## 摘要机制

`app/memory/summarizer.py`

- 触发点：每次 inbound 消息入会话后调用 `maybe_refresh(...)`
- 节流：自上次摘要后客户新消息数 ≥ `MEMORY_SUMMARY_EVERY`（默认 10）才生成
- 模型：当前用 LLMProvider 走一次 `_SUMMARY_PROMPT`，输出 ≤ 120 字摘要
- 写入：
  - `user_profiles.summary` 覆盖更新
  - `user_memories` 追加一条 `kind=summary` 带 embedding
  - `last_summary_message_id` 标记水位

## 注入 AI

`workflow._load_memory(contact_id)` 在生成阶段读 `summary`，注入 system 段：
```
【客户画像】<summary>
```

下次该客户再来,AI 能直接引用历史脉络（"上次提到的 ProMax 价格..."）。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/memory/{contact_id}` | 画像 |
| GET | `/memory/{contact_id}/memories` | 记忆条目列表 |

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MEMORY_SUMMARY_EVERY` | `10` | 每 N 条 inbound 消息触发一次摘要 |

## 验收 ✅
- [x] 发够 N 条消息后 `/memory/{cid}` 返回非空 summary
- [x] 第二天客户回来,AI prompt 中能看到画像摘要

实跑见：`tools/kb_smoke.py`（末尾验证 profile + memories）。
