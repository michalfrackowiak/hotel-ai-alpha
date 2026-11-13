"""Persistent cache backing for DRF throttling."""

from __future__ import annotations

import pytest
from django.conf import settings
from django.core.cache import cache, caches
from django.core.management import call_command


def test_production_cache_backend_is_database_backed():
    from config.settings import base

    assert base.CACHES["default"]["BACKEND"].endswith("DatabaseCache")
    assert base.CACHES["default"]["LOCATION"] == "django_cache"


def test_throttle_rates_are_configured():
    assert settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["anon"]
    assert settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["chat"]


@pytest.mark.django_db
def test_database_cache_survives_a_handler_restart(settings):
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "test_throttle_cache",
        }
    }
    call_command("createcachetable", verbosity=0)

    cache.set("throttle:127.0.0.1", 7, timeout=60)
    caches.close_all()

    assert cache.get("throttle:127.0.0.1") == 7


@pytest.mark.django_db
def test_createcachetable_is_idempotent(settings):
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "test_throttle_cache",
        }
    }

    call_command("createcachetable", verbosity=0)
    call_command("createcachetable", verbosity=0)
