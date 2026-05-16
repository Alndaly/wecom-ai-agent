# 09 · 数据模型

> 字段类型用泛型描述（`bigint` / `text` / `jsonb` / `timestamptz`）。本地可用 SQLite 跑通闭环，正式部署建议 PostgreSQL。

## 9.1 实体关系一览

```
teams ─┬─ users ─── user_roles ─── roles ─── role_permissions ─── permissions
       ├─ robots ─── robot_tasks ─── robot_task_logs
       │         └── robot_status_logs
       ├─ contacts ─── conversations ─── messages
       │                       └── conversation_modes (历史)
       ├─ knowledge_bases ── knowledge_documents ── knowledge_chunks
       ├─ team_settings
       ├─ ai_prompts
       ├─ ai_reply_logs
       ├─ user_profiles ─── user_memories
       ├─ sop_rules
       ├─ moment_tasks
       ├─ risk_rules ─── risk_events
       └─ audit_logs
```

## 9.2 核心表

### teams
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigint pk | |
| name | text | |
| created_at | timestamptz | |

### users
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigint pk | |
| team_id | fk teams | |
| email | text uniq | |
| password_hash | text | |
| display_name | text | |
| created_at | timestamptz | |

### robots
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigint pk | |
| team_id | fk teams | |
| name | text | |
| robot_id | text uniq | Android 对外标识 |
| token | text | Android 鉴权 |
| status | enum(offline/online/busy) | |
| current_page | text null | 最近心跳页面 |
| last_seen_at | timestamptz null | |
| created_at | timestamptz | |

### contacts
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigint pk | |
| team_id | fk teams | |
| robot_id | fk robots | |
| external_id | text | 企微侧联系人标识 |
| nickname | text | |
| avatar | text null | |
| stage | text default 'new' | |
| tags_json | jsonb default '[]' | |
| created_at | timestamptz | |

UNIQUE (`robot_id`, `external_id`)

### conversations
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigint pk | |
| team_id | fk teams | |
| robot_id | fk robots | |
| contact_id | fk contacts | |
| mode | enum(ai/human/mixed) default 'mixed' | AI / 人工 / 混合 |
| operator_id | fk users null | 当前人工处理人 |
| unread_count | int default 0 | Web 未读展示 |
| last_message_at | timestamptz null | |
| last_message_preview | text null | |
| created_at | timestamptz | |

UNIQUE (`robot_id`, `contact_id`)

### messages
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigint pk | |
| conversation_id | fk conversations | |
| direction | enum(in/out) | 入站 / 出站 |
| sender_type | enum(customer/ai/human/system) | 当前系统消息通常不落库 |
| sender_id | bigint null | `sender_type=human` 时为 users.id |
| type | enum(text/image/file/voice/video/system) | |
| content | text | |
| status | enum(pending/sending/sent/failed/cancelled) null | 出站消息发送状态 |
| external_msg_id | text null | 入站去重键 |
| task_id | bigint null | 出站关联 robot_tasks |
| feedback_status | text null | 入站客户消息的反馈状态 |
| feedback_trace_id | text null | AI 决策 trace |
| feedback_at | timestamptz null | 最近反馈状态变更时间 |
| feedback_reply_task_ids | jsonb null | 覆盖该批入站的回复任务 id 列表 |
| created_at | timestamptz | |

INDEX (`conversation_id`, `created_at`)
UNIQUE (`external_msg_id`) WHERE NOT NULL
INDEX (`conversation_id`, `direction`, `sender_type`, `feedback_status`) 用于待反馈扫描

#### feedback_status

| 状态 | 含义 |
| --- | --- |
| `pending` | 真客户消息已入库，尚未被 AI / 人工处理 |
| `processing` | 自动回复调度器已锁定该批消息，正在运行 AI |
| `queued` | 已创建回复任务，等待或正在设备发送 |
| `replied` | 关联回复任务发送成功 |
| `suggested` | 混合模式下生成建议，等待人工采纳 |
| `skipped` | AI 判断本轮不需要回复 |
| `failed` | AI 决策、发送或取消失败，需要人工处理或重试 |

一条客户消息只要进入系统，就必须最终离开 `pending/processing`，否则会被启动恢复逻辑重新唤醒。

### robot_tasks
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigint pk | |
| robot_id | fk robots | |
| type | text | `send_text` / `agent_goal` 等 |
| payload_json | jsonb | `send_text` 会包含 `feedback_message_ids` |
| status | enum(pending/dispatched/queued/running/completed/failed/timeout/cancelled) | |
| attempts | int default 0 | |
| max_attempts | int default 2 | |
| last_error | text null | |
| conversation_id | fk conversations null | |
| message_id | bigint null | 出站消息软关联 |
| created_at / updated_at | timestamptz | |

### robot_task_logs
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigint pk | |
| task_id | bigint null | 任务删除后可置空 |
| robot_id | fk robots | |
| level | text | info / warn / error |
| message | text | 队列和 ReAct 步骤日志 |
| payload_json | jsonb null | |
| created_at | timestamptz | |

## 9.3 AI / 知识库 / 记忆

- `ai_prompts(id, team_id, key, content, variables_json, version)`
- `ai_reply_logs(id, conversation_id, message_id, decision_json, trace_id, latency_ms, model, created_at)`
- `team_settings(team_id, scope, key, value_json)`
- `user_profiles(contact_id pk, summary, preferences_json, stage, updated_at)`
- `user_memories(id, contact_id, kind, content, vector_id, created_at)`
- `knowledge_bases / knowledge_documents / knowledge_chunks` 见 [knowledge.md](modules/knowledge.md)
- `sop_rules` / `moment_tasks` / `risk_rules` / `risk_events` / `audit_logs`
