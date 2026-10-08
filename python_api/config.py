from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    database_url: str = ""
    session_secret: str = ""
    issuer_url: str = "https://replit.com/oidc"

    llm_provider: str = "openai-compatible"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    llm_max_output_tokens: int = Field(default=1200, ge=1, le=8192)

    embedding_provider: str = "openai-compatible"
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_api_key: str = ""
    embedding_dimensions: int = 1536
    embedding_timeout_seconds: float = Field(default=45.0, gt=0, le=300)

    upload_max_bytes: int = 20 * 1024 * 1024
    chunk_size_tokens: int = 500
    chunk_overlap_tokens: int = 80
    retrieval_top_k_default: int = 5
    retrieval_min_score: float = 0.2
    database_pool_size: int = 5

    rag_internal_storage_base_url: str = ""
    rag_worker_url: str = ""
    rag_worker_port: int = 18082

    @model_validator(mode="after")
    def validate_runtime_limits(self) -> "Settings":
        if self.embedding_dimensions != 1536:
            raise ValueError("EMBEDDING_DIMENSIONS must match the provisioned vector(1536) schema.")
        if self.chunk_size_tokens < 32:
            raise ValueError("CHUNK_SIZE_TOKENS must be at least 32.")
        if not 0 <= self.chunk_overlap_tokens < self.chunk_size_tokens:
            raise ValueError("CHUNK_OVERLAP_TOKENS must be non-negative and less than CHUNK_SIZE_TOKENS.")
        if self.upload_max_bytes < 1024:
            raise ValueError("UPLOAD_MAX_BYTES must be at least 1024.")
        return self

    @property
    def llm_configured(self) -> bool:
        return bool(
            self.llm_provider == "openai-compatible"
            and self.llm_base_url
            and self.llm_model
        )

    @property
    def embeddings_configured(self) -> bool:
        return bool(
            self.embedding_provider == "openai-compatible"
            and self.embedding_base_url
            and self.embedding_model
            and self.embedding_dimensions == 1536
        )

    @staticmethod
    def _endpoint(base_url: str, endpoint: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/{endpoint}"
        return f"{base}/v1/{endpoint}"

    @property
    def embeddings_url(self) -> str:
        return self._endpoint(self.embedding_base_url, "embeddings")

    @property
    def chat_completions_url(self) -> str:
        return self._endpoint(self.llm_base_url, "chat/completions")


@lru_cache
def get_settings() -> Settings:
    return Settings()
