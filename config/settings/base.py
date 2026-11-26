"""Shared settings for the Hotel AI alpha."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "django_openline",
    "apps.hotel",
    "apps.api",
    "apps.agents",
    "apps.voice",
    "apps.calendar_sync",
    "apps.web",
]

try:
    import daphne  # noqa: F401

    INSTALLED_APPS.insert(0, "daphne")
except Exception:
    pass

try:
    import channels  # noqa: F401

    # Insert channels after rest_framework/openline area; position is not critical.
    if "channels" not in INSTALLED_APPS:
        INSTALLED_APPS.insert(INSTALLED_APPS.index("rest_framework") + 1, "channels")
except Exception:
    pass

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.web.currency.CurrencyMiddleware",
]
ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.web.context_processors.site_context",
                "apps.web.context_processors.display_currency",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "db.sqlite3")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DB_PATH,
        "OPTIONS": {"timeout": 20},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "pl"
LANGUAGES = [
    ("en", "English"),
    ("pl", "Polski"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "Europe/Warsaw"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if not DEBUG
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
    },
}
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Persistent, database-backed cache. DRF throttling uses the default cache, so
# rate limits survive process restarts and are shared by every gunicorn worker.
# Run `manage.py createcachetable` once (the Docker entrypoint does it on boot).
CACHE_BACKEND = os.getenv("CACHE_BACKEND", "django.core.cache.backends.db.DatabaseCache")
CACHE_LOCATION = os.getenv("CACHE_LOCATION", "django_cache")
CACHES = {
    "default": {
        "BACKEND": CACHE_BACKEND,
        "LOCATION": CACHE_LOCATION,
    }
}

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": (
        ["rest_framework.renderers.JSONRenderer", "rest_framework.renderers.BrowsableAPIRenderer"]
        if DEBUG
        else ["rest_framework.renderers.JSONRenderer"]
    ),
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.getenv("API_ANON_RATE", "240/hour"),
        "user": os.getenv("API_USER_RATE", "2000/hour"),
        "chat": os.getenv("API_CHAT_RATE", "120/hour"),
    },
    "EXCEPTION_HANDLER": "apps.api.exceptions.api_exception_handler",
    "DEFAULT_METADATA_CLASS": "rest_framework.metadata.SimpleMetadata",
}

HOTEL = {
    "name": os.getenv("HOTEL_NAME", "Hotel Aurora"),
    "tagline": os.getenv("HOTEL_TAGLINE", "Hotel Aurora · Jastrzębia Góra"),
    "public_url": os.getenv("HOTEL_PUBLIC_URL", "http://localhost:8000").rstrip("/"),
    "currency": os.getenv("HOTEL_CURRENCY", "PLN"),
    "tax_rate": Decimal(os.getenv("HOTEL_TAX_RATE", "0.08")),
    "hold_minutes": int(os.getenv("BOOKING_HOLD_MINUTES", "30")),
    "default_language": os.getenv("HOTEL_DEFAULT_LANGUAGE", "pl"),
    "supported_languages": ["en", "pl"],
    "check_in_time": "15:00",
    "check_out_time": "11:00",
    "max_booking_nights": int(os.getenv("BOOKING_MAX_NIGHTS", "30")),
    "advance_booking_days": int(os.getenv("BOOKING_ADVANCE_DAYS", "365")),
    "cancellation_free_days": int(os.getenv("CANCELLATION_FREE_DAYS", "3")),
    "cancellation_fee_nights": int(os.getenv("CANCELLATION_FEE_NIGHTS", "1")),
    "deposit_policy": os.getenv("DEPOSIT_POLICY", "none").strip().lower(),
    "deposit_value": Decimal(os.getenv("DEPOSIT_VALUE", "0")),
    "contact_email": os.getenv("HOTEL_CONTACT_EMAIL", "reception@hotel-aurora.example"),
    "contact_phone": os.getenv("HOTEL_CONTACT_PHONE", "+48 58 000 00 00"),
    "address": os.getenv("HOTEL_ADDRESS", "ul. Morska 1, 84-104 Jastrzębia Góra"),
    "supported_display_currencies": [
        code.strip().upper()
        for code in os.getenv("SUPPORTED_DISPLAY_CURRENCIES", "PLN,EUR,USD,GBP").split(",")
        if code.strip()
    ],
    "exchange_rates": json.loads(
        os.getenv(
            "EXCHANGE_RATES",
            '{"PLN": 1.0, "EUR": 0.23, "USD": 0.25, "GBP": 0.20}',
        )
    ),
}

AI = {
    "default_timeout": float(os.getenv("AI_TIMEOUT_SECONDS", "45")),
    "max_tool_iterations": int(os.getenv("AI_MAX_TOOL_ITERATIONS", "6")),
    "max_message_length": int(os.getenv("AI_MAX_MESSAGE_LENGTH", "4000")),
    "history_limit": int(os.getenv("AI_HISTORY_LIMIT", "40")),
}

# Booking confirmation email. The console backend is a safe default for local
# demos; production sets EMAIL_BACKEND=smtp and the EMAIL_* variables.
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", HOTEL["contact_email"])
BOOKING_EMAILS_ENABLED = env_bool("BOOKING_EMAILS_ENABLED", True)
EMAIL_WEBHOOK_SECRET = os.getenv("EMAIL_WEBHOOK_SECRET", "")
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "smtp").lower()
SES_ACCESS_KEY_ID = os.getenv("SES_ACCESS_KEY_ID", "")
SES_SECRET_ACCESS_KEY = os.getenv("SES_SECRET_ACCESS_KEY", "")
SES_REGION = os.getenv("SES_REGION", "eu-central-1")
POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN", "")

# Booking confirmation SMS. The console provider logs the message instead of
# sending it, so the scenario works offline; set SMS_PROVIDER=twilio and the
# Twilio credentials for real delivery.
BOOKING_SMS_ENABLED = env_bool("BOOKING_SMS_ENABLED", True)
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "console").lower().strip()
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")

# Retry policy for confirmation emails. Failed sends are retried by the
# scheduler with exponential backoff until they succeed or hit the cap.
EMAIL_MAX_ATTEMPTS = int(os.getenv("EMAIL_MAX_ATTEMPTS", "5"))
EMAIL_RETRY_BASE_SECONDS = int(os.getenv("EMAIL_RETRY_BASE_SECONDS", "300"))
EMAIL_RETRY_MAX_SECONDS = int(os.getenv("EMAIL_RETRY_MAX_SECONDS", "21600"))

# Confirmation SMS retry policy (mirrors email backoff).
SMS_MAX_ATTEMPTS = int(os.getenv("SMS_MAX_ATTEMPTS", "5"))
SMS_RETRY_BASE_SECONDS = int(os.getenv("SMS_RETRY_BASE_SECONDS", "300"))
SMS_RETRY_MAX_SECONDS = int(os.getenv("SMS_RETRY_MAX_SECONDS", "21600"))

# Signed booking management links. Guests can always request a fresh link with
# their reference and contact details, so expiry is a safety net, not a trap.
BOOKING_ACCESS_MAX_AGE_DAYS = int(os.getenv("BOOKING_ACCESS_MAX_AGE_DAYS", "180"))

# Guest account access. Magic-link tokens are single use and short lived; email
# verification tokens confirm new registrations and verified email changes. The
# cooldowns/throttles limit inbox flooding and brute-force attempts.
GUEST_LOGIN_MAX_AGE_MINUTES = int(os.getenv("GUEST_LOGIN_MAX_AGE_MINUTES", "30"))
GUEST_LOGIN_COOLDOWN_SECONDS = int(os.getenv("GUEST_LOGIN_COOLDOWN_SECONDS", "300"))
GUEST_LOGIN_IP_COOLDOWN_SECONDS = int(os.getenv("GUEST_LOGIN_IP_COOLDOWN_SECONDS", "60"))
GUEST_EMAIL_VERIFICATION_MAX_AGE_MINUTES = int(
    os.getenv("GUEST_EMAIL_VERIFICATION_MAX_AGE_MINUTES", "1440")
)
GUEST_EMAIL_VERIFICATION_COOLDOWN_SECONDS = int(
    os.getenv("GUEST_EMAIL_VERIFICATION_COOLDOWN_SECONDS", "300")
)
GUEST_PASSWORD_LOGIN_WINDOW_SECONDS = int(os.getenv("GUEST_PASSWORD_LOGIN_WINDOW_SECONDS", "900"))
GUEST_PASSWORD_LOGIN_MAX_ATTEMPTS_PER_EMAIL = int(
    os.getenv("GUEST_PASSWORD_LOGIN_MAX_ATTEMPTS_PER_EMAIL", "10")
)
GUEST_PASSWORD_LOGIN_MAX_ATTEMPTS_PER_IP = int(
    os.getenv("GUEST_PASSWORD_LOGIN_MAX_ATTEMPTS_PER_IP", "20")
)
GUEST_BOOKING_LOOKUP_WINDOW_SECONDS = int(os.getenv("GUEST_BOOKING_LOOKUP_WINDOW_SECONDS", "300"))
GUEST_BOOKING_LOOKUP_MAX_ATTEMPTS_PER_IP = int(
    os.getenv("GUEST_BOOKING_LOOKUP_MAX_ATTEMPTS_PER_IP", "15")
)

# Encryption material for provider API keys at rest. When unset, a key is
# derived from DJANGO_SECRET_KEY; set a dedicated value in production.
FIELD_ENCRYPTION_KEY = os.getenv("FIELD_ENCRYPTION_KEY", "")
FIELD_ENCRYPTION_KEY_OLD = os.getenv("FIELD_ENCRYPTION_KEY_OLD", "")

GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
GOOGLE_CALENDAR_CREDENTIALS_FILE = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_FILE", "")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")

# Guest "Sign in with Google" (OAuth 2.0 / OpenID Connect).
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
GOOGLE_OAUTH_SCOPES = ["openid", "email", "profile"]

# Channel-manager iCal imports: fail fast so a slow OTA cannot stall the scheduler.
CHANNEL_FEED_TIMEOUT_SECONDS = float(os.getenv("CHANNEL_FEED_TIMEOUT_SECONDS", "10"))

# OpenLine self-hosted voice integration (optional). BASE_URL has no /v1 suffix.
OPENLINE = {
    "BASE_URL": os.getenv("OPENLINE_URL", "").strip().rstrip("/"),
    "API_KEY": os.getenv("OPENLINE_API_KEY", "").strip(),
    "TIMEOUT": int(os.getenv("OPENLINE_TIMEOUT", "15")),
    "VERIFY_TLS": env_bool("OPENLINE_VERIFY_TLS", True),
    "WEBHOOK_SECRET": os.getenv("OPENLINE_WEBHOOK_SECRET", "").strip(),
    "ALLOW_UNSIGNED_WEBHOOKS": env_bool("OPENLINE_ALLOW_UNSIGNED_WEBHOOKS", False),
    "DEFAULT_DEVICE_ID": os.getenv("OPENLINE_DEFAULT_DEVICE_ID", "").strip() or None,
    "DEFAULT_STREAM_URL": os.getenv("OPENLINE_DEFAULT_STREAM_URL", "").strip() or None,
    "DEFAULT_STREAM_FORMAT": os.getenv("OPENLINE_DEFAULT_STREAM_FORMAT", "twilio").strip() or "twilio",
}

# OpenLine is optional. Silence its "not configured" checks only while none of
# its settings are supplied, so a partial configuration still surfaces warnings.
if not any((OPENLINE["BASE_URL"], OPENLINE["API_KEY"], OPENLINE["WEBHOOK_SECRET"])):
    SILENCED_SYSTEM_CHECKS = ["openline.W003", "openline.W004"]

# Phone voice integration (OpenLine media bridge). Disabled by default; the
# feature flag keeps existing chat/browser voice behavior unchanged.
VOICE_PHONE_ENABLED = env_bool("VOICE_PHONE_ENABLED", False)
VOICE_PUBLIC_WSS_URL = os.getenv("VOICE_PUBLIC_WSS_URL", "").strip()
OPENLINE_INCOMING_STREAM_MODE = (
    os.getenv("OPENLINE_INCOMING_STREAM_MODE", "webhook_attach").strip() or "webhook_attach"
)
VOICE_STT_PROVIDER = os.getenv("VOICE_STT_PROVIDER", "openai").strip() or "openai"
VOICE_TTS_PROVIDER = os.getenv("VOICE_TTS_PROVIDER", "openai").strip() or "openai"
VOICE_STT_MODEL = os.getenv("VOICE_STT_MODEL", "").strip()
VOICE_TTS_MODEL = os.getenv("VOICE_TTS_MODEL", "").strip()
VOICE_TTS_VOICE = os.getenv("VOICE_TTS_VOICE", "").strip()
VOICE_MAX_CONCURRENT_CALLS = int(os.getenv("VOICE_MAX_CONCURRENT_CALLS", "2"))
VOICE_MAX_MINUTES_PER_DAY = int(os.getenv("VOICE_MAX_MINUTES_PER_DAY", "120"))
VOICE_MAX_CALLS_PER_NUMBER_PER_HOUR = int(os.getenv("VOICE_MAX_CALLS_PER_NUMBER_PER_HOUR", "6"))
VOICE_PHONE_SILENCE_TIMEOUT_SECONDS = int(os.getenv("VOICE_PHONE_SILENCE_TIMEOUT_SECONDS", "20"))
VOICE_PHONE_MAX_SECONDS = int(os.getenv("VOICE_PHONE_MAX_SECONDS", "900"))
VOICE_ESTIMATED_COST_PER_MINUTE_USD = float(os.getenv("VOICE_ESTIMATED_COST_PER_MINUTE_USD", "0"))
VOICE_PHONE_RETENTION_DAYS = int(os.getenv("VOICE_PHONE_RETENTION_DAYS", "30"))

REDIS_URL = os.getenv("REDIS_URL", "").strip()
if REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [REDIS_URL]},
        }
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "apps": {"handlers": ["console"], "level": os.getenv("APP_LOG_LEVEL", "INFO"), "propagate": False},
    },
}

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"
