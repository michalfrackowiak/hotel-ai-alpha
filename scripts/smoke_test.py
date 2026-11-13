"""End-to-end smoke test for a running Hotel Aurora instance.

Usage::

    .\\.venv\\Scripts\\python.exe scripts/smoke_test.py http://localhost:8000

Exercises the full guest flow: availability, booking creation, confirmation,
modification, cancellation-preview and cancellation, plus the operations and
health endpoints. Exits non-zero on the first failure. Works against any
running instance (local runserver, Docker, staging).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta


def request(base: str, method: str, path: str, *, body=None, token=None, headers=None):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Booking-Token", token)
    if headers:
        for key, value in headers.items():
            req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            payload = raw
        return exc.code, payload


def fail(message: str) -> None:
    print(f"SMOKE FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

    # Health
    status, payload = request(base, "GET", "/healthz")
    if status != 200 or not (payload or {}).get("status") == "ok":
        fail(f"healthz returned {status}: {payload}")
    print("healthz: ok")

    today = date.today()
    check_in = (today + timedelta(days=14)).isoformat()
    check_out = (today + timedelta(days=16)).isoformat()

    # Availability
    status, payload = request(base, "GET", f"/api/v1/availability/?check_in={check_in}&check_out={check_out}&guests=2")
    if status != 200 or not (payload or {}).get("results"):
        fail(f"availability returned {status}: {payload}")
    slug = payload["results"][0]["room_type"]["slug"]
    print(f"availability: {slug}")

    # Create a hold
    status, created = request(base, "POST", "/api/v1/bookings/", body={
        "room_type": slug,
        "check_in": check_in,
        "check_out": check_out,
        "first_name": "Smoke",
        "last_name": "Test",
        "email": "smoke@example.com",
        "phone": "+48 600 000 000",
        "adults": 2,
    })
    if status != 201:
        fail(f"create booking returned {status}: {created}")
    reference = created["reference"]
    token = created["manage_token"]
    print(f"created hold: {reference}")

    # Token gate
    status, _ = request(base, "GET", f"/api/v1/bookings/{reference}/")
    if status != 403:
        fail(f"detail without token expected 403, got {status}")
    status, detail = request(base, "GET", f"/api/v1/bookings/{reference}/", token=token)
    if status != 200 or detail["status"] != "pending":
        fail(f"detail with token returned {status}: {detail}")
    print("token gate: ok")

    # Confirm
    status, confirmed = request(base, "POST", f"/api/v1/bookings/{reference}/confirm/", token=token)
    if status != 200 or confirmed["status"] != "confirmed":
        fail(f"confirm returned {status}: {confirmed}")
    print("confirm: ok")

    # Modify dates
    new_check_out = (today + timedelta(days=17)).isoformat()
    status, modified = request(base, "PATCH", f"/api/v1/bookings/{reference}/", token=token, body={"check_out": new_check_out})
    if status != 200 or modified["check_out"] != new_check_out:
        fail(f"modify returned {status}: {modified}")
    print("modify: ok")

    # Cancellation preview
    status, preview = request(base, "GET", f"/api/v1/bookings/{reference}/cancellation-preview/", token=token)
    if status != 200 or "preview" not in (preview or {}):
        fail(f"preview returned {status}: {preview}")
    print(f"preview: {preview['preview']['reason']}")

    # Cancel
    status, cancelled = request(base, "POST", f"/api/v1/bookings/{reference}/cancel/", token=token, body={"reason": "plans_changed", "note": "smoke"})
    if status != 200 or cancelled["status"] != "cancelled":
        fail(f"cancel returned {status}: {cancelled}")
    print("cancel: ok")

    # Ops dashboard (requires staff login; just check it responds)
    req = urllib.request.Request(base.rstrip("/") + "/ops/", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    if status not in (200, 302, 403):
        fail(f"/ops/ returned {status}")
    print("/ops/: ok")

    print("SMOKE PASSED")


if __name__ == "__main__":
    main()