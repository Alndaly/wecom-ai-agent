from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./dev.db"
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_min: int = 60 * 24
    jwt_refresh_expire_days: int = 30
    cors_origins: str = "http://localhost:3000"
    log_level: str = "INFO"

    heartbeat_timeout_sec: int = 90
    # ---- AI ----
    llm_provider: str = "mock"  # mock | openai
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = ""
    llm_base_url: str = ""  # for openai-compatible endpoints
    llm_temperature: float = 0.7
    # Set true when llm_model can read inline images (gpt-4o / qwen-vl / glm-4v).
    # The ReAct device agent attaches the current screenshot to each step when
    # this is on.
    llm_supports_vision: bool = False
    ai_confidence_threshold: float = 0.55  # below → escalate to human
    ai_context_window: int = 10  # how many recent messages to feed
    ai_max_tokens: int = 8192  # output ceiling per reply
    # Master kill-switch for device-driving tasks. Keep this off while testing
    # raw message callback coverage so inbound messages only log + persist.
    task_queue_enabled: bool = True
    auto_reply_enabled: bool = True
    inbound_content_dedupe_enabled: bool = False
    # When a deterministic UI task fails, escalate to the ReAct fallback agent
    # (observes UI tree, asks LLM, executes primitives). Off by default — it
    # costs extra LLM calls per failure.
    react_fallback_enabled: bool = True
    react_max_steps: int = 6
    react_step_timeout_sec: float = 12.0
    # When False (default): deterministic locators try first, LLM is fallback
    # (cheaper and faster for routine flows). When True: every step goes to
    # the LLM with UI tree + screenshot, no rule shortcut. AI always picks a
    # node id (never raw x/y) — backend resolves to coordinates.
    react_force_llm: bool = False
    # ---- Conversational agent (ReAct + Tools / Skills / MCP) ----
    agent_mode_enabled: bool = True
    agent_max_steps: int = 5
    # Where to look for user-authored skill modules. Each *.py exports `tool`
    # (or `tools: list[Tool]`); see app/ai/tools/skills.py for an example.
    skills_dir: str = "skills"
    # MCP servers to spawn at startup. JSON array, see
    # app/ai/tools/mcp_adapter.py for the shape.
    mcp_servers_json: str = ""
    ai_default_prompt: str = (
        "你是企业的私域客服助手。请用简洁、礼貌、不啰嗦的中文回复客户。"
        "如果你不确定答案,请回复一句简短的承接语并标注 confidence 较低。"
    )

    # ---- Embedding / Vector / Graph ----
    embedding_provider: str = "mock"        # mock | openai
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 256                # mock default; OpenAI 1536/3072
    embedding_api_key: str = ""             # fallback to llm_api_key
    embedding_base_url: str = ""

    vector_store: str = "milvus"            # memory | milvus
    milvus_uri: str = "http://localhost:19530"
    milvus_collection: str = "kb_chunks"

    graph_store: str = "neo4j"              # memory | neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "12345678"  # matches the running neo4j container; override via env

    # ---- KB retrieval ----
    kb_top_k: int = 5
    # Min cosine for a hit. Mock embedding (char-bigrams, dim=256) peaks around 0.2 for
    # very similar Chinese text, so default low. With OpenAI / dense models, raise to
    # ~0.5 via env (KB_MIN_SCORE=0.5).
    kb_min_score: float = 0.05
    kb_chunk_size: int = 400
    kb_chunk_overlap: int = 60

    # ---- Document parser (MinerU) ----
    # builtin: pypdf + plain text. mineru_local: invoke the `mineru` CLI on the
    # backend host. mineru_api: hit mineru.net cloud API with a bearer token.
    parser_backend: str = "builtin"  # builtin | mineru_local | mineru_api
    mineru_api_base: str = "https://mineru.net/api/v4"
    mineru_api_token: str = ""
    mineru_model_version: str = "vlm"  # vlm | pipeline
    mineru_local_cmd: str = "mineru"
    mineru_local_extra_args: str = ""  # e.g. "-b pipeline" for CPU-only
    mineru_timeout_sec: int = 600

    # ---- Long-term memory ----
    memory_summary_every: int = 10  # generate / refresh summary every N inbound msgs
    memory_refresh_enabled: bool = False
    # ---- Message retention ----
    # Daily sweep deletes messages older than this. Set to 0 to disable.
    message_retention_days: int = 30
    retention_sweep_interval_sec: int = 6 * 60 * 60  # 6h

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
