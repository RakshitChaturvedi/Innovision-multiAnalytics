from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Only what every service needs. Unrelated env (Grafana, JWT, MinIO...)
    is ignored, never required."""

    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    # Database index. The platform uses 0; tests use TEST_REDIS_DB (never 0).
    REDIS_DB: int = 0
    DATABASE_URL: str = (
        "postgresql+asyncpg://analytics:analytics@localhost:5432/innovision_analytics"
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def redis_url(self) -> str:
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"


settings = Settings()
