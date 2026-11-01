"""Central configuration. Every value can be overridden with a CRICKET_* env var or backend/.env."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent
DEV_SESSION_SECRET = "dev-only-change-me-0123456789abcdef"
DEV_ADMIN_TOKEN = "dev-admin-token-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CRICKET_", env_file=BACKEND_DIR / ".env", extra="ignore")

    # Server. Bind to loopback by default: this is a local app, not an internet service.
    host: str = "127.0.0.1"
    port: int = 8000
    allowed_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8000", "http://localhost:8000"]
    frontend_dist: Path = PROJECT_DIR / "frontend" / "dist"

    # Secrets. Generated into .env by scripts/setup.sh. The defaults are public (they're in this file), so the app refuses
    # to start with them unless allow_dev_secrets is set on purpose (see main.create_app).
    session_secret: str = Field(default=DEV_SESSION_SECRET, min_length=32)
    admin_token: str = Field(default=DEV_ADMIN_TOKEN, min_length=16)
    allow_dev_secrets: bool = False
    session_ttl_s: int = 24 * 3600

    # Data
    data_dir: Path = PROJECT_DIR / "Cricket-data"
    var_dir: Path = BACKEND_DIR / "var"
    chroma_collection: str = "cricket_knowledge"

    # Ollama
    ollama_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen2.5:3b"
    think_model: str = "qwen3:1.7b"
    embed_model: str = "nomic-embed-text"
    llm_timeout_s: float = 90.0
    llm_connect_timeout_s: float = 3.0
    embed_timeout_s: float = 15.0
    llm_num_ctx: int = 4096
    llm_max_output_tokens: int = 400
    agent_max_tool_rounds: int = 3

    # Live feed replay
    replay_match_id: int = 59  # IPL 2017 final, Mumbai Indians vs Rising Pune Supergiant
    replay_ball_interval_s: float = 4.0
    replay_autostart: bool = True

    # Traffic control
    rate_per_client_per_min: int = 20   # sustained questions per client
    rate_burst_per_client: int = 6
    rate_global_per_s: float = 10.0
    session_create_per_min: int = 10    # new sessions per IP: stops token farming to dodge per-fan limits
    llm_max_concurrency: int = 1        # CPU inference: parallel calls only slow everyone down
    llm_queue_max: int = 6              # waiting requests beyond this are shed immediately
    llm_queue_timeout_s: float = 45.0
    max_question_chars: int = 500
    max_body_bytes: int = 16_384

    # Circuit breakers
    breaker_failure_threshold: int = 3
    breaker_reset_s: float = 20.0

    # Answer cache
    answer_cache_ttl_s: float = 120.0
    answer_cache_size: int = 2000

    # Observability
    log_level: str = "INFO"
    # LLM cost for the observability dashboard, in USD per million tokens. Local Ollama has no per-token charge, so 0;
    # set these to estimate what a hosted model would cost for the same traffic.
    llm_price_in_per_mtok: float = Field(default=0.0, ge=0, le=1000)
    llm_price_out_per_mtok: float = Field(default=0.0, ge=0, le=1000)
    otlp_endpoint: str | None = None  # e.g. http://127.0.0.1:4318/v1/traces to ship spans to Jaeger/Tempo

    @property
    def sqlite_path(self) -> Path:
        return self.var_dir / "cricket.sqlite3"

    @property
    def chroma_path(self) -> Path:
        return self.var_dir / "chroma"


@lru_cache
def get_settings() -> Settings:
    return Settings()
