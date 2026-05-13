# 11 · 里程碑

| MVP | 目标 | 关键产出 | 状态 |
| --- | --- | --- | --- |
| 1 | 消息闭环 | Android 监听 ⇄ 后端 ⇄ Web 工作台人工回复 | ✅ |
| 2 | AI 自动回复 | LLMProvider 抽象 + 工作流 + Prompt 配置 + AI/人工/混合切换 | ✅ |
| 3 | 知识库 + 记忆 | Embedding / VectorStore / GraphStore 抽象 + Graph RAG + 长期摘要 | ✅ |
| 4 | 主动运营 | 欢迎语 / SOP / 老客促活 / 朋友圈 | ⏳ |
| 5 | 规模化 | 多租户 RBAC + 风控 + 数据分析 + 设备健康监控 | ⏳ |

## MVP1 完成标准 ✅

- [x] Android（或 mock client）连 WS，注册成功
- [x] mock client 触发 `message.received` → Web 工作台 1s 内看到
- [x] Web 客服回复 → 后端创建 task → 下发 mock → 回 `task.completed` → 前端消息状态从 pending 变 sent
- [x] 登录页 → 工作台 → 三栏布局 → 设备管理可新建设备并复制 token
- [x] `tools/e2e_smoke.py` 通过

## MVP2 完成标准 ✅

- [x] LLMProvider 抽象（mock + OpenAI 兼容）通过 `LLM_PROVIDER` 切换
- [x] `mode=ai`：客户消息触发 AI → 自动 dispatch `send_text` → 消息 `sender_type=ai` + `status=sent`
- [x] `mode=mixed` + 低置信：不自动发送,写日志 `action=suggest`,推 `ai.suggestion` 给 Web
- [x] `mode=human`：AI 跳过
- [x] 每次决策落 `ai_reply_logs(trace_id, action, confidence, model, latency_ms)`
- [x] `GET /ai/logs` / `PUT /ai/prompts` / `GET /ai/info`
- [x] 前端工作台右栏 AI 推荐卡片 + 消息气泡区分 AI / 人工
- [x] `tools/ai_smoke.py` 通过

## MVP3 完成标准 ✅

- [x] 三个新抽象：`EmbeddingProvider` / `VectorStore` / `GraphStore`,各带 mock/memory 默认实现 + 真实适配器（OpenAI / Milvus / Neo4j）
- [x] 文档上传 → 解析（txt/md/pdf） → chunk → embed → 向量库 + 图谱库
- [x] 检索：向量 top-K + 1-hop 图扩展 + 跨租户隔离
- [x] AI workflow 注入：`load_memory` + `retrieve` 两节点位于 `generate` 之前
- [x] WS 推 `kb.hits`,前端右栏渲染命中片段
- [x] 长期记忆：每 N 条 inbound 自动摘要 → `user_profiles.summary` + `user_memories` 向量条目
- [x] REST：`/kb`、`/kb/{id}/docs(/paste)`、`/kb/search`、`/kb/chunks/by-ids`、`/memory/{cid}`
- [x] 前端 `/knowledge` 列表 + `/knowledge/{id}` 详情（上传 / 粘贴 / 文档表 / 检索测试 / 图谱展示）
- [x] `tools/kb_smoke.py` 通过

## MVP4 计划（下一步）

- 新客欢迎策略（按渠道 / 时段）
- SOP 触达（阶段停留 N 天触发动作集合）
- 老客促活（沉默唤醒）
- 朋友圈批量发布 + 多号文案差异化
- 运营策略可配置 UI
