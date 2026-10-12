"""
Application configuration loaded from environment variables.
"""
import os
from functools import lru_cache


class Settings:
    """Centralized app settings."""

    # App
    APP_NAME: str = "Marine Intelligence"
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    API_V1_PREFIX: str = "/api/v1"

    # CORS
    ALLOWED_ORIGINS: list[str] = os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173,http://127.0.0.1:3000",
    ).split(",")

    # Security / Auth
    API_SECRET_KEY: str | None = os.getenv("API_SECRET_KEY")
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "change-me-in-production-super-secret-key")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "14400"))
    ALLOW_REGISTRATION: bool = os.getenv("ALLOW_REGISTRATION", "true").lower() == "true"

    # News Intelligence Database (Time_Series)
    NEWS_DB_HOST: str = os.getenv("DB_HOST", "your-db-host.example.com")
    NEWS_DB_PORT: int = int(os.getenv("DB_PORT", "3306"))
    NEWS_DB_USER: str = os.getenv("DB_USER", "your-db-user")
    NEWS_DB_PASSWORD: str = os.getenv("DB_PASSWORD", "your-db-password")
    NEWS_DB_NAME: str = os.getenv("DB_NAME", "Time_Series")
    NEWS_TABLE: str = os.getenv("NEWS_TABLE", "ship_news")
    VERIFIED_NEWS_TABLE: str = os.getenv("VERIFIED_NEWS_TABLE", "verified_news")

    @property
    def NEWS_DB_CONFIG(self) -> dict:
        return {
            "host": self.NEWS_DB_HOST,
            "port": self.NEWS_DB_PORT,
            "user": self.NEWS_DB_USER,
            "password": self.NEWS_DB_PASSWORD,
            "database": self.NEWS_DB_NAME,
            "charset": "utf8mb4",
            "connect_timeout": 30,
            "use_pure": True,
        }


@lru_cache()
def get_settings() -> Settings:
    return Settings()
