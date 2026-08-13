from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    # DEPRECATED — Twilio fields kept so existing .env files don't break on startup
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_WHATSAPP_FROM: str = "whatsapp:+14155238886"  # Twilio sandbox default
    TWILIO_SMS_FROM: str = ""  # optional: your Twilio SMS phone number e.g. +918XXXXXXXXX

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""

    ADMIN_EMAIL: str = ""  # platform owner email — set in .env

    # Public base URL used to build patient-facing links (e.g. the feedback
    # link in the WhatsApp bill receipt). Override in .env for staging/local.
    PUBLIC_BASE_URL: str = "https://www.nivora.store"

    # Set ENVIRONMENT=development in local .env to allow http:// cookies.
    # In production (Railway) leave unset — defaults to "production" so
    # cookies get the Secure flag and only travel over HTTPS.
    ENVIRONMENT: str = "production"

    class Config:
        env_file = ".env"


settings = Settings()
