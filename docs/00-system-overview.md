# WeCom AI Agent — 系统总览

代码整体走读的单页文档。新人先读这一份；老的 `02-architecture.md` /
`08-scenarios.md` 保留但在 ReAct 重写 + Milvus/Neo4j 接入 + agent 驱动发送
之前的版本，已经和现状对不上。

最近更新：2026-05-16。文档与代码冲突时**以代码为准** —— 每一节的小标题都
标了对应源文件路径。

## 0. 三个进程

```
                 ┌───────────────────┐
                 │  Web 控制台       │
                 │  (Next.js 14)     │
                 └────────┬──────────┘
              HTTPS + WS  │
                 ┌────────▼──────────┐         ┌───────────────────┐
                 │  后端              │ ws/req  │  安卓 Agent       │
                 │  (FastAPI)        │◀───────▶│  (Kotlin 服务)   │
                 └─┬──────┬────────┬─┘         └─────────┬─────────┘
            SQL   │ vec  │ graph  │              系统 UI 桥
        ┌─────────▼┐ ┌───▼───┐ ┌──▼────┐                ▼
        │ Postgres │ │Milvus │ │ Neo4j │       ┌──────────────────┐
        └──────────┘ └───────┘ └───────┘       │  企业微信 (WeCom)│
                                               └──────────────────┘
```

- **后端**是唯一有状态的组件。掌管 SQL / 向量 / 图谱 / WebSocket 广播
  / 每一次 LLM 调用。
- **Web 控制台**是个轻量运营 UI —— JWT 鉴权，实时数据走和安卓相同的 WS Hub
  （路径不同）。
- **安卓 Agent**是一个前台服务，维护一条到后端的 WS、一个无障碍服务来读
  /驱动 WeCom、一个通知监听器（WeCom 退到后台时仍然能抓到来访消息）。

存储后端可配置，但默认值现在都是真实系统：

| 层         | 默认后端       | 兜底                       | 配置入口                            |
| ---------- | -------------- | -------------------------- | ----------------------------------- |
| 关系型     | Postgres       | SQLite（`./dev.db`）       | `DATABASE_URL`                      |
| 向量       | Milvus 2.x     | 进程内 dict（重启即丢）    | `VECTOR_STORE` / `MILVUS_URI`       |
| 图谱       | Neo4j 5.x      | 进程内邻接表（重启即丢）   | `GRAPH_STORE` / `NEO4J_*`           |
| LLM        | 任何 OpenAI 兼容端（DeepSeek / 通义 / Ollama / OpenAI）；`mock` 兜底 | | `/settings/llm` 按团队改     |
| Embedding  | 同上的 provider 抽象；可换模型 + dim | | `/settings/embedding`        |
| 文档解析   | 内建 (pypdf+text) → MinerU 本地 CLI → MinerU 官方云端 API | | `/settings/parser`          |


## 1. 目录速查

### 后端 — `backend/app/`

```
main.py            FastAPI 应用 + lifespan（init_db、seed、注册工具、
                   向量回灌、retention 循环、MCP shutdown）
deps.py            current_user 依赖、JWT 解码
models.py          SQLAlchemy ORM：users / teams / robots / conversations /
                   messages / robot_tasks / robot_task_logs /
                   knowledge_bases / knowledge_documents / knowledge_chunks /
                   user_profiles / user_memories / ai_reply_logs /
                   ai_prompts / team_settings
schemas.py         Pydantic 请求/响应模型

core/              基础设施
  config.py        pydantic-settings；读 .env
  db.py            异步 SQLAlchemy session 工厂 + 启动时跑 alembic
  security.py      密码哈希、JWT 工具、robot-token HMAC
  ws_manager.py    WsHub：按 team 广播 web、按 robot_id 单发 android、
                   设备命令的 request_id 关联器

ai/                AI 编排
  workflow.py      入站 → Decision 管线（单跳兜底分支）
  conv_agent.py    会话 ReAct 循环，调用 Tool 注册表
  react_agent.py   设备 ReAct 循环
  react_locators.py 设备快路径用到的节点 locator 缓存
  providers/       LLM 适配器（openai_compatible / mock / base）
  tools/           Tool 注册表 + 内置 + skill 文件加载 + MCP 适配

kb/                知识库
  pipeline.py      入库流水：bytes → 文本 → 切片 → 向量 → 存（向量 + 图谱）
  retriever.py     混合检索（向量 + 图谱扩展）
  chunker.py       简单滑动窗口（chunk_size / overlap 可配）
  entities.py      用 KB 描述里的种子词做轻量 NER
  parsers/         文档解析 —— builtin / mineru_local / mineru_api
  embeddings/      Provider 抽象
  vectorstore/     memory.py + milvus.py（FLOAT_VECTOR + team_id 标量过滤）
  graphstore/      memory.py + neo4j_store.py

device/            包装 WS 命令通道，提供类型安全接口
  protocol.py      DeviceCommandName / DeviceCommandResult / UiDump / UiNode
  client.py        DeviceClient(robot).{open_wecom, tap_xy, dump_ui, ...}

services/          业务粘合层
  conversation.py  ingest_inbound_message（每条客户消息的入口）—— 去重、
                   系统消息过滤、按会话加锁、AI 分发、多条回复扇出
  send_orchestrator.py
                   create_and_dispatch_send_text（建 Message+RobotTask
                   并 spawn _run_send_via_react）、update_task_on_callback、
                   append_task_log
  retention.py     消息每日清理（默认 30 天）
  settings_service.py
                   团队级运行时配置，叠加在 env 默认值之上

memory/
  summarizer.py    每 N 条入站消息刷新一次 UserProfile.summary

routers/           FastAPI 路由（按功能划分）
  auth, robots, conversations, ai, kb, memory, settings, ui_analysis

ws/                WebSocket 处理
  android.py       按 robot_id 鉴权的设备 socket，分发设备事件
  web.py           按团队鉴权的 web socket，广播 UI 更新
```

### 安卓 — `android/app/src/main/kotlin/com/wecom/agent/`

```
ui/MainActivity.kt              设置面板：URL + robot_id + token，
                                权限检查，a11y-ingest 开关，dry-run
service/AgentForegroundService  核心服务：拥有 WS + executor +
                                scanner 调度 + ReAct 命令分发
service/WeComAccessibilityService
                                AccessibilityService（页面追踪、聊天页/
                                首页消息列表 harvest、带编号的 UI dump、
                                截图）
service/MessageNotificationListener
                                通知监听路径（WeCom 退后台时仍能抓消息）
service/MessageListScanner      三档周期性会话列表巡检
service/WeComAutomator          通用原语（tap_text / tap_xy / swipe /
                                input_text / back / home / open_wecom）
service/TaskExecutor            现在基本是个兜底壳 —— send_text 已经由
                                后端 ReAct agent 全权负责
net/BackendClient               OkHttp WS 客户端，指数退避重连
net/BackendApi                  REST 工具（目前只在 UI 分析探针里用到）
model/Events.kt                 wire 数据类型：HeartbeatPayload /
                                UiDumpPayload（带编号 UiNode 列表）/
                                DeviceCommand* / ScreenFramePayload /
                                MessageReceivedPayload …
```

### Web — `web/`

```
app/page.tsx                    登录
app/workbench/page.tsx          会话面板 —— 左：列表，中：对话，
                                右：AI 建议 + KB 命中 + 客户画像
app/devices/page.tsx            Robot CRUD + token 一次性展示
app/devices/[id]/page.tsx       实时屏幕、UI 树弹窗、语义指令输入、
                                任务日志面板（含清空按钮）
app/knowledge/page.tsx          知识库列表（创建 / 删除）
app/knowledge/[id]/page.tsx     文档上传（拖拽 + 粘贴）、检索探针
app/settings/page.tsx           分卡片表单：LLM / Embedding / Parser /
                                Retrieval / AI 行为（含 agent_mode +
                                React force_llm 切换） / Infra 只读
components/AuthGate.tsx         JWT 网关
components/Sidebar.tsx          导航
components/ui/                  shadcn 组件（弹窗一律用 Dialog /
                                AlertDialog —— 没有 native confirm()）
lib/api.ts                      fetch 封装，自动刷新 token
lib/ws.ts                       useWebWs hook，按团队订阅 WS
```


## 2. 鉴权与多租户

- **用户**属于某个 `Team`。REST / WS 全部用 `Authorization: Bearer` 携带
  JWT。`current_user` 依赖返回 user；`user.team_id` 是下游所有过滤的租户键。
- **机器人**用一个长随机 token 鉴权（`secrets.token_hex(32)`），数据库里
  存的是 HMAC-SHA256(JWT secret) —— token 本身就有 ~256 bit 熵，没必要再
  上 bcrypt 那种慢 KDF。创建时 token 只展示一次，之后无法回读。详见
  `routers/robots.py:_hash_token`。
- 每一行 `knowledge_bases`、`conversations`、`messages` 等等都带
  `team_id`（直接列或者通过外键）；路由总是先 `user.team_id` 过滤再返回
  数据。纵深防御：向量 / 图谱存储在元数据 + Cypher 属性里也都嵌入了
  `team_id`，跨租户检索即便代码出 bug 也不会成功。


## 3. WebSocket 协议

两个 WS 端点（`backend/app/ws/`）：

- `/ws/web?token=...` —— 按团队广播总线。Hub key = `team_id`。
- `/ws/android?robot_id=...&token=...` —— 按 robot 双向。Hub key = robot_id
  字符串。重连会踢掉旧 socket。

`core/ws_manager.WsHub` 同时拥有上面两个注册表 + 第三张表：

- `_pending: dict[str, Future]` —— ReAct agent 用的**请求/响应关联器**。
  `send_request(robot_id, event, payload)` 注入一个 `request_id` UUID，
  await 一个 Future，安卓侧把 `request_id` 原样回写到 `device.ui_dump` 或
  `device.command_result` 事件里时就解锁。`ws/android.py` 两个事件处理器
  都会调 `hub.resolve_request(request_id, payload)`。

### 后端 → 安卓

| 事件             | 载荷                                | 触发时机                              |
| ---------------- | ----------------------------------- | ------------------------------------- |
| `device.command` | `{command, request_id, ...args}`    | 每个 ReAct 原语（`open_wecom` / `dump_ui` / `tap_text` / `tap_xy` / `swipe` / `input_text` / `back` / `home` / `screenshot_once` / `screen_start` / `screen_stop`） |

注：`task.dispatch` 不再用于 `send_text`。agent 路径取代了它 —— 安卓只看到
离散的原语。

### 安卓 → 后端

| 事件                    | 说明                                  |
| ----------------------- | ------------------------------------- |
| `device.hello`          | 连接后第一条消息（设备信息）          |
| `device.heartbeat`      | 每 30s —— 刷新 `robot.last_seen_at`   |
| `device.ui_dump`        | `dump_ui` 的响应（带 `request_id` / `nodes` / 屏幕尺寸）|
| `device.command_result` | 每个 ReAct 原语的响应                 |
| `device.screen_frame`   | 实时屏开启时每帧一次                  |
| `device.command_ack`    | 旧的 / 手动命令 ack（screen_start/stop）—— 不走 req/resp |
| `message.received`      | 客户消息（a11y harvest 或通知监听）   |
| `task.completed` / `task.failed` / `task.log` | 旧任务回调路径 —— 留给非 send_text 任务用 |

### 后端 → Web（按 team 广播）

| 事件                   | 载荷                                | 触发时机                              |
| ---------------------- | ----------------------------------- | ------------------------------------- |
| `message.new`          | `{conversation_id, message}`        | 任何新入站 / 出站消息                  |
| `message.updated`      | `{conversation_id, message}`        | 状态变化（sent / failed）             |
| `message.deleted`      | `{conversation_id, message_id}`     | 运营手动删除                          |
| `conversation.updated` | 完整 ConversationOut                | 模式切换 / 未读数变化                  |
| `conversation.deleted` | `{conversation_id}`                 | 运营删除会话                          |
| `robot.status`         | `{robot_id, status}`                | 上下线切换                            |
| `robot.updated`        | 部分 Robot                          | hello / heartbeat 携带设备信息时      |
| `robot.logs_cleared`   | `{robot_id}`                        | 运营清空任务日志                      |
| `task.updated`         | `{task_id, status, error}`          | agent 运行进度                        |
| `task.log`             | `{robot_id, task_id, level, message}` | 每个 ReAct step 推一条                |
| `device.screen_frame`  | 流式图像帧                          | 运营开启实时屏幕时                    |
| `device.ui_dump`       | 完整 dump（手动或 react 触发）       | 调试用                                |
| `device.command_result`| 同安卓侧                            | 调试用                                |
| `ai.suggestion`        | mixed 模式下低置信回复的草稿         | conv_agent 升级建议                   |
| `kb.hits`              | 检索命中的 chunk id                 | 工作台右栏显示                        |


## 4. 数据模型

`backend/app/models.py` 是唯一权威。迁移到 Postgres 之后的当前形态：

```
teams 1───* users
       │
       └───* robots ─────* conversations ────* messages
                            │                 │
                            └─ contacts       └─ ai_reply_logs
                            │
                            └─ user_profiles
                            └─ user_memories

teams 1───* knowledge_bases ──* knowledge_documents ──* knowledge_chunks
teams 1───* team_settings   （按 scope 存 key/value JSON：
                              llm / embedding / parser / retrieval / ai）
teams 1───* ai_prompts      （命名 system prompt；"default" 是激活的那个）

robots 1──* robot_tasks 1──* robot_task_logs
```

迁移文件在 `backend/alembic/versions/`。`backend/app/core/db.py` 的
`init_db()` 每次启动都 shell 调用 `alembic upgrade head` —— 改代码后不用
手动跑迁移。

软外键注意：
- `RobotTask.message_id` 是普通 `BigInteger`，**不是** FK —— 删消息不会
  连带砍掉任务历史。
- `AIReplyLog.message_id` 是 FK；删消息前先把它置 NULL。
- 近期修过的：`append_task_log` 在 task 行已不存在时会防御性地把
  `task_id` 置 NULL。Postgres 严格的 FK 暴露了 SQLite 时代隐藏的 bug。


## 5. 主链路：客户入站消息 → AI 回复 → 设备发送

整个系统的脊柱。六个逻辑环节：

### 5.1 采集（安卓）

两条并行通道都会把 `message.received` 发给后端：

1. **通知监听器**（`MessageNotificationListener.kt`）—— WeCom **不**在前台
   时触发。按 notification key 去重；剥掉 `[N条]` 聚合前缀。
2. **无障碍 harvest**（`WeComAccessibilityService.kt`）—— WeCom **在**前台
   时触发。两个子分支：
   - `TYPE_WINDOW_CONTENT_CHANGED` 且当前 root 是聊天页 →
     `maybeHarvestChat()` 扫描消息气泡，过滤出站，用「baseline 后才发 diff」
     规则避免刚进会话时把历史全部重放。
   - root 是消息 tab 首页 → `maybeHarvestHomeList()` 对会话行预览做同样
     的事。

第三条通道 —— `MessageListScanner` —— 定时驱动首页 harvest（tier 1：每 30s
扫可见；tier 2：每 5min 滑 3 页；tier 3：每 30min 滑到底）。所有滑屏只在
**用户已经在消息 tab** 时执行（不会劫持设备）。

### 5.2 入库（后端）

`ws/android.py` 收到 `message.received` →
`services/conversation.ingest_inbound_message`：

1. 按 `external_msg_id` 去重。
2. 找到或新建 `Contact` 与 `Conversation`。
3. 再剥一次 `[N条]` 前缀；忽略 bot 自身回声（`收到您说的「…」`）、纯时间分
   隔线，以及 **WeCom 系统通知**（周报 banner / 登录提醒等 —— 见
   `_is_wecom_system_message`）。
4. 持久化 `Message`，递增 `conv.unread_count`，广播 `message.new` +
   `conversation.updated`。

### 5.3 AI 分发（后端）

同一个函数，持久化之后：

1. 如果 `conv.mode == "human"` 跳过。
2. 拿**会话级锁**（`_CONV_LOCKS[conv.id]`）。这把锁让连发的 5 条消息
   3 秒内不会扇成 5 个并发的 agent run。第二条入站拿到锁之后会调
   `_has_been_replied_after(conv.id, msg.created_at)`，如果上一批已经覆盖
   到了，便宜跳过。
3. 调用 `ai.workflow.handle_inbound`：
   - 拉历史（最近 `context_window` 条）；
   - 拉 system prompt（按团队的 `ai_prompts.default` 覆盖 env 默认）；
   - 拉 `user_profiles.summary`；
   - 拉**未回复链**（最后一条出站之后的所有入站，封顶 20 条）；
   - 跑检索（`kb.retriever.retrieve`）—— query 是整条链拼起来的，所以连发
     多条客户问题能拿到一次更准的检索，而不是 N 次窄检索；
   - 按 `ai_cfg.agent_mode` 分叉：
     - **开 agent（默认）** → `_generate_via_agent` 调
       `ai.conv_agent.run_conv_agent`（见 §6）；
     - **关 agent** → `_generate` 单跳一次 LLM，把检索结果拼到 system
       message 前面。
4. 返回的 `Decision` 有 `action ∈ {reply, suggest, skip}`，再加一个可选的
   `replies: list[str]`。conv-agent 可以单条 `text` 也可以最多 6 条
   `replies`（多气泡）—— `Decision.all_texts` 把两种形态都展平。
5. `mixed` 模式下，`confidence < threshold` 的回复降级为 `suggest` —— 通过
   `ai.suggestion` 推到工作台，由人审核。
6. 不管什么决策都写一条 `AIReplyLog`。右栏 KB 命中通过 `kb.hits` WS
   事件流过去。

### 5.4 发送下发（后端）

对 `decision.all_texts` 里每条文本，
`services/send_orchestrator.create_and_dispatch_send_text`：

1. 建出站 `Message`（status=`pending`）。
2. 建 `RobotTask`（`type=send_text` / `payload_json={contact, text}` /
   `status=dispatched`）。
3. `asyncio.create_task(_run_send_via_react(...))` —— **fire-and-forget**。
4. 广播 `message.new`。

注意：后端不再发 `task.dispatch` 给安卓。设备侧那套确定性自动化已经被拆掉
（只剩通用原语）。后端自己通过 ReAct agent 驱动设备。

### 5.5 设备自动化（后端 ↔ 安卓 多步）

`_run_send_via_react`：

1. 预热 `open_wecom`，避免 agent 第一次 observe 时看到的是 launcher。
2. 调 `ai.react_agent.run_react(robot, goal, max_steps, …, force_llm)` ——
   goal 是一句中文："打开与「七月」的聊天，并发送下面这段文本：{text}"。
3. 每一步通过 `services/send_orchestrator.append_task_log` 落到
   `robot_task_logs` + 广播 `task.log`。运营在设备详情页实时看到轨迹。
4. 完成后改 task 为 `completed`/`failed`，改 message 为 `sent`/`failed`，
   广播 `task.updated` + `message.updated`。

agent 循环本身详见 §7。

### 5.6 UI 渲染（web）

工作台页订阅 WS 后合并：

- `message.new` → 追加到气泡列表，把会话标为已读。
- `message.updated` → 气泡状态更新。
- `message.deleted` / `conversation.deleted` → 从视图移除。
- `ai.suggestion` → 显示在右栏；点「采用」就以人工 operator 发出。
- `kb.hits` → 右栏知识卡片；用 `/kb/chunks/by-ids` 解析 chunk id。


## 6. 会话 agent（带工具的回复 agent）

`ai/conv_agent.py:run_conv_agent` 是一个严格 JSON 的 ReAct 循环，调用 Tool
注册表。**和设备 ReAct 是不同的循环**（一个生成回复文本，一个驱动 UI）。

### 工具注册表 —— `ai/tools/__init__.py`

一个 `Tool` 包含 `{name, description, params, call: async (ctx, args) -> str,
source}`。`ToolRegistry` 是进程全局单例；FastAPI lifespan 启动时注册：

1. **内置工具** —— `ai/tools/builtin.py`：
   - `kb_search(query, top_k)` —— 调 `kb.retriever.retrieve`，把命中 chunk
     id 累积到 `ctx.scratch["kb_hit_ids"]`，workflow 顺手广播到右栏。
   - `set_profile_field(key, value)` —— 写 `UserProfile.preferences_json`。
   - `escalate_to_human(reason)` —— 在 scratch 上打个 flag；workflow 把
     decision 降级为 `suggest`。
   - `final_reply(text | replies, confidence)` —— 终止工具。接受单条 `text`
     或多条 `replies`。有个 `_clean_reply` 步骤剥掉行尾的 `…` / `...` /
     `等等`（本地模型有时会用这种 stylistic 省略号结尾）。
2. **文件 skills** —— `ai/tools/skills.py` 启动时扫 `<repo>/skills/*.py`，
   导入每个模块，识别 `tool: Tool` 或 `tools: list[Tool]`。source 标记
   `skill:<filename>`。单个文件出错只记录日志、跳过。
3. **MCP 服务器** —— `ai/tools/mcp_adapter.py` 解析 `MCP_SERVERS_JSON` env，
   按配置 spawn 每个 stdio 子进程（用 `mcp` SDK —— 软依赖），把远端工具包
   起来。命名空间是 `<server>_<tool>` 避免冲突。Session 持续整个进程生命
   周期；lifespan 退出时调 `shutdown()`。`mcp` 没装就 warn 跳过。

### 循环

```
建 system prompt = 基础 prompt
                + 客户画像 summary
                + KB 提示块（预检索的）
                + 工具目录（从注册表渲染）
                + 规则块（升级人工策略、多条回复指南）

建 user turn = 入站文本
              或 "客户最近连续发了 N 条消息：\n[1]…\n[2]…\n…"

for step in 1..max_steps:
    LLM(messages, temp ≤ 0.25, max_tokens=8192, timeout=45s)
    严格 JSON parse → {thought, tool, args}
    if tool 不在注册表: 把 "未知工具" 当 observation 喂回，continue
    obs = await tool.call(ctx, args)   # 20s 超时
    if tool in {final_reply, escalate_to_human}: break
    把 {assistant: <json>, user: [observation]\n<obs>} 追加到 scratch
```

注册表为空 → 直接 abort，吐一句兜底「稍等，我先核实一下再回您」。LLM 错误
/ 超时 → 同样的兜底。


## 7. 设备 ReAct agent（驱动 UI）

`ai/react_agent.py:run_react(robot, goal, …, force_llm=False)`。循环最多
`max_steps` 次（send 默认 6 次，`/agent/run` 走 web 时可配）。

### Observation（`_observe`）

1. `device.command{command:"dump_ui", reason:"react"}` → 安卓侧
   `WeComAccessibilityService.dumpTreeWithNodes()` 同时产出**带编号的文本
   树**（`[1] [FrameLayout] …`）**和**一个扁平的 `nodes: List<UiNode>` JSON
   列表（包 bounds + flags）。后端构造 `_Observation` 包含
   `{tree, nodes: dict[id → _Node], screen_size}`。
2. 如果开了视觉（按团队的 `llm.supports_vision`），`_attach_screenshot` 再
   piggyback 一次 `screenshot_once`，把 base64 JPEG 挂到 observation。
   多模态 LLM（gpt-4o / qwen-vl / glm-4v / DashScope vlm）就同时看到两路
   信号。

`_shrink_tree` 丢掉无装饰的节点（既无 text/id 也无 flag 的 FrameLayout），
文本封顶 4500 字符。**保留** clickable / editable 节点即便没文字 —— 因为
那些就是图标按钮，截图正好补足语义。

### Decision

两条路径，由 `react_force_llm` 设置控制：

1. **快路径**（`_fast_decide`，常规流程默认走）：
   - `parse_send_goal` 正则识别「发文本」类目标。
   - 按历史状态机：
     - 不在 WeCom → `open_wecom`
     - 上一步是 `tap_node(send_button)` → `done(success)`
     - 上一步是 `input_text` 且是搜索 → `tap_node(chat_target)`
     - 上一步是 `input_text` → `tap_node(send_button)`
     - 上一步是 `tap_node(chat_target)` → `input_text(message_input)`
   - 每次找节点先查 **locator 缓存**（`react_locators.LocatorStore`，按
     role + 特征键）。缓存未命中再 fallback 到通用特征启发式
     （`_find_message_input` / `_find_send_button` 等）。缓存命中后操作失败
     会被标 dirty。
   - 来源用 `cache` vs `rule` 区分，便于追溯。
2. **LLM 路径**（`force_llm=True` 永远走，或快路径返回 `None` 时也走）：
   - system prompt 列出所有工具，明确告诉模型：**只从 UI tree 里挑节点
     编号；不要猜坐标**。
   - JSON 输出 `{thought, action, args:{node_id, …}}`。
   - 主 LLM 返回 `done(success=false)` 时，用配置的 **fallback** LLM 再
     试一次（不同模型，互补价格/质量）。`get_fallback_provider` 和
     `get_provider` 在一块。

### Execute（`_execute`）

拿到 LLM 的 action 和 observation 的节点表后：

- `tap_node(node_id)` →
  1. 节点 `text` ≥ 2 字符就先试 `tap_text(node.text)`（accessibility 的
     ACTION_CLICK 对小幅度布局变化更鲁棒）。失败的话…
  2. 退回 `tap_xy(node.bounds 的中心)`。
- `input_text(node_id, text)` → 先 `tap_xy(center)` 聚焦，再 `input_text`。
- `swipe(direction, node_id?)` → 默认从屏幕计算坐标，传了节点就在节点
  bounds 内滑（用于滚动特定列表）。
- `back / home / open_wecom` → 直接原语。
- `done(success, summary)` → 终止循环。

**LLM 永远不会产出原始 x / y**。后端总是从 `_Node.bounds` 自行算坐标。
一行 grep 就能验证：`grep -n "tap_xy" backend/app/ai/react_agent.py` ——
每个调用点都是读 `.center()` 而不是接收 LLM 提供的坐标。

### Locator 缓存

`react_locators.LocatorStore` 在 LLM fallback 成功之后，把节点特征写到
`var/react_locator_cache/<robot>.json`。下次同样的 role + target 通过特征
指纹（view_id / class / text / desc / 相对 bounds）直接命中。匹配失败计数
累加；累计失败超阈值就被禁用。每次成功 LLM 回复也会再保存一份原始 payload
到 `var/react_fallbacks/`，供离线分析。


## 8. 知识库

### 入库流水 —— `kb/pipeline.py`

```
UploadFile / 粘贴文本
  → KnowledgeDocument 行（status=pending，原始 bytes 暂存在 _PENDING dict）
  → 后台协程：ingest_document(db, doc_id)
    1. parsers.parse_for_team(team_id, name, mime, data) —— 见 §8.2
    2. chunker.chunk(text, kb.chunk_size, kb.chunk_overlap)
       → list[str]
    3. embedder.embed(pieces) —— embedding provider（mock / openai-compat）
    4. for each (piece, vector)：
         insert KnowledgeChunk(text, embedding_json=vec, …)
         entities.extract(piece, product_seeds=kb.description.split(','))
         graph_store.upsert_node(Chunk("chunk-<id>"))
         for each entity:
            graph_store.upsert_edge(Chunk -> Entity, "MENTIONS")
         实体两两加 CO_OCCURS 边
    5. vector_store.upsert(ids, vectors, metas={team_id, kb_id, doc_id,
                                                  chunk_id, text})
    6. doc.chunk_count、doc.status = "ready"
```

任何失败把 `doc.status = "failed"` + `error` 填进去；web 详情页会显示失败
徽章。

### 解析器 —— `kb/parsers/__init__.py`

按团队 `parser.backend` 选：

- `builtin`（默认）：`.txt`/`.md` → UTF-8 decode；`.pdf` → `pypdf`。
- `mineru_local`：在临时目录 spawn `mineru` CLI，把生成的所有 `.md` 拼起来。
- `mineru_api`：完整的 v4 云端流程 —— `POST /file-urls/batch` →
  `PUT presigned` → 轮询 `/extract-results/batch/{batch_id}` → 下载 zip →
  抽取 markdown。

在 `/settings/parser` 配置，每种后端都有探针按钮。

### 存储 —— 向量 + 图谱

- `kb/vectorstore/milvus.py` —— 集合在**第一个向量真实写入时**按它的维度
  自动建（不再依赖配置的 dim，所以从 mock-256 切到 Ollama-768 不需要手动
  drop）。删 KB / 删文档时 `delete_by_meta` 级联。filter 表达式嵌
  `team_id` 做租户隔离。
- `kb/graphstore/neo4j_store.py` —— 关系名清洗为 `^[A-Za-z0-9_]+$` 保证
  Cypher 安全；节点带 `team_id`。`delete_chunks` 用
  `MATCH (c:Entity{team_id, label:'Chunk'}) WHERE c.name IN $names
  DETACH DELETE c`。实体节点不删（可能被其它 chunk 引用）。
- 两个接口都各有 memory 实现；测试和「没 docker 的开发场景」用。
  **局限**：memory 向量存储重启即丢。`main._hydrate_vector_store` 在每次
  启动都从 SQL `KnowledgeChunk.embedding_json` 重新灌一次，**任何后端都
  跑**（upsert 是幂等的，对已就绪的 Milvus 是 no-op）。图谱状态不会被
  rehydrate —— 切图谱后端要重新入库。

### 检索 —— `kb/retriever.retrieve`

1. 把 query embed（按团队的 embedding provider）。
2. `vector_store.search(embed, top_k, filter={team_id})` → `VectorHit[]`。
3. 按 `min_score` 过滤（按团队配置，按 embedding 模型调）。
4. 对 top 命中做 1-hop 图谱扩展，借助 chunk 的 `entities_json`。图谱命中
   作为 `graph_facts` 一起返回，喂给 conv-agent 的 KB search 工具块。


## 9. 设置系统 —— `services/settings_service.py`

目前 5 个 scope：`llm` / `embedding` / `retrieval` / `ai` / `parser`。每个
scope 在 `team_settings` 表是一行（JSONB `value_json`），按 `(team_id, key)`
唯一。

`get(db, team_id, scope)` 分层：
1. `config.Settings` 的 env 默认（新字段的源真相）。
2. DB 覆盖（只允许 scope 白名单 `_ALLOWED` 里的 key）。

写入走 `upsert(db, team_id, scope, value)`，把 `_ALLOWED` 之外的 key 过滤
掉（纵深防御 —— Pydantic schema 是 router 层的第一道闸）。

密钥处理：`api_key` 类字段 GET 时返回 mask（`********`）；PUT 时空值或
mask 解释为「保留已保存的值」。`routers/settings.py` 的 `_mask_api_key`
对三个含密钥的 scope（`llm` / `embedding` / `parser`）统一处理。

探针端点（`POST /settings/test/{scope}`）实时探活但不持久化表单值 ——
方便在保存新 key 之前先验。


## 10. 长期记忆

`memory/summarizer.py`：

- 每一会话里每 N 条入站消息（默认 10）异步刷新一次
  `UserProfile.summary`。
- summary + `preferences_json` 会展示在工作台右栏，并拼到 conv-agent 的
  system prompt 前面。
- `set_profile_field` 工具让 agent 在对话中途记录长期事实（行业 / 角色 /
  预算）。这些会持续跨该联系人的所有会话。

`UserMemory` 行是更细粒度的存储，目前只有 `/memory/{contact_id}` REST 端点
读，前端还没用上。


## 11. Web 控制台 —— 每个页面做什么

`/workbench` —— 三栏实时对话：
- 左：会话列表，带未读徽章
- 中：消息气泡（每条带删除图标，会话头部有整会话删除）
- 右：AI 建议草稿、agent 命中的 KB 片段、客户画像

`/devices` —— 列表 + 创建 + 删除 + token 一次性展示
`/devices/[id]` —— 三栏布局：
- 顶：身份卡 + 控制状态（实时屏开关、最近命令）
- 中：实时屏卡片
- 右栏：语义指令卡（→ `/agent/run`）+ 任务日志面板（带清空，AlertDialog
  二次确认）

`/knowledge` —— KB 卡片。删除会级联 docs + chunks + 向量 + 图谱。
`/knowledge/[id]` —— 拖拽多文件上传、粘贴文本、文档表格按行删、检索探针。

`/settings` —— 分节表单，每张卡都有保存 + 测试（适用时）：
- **LLM** —— provider / model / key / url / temperature + `supports_vision`
  开关（开了设备 ReAct 才带截图）。
- **Embedding** —— provider / model / key / url / dim。换 dim 时 Milvus
  集合会作废（首次写入按新 dim 重建，老 chunk 可能要重新入库）。
- **Parser** —— backend（builtin / mineru_local / mineru_api）+ 各 backend
  的子配置（CLI 路径 / API key / model_version）。
- **Retrieval** —— top_k + min_score。
- **AI 行为** —— confidence_threshold / context_window / default_prompt /
  max_tokens / agent_mode / agent_max_steps / **react_force_llm**
  （设备 ReAct 决策：规则快路径 vs 每步走 LLM）。
- **Infra（只读）** —— vector backend / graph backend / Milvus / Neo4j
  URI。改这些走 env，不走 UI。

前端所有确认都用 shadcn `AlertDialog`。`Dialog` 留给表单 / 多步选择器。
**没有任何** `confirm()` / `alert()` 调用。


## 12. 安卓客户端生命周期

### 设置（`MainActivity`）

- 运营输入后端 WS URL、robot_id、robot token。
- 三个权限行：AccessibilityService、NotificationListener、电池优化豁免。
  每行带一键深链到对应的系统设置页。
- 三个开关：dry-run（不真正驱动 UI）、屏常亮（`FLAG_KEEP_SCREEN_ON`）、
  a11y 入站采集（**默认开** —— 不开的话 WeCom 前台收到的消息永远到不了
  后端）。
- 点「启动 Agent」 → 用这些参数启动 `AgentForegroundService`。

### `AgentForegroundService`

整个服务生命周期的协程 scope。拥有：

- `BackendClient` —— 单一 WS；指数退避重连。
- `TaskExecutor` —— 接收 `task.dispatch`。send_text 已经搬到后端 agent
  之后，这里基本是个 no-op 壳。
- 三个 scanner job —— `MessageListScanner` tier 1/2/3（30s / 5min / 30min）。
- 心跳 job —— 每 30s 发 `device.heartbeat`，带电量 + 当前页面。
- `handleReactCommand` —— 把每个 `device.command` 路由到 `WeComAutomator`
  的原语（或 `WeComAccessibilityService` 的 dump / 截图），用同一个
  `request_id` 回 `device.command_result`。
- `runSendTest` —— 应用内的「本地测试发送」按钮。用 `task_id=-1` 哨兵；
  后端拿到带这个 id 的回调直接 return。

### `WeComAccessibilityService`

单例 —— `instance` 是外面所有用法的入口。

- `currentPage` 在 `TYPE_WINDOW_STATE_CHANGED` 时更新（按 class name 粗映
  射）。作为提示但**不**是 ReAct 决策的依据 —— agent 每步都重新 observe
  root 的实际内容。
- `maybeHarvestChat` / `maybeHarvestHomeList` 在每个
  `TYPE_WINDOW_CONTENT_CHANGED` 上跑；两者都遍历树、按启发式过滤、用
  baseline-then-diff 避免重放历史。
- `dumpTreeWithNodes()` 是规范的 UI 快照 —— 同时产文本 + 平铺
  `DumpedNode` 列表，两者编号对齐通过同一 `device.ui_dump` 载荷送给后端。
- `captureScreenJpegBase64` 用 API 30+ 的 `takeScreenshot`；返回 base64
  JPEG。

### `MessageListScanner`

三个方法（`scanVisible` / `scanPagesDown(n)` / `scanToBottom`）。每个都先
检查 (a11y 在跑) ∧ (WeCom 在前台) ∧ (在消息 tab)。滑动版本只在会话列表
bounds 内滑（不是全屏）并且**滑完恢复滚动位置**，用户感知不到。
`scanToBottom` 用指纹（前 6 个可见行文本的轻量 hash）判断到底了没。

### `WeComAutomator`

重写之后这个文件只有**通用**原语（`reactTapText` / `reactTapXY` /
`reactSwipe` / `reactInputText` / `reactBack` / `reactHome` / `openWeCom` /
`dumpTree`）。`sendText` / `ensureTargetChat` / `isHomeLike` /
`inferChatTitle` 等等全部砍掉 —— 所有这些判断逻辑搬到后端 ReAct 循环里
（可以借同一套基础设施调试 + 观察）。


## 13. 后台任务（FastAPI lifespan）

`backend/app/main.py` —— 启动时按顺序：

1. `init_db()` —— 在线程里跑 `alembic upgrade head`。
2. `_ensure_seed()` —— 用户表为空时创 `admin@example.com / admin123` + 一个
   团队。
3. `_bootstrap_agent_tools()` —— 注册内置工具，加载 `skills/*.py`，连接
   MCP 服务器（best-effort）。
4. `_hydrate_vector_store()` —— 把 SQL 里的 chunk embedding 灌进当前的
   向量存储。**任何后端都跑**（Milvus 幂等，memory 必须）。
5. `asyncio.create_task(retention.run_loop())` —— 每日消息清理，shutdown
   可取消。

Shutdown 时：

1. `retention_task.cancel()` + await。
2. `mcp_adapter.shutdown()` —— 关掉每个 MCP stdio 子进程。

会话锁字典、工具注册表、MCP session 都是进程全局；**多 worker 部署会出
问题**。生产环境暂时只能跑 1 个 uvicorn worker，直到我们加上 Redis 分布式
锁 + 共享注册表（详见 `services/conversation.py` 里的 TODO）。


## 14. 配置速查

完整列表在 `core/config.py`。最常调的 env：

```bash
# 存储
DATABASE_URL=postgresql+asyncpg://wecom_ai:...@localhost:5432/wecom_ai_agent
VECTOR_STORE=milvus            # 或 memory
MILVUS_URI=http://localhost:19530
GRAPH_STORE=neo4j              # 或 memory
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=...

# AI 默认（团队级在 /settings 覆盖）
LLM_PROVIDER=openai            # 或 mock
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1
LLM_SUPPORTS_VISION=false      # 想让设备 ReAct 带截图就打开
LLM_TEMPERATURE=0.7
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536

# AI 行为
AI_MAX_TOKENS=8192
AGENT_MODE_ENABLED=true
AGENT_MAX_STEPS=5
REACT_FALLBACK_ENABLED=true    # 历史字段；现在 send 主路径就是 react
REACT_MAX_STEPS=6
REACT_STEP_TIMEOUT_SEC=12.0
REACT_FORCE_LLM=false          # true = 跳过规则快路径，每步都走 LLM

# Parser
PARSER_BACKEND=builtin         # 或 mineru_local / mineru_api
MINERU_API_BASE=https://mineru.net/api/v4
MINERU_API_TOKEN=...

# Skills + MCP
SKILLS_DIR=skills
MCP_SERVERS_JSON='[{"name":"weather","command":"uvx","args":["mcp-server-weather"]}]'

# Retention
MESSAGE_RETENTION_DAYS=30
RETENTION_SWEEP_INTERVAL_SEC=21600
```

放在 `backend/.env`（已 gitignore；pydantic-settings 自动加载）。web 的
`/settings` 页可以无重启覆盖任意团队级字段。


## 15. 可观测性

- **后端日志**：每个 workflow + agent 决策都打结构化 info。关键 tag：
  - `[workflow] inbound conv=… text=…` / `[workflow] decision action=…`
  - `[agent] enter team=… tools=N text=…`
  - `[agent] step i/N tool=… thought=… args=…`
  - `[react] goal=… max_steps=N mode={rule+llm|llm_only}`
  - `[react] step i observed nodes=N screen=WxH screenshot=yes|no`
  - `[react] step i source={rule|cache|llm} thought=… action=… args=…`
  - `[react] step i → ok=… msg=… (Xms)`
  - `[react] result ok=… steps=N summary=…`

- **任务日志表（`robot_task_logs`）** —— ReAct 每一步都通过
  `append_task_log` 写一行。web 设备详情页解析渲染（每步加 badge，结果
  行按状态着色），并支持按设备一键清空。

- **UI dump** —— agent 或运营请求一次就保存到
  `var/ui_dumps/<robot>-<ts>-<page>-<reason>.txt`。节点 JSON 通过 WS 推给
  运营但不单独落盘。

- **Locator 工件** —— `var/react_fallbacks/<robot>-<ts>.json` 在 agent 首
  次通过 LLM 成功命中一个新 target 时落一份原始 payload。便于把基线
  locator 缓存搬到不同环境。


## 16. 已知问题 / 限制

走读时应该心里有数：

1. **单 worker 假设**。`_CONV_LOCKS`、工具注册表、MCP session、WS Hub、
   `request_id` future 全部在进程内存里。生产用 1 个 uvicorn worker。HA 需
   要加 Redis pub/sub + Redis 锁。
2. **memory 向量 / 图谱存储不持久**。生产必须用 Milvus + Neo4j。hydrate
   步骤让 memory 在重启后能从 SQL 恢复，但图谱状态除非用 Neo4j 否则每次
   重启都丢。
3. **conv-agent 和设备 ReAct 是两个独立的循环**。它们共享的只是 `Tool`
   这个抽象名字 —— 设备侧有固定的原语集，不接 skill / MCP。给会话 agent
   加新 skill **不会**自动让设备 agent 用上。
4. **目前不用 OCR**。`screenshot_once` 已经暴露，设备 ReAct 在
   `supports_vision=true` 时能带截图，但流水里没有 OCR 步骤 —— 多模态 LLM
   直接读像素。
5. **目标语句以中文为中心**。`parse_send_goal` 正则匹配的是中文模板；其它
   语言的 goal 每一步都会落到 LLM 路径。
6. **`task_id=-1` 哨兵**用于本地测试发送，绕开 agent / 任务回调路径。
   这是有意的，但如果你疑惑「为什么应用内『本地测试』按钮不出现在任务日
   志里」，就是这个原因。


## 17. 新人阅读指南

按顺序，按文件：

1. `backend/app/services/conversation.py` —— 入站管线（一个函数从头到尾）。
2. `backend/app/ai/workflow.py` —— AI 分发分叉（单跳 vs agent）。
3. `backend/app/ai/conv_agent.py` —— 回复侧 ReAct 循环。
4. `backend/app/ai/react_agent.py` —— 设备侧 ReAct 循环。
5. `backend/app/services/send_orchestrator.py` —— 一个 Decision 怎么变成
   一个在跑的 agent 任务。
6. `backend/app/ws/android.py` + `backend/app/core/ws_manager.py` —— 请求
   /响应总线。
7. `android/.../service/WeComAccessibilityService.kt` —— 设备侧的 dump +
   harvest 对应实现。
8. `android/.../service/AgentForegroundService.kt` —— 服务生命周期 + 命令
   分发。

其它文件等用到再翻。大部分「X 在哪发生」的问题 `grep -rn` 上面这 8 个文
件就能查到。
