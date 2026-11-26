"""Telephony domain models for phone voice calls."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.hotel.models import TimeStampedModel


class PhoneCall(TimeStampedModel):
    """A phone call handled by the AI receptionist.

    This is the domain record linked to a conversation. The provider-specific
    webhook/audit record lives in ``django_openline.Call``.
    """

    class Provider(models.TextChoices):
        OPENLINE = "openline", _("OpenLine")

    class Direction(models.TextChoices):
        INBOUND = "inbound", _("Inbound")
        OUTBOUND = "outbound", _("Outbound")

    class Status(models.TextChoices):
        RINGING = "ringing", _("Ringing")
        ANSWERED = "answered", _("Answered")
        ACTIVE = "active", _("Active")
        ENDED = "ended", _("Ended")
        FAILED = "failed", _("Failed")
        TRANSFERRED = "transferred", _("Transferred")

    conversation = models.ForeignKey(
        "agents.Conversation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="phone_calls",
        verbose_name=_("conversation"),
    )
    provider = models.CharField(
        _("provider"), max_length=30, choices=Provider.choices, default=Provider.OPENLINE
    )
    external_id = models.CharField(_("external call id"), max_length=128, unique=True)
    direction = models.CharField(
        _("direction"), max_length=16, choices=Direction.choices, default=Direction.INBOUND
    )
    from_number = models.CharField(_("from number"), max_length=64, blank=True)
    to_number = models.CharField(_("to number"), max_length=64, blank=True)
    status = models.CharField(
        _("status"), max_length=20, choices=Status.choices, default=Status.RINGING, db_index=True
    )
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    answered_at = models.DateTimeField(_("answered at"), null=True, blank=True)
    ended_at = models.DateTimeField(_("ended at"), null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(_("duration (seconds)"), null=True, blank=True)
    recording_url = models.URLField(_("recording URL"), blank=True)
    transcript = models.JSONField(_("transcript"), default=list, blank=True)
    summary = models.TextField(_("summary"), blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("phone call")
        verbose_name_plural = _("phone calls")

    def __str__(self) -> str:
        return f"{self.external_id} ({self.get_direction_display()}, {self.get_status_display()})"
