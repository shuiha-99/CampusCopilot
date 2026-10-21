from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_provider: Literal["mock", "deepseek", "ollama", "router"] = "mock"
    manual_operator_token: SecretStr | None = None
    manual_operator_id: str = "local-operator"

    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"

    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    ollama_model: str = "qwen2.5:7b"
    embedding_provider: Literal["hash", "ollama"] = "hash"
    ollama_embedding_model: str = "bge-m3"
    circuit_failure_threshold: int = Field(default=3, ge=1, le=20)
    circuit_reset_seconds: float = Field(default=30.0, gt=0, le=600)

    model_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=120,
    )
    ollama_generation_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        le=300,
    )
    # 数据库配置
    database_host: str = "127.0.0.1"
    database_port: int = Field(
        default=3306,
        ge=1,
        le=65535,
    )
    database_user: str = "campus_app"
    database_password: SecretStr = Field(
        min_length=1,
    )
    database_name: str = "campus_copilot"

    # Redis 连接配置
    redis_host: str = "127.0.0.1"
    redis_port: int = Field(
        default=6380,
        ge=1,
        le=65535,
    )
    redis_db: int = Field(
        default=0,
        ge=0,
    )
    redis_connect_timeout_seconds: float = Field(
        default=1.0,
        gt=0,
        le=10,
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 判断模型是不是deepseek模型
    @model_validator(mode="after")
    def require_deepseek_api_key(self) -> Self:
        if self.model_provider in {"deepseek", "router"}:
            api_key = self.deepseek_api_key

            if api_key is None or not api_key.get_secret_value().strip():
                raise ValueError("使用 DeepSeek 时必须配置 DEEPSEEK_API_KEY")

        return self


settings = Settings()
