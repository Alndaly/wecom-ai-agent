# Backend (FastAPI)

## Run

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e .
uvicorn app.main:app --reload --port 8000
```

首次启动会自动建表，并播种一个管理员：

```
email:    admin@example.com
password: admin123
```

OpenAPI 文档：<http://localhost:8000/docs>

## 设备 token
`POST /robots` 返回 `token` 只显示一次（数据库里只存哈希）。Android 用 `?robot_id=&token=` 连接 `/ws/android`。

## 测试
```bash
pip install -e ".[dev]"
pytest
```
