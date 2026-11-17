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
    ADMIN_PASSWORD: str = "12345678"
    # security pepper / secret
    APP_SECRET: str = "survey_secret_salt_pepper_survey_pipeline_2026"

    # database & redis
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/surveydb"
    REDIS_URL: str = "redis://localhost:6379/0"

    # bytedance lark / feishu
    LARK_APP_ID: str = "cli_mock_id"
    LARK_APP_SECRET: str = "mock_secret"
    LARK_ENCRYPT_KEY: str = "mock_encrypt_key"
    LARK_VERIFICATION_TOKEN: str = "mock_token"
    LARK_BASE_APP_TOKEN: str = "UwVabU53laZe5OsKgEGjy7QRpOh"
    LARK_BASE_TABLE_ID: str = "tblfHqhN3FCCxrdP"

    # telegram bots (admin & driver dual-bot architecture)
    TELEGRAM_BOT_TOKEN: str = "mock_tg_token"
    TELEGRAM_ADMIN_BOT_TOKEN: Optional[str] = None
    TELEGRAM_DRIVER_BOT_TOKEN: Optional[str] = None
    TELEGRAM_WEBHOOK_SECRET: str = "mock_webhook_secret"
    TELEGRAM_ADMIN_GROUP_ID: str = "-1001234567890"
    TELEGRAM_POLLING_ENABLED: bool = False
    TELEGRAM_BOT_USERNAME: str = "TrustBridgeVNAdmin_Bot"
    TELEGRAM_DRIVER_BOT_USERNAME: str = "TrustBridgeVN_bot"
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

    # google drive / workspace delivery
    GOOGLE_ADMIN_EMAIL: str = "admin@trustbridgevn.com"
    GOOGLE_DRIVE_ROOT_FOLDER_ID: Optional[str] = "1zWaDQP0cJxt-dGiJPrZIKfCucl6ig4Bp"
    GOOGLE_SERVICE_ACCOUNT_JSON: Optional[str] = None
    GOOGLE_DRIVE_REFRESH_TOKEN: Optional[str] = None
    GOOGLE_DRIVE_CLIENT_ID: Optional[str] = None
    GOOGLE_DRIVE_CLIENT_SECRET: Optional[str] = None
    GOOGLE_DRIVE_ACCESS_TOKEN: Optional[str] = None
    # tuning thresholds
    BLUR_THRESHOLD: float = 15.0
    IMAGE_MAX_DIMENSION: int = 1024
    WEBP_QUALITY: int = 82
    AI_TIMEOUT_SECONDS: float = 10.0
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
