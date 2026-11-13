"""Consistent JSON error envelopes for the API."""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class ApiError(APIException):
    """An API error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: dict[str, Any] | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(detail=self.message, code=code)

    def as_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def api_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    data = response.data
    if isinstance(exc, ApiError):
        payload = {"error": exc.as_payload()}
    elif isinstance(data, dict) and "detail" in data and len(data) == 1:
        payload = {
            "error": {
                "code": getattr(exc, "default_code", "error"),
                "message": str(data["detail"]),
                "details": {},
            }
        }
    elif isinstance(data, dict):
        first_message = _first_message(data)
        code = getattr(exc, "default_code", "validation_error")
        if code == "invalid":
            code = "validation_error"
        payload = {
            "error": {
                "code": code,
                "message": first_message or "The request is invalid.",
                "details": data,
            }
        }
    else:
        payload = {
            "error": {
                "code": "error",
                "message": str(data),
                "details": {},
            }
        }

    response.data = payload
    return response


def _first_message(data: dict) -> str:
    for value in data.values():
        if isinstance(value, (list, tuple)) and value:
            return str(value[0])
        if isinstance(value, dict):
            nested = _first_message(value)
            if nested:
                return nested
        if isinstance(value, str):
            return value
    return ""
