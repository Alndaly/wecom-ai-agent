# AI Workflow

## 状态
**MVP2 已落地（hand-rolled state machine + LLMProvider 抽象）**。后续可平滑迁移到 LangGraph，节点 API 不变。

## 决策模型

`handle_inbound(robot, conv, message)` 返回一个 `Decision`：

```python
@dataclass
class Decision:
    action: Literal["reply", "suggest", "skip"]
    text: str | None
    confidence: float
    trace_id: str           # 12-hex, 用于审计 / 排错
    reason: str             # 当 skip / suggest 时说明原因
    latency_ms: int
    model: str
```

| action | 含义 | 后续动作 |
| --- | --- | --- |
| `reply` | AI 直接回复 | 自动创建 `send_text` 任务，进入每机器人队列 |
| `suggest` | AI 仅生成建议（混合模式 + 低置信） | 走 `ai.suggestion` 推 Web 工作台右栏 |
| `skip` | 不参与（mode=human / 人工锁中 / LLM 失败） | 无动作 |

## 节点

```
[entry]
  ↓
mode_gate          mode == human  → skip
  ↓
load_history       拿 settings.ai_context_window 条
  ↓
load_prompt        ai_prompts.key='default' 或 fallback settings.ai_default_prompt
  ↓
generate           LLMProvider.chat(...)
  ↓
confidence_gate    mixed mode 且 confidence < threshold → suggest
  ↓
finalize           写 ai_reply_logs
  ↓ (action=reply)
enqueue_task        auto_reply_scheduler 调 send_orchestrator 创建 send_text 任务
```

自动回复不是在入站落库函数里直接执行。`conversation.py` 只把真实客户消息标为
`feedback_status=pending` 并唤醒 `auto_reply_scheduler`。调度器按 robot 公平轮转：
同一会话最多连续处理 2 轮；一轮最多聚合 20 条待反馈入站；回复任务最多创建 2 条。

## LLMProvider 抽象

文件 `backend/app/ai/providers/`：

| Provider | 用途 | 环境变量 |
| --- | --- | --- |
| `MockProvider` (默认) | 无须 API key，规则驱动，覆盖几种典型场景 | `LLM_PROVIDER=mock` |
| `OpenAICompatibleProvider` | OpenAI / DeepSeek / 通义 / Zhipu / Ollama-v1 | `LLM_PROVIDER=openai` + `LLM_API_KEY` + `LLM_MODEL` + 可选 `LLM_BASE_URL` |

新增 Provider 只需实现 `LLMProvider.chat(...)` 并在 `__init__.py` 的 `get_provider()` 注册。

## 数据
- `ai_prompts(team_id, key, content, version)` —— 每团队按 `key` 维护多份提示词，`default` 是兜底
- `ai_reply_logs(team_id, conversation_id, message_id, trace_id, action, text, confidence, model, latency_ms, reason, created_at)`

每次 AI 决策都会写日志（包括 skip / suggest），用于后续命中率、置信度分布、转人工率统计。

## API

```
GET  /ai/info                          → provider / model / 阈值
GET  /ai/prompts                       → 列出
PUT  /ai/prompts          {key,content}→ 新增 / 覆盖（版本号 +1）
GET  /ai/prompts/default               → 默认提示词
GET  /ai/logs?conversation_id=&limit=  → 决策日志
```

## 人工协同

当前依赖 `conversation.mode`：客服在 Web 把会话切到 `human` 后，自动回复调度器不会为该会话继续触发 AI。人工发送会覆盖本会话待反馈客户消息并记录到 `feedback_reply_task_ids`。

## 验收（MVP2）已通过 ✅
- [x] mode=ai 时，客户消息 → AI 自动创建队列任务，消息标 `sender_type=ai`
- [x] mode=mixed 且置信度低 → AI 不发送，仅推 `ai.suggestion` 给 Web
- [x] mode=human → AI 跳过，仅推 Web
- [x] 每次决策都写 `ai_reply_logs`，含 `trace_id`
- [x] Provider 用 `LLM_PROVIDER` 环境变量切换，默认 mock 可零配置启动

实跑见：`tools/ai_smoke.py`。
