from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Production RAG API"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    api_key: str = "local-development-key"
    max_upload_bytes: int = Field(default=5_000_000, ge=1_024)
    max_query_length: int = Field(default=2_000, ge=32)
    top_k: int = Field(default=5, ge=1, le=20)
    min_relevance_score: float = Field(default=0.10, ge=0, le=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
