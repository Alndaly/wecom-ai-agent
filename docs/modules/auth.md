# 认证与权限

## 职责
- 用户登录（账号密码 / 后续可接 SSO）
- JWT 签发与校验
- 多租户隔离（`team_id` 贯穿所有业务表）
- RBAC：`role → permission[]`
- API Token（机器对机器场景，如 Android、外部回调）
- 操作审计 `audit_logs`

## 关键模型
- `teams`、`users`、`roles`、`permissions`、`user_roles`、`role_permissions`、`api_tokens`、`audit_logs`

## MVP1 范围
仅做：用户表 + 登录返回 JWT + Depends 校验。RBAC / 多租户 / 审计放 MVP5。

## 接口（MVP1）
- `POST /auth/login` → `{access_token, token_type}`
- `GET /auth/me` → 当前用户

## 验收
- [ ] 用户能登录拿到 token
- [ ] 带 token 调 `/auth/me` 返回用户信息
- [ ] 不带 token 的受保护接口返回 401
