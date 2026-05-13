# 09 · 数据模型

> 字段类型用泛型描述（`bigint` / `text` / `jsonb` / `timestamptz`）。MVP1 用 SQLite 跑通，正式部署用 Postgres。

## 9.1 实体关系一览
```
teams ─┬─ users ─── user_roles ─── roles ─── role_permissions ─── permissions
       ├─ robots ─── robot_tasks
       │         └── robot_status_logs
       ├─ contacts ─── conversations ─── messages
       │                       └── conversation_modes (历史)
       ├─ knowledge_bases ── knowledge_documents ── knowledge_chunks
       ├─ ai_prompts
       ├─ ai_reply_logs
       ├─ user_profiles ─── user_memories
       ├─ sop_rules
       ├─ moment_tasks
       ├─ risk_rules ─── risk_events
       └─ audit_logs
```

## 9.2 MVP1 必备表

### teams
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | bigint pk | |
| name | text | |
| created_at | timestamptz | |

### users
| id | bigint pk |
| team_id | fk teams |
| email | text uniq |
| password_hash | text |
| display_name | text |
| created_at | timestamptz |

### robots
| id | bigint pk |
| team_id | fk teams |
| name | text |
| robot_id | text uniq (对外标识) |
| token | text (Android 鉴权) |
| status | enum(offline/online/busy) |
| current_page | text null |
| last_seen_at | timestamptz null |
| created_at | timestamptz |

### contacts
| id | bigint pk |
| team_id | fk teams |
| robot_id | fk robots |
| external_id | text (wxid) |
| nickname | text |
| avatar | text null |
| stage | text default 'new' |
| tags_json | jsonb default '[]' |
| created_at | timestamptz |

UNIQUE (`robot_id`, `external_id`)

### conversations
| id | bigint pk |
| team_id | fk teams |
| robot_id | fk robots |
| contact_id | fk contacts |
| mode | enum(ai/human/mixed) default 'mixed' |
| operator_id | fk users null |
| unread_count | int default 0 |
| last_message_at | timestamptz null |
| last_message_preview | text null |
| created_at | timestamptz |

UNIQUE (`robot_id`, `contact_id`)

### messages
| id | bigint pk |
| conversation_id | fk |
| direction | enum(in/out) |
| sender_type | enum(customer/ai/human/system) |
| sender_id | bigint null (users.id 当 sender_type=human) |
| type | enum(text/image/file/voice/video/system) |
| content | text |
| status | enum(pending/sending/sent/failed) null (仅 out) |
| external_msg_id | text null (in) |
| task_id | fk robot_tasks null (out) |
| created_at | timestamptz |

INDEX (`conversation_id`, `created_at`)
UNIQUE (`external_msg_id`) WHERE NOT NULL

### robot_tasks
| id | bigint pk |
| robot_id | fk robots |
| type | text |
| payload_json | jsonb |
| status | enum(pending/dispatched/running/completed/failed/timeout) |
| attempts | int default 0 |
| max_attempts | int default 2 |
| last_error | text null |
| conversation_id | fk null |
| message_id | fk messages null |
| created_at / updated_at | timestamptz |

## 9.3 MVP2+ 表（占位）
- `ai_prompts(id, team_id, key, content, variables_json, version)`
- `ai_reply_logs(id, conversation_id, message_id, decision_json, trace_id, latency_ms, model, created_at)`
- `user_profiles(contact_id pk, summary, preferences_json, stage, updated_at)`
- `user_memories(id, contact_id, kind, content, vector_id, created_at)`
- `knowledge_bases / knowledge_documents / knowledge_chunks` 见 [knowledge.md](modules/knowledge.md)
- `sop_rules` / `moment_tasks` / `risk_rules` / `risk_events` / `audit_logs`
