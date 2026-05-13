# ADR-0002 · SQLite 起步，Postgres 收尾

**状态**：accepted

## 背景
MVP1 目标是跑通闭环，不需要并发 / JSONB / 全文检索的能力。

## 决策
- 开发与 MVP1 用 SQLite（零依赖、单文件）。
- MVP2 起加 Alembic 迁移，切换 Postgres。
- 业务代码用 SQLAlchemy ORM 屏蔽差异；避免使用 Postgres 专有语法。

## 后果
- 提速 MVP1 启动
- 切库时需要回归一次（已有 e2e 用例兜底）
