# ADR-0002 · SQLite 起步，Postgres 收尾

**状态**：accepted · 已实现 (MVP3.5)

## 背景
MVP1 目标是跑通闭环，不需要并发 / JSONB / 全文检索的能力。

## 决策
- 开发用 SQLite（零依赖、单文件）。
- 生产用 Postgres（asyncpg）。
- 两者**共用同一份 Alembic 迁移**——`alembic/env.py` 在跑迁移时把 async 驱动转成同步等价物（aiosqlite→pysqlite，asyncpg→psycopg）。
- 业务代码用 SQLAlchemy ORM 屏蔽差异；避免 Postgres 专有语法。
- 后端启动时 `init_db()` 自动 `alembic upgrade head`；不再使用 `Base.metadata.create_all`。

## 后果
- 一份 `DATABASE_URL` 同时驱动 runtime 和迁移
- 切库等价于改一个 env 变量；schema 漂移由迁移单一来源保证
- 多实例并发首次启动会竞争迁移；正经部署应用 init container 单独跑（见 [15-database-migrations.md](../15-database-migrations.md#157-生产部署注意)）

## 验证
- `tools/postgres_smoke.py` 用 `PG_TEST_URL` 拉真 Postgres 跑完整流程
- 5 个 SQLite smoke 全绿 = 同一份迁移在 SQLite 上也能跑
