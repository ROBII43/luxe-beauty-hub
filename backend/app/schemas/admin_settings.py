from pydantic import AnyHttpUrl, BaseModel, Field


class MfaRequestResponse(BaseModel):
    message: str
    expires_in_seconds: int


class MfaVerifyRequest(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class MfaVerifyResponse(BaseModel):
    mfa_token: str
    expires_in_seconds: int


class AdminSettingsResponse(BaseModel):
    public_base_url: AnyHttpUrl
    app_env: str
    mpesa_environment: str
    mpesa_shortcode: str | None
    mpesa_consumer_key_configured: bool
    mpesa_consumer_secret_configured: bool
    mpesa_passkey_configured: bool
    mpesa_callback_url: AnyHttpUrl | None
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_configured: bool


class AdminSettingsUpdate(BaseModel):
    public_base_url: AnyHttpUrl
    app_env: str = Field(pattern="^(staging|production)$")
    mpesa_environment: str = Field(pattern="^(sandbox|production)$")
    mpesa_shortcode: str = Field(min_length=3, max_length=20)
    mpesa_consumer_key: str = Field(min_length=8, max_length=255)
    mpesa_consumer_secret: str = Field(min_length=8, max_length=255)
    mpesa_passkey: str = Field(min_length=8, max_length=255)
    mpesa_callback_url: AnyHttpUrl


class SmtpSettingsUpdate(BaseModel):
    smtp_host: str = Field(min_length=3, max_length=255)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = Field(min_length=3, max_length=255)
    smtp_password: str = Field(min_length=1, max_length=512)
    smtp_from: str = Field(min_length=3, max_length=255)