# Web (Next.js)

## Run

```bash
cd web
cp .env.local.example .env.local   # 视情况修改
npm install
npm run dev
```

打开 <http://localhost:3000>，默认账号 `admin@example.com / admin123`（来自后端首启播种）。

## 页面

- `/`            登录
- `/workbench`   客服工作台（会话列表 / 聊天 / 客户面板）
- `/devices`     设备管理（创建 / token 一次性展示 / 删除）

## 注意
- token 只在 `localStorage` 保存（MVP1 简单方案）；生产应换 httpOnly cookie。
- WS 在每个页面里自动连上 `/ws/web?token=...`，断开自动重连。
