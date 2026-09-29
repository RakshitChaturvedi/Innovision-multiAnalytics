"""Base for per-service settings: environment variables first, then a `.env`
file in the working directory, then the in-code defaults (same approach as
the detection service). Unrelated keys are ignored."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )
