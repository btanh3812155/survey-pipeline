import socket
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

from core.passwords import is_supported_password_hash, MIN_PRODUCTION_ITERATIONS

VALID_ENVIRONMENTS = {"production", "development", "test"}
BUILTIN_ADMIN_PASSWORD = "12345678"
BUILTIN_APP_SECRET = "survey_secret_salt_pepper_survey_pipeline_2026"
BUILTIN_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/surveydb"
BUILTIN_TELEGRAM_WEBHOOK_SECRET = "mock_webhook_secret"


class Settings(BaseSettings):
    # environment: no implicit development fallback
    ENVIRONMENT: str
    DEBUG: bool = False

    # server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    # admin credentials
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "12345678"
    ADMIN_PASSWORD_HASH: Optional[str] = None
    ADMIN_RECOVERY_EMAIL: str = "admin@trustbridgevn.com"
    # Admin recovery uses authenticated SMTP over certificate-validated TLS only.
    SMTP_HOST: str = ""
    SMTP_PORT: str = "587"
    SMTP_TLS_MODE: str = "starttls"
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: Optional[SecretStr] = None
    SMTP_FROM_EMAIL: str = "admin@trustbridgevn.com"
    # security pepper / secret
    APP_SECRET: str = "survey_secret_salt_pepper_survey_pipeline_2026"

    # database & redis
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/surveydb"
    REDIS_URL: str = "redis://localhost:6379/0"
    # Claims and each producer cohort require explicit activation; defaults stay inactive.
    OUTBOX_V2_WORKER_ENABLED: bool = False
    OUTBOX_WORKER_ID: str = socket.gethostname()
    APP_BUILD_ID: str = "local"
    DURABLE_WEBRTC_ENABLED: bool = False
    DURABLE_LARK_ENABLED: bool = False
    DURABLE_MODERATION_ENABLED: bool = False
    DURABLE_DRIVE_ENABLED: bool = False
    DURABLE_APPEALS_ENABLED: bool = False
    BILLING_PAYOUT_VND: Optional[int] = None
    BILLING_POLICY_VERSION: str = "configured-v1"

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
    R2_PRIVATE_BUCKET_NAME: Optional[str] = None
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
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    @model_validator(mode="after")
    def _validate_environment_and_production_settings(self):
        environment = str(self.ENVIRONMENT or "").strip().lower()
        if environment not in VALID_ENVIRONMENTS:
            raise ValueError(
                f"ENVIRONMENT must be one of {sorted(VALID_ENVIRONMENTS)}, got {self.ENVIRONMENT!r}"
            )
        self.ENVIRONMENT = environment

        if self.ADMIN_PASSWORD_HASH:
            minimum = MIN_PRODUCTION_ITERATIONS if environment == "production" else 1
            if not is_supported_password_hash(self.ADMIN_PASSWORD_HASH, min_iterations=minimum):
                raise ValueError("ADMIN_PASSWORD_HASH is malformed or unsupported")
        if environment == "production":
            if not self.ADMIN_PASSWORD_HASH and (
                not self.ADMIN_PASSWORD or self.ADMIN_PASSWORD == BUILTIN_ADMIN_PASSWORD
            ):
                raise ValueError(
                    "production requires ADMIN_PASSWORD_HASH or a non-default ADMIN_PASSWORD"
                )
            if (
                not self.APP_SECRET
                or self.APP_SECRET == BUILTIN_APP_SECRET
                or len(self.APP_SECRET) < 32
            ):
                raise ValueError("production APP_SECRET must be a random value of at least 32 characters")
            database_url = str(self.DATABASE_URL or "").strip()
            if not database_url.startswith(("postgresql://", "postgres://")):
                raise ValueError("production DATABASE_URL must be a PostgreSQL URL")
            if database_url == BUILTIN_DATABASE_URL:
                raise ValueError("production DATABASE_URL must not be the built-in default")
        return self


settings = Settings()
