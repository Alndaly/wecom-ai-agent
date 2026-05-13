# 设备管理

## 职责
- 注册 Android 设备并分配 `robot_id`
- 维护设备在线状态（基于 WS 连接 + 心跳）
- 任务队列每设备一条
- 状态上报：当前页面、当前任务、错误日志、截图

## 关键模型
- `robots(id, team_id, name, robot_id, token, status, last_seen_at, current_page, created_at)`
- `robot_status_logs(id, robot_id, page, payload_json, created_at)`

## 状态
`OFFLINE → ONLINE → BUSY → ONLINE → OFFLINE`

- `ONLINE`：WS 连上 + 心跳正常
- `BUSY`：有任务执行中
- `OFFLINE`：心跳超时（默认 90s）

## 接口（MVP1）
- `POST /robots` 创建（返回 `robot_id` + `token`）
- `GET /robots` 列表
- `GET /robots/{id}` 详情
- `DELETE /robots/{id}` 删除

WS：`ws://.../ws/android?robot_id=...&token=...`

## 验收
- [ ] 后台能新建设备并复制其 token
- [ ] Android 拿 token 连上 → 后台显示 ONLINE
- [ ] 断开 90s → 显示 OFFLINE
