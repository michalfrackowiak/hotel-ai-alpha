"""Import OTA iCal feeds into room blocks and export a signed room calendar.

The channel manager is deliberately one-way for availability: feeds are read
into :class:`~apps.hotel.models.RoomBlock` rows, so the booking write path is
never touched by an OTA. The export feed exposes the hotel's own blocking
bookings as a signed iCal URL that an OTA can subscribe to.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.utils import timezone

from .availability import blocking_filter
from .models import Booking, ChannelFeed, Room, RoomBlock

logger = logging.getLogger(__name__)

FEED_TOKEN_SALT = "hotel.channel_feed"
FEED_TOKEN_MAX_AGE = 60 * 60 * 24 * 365 * 5
FEED_USER_AGENT = "ResortNeptun/1.0 channel-sync"


@dataclass(frozen=True)
class FeedEvent:
    """One VEVENT from a channel feed, reduced to what availability needs."""

    uid: str
    check_in: date
    check_out: date
    summary: str = ""
    cancelled: bool = False


@dataclass
class SyncResult:
    feed: ChannelFeed
    dry_run: bool = False
    ok: bool = True
    created: int = 0
    updated: int = 0
    deleted: int = 0
    conflicts: int = 0
    event_count: int = 0
    error: str = ""
    errors: list[str] = field(default_factory=list)


def unfold(text: str) -> list[str]:
    """Split an iCalendar document into logical lines (RFC 5545 folding)."""
    physical = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    logical: list[str] = []
    for line in physical:
        if line[:1] in (" ", "\t") and logical:
            logical[-1] += line[1:]
        else:
            logical.append(line)
    return logical


def _split_property(line: str) -> tuple[str, dict[str, str], str]:
    if ":" not in line:
        return "", {}, ""
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, raw = part.split("=", 1)
            params[key.strip().upper()] = raw.strip().strip('"')
    return name, params, value


def _parse_date_value(value: str, params: dict[str, str]) -> date | None:
    raw = value.strip()
    if not raw:
        return None
    if params.get("VALUE", "").upper() == "DATE" or "T" not in raw:
        try:
            return datetime.strptime(raw[:8], "%Y%m%d").date()
        except ValueError:
            return None
    stripped = raw.rstrip("Z")
    try:
        moment = datetime.strptime(stripped[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if raw.endswith("Z"):
        moment = moment.replace(tzinfo=UTC)
    else:
        tzid = params.get("TZID")
        zone = timezone.get_current_timezone()
        if tzid:
            try:
                zone = ZoneInfo(tzid)
            except (ZoneInfoNotFoundError, ValueError):
                zone = timezone.get_current_timezone()
        moment = moment.replace(tzinfo=zone)
    return timezone.localtime(moment).date()


def _build_event(fields: dict[str, tuple[str, dict[str, str]]]) -> FeedEvent | None:
    start = fields.get("DTSTART")
    end = fields.get("DTEND")
    if start is None or end is None:
        return None
    check_in = _parse_date_value(*start)
    check_out = _parse_date_value(*end)
    if check_in is None or check_out is None:
        return None
    if check_out <= check_in:
        check_out = check_in + timedelta(days=1)
    summary = (fields.get("SUMMARY", ("", {}))[0] or "").strip()
    uid = (fields.get("UID", ("", {}))[0] or "").strip()
    if not uid:
        digest = hashlib.sha1(f"{check_in}{check_out}{summary}".encode()).hexdigest()[:10]
        uid = f"auto-{check_in:%Y%m%d}-{check_out:%Y%m%d}-{digest}"
    cancelled = (fields.get("STATUS", ("", {}))[0] or "").strip().upper() == "CANCELLED"
    return FeedEvent(uid=uid, check_in=check_in, check_out=check_out, summary=summary, cancelled=cancelled)


def parse_ical(text: str) -> list[FeedEvent]:
    """Parse VEVENT blocks, including folded lines and DATE/DATE-TIME values."""
    events: list[FeedEvent] = []
    current: dict[str, tuple[str, dict[str, str]]] | None = None
    for line in unfold(text):
        name, params, value = _split_property(line)
        if name == "BEGIN" and value.strip().upper() == "VEVENT":
            current = {}
            continue
        if name == "END" and value.strip().upper() == "VEVENT":
            if current is not None:
                event = _build_event(current)
                if event is not None and not event.cancelled:
                    events.append(event)
            current = None
            continue
        if current is None:
            continue
        if name in {"UID", "SUMMARY", "STATUS", "DTSTART", "DTEND"}:
            current[name] = (value, params)
    return events


def fetch_feed(url: str) -> str:
    """Download an iCal feed with a bounded timeout."""
    timeout = settings.CHANNEL_FEED_TIMEOUT_SECONDS
    request = Request(url, headers={"User-Agent": FEED_USER_AGENT})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - feed URLs are operator supplied
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, "replace")


def _conflict_count(room: Room, events: list[FeedEvent]) -> int:
    conflicts = 0
    for event in events:
        overlaps = Booking.objects.filter(
            room=room,
            check_in__lt=event.check_out,
            check_out__gt=event.check_in,
        ).filter(blocking_filter())
        if overlaps.exists():
            conflicts += 1
    return conflicts


def sync_feed(feed: ChannelFeed, *, dry_run: bool = False, text: str | None = None) -> SyncResult:
    """Reconcile one feed with its room blocks.

    ``text`` lets callers (and tests) supply the payload directly; otherwise the
    feed URL is fetched. A fetch or parse failure leaves existing blocks in
    place and records the error on the feed.
    """
    result = SyncResult(feed=feed, dry_run=dry_run)
    try:
        payload = fetch_feed(feed.url) if text is None else text
        parsed = parse_ical(payload)
    except Exception as exc:  # noqa: BLE001 - a bad feed must not break the scheduler
        result.ok = False
        result.error = str(exc)
        result.errors.append(result.error)
        logger.warning("Channel feed %s failed: %s", feed.pk, exc)
        if not dry_run:
            feed.last_error = result.error[:2000]
            feed.save(update_fields=["last_error", "updated_at"])
        return result

    active = {event.uid: event for event in parsed if not event.cancelled}
    result.event_count = len(active)
    result.conflicts = _conflict_count(feed.room, list(active.values()))

    existing = {block.external_uid: block for block in feed.blocks.all()}
    stale_ids: list[int] = []
    for uid, event in active.items():
        block = existing.get(uid)
        if block is None:
            result.created += 1
            if not dry_run:
                RoomBlock.objects.create(
                    room=feed.room,
                    feed=feed,
                    check_in=event.check_in,
                    check_out=event.check_out,
                    source=RoomBlock.Source.CHANNEL,
                    external_uid=uid,
                    summary=event.summary[:255],
                )
            continue
        if (block.check_in, block.check_out, block.summary) != (
            event.check_in,
            event.check_out,
            event.summary[:255],
        ):
            result.updated += 1
            if not dry_run:
                block.check_in = event.check_in
                block.check_out = event.check_out
                block.summary = event.summary[:255]
                block.save(update_fields=["check_in", "check_out", "summary", "updated_at"])

    for uid, block in existing.items():
        if uid not in active:
            stale_ids.append(block.pk)
    result.deleted = len(stale_ids)
    if not dry_run and stale_ids:
        RoomBlock.objects.filter(pk__in=stale_ids).delete()

    if not dry_run:
        feed.last_synced_at = timezone.now()
        feed.last_error = ""
        feed.last_event_count = result.event_count
        feed.conflict_count = result.conflicts
        feed.save(
            update_fields=[
                "last_synced_at",
                "last_error",
                "last_event_count",
                "conflict_count",
                "updated_at",
            ]
        )
    return result


def sync_all_feeds(*, dry_run: bool = False) -> dict:
    feeds = list(ChannelFeed.objects.filter(is_active=True).select_related("room"))
    results = [sync_feed(feed, dry_run=dry_run) for feed in feeds]
    return {
        "feeds": len(results),
        "ok": sum(1 for result in results if result.ok),
        "failed": sum(1 for result in results if not result.ok),
        "created": sum(result.created for result in results),
        "updated": sum(result.updated for result in results),
        "deleted": sum(result.deleted for result in results),
        "conflicts": sum(result.conflicts for result in results),
        "results": results,
    }


def make_feed_token(room: Room) -> str:
    """Return the signed token used in a room's export feed URL."""
    return dumps({"room": room.pk}, salt=FEED_TOKEN_SALT)


def resolve_feed_token(token: str) -> Room | None:
    """Return the room for a valid export token, or ``None`` when it is invalid."""
    try:
        data = loads(token, salt=FEED_TOKEN_SALT, max_age=FEED_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    room_id = data.get("room")
    if not isinstance(room_id, int):
        return None
    return Room.objects.filter(pk=room_id).first()
