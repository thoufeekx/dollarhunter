from decimal import Decimal
from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    app_name: str = Field("Dollar Hunter", validation_alias="DH_APP_NAME")
    env: str = Field("development", validation_alias="DH_ENV")
    database_url: str = Field("postgresql+psycopg://dollarhunter:dollarhunter@db:5432/dollarhunter", validation_alias="DH_DATABASE_URL")
    public_base_url: str = Field("http://localhost:8000", validation_alias="DH_PUBLIC_BASE_URL")
    timezone: str = Field("America/Toronto", validation_alias="DH_TIMEZONE")
    currency: str = Field("CAD", validation_alias="DH_CURRENCY")
    demo_mode: bool = Field(True, validation_alias="DH_DEMO_MODE")
    sms_provider: str = Field("console", validation_alias="DH_SMS_PROVIDER")
    ai_provider: str = Field("mock", validation_alias="DH_AI_PROVIDER")
    price_source: str = Field("fixtures", validation_alias="DH_PRICE_SOURCE")
    storage_backend: str = Field("local", validation_alias="DH_STORAGE_BACKEND")
    storage_dir: str = Field("/data/private_uploads", validation_alias="DH_STORAGE_DIR")
    file_retention_days: int = Field(7, validation_alias="DH_FILE_RETENTION_DAYS")
    link_ttl_minutes: int = Field(30, validation_alias="DH_LINK_TTL_MINUTES")
    fee_rate: Decimal = Field(Decimal("0.25"), validation_alias="DH_FEE_RATE")
    default_budget_cad: Decimal | None = Field(None, validation_alias="DH_DEFAULT_BUDGET_CAD")  # no cap unless set
    quote_ttl_hours: int = Field(24, validation_alias="DH_QUOTE_TTL_HOURS")
    daily_sms_limit: int = Field(40, validation_alias="DH_DAILY_SMS_LIMIT")
    daily_receipt_limit: int = Field(10, validation_alias="DH_DAILY_RECEIPT_LIMIT")
    max_upload_bytes: int = Field(10_000_000, validation_alias="DH_MAX_UPLOAD_BYTES")
    payments_enabled: bool = Field(False, validation_alias="DH_PAYMENTS_ENABLED")
    real_orders_enabled: bool = Field(False, validation_alias="DH_REAL_ORDERS_ENABLED")
    admin_token: str = Field("", validation_alias="DH_ADMIN_TOKEN")

    twilio_account_sid: str = Field("", validation_alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = Field("", validation_alias="TWILIO_AUTH_TOKEN")
    twilio_from_number: str = Field("", validation_alias="TWILIO_FROM_NUMBER")
    twilio_api_key_sid: str = Field("", validation_alias="TWILIO_API_KEY_SID")
    twilio_api_key_secret: str = Field("", validation_alias="TWILIO_API_KEY_SECRET")

    ai_base_url: str = Field("https://openrouter.ai/api/v1", validation_alias="DH_AI_BASE_URL")
    ai_api_key: str = Field("", validation_alias="DH_AI_API_KEY")
    receipt_model: str = Field("", validation_alias="DH_RECEIPT_MODEL")
    transcription_model: str = Field("", validation_alias="DH_TRANSCRIPTION_MODEL")
    agent_model: str = Field("", validation_alias="DH_AGENT_MODEL")
    agent_backup_model: str = Field("openai/gpt-6-luna", validation_alias="DH_AGENT_BACKUP_MODEL")
    match_model: str = Field("typesafe/jev-router", validation_alias="DH_MATCH_MODEL")
    decision_model: str = Field("~typesafe/jev-latest", validation_alias="DH_DECISION_MODEL")
    firecrawl_api_key: str = Field("", validation_alias="FIRECRAWL_API_KEY")
    daily_firecrawl_pages: int = Field(30, validation_alias="DH_DAILY_FIRECRAWL_PAGES")
    daily_agent_turns: int = Field(60, validation_alias="DH_DAILY_AGENT_TURNS")

    r2_account_id: str = Field("", validation_alias="R2_ACCOUNT_ID")
    r2_access_key_id: str = Field("", validation_alias="R2_ACCESS_KEY_ID")
    r2_secret_access_key: str = Field("", validation_alias="R2_SECRET_ACCESS_KEY")
    r2_bucket: str = Field("dollarhunter-uploads", validation_alias="R2_BUCKET")

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)

    @property
    def effective_sms_provider(self) -> str:
        return "twilio" if self.sms_provider == "twilio" and self.twilio_configured else "console"

    @property
    def effective_ai_provider(self) -> str:
        return "openrouter" if self.ai_provider == "openrouter" and self.ai_api_key and self.receipt_model else "mock"

    def validate_startup(self) -> list[str]:
        if self.payments_enabled or self.real_orders_enabled:
            raise RuntimeError("DH_PAYMENTS_ENABLED and DH_REAL_ORDERS_ENABLED must be false in this build")
        warnings = []
        if self.sms_provider == "twilio" and not self.twilio_configured:
            warnings.append("Twilio credentials missing: using console SMS (simulated)")
        if self.ai_provider == "openrouter" and self.effective_ai_provider == "mock":
            warnings.append("AI credentials missing: using labelled mock extraction")
        if self.env == "production":
            if len(self.admin_token) < 16 or self.admin_token.startswith("dev-"):
                raise RuntimeError("Production requires a strong non-default DH_ADMIN_TOKEN")
            if not self.public_base_url.startswith("https://"):
                raise RuntimeError("Production requires an https DH_PUBLIC_BASE_URL")
        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()
