from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    # 60 minutes. Short by design — sliding renewal in main.py keeps an
    # active doctor signed in through a full clinic, while an abandoned
    # session dies within the hour.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # DEPRECATED — Twilio fields kept so existing .env files don't break on startup
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_WHATSAPP_FROM: str = "whatsapp:+14155238886"  # Twilio sandbox default
    TWILIO_SMS_FROM: str = ""  # optional: your Twilio SMS phone number e.g. +918XXXXXXXXX

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""

    ADMIN_EMAIL: str = ""  # platform owner email — set in .env

    # ── Transactional email (Resend) ──────────────────────────────────────
    # Used for email verification codes, password resets, and clinic invites.
    # When RESEND_API_KEY is empty, send_email() logs and no-ops rather than
    # raising — mirrors how send_whatsapp() degrades without Twilio creds.
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "Med Track <onboarding@resend.dev>"  # override once the domain is verified
    EMAIL_REPLY_TO: str = ""

    # Support WhatsApp number in E.164 WITHOUT the leading '+' (wa.me format),
    # e.g. 919812345678. Shown to doctors who can't receive a verification
    # code or whose plan has lapsed. Set this in .env — the fallback below is
    # a placeholder and will not reach anyone.
    SUPPORT_WHATSAPP: str = "919999999999"

    # Public base URL used to build patient-facing links (e.g. the feedback
    # link in the WhatsApp bill receipt). Override in .env for staging/local.
    PUBLIC_BASE_URL: str = "https://www.medtrack.life"

    # Set ENVIRONMENT=development in local .env to allow http:// cookies.
    # In production (Railway) leave unset — defaults to "production" so
    # cookies get the Secure flag and only travel over HTTPS.
    ENVIRONMENT: str = "production"

    class Config:
        env_file = ".env"


settings = Settings()
