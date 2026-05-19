# WeCom Local CLIP Service

独立的本地图片/文本多模态 embedding 服务，给资源库的 `image_clip` 检索使用。

## 启动

```bash
cd /Users/kinda/Developer/wecom-ai-agent/clip-service
uv sync
CLIP_SERVICE_API_KEY=local uv run uvicorn clip_service.main:app --host 127.0.0.1 --port 7867
```

默认模型是 `jinaai/jina-clip-v2`。首次启动会下载模型。
如果下载 Hugging Face 模型时遇到限速，可以设置 `HF_TOKEN`。
该模型依赖 `sentence-transformers`、`einops`、`timm`、`pillow`、`torch` 和 `transformers<5`，都已写入本服务的 `pyproject.toml`。

## 后端配置

在 `backend/.env` 中配置：

```env
VISION_EMBEDDING_PROVIDER=openai
VISION_EMBEDDING_MODEL=jinaai/jina-clip-v2
VISION_EMBEDDING_API_KEY=local
VISION_EMBEDDING_BASE_URL=http://127.0.0.1:7867/v1
VISION_EMBEDDING_DIM=1024
VISION_EMBEDDING_INPUT_FORMAT=data_url
VISION_EMBEDDING_MIN_SCORE=0.25
```

然后重启 backend。

## API

健康检查：

```bash
curl http://127.0.0.1:7867/health
```

OpenAI-compatible embedding：

```bash
curl -sS http://127.0.0.1:7867/v1/embeddings \
  -H "Authorization: Bearer local" \
  -H "Content-Type: application/json" \
  -d '{"model":"jinaai/jina-clip-v2","input":"测试文本"}'
```

图片输入支持 data URL 字符串，或对象：

```json
{
  "model": "jinaai/jina-clip-v2",
  "input": {
    "type": "image",
    "mime_type": "image/jpeg",
    "data": "base64..."
  }
}
```

## 可选环境变量

- `CLIP_SERVICE_API_KEY`：启用 bearer token 校验。为空时不校验。
- `CLIP_SERVICE_MODEL`：默认模型名。
- `CLIP_SERVICE_LOCAL_MODEL_PATH`：从本地模型目录加载，避开 Hugging Face 网络。
- `CLIP_SERVICE_REVISION`：固定远程模型版本。
- `CLIP_SERVICE_DIM`：截断输出维度并重新归一化。默认不截断。

## 网络或镜像不稳定

如果 `HF_ENDPOINT=https://hf-mirror.com` 不可用，可以先取消镜像：

```bash
unset HF_ENDPOINT
```

如果本机配置了代理但代理进程没启动，Hugging Face 下载也会失败。先检查：

```bash
env | grep -E 'HF_|HUGGINGFACE|HTTPS?_PROXY|ALL_PROXY'
```

如果看到类似 `HTTP_PROXY=http://127.0.0.1:7897`，但该端口没有代理服务，先取消：

```bash
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY
unset http_proxy https_proxy all_proxy
```

如果缓存已经下坏，清理后重启：

```bash
rm -rf ~/.cache/huggingface/hub/models--jinaai--jina-clip-v2
rm -rf ~/.cache/huggingface/hub/models--jinaai--jina-clip-implementation
```

更稳定的方式是先下载到本地目录，然后让服务从本地加载：

```bash
cd /Users/kinda/Developer/wecom-ai-agent/clip-service
uv run python -c "from huggingface_hub import snapshot_download; snapshot_download('jinaai/jina-clip-v2', local_dir='./models/jina-clip-v2')"
CLIP_SERVICE_LOCAL_MODEL_PATH=./models/jina-clip-v2 CLIP_SERVICE_API_KEY=local uv run uvicorn clip_service.main:app --host 127.0.0.1 --port 7867
```
