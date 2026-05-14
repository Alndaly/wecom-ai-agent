# 15 · 数据库与迁移

> 这一章的目标：本地随手玩用 SQLite，准备上线就切 Postgres，**两条路都走同一份 Alembic 迁移**。

## 15.1 一张表总览

| 环境 | URL 例子 | 用谁 |
| --- | --- | --- |
| 开发（默认） | `sqlite+aiosqlite:///./dev.db` | runtime: aiosqlite · alembic: pysqlite |
| Docker Compose | `postgresql+asyncpg://wecom:wecom@postgres:5432/wecom` | runtime: asyncpg · alembic: psycopg |
| 生产 | `postgresql+asyncpg://…` | 同上 |

env.py 在跑 alembic 时**自动**把 `+aiosqlite` / `+asyncpg` 替换成它们的同步对应：

```python
sqlite+aiosqlite:///x  →  sqlite:///x
postgresql+asyncpg://x →  postgresql+psycopg://x
```

所以 **一份 `DATABASE_URL` env 就够了**，runtime 和迁移都用它。

## 15.2 启动时的迁移

`app.core.db.init_db()` 不再调 `Base.metadata.create_all`。它现在做：

```
asyncio.to_thread(_upgrade_to_head)
   └─ alembic.command.upgrade(cfg, "head")
```

也就是说 **每次后端启动都会自动把 schema 升到最新**。

- 首次启动空库：跑全部迁移
- 已是最新：alembic 看到 `alembic_version` 表的 head 标记，直接跳过
- 落后几个版本：从当前位置往后跑

不需要手动 `alembic upgrade` —— 但你想手动也可以（见下）。

## 15.3 手动操作

```bash
cd backend
. .venv/bin/activate

# 看当前在哪个版本
DATABASE_URL=postgresql+asyncpg://… alembic current

# 看所有迁移
alembic history

# 升到最新
DATABASE_URL=postgresql+asyncpg://… alembic upgrade head

# 退到上一版
DATABASE_URL=postgresql+asyncpg://… alembic downgrade -1

# 退到空库
DATABASE_URL=postgresql+asyncpg://… alembic downgrade base

# 不连数据库，只输出 SQL (适合 DBA 审 review)
DATABASE_URL=postgresql+asyncpg://… alembic upgrade head --sql > migrate.sql
```

## 15.4 改了模型，怎么写新迁移

1. 改 `backend/app/models.py`
2. 准备一个**干净的空库**（最简单：跑迁移到最新，然后再 `downgrade base` + `upgrade head` 拿到当前 head 状态——alembic autogen 是和 *DB 现状* 做 diff，不是和上一版迁移做 diff）

   ```bash
   alembic upgrade head
   ```

3. 生成草稿迁移：

   ```bash
   alembic revision --autogenerate -m "add foo to messages"
   ```

4. **打开生成的文件审一遍**。autogen 常见漏：
   - `ALTER COLUMN` 类型改动（特别是 SQLite → Postgres 之间的 JSON）
   - `server_default` 的细节
   - 索引重命名
5. 重命名文件为 `00NN_xxx.py`，把 `revision = "..."` 字符串也改成同样名字（保持可读 + 顺序明显）
6. 跑一遍 `alembic upgrade head`，确认 OK
7. 写 `downgrade()`（autogen 一般会写,但要核对反向是否真的能跑）
8. 提交

## 15.5 切换到 Postgres（dev）

```bash
docker compose up postgres -d              # 单独起 postgres
export DATABASE_URL="postgresql+asyncpg://wecom:wecom@localhost:5432/wecom"
cd backend && . .venv/bin/activate
uvicorn app.main:app --reload --port 8000   # 启动时自动跑迁移
```

或者一锅端：

```bash
docker compose up                           # backend 默认连 compose 里的 postgres
```

## 15.6 Postgres 端到端验证

提供了一个 `tools/postgres_smoke.py`,用 `PG_TEST_URL` 启用：

```bash
export PG_TEST_URL="postgresql+asyncpg://<user>:<pwd>@<host>:5432/wecom_ai_test"
backend/.venv/bin/python tools/postgres_smoke.py
```

会做这几件事：
1. `alembic downgrade base` + `alembic upgrade head` —— 验证迁移可逆
2. 启动一个独立端口的 backend 连这个 DB
3. 跑登录 / 建机器人 / inbound 消息 / KB 上传 / settings 写入
4. 验证 `team_settings` JSON 列、`knowledge_documents.status` 状态机、外键约束都正常工作

**它会清空 `PG_TEST_URL` 指向的 DB**，不要指生产。

## 15.7 生产部署注意

- 上线前先在 staging 跑 `alembic upgrade head --sql > migrate.sql`，让 DBA 看一眼 SQL
- 大表 `ALTER COLUMN TYPE` / 加非空列要写 **online migration**（分多步：加可空列 → 回填 → 加约束）。Alembic 不会替你想这些，要自己写
- 迁移失败时 Alembic **不会自动回滚**——transaction 是逐 op 提交的。准备好 `downgrade` 路径
- 多实例 (k8s replicas) 同时启动会有多个 `alembic upgrade head` 并发竞争。临时方案：`alembic_version` 表的行锁让它们排队（短迁移没事）；正经做法是用 init container 或 helm hook 先跑一次再起 backend

## 15.8 备份

- Postgres：常规 `pg_dump`,或上 `pgbackrest`
- SQLite：直接 cp `dev.db` 即可
- 业务数据 + `var/ui_dumps/` 都要备：前者是数据，后者是 RPA 校准基线
