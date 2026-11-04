from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    # environment
    ENVIRONMENT: str = "development"
    DEBUG: bool = False

    # server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    # admin credentials
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "Legendofzeus2@"

    # database & redis
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/surveydb"
    REDIS_URL: str = "redis://localhost:6379/0"

    # bytedance lark / feishu
    LARK_APP_ID: str = "cli_mock_id"
    LARK_APP_SECRET: str = "mock_secret"
    LARK_ENCRYPT_KEY: str = "mock_encrypt_key"
    LARK_VERIFICATION_TOKEN: str = "mock_token"

    # telegram bot
    TELEGRAM_BOT_TOKEN: str = "mock_tg_token"
    TELEGRAM_WEBHOOK_SECRET: str = "mock_webhook_secret"
    TELEGRAM_ADMIN_GROUP_ID: str = "-1001234567890"
    TELEGRAM_POLLING_ENABLED: bool = False
    # ai models
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    DEEPSEEK_API_KEY: Optional[str] = None
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"

    # cloudflare r2 / s3 storage
    R2_ACCOUNT_ID: Optional[str] = None
    R2_ACCESS_KEY_ID: Optional[str] = None
    R2_SECRET_ACCESS_KEY: Optional[str] = None
    R2_BUCKET_NAME: str = "surveys"
    R2_PUBLIC_URL_PREFIX: str = "https://cdn.example.com"

    # tuning thresholds
    BLUR_THRESHOLD: float = 100.0
    IMAGE_MAX_DIMENSION: int = 1024
    WEBP_QUALITY: int = 82
    AI_TIMEOUT_SECONDS: float = 8.0
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
