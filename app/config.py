import os
from functools import lru_cache
from typing import Optional

from pydantic import BaseModel, Field


class Settings(BaseModel):
    secret_key: str = Field(default_factory=lambda: os.getenv("SECRET_KEY", "change-me-in-env"))
    access_token_expire_minutes: int = 60 * 24
    algorithm: str = "HS256"
    database_url: str = Field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./app.db"))
    environment: str = Field(default_factory=lambda: os.getenv("ENVIRONMENT", "local"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
