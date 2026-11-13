"""Shared settings for the Hotel AI alpha."""

from __future__ import annotations

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
    "apps.hotel",
    "apps.api",
    "apps.agents",
    "apps.voice",
    "apps.calendar_sync",
    "apps.web",
]

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

LANGUAGE_CODE = "en"
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
    "tagline": os.getenv("HOTEL_TAGLINE", "Your AI-powered stay in the heart of the city"),
    "public_url": os.getenv("HOTEL_PUBLIC_URL", "http://localhost:8000").rstrip("/"),
    "currency": os.getenv("HOTEL_CURRENCY", "PLN"),
    "tax_rate": Decimal(os.getenv("HOTEL_TAX_RATE", "0.08")),
    "hold_minutes": int(os.getenv("BOOKING_HOLD_MINUTES", "30")),
    "default_language": os.getenv("HOTEL_DEFAULT_LANGUAGE", "en"),
    "supported_languages": ["en", "pl"],
    "check_in_time": "15:00",
    "check_out_time": "11:00",
    "max_booking_nights": int(os.getenv("BOOKING_MAX_NIGHTS", "30")),
    "advance_booking_days": int(os.getenv("BOOKING_ADVANCE_DAYS", "365")),
    "cancellation_free_days": int(os.getenv("CANCELLATION_FREE_DAYS", "3")),
    "cancellation_fee_nights": int(os.getenv("CANCELLATION_FEE_NIGHTS", "1")),
    "contact_email": os.getenv("HOTEL_CONTACT_EMAIL", "reception@example.com"),
    "contact_phone": os.getenv("HOTEL_CONTACT_PHONE", "+48 22 000 00 00"),
    "address": os.getenv("HOTEL_ADDRESS", "ul. Przykładowa 1, 00-001 Warszawa"),
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

# Signed booking management links. Guests can always request a fresh link with
# their reference and contact details, so expiry is a safety net, not a trap.
BOOKING_ACCESS_MAX_AGE_DAYS = int(os.getenv("BOOKING_ACCESS_MAX_AGE_DAYS", "180"))

# Encryption material for provider API keys at rest. When unset, a key is
# derived from DJANGO_SECRET_KEY; set a dedicated value in production.
FIELD_ENCRYPTION_KEY = os.getenv("FIELD_ENCRYPTION_KEY", "")
FIELD_ENCRYPTION_KEY_OLD = os.getenv("FIELD_ENCRYPTION_KEY_OLD", "")

GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
GOOGLE_CALENDAR_CREDENTIALS_FILE = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_FILE", "")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")

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
