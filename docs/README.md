# 产品文档索引

> AI + 人工协同的企微私域运营智能体平台

## 阅读顺序

| 编号 | 文档 | 说明 |
| --- | --- | --- |
| 01 | [项目定位](01-overview.md) | 产品目标、用户、核心闭环 |
| 02 | [系统架构](02-architecture.md) | 三端拓扑、技术栈、数据流 |
| 03 | [Android 执行端](03-android-client.md) | RPA 监听 / 发送 / 状态机 |
| 04 | [Web 管理台](04-web-console.md) | 客服工作台 / 设备 / 知识库 / 运营 |
| 05 | [后端服务](05-backend.md) | 业务中枢总览 |
| 06 | [后端模块](modules/README.md) | 各模块详细设计（一个模块一文件） |
| 07 | [AI 与知识库](07-ai-knowledge.md) | Milvus + Neo4j + Graph RAG |
| 08 | [业务场景](08-scenarios.md) | 新客欢迎 / 老客促活 / 朋友圈 / 风控 |
| 09 | [数据模型](09-data-model.md) | 所有表结构与关系 |
| 10 | [API 与 WS 协议](10-api-contracts.md) | REST / WebSocket 契约 |
| 11 | [里程碑](11-milestones.md) | MVP1~5 与验收标准 |
| 12 | [术语表](12-glossary.md) | 名词约定 |
| 13 | [接入真模型 / Milvus / Neo4j](13-real-providers.md) | 切真东西的完整步骤 |
| ADR | [架构决策](adr/README.md) | 关键技术选型记录 |

## 文档约定

- **强制语气**：MUST / SHOULD / MAY 遵循 RFC 2119。
- **示例字段**：所有 JSON 示例字段均为真实字段（不是占位符），改字段需同步更新 [09-data-model.md](09-data-model.md) 与 [10-api-contracts.md](10-api-contracts.md)。
- **验收标准**：每个模块文档末尾都有「验收标准」一节，MVP 验收以此为准。
- **ADR**：架构层面的不可逆决策一律写入 `adr/`，不要散落在模块文档里。
