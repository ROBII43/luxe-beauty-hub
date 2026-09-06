from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Luxe Beauty Hub API"
    app_env: str = "development"
    secret_key: str = "change-me"
    admin_email: str = Field(default="admin@luxe.local", validation_alias="LUXE_ADMIN_EMAIL")
    admin_password: str | None = Field(default=None, validation_alias="LUXE_ADMIN_PASSWORD")
    database_url: str | None = Field(default=None, validation_alias="LUXE_DB_URL")
    mysql_host: str = Field(default="127.0.0.1", validation_alias="MYSQL_HOST")
    mysql_port: int = Field(default=3306, validation_alias="MYSQL_PORT")
    mysql_database: str = Field(default="luxe", validation_alias=AliasChoices("LUXE_MYSQL_DATABASE", "MYSQL_DATABASE"))
    mysql_user: str = Field(default="root", validation_alias="MYSQL_USER")
    mysql_password: str = Field(default="", validation_alias="MYSQL_PASSWORD")
    cors_origins: str = "http://localhost:5173,http://localhost:8000"
    public_base_url: str = "http://localhost:8000"
    mpesa_environment: str = "sandbox"
    mpesa_shortcode: str | None = None
    mpesa_consumer_key: str | None = None
    mpesa_consumer_secret: str | None = None
    mpesa_passkey: str | None = None
    mpesa_callback_url: str | None = None
    settings_encryption_key: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def __init__(self, **values):
        super().__init__(**values)
        if self.app_env == "production":
            if self.secret_key == "change-me":
                raise ValueError("SECRET_KEY must be configured in production")
            if not self.mysql_password and not self.database_url:
                raise ValueError("MYSQL_PASSWORD or LUXE_DB_URL must be configured in production")
            if self.mysql_user == "root":
                raise ValueError("MYSQL_USER must be a dedicated application user in production")
            if not self.settings_encryption_key:
                raise ValueError("SETTINGS_ENCRYPTION_KEY must be configured in production")
            if self.mpesa_environment not in {"sandbox", "production"}:
                raise ValueError("MPESA_ENVIRONMENT must be sandbox or production")
            if not all((self.mpesa_shortcode, self.mpesa_consumer_key, self.mpesa_consumer_secret, self.mpesa_passkey, self.mpesa_callback_url)):
                raise ValueError("M-Pesa credentials and callback URL must be configured in production")
            if not self.mpesa_callback_url.startswith("https://"):
                raise ValueError("MPESA_CALLBACK_URL must use HTTPS in production")
        if "LUXE_MYSQL_DATABASE" not in __import__("os").environ:
            dotenv = Path(__file__).resolve().parents[3] / ".env"
            if dotenv.exists():
                for line in dotenv.read_text(encoding="utf-8").splitlines():
                    if line.startswith("LUXE_MYSQL_DATABASE="):
                        object.__setattr__(self, "mysql_database", line.split("=", 1)[1].strip())
                        break

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def resolved_database_url(self) -> str:
        return self.database_url or f"mysql+mysqlconnector://{self.mysql_user}:{self.mysql_password}@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
