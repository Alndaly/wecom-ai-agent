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
    task_dispatch_timeout_sec: int = 60

    # ---- AI ----
    llm_provider: str = "mock"  # mock | openai
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = ""
    llm_base_url: str = ""  # for openai-compatible endpoints
    llm_temperature: float = 0.7
    ai_confidence_threshold: float = 0.55  # below → escalate to human
    ai_context_window: int = 10  # how many recent messages to feed
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

    vector_store: str = "memory"            # memory | milvus
    milvus_uri: str = "http://localhost:19530"
    milvus_collection: str = "kb_chunks"

    graph_store: str = "memory"             # memory | neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j"

    # ---- KB retrieval ----
    kb_top_k: int = 5
    # Min cosine for a hit. Mock embedding (char-bigrams, dim=256) peaks around 0.2 for
    # very similar Chinese text, so default low. With OpenAI / dense models, raise to
    # ~0.5 via env (KB_MIN_SCORE=0.5).
    kb_min_score: float = 0.05
    kb_chunk_size: int = 400
    kb_chunk_overlap: int = 60

    # ---- Long-term memory ----
    memory_summary_every: int = 10  # generate / refresh summary every N inbound msgs

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
