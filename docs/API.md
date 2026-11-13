# REST API

Base URL: `/api/v1/`. All endpoints are public (`AllowAny`) in the alpha; booking references are the only secret. Responses are JSON unless noted.

Errors use a stable envelope:

```json
{
  "error": {
    "code": "no_availability",
    "message": "No rooms of this type are available for the selected dates.",
    "details": {}
  }
}
```

| HTTP | Meaning |
| --- | --- |
| `200` | Success |
| `201` | Created |
| `400` | Validation or domain error |
| `404` | Unknown room type, booking or session |
| `409` | Booking state conflict (expired hold, not confirmable) |
| `429` | Throttled |
| `502` | Speech provider failure |
| `503` | Assistant disabled |

Common error codes: `validation_error`, `invalid_stay`, `capacity_exceeded`, `room_type_not_found`, `no_availability`, `invalid_room`, `contact_required`, `booking_not_found`, `hold_expired`, `not_confirmable`, `not_cancellable`, `agent_disabled`, `stt_failed`, `tts_failed`.

## Health

### `GET /api/v1/health/`

```json
{
  "status": "ok",
  "time": "2026-11-13T10:15:00+01:00",
  "service": "hotel-ai-alpha"
}
```

`GET /healthz` (outside `/api/v1/`) also checks the database and returns `503` when it is unavailable.

## Room types

### `GET /api/v1/room-types/`

Returns active room types with the number of active physical rooms.

```json
[
  {
    "id": 1,
    "slug": "standard-double",
    "name": "Standard Double",
    "short_description": "Comfortable double room with a queen bed and city or garden view.",
    "description": "...",
    "capacity": 2,
    "base_price": "420.00",
    "currency": "PLN",
    "size_sqm": 22,
    "bed_configuration": "Queen bed",
    "amenities": ["Free Wi-Fi", "Air conditioning"],
    "rooms_available": 3
  }
]
```

## Availability

### `GET /api/v1/availability/`

Query parameters:

| Name | Type | Required | Notes |
| --- | --- | --- | --- |
| `check_in` | ISO date | yes | `YYYY-MM-DD`, not in the past |
| `check_out` | ISO date | yes | Must be after `check_in` |
| `guests` | integer | no | Default `2`, 1–12 |
| `room_type` | slug | no | Restrict to one room type |

```json
{
  "query": { "check_in": "2026-12-12", "check_out": "2026-12-15", "guests": 2 },
  "results": [
    {
      "room_type": {
        "slug": "standard-double",
        "name": "Standard Double",
        "short_description": "...",
        "capacity": 2,
        "base_price": "420.00",
        "currency": "PLN",
        "size_sqm": 22,
        "bed_configuration": "Queen bed",
        "amenities": ["Free Wi-Fi"]
      },
      "available_rooms": 3,
      "room_numbers": ["101", "102", "103"],
      "quote": {
        "room_type": "standard-double",
        "room_type_name": "Standard Double",
        "room": "101",
        "check_in": "2026-12-12",
        "check_out": "2026-12-15",
        "nights": 3,
        "adults": 2,
        "children": 0,
        "currency": "PLN",
        "nightly": [
          { "date": "2026-12-12", "base_rate": "420.00", "rate": "420.00", "rules": [] }
        ],
        "subtotal": "1260.00",
        "total": "1260.00",
        "tax_rate": "0.08",
        "tax_included": "93.33"
      }
    }
  ]
}
```

Results are sorted by total price, then name.

## Pricing

### `GET /api/v1/pricing/` or `POST /api/v1/pricing/`

GET uses query parameters; POST accepts the same fields as JSON.

| Name | Type | Required |
| --- | --- | --- |
| `room_type` | slug | yes |
| `check_in` | ISO date | yes |
| `check_out` | ISO date | yes |
| `adults` | integer 1–8 | no (default 2) |
| `children` | integer 0–6 | no (default 0) |

```json
{ "quote": { "nights": 3, "subtotal": "1260.00", "total": "1260.00", "currency": "PLN" } }
```

The full quote object matches the availability example.

## Bookings

### `POST /api/v1/bookings/`

Creates a **pending hold**. This does not confirm the reservation.

```json
{
  "room_type": "standard-double",
  "check_in": "2026-12-12",
  "check_out": "2026-12-15",
  "first_name": "Anna",
  "last_name": "Kowalska",
  "email": "anna@example.com",
  "phone": "",
  "adults": 2,
  "children": 0,
  "language": "en",
  "source": "web",
  "special_requests": "High floor please."
}
```

`email` or `phone` is required. `source` is one of `web`, `chat`, `voice`, `api`. `language` is `en` or `pl`.

Response `201`:

```json
{
  "reference": "HA-7KQ2M4",
  "status": "pending",
  "status_display": "Pending",
  "source": "web",
  "source_display": "Website",
  "language": "en",
  "guest": {
    "id": 1,
    "first_name": "Anna",
    "last_name": "Kowalska",
    "full_name": "Anna Kowalska",
    "email": "anna@example.com",
    "phone": "",
    "language": "en",
    "country": ""
  },
  "room": "101",
  "room_type": "standard-double",
  "room_type_name": "Standard Double",
  "check_in": "2026-12-12",
  "check_out": "2026-12-15",
  "nights": 3,
  "adults": 2,
  "children": 0,
  "currency": "PLN",
  "total_price": "1260.00",
  "price_breakdown": { "nightly": [], "subtotal": "1260.00", "total": "1260.00" },
  "special_requests": "High floor please.",
  "hold_expires_at": "2026-11-13T10:45:00+01:00",
  "can_confirm": true,
  "calendar": null,
  "created_at": "2026-11-13T10:15:00+01:00",
  "confirmed_at": null,
  "cancelled_at": null
}
```

### `GET /api/v1/bookings/<REFERENCE>/`

Returns the same booking object. References are case-insensitive (`ha-7kq2m4` is accepted).

### `POST /api/v1/bookings/<REFERENCE>/confirm/`

Confirms a pending hold. Idempotent for an already confirmed booking. Returns `409` with `hold_expired` when the hold has lapsed, or `not_confirmable` for other terminal states.

### `POST /api/v1/bookings/<REFERENCE>/cancel/`

Cancels a pending or confirmed booking. Idempotent for already cancelled/expired bookings. Returns `409` with `not_cancellable` after check-out or no-show.

## Chat

### `POST /api/v1/chat/sessions/`

```json
{ "language": "pl", "channel": "chat" }
```

`channel` is `chat` or `voice`. Response `201`:

```json
{
  "session_key": "4f2c9b1d8e7a4c6b9f0a2d3e5c7b8a1f",
  "channel": "chat",
  "language": "pl",
  "greeting": "Dzień dobry, tu Hotel Aurora. ...",
  "provider": { "name": "Demo policy (offline)", "type": "demo", "model": "demo-policy-v1" }
}
```

### `GET /api/v1/chat/sessions/<SESSION_KEY>/`

Returns the conversation metadata, user/assistant history and the attached booking (if any).

### `POST /api/v1/chat/sessions/<SESSION_KEY>/messages/`

```json
{ "message": "Czy macie pokój dla 2 gości od 12 do 15 grudnia 2026?" }
```

Response:

```json
{
  "session_key": "4f2c9b1d8e7a4c6b9f0a2d3e5c7b8a1f",
  "language": "pl",
  "reply": "Mamy Standard Double ...",
  "tool_calls": [
    { "name": "check_availability", "arguments": { "check_in": "2026-12-12" }, "ok": true }
  ],
  "provider": "demo",
  "model": "demo-policy-v1",
  "latency_ms": 4,
  "booking_reference": "",
  "booking": null,
  "error": ""
}
```

`booking` is populated once `create_booking` succeeds. `tool_calls` is a trace for debugging and UI rendering; clients should not depend on its exact shape.

## Voice

### `GET /api/v1/voice/config/`

```json
{
  "enabled": true,
  "languages": [
    { "code": "en", "label": "English", "speech": "en-US" },
    { "code": "pl", "label": "Polski", "speech": "pl-PL" }
  ],
  "stt": { "provider": "browser", "mode": "browser", "languages": {} },
  "tts": { "provider": "browser", "mode": "browser", "languages": {} },
  "voice_names": { "en": "", "pl": "" },
  "greeting": { "en": "Good day...", "pl": "Dzień dobry..." },
  "chat_enabled": true,
  "provider": { "name": "Demo policy (offline)", "type": "demo", "model": "demo-policy-v1" }
}
```

When `mode` is `browser`, use the Web Speech API directly. When it is `server`, call the endpoints below.

### `POST /api/v1/voice/transcribe/`

`multipart/form-data` with:

| Field | Type | Required |
| --- | --- | --- |
| `audio` | file | yes |
| `language` | `en` / `pl` | no (default `en`) |

```json
{ "text": "I would like a room for two" }
```

Requires `OPENAI_API_KEY` when the configured STT provider is `openai`.

### `POST /api/v1/voice/speak/`

```json
{ "text": "Your room is confirmed.", "language": "en", "voice": "alloy" }
```

Returns `audio/mpeg` bytes. Requires `OPENAI_API_KEY` when the configured TTS provider is `openai`.

## Throttling

Default DRF rates (configurable through env):

| Scope | Default |
| --- | --- |
| anonymous | `240/hour` |
| user | `2000/hour` |
| chat (session create, messages, voice) | `120/hour` |

Throttling is in-memory per process and disabled in tests.

## curl examples

```bash
# Availability
curl "http://localhost:8000/api/v1/availability/?check_in=2026-12-12&check_out=2026-12-15&guests=2"

# Create a hold
curl -X POST http://localhost:8000/api/v1/bookings/ \
  -H "Content-Type: application/json" \
  -d '{"room_type":"standard-double","check_in":"2026-12-12","check_out":"2026-12-15","first_name":"Anna","email":"anna@example.com"}'

# Start a chat and send a message
curl -X POST http://localhost:8000/api/v1/chat/sessions/ \
  -H "Content-Type: application/json" -d '{"language":"en"}'
curl -X POST http://localhost:8000/api/v1/chat/sessions/<KEY>/messages/ \
  -H "Content-Type: application/json" -d '{"message":"A room for two from 12 to 15 December 2026"}'
```
