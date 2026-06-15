"""
Application configuration.

All environment variables should be loaded here.

Do NOT call os.getenv() throughout the codebase.
Import settings instead.

Example:
    from services.api.core.config import settings

    database_url = settings.DATABASE_URL
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


# Centralized application configuration loaded from environment variables. Converts .env into python objects
class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.
    """

    # ==========================================================
    # Application
    # ==========================================================

    APP_NAME: str = "Innovision API"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"

    # ==========================================================
    # Database
    # ==========================================================
    # Reserved for future database integration.

    DATABASE_URL: str = ""

    # ==========================================================
    # MinIO Storage
    # ==========================================================
    # Used by shared/storage/storage_minio_client.py

    MINIO_ENDPOINT: str = ""
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_SECURE: bool = False

    # ==========================================================
    # Authentication
    # ==========================================================
    # Used when JWT authentication is implemented.

    JWT_SECRET_KEY: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60

    # ==========================================================
    # Logging
    # ==========================================================

    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()