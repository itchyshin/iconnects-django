"""Celery tasks for the notifications app.

These are the user-facing entry points called by cron jobs:

- :func:`send_digest_email_task` compiles recent notifications + overdue
  tasks for a single user and emails them.
- :func:`broadcast_announcement_task` fans out an ``announcement=True``
  notification by email to all its recipients.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.core.emails import send_templated_email
from apps.core.enums import EmailFrequency

logger = logging.getLogger(__name__)


DIGEST_WINDOWS = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
}


def _recent_notifications(user, since):
    from .models import Notification

    return list(
        Notification.objects.for_user(user)
        .filter(created_at__gte=since)
        .select_related("project")
        .order_by("-created_at")[:50]
    )


def _overdue_tasks_for(user):
    """Return open tasks ``user`` is assigned to that are past *their*
    deadline: their individual one where a Lead set it, else the task's.
    Each carries ``my_deadline``."""
    from apps.tasks.models import Task

    now = timezone.now()
    return list(
        Task.objects.filter(assigned_members__users=user)
        .overdue_for(user, now)
        .select_related("project")
        .distinct()
        .order_by("my_deadline")[:50]
    )


@shared_task(name="notifications.send_digest_email")
def send_digest_email_task(user_id: int, frequency: str = "daily") -> None:
    """Compile and email a digest for ``user_id``.

    ``frequency`` must be ``"daily"`` or ``"weekly"`` — it controls the
    lookback window for recent notifications.
    """
    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.warning("send_digest_email_task: user %s not found", user_id)
        return

    window = DIGEST_WINDOWS.get(frequency, DIGEST_WINDOWS["daily"])
    since = timezone.now() - window

    notifications = _recent_notifications(user, since)
    overdue = _overdue_tasks_for(user)

    if not notifications and not overdue:
        logger.info("send_digest_email_task: nothing to send for user %s", user_id)
        return

    context = {
        "user": user,
        "frequency": frequency,
        "notifications": notifications,
        "overdue_tasks": overdue,
        "since": since,
    }
    send_templated_email(
        subject_template="emails/digest/subject.txt",
        html_template="emails/digest/body.html",
        text_template="emails/digest/body.txt",
        context=context,
        to=[user.email],
    )


@shared_task(name="notifications.broadcast_announcement")
def broadcast_announcement_task(announcement_id: int) -> None:
    """Email every recipient of an announcement notification."""
    from .models import Notification

    try:
        announcement = Notification.objects.prefetch_related("recipients").get(
            pk=announcement_id
        )
    except Notification.DoesNotExist:
        logger.warning(
            "broadcast_announcement_task: notification %s not found", announcement_id
        )
        return

    recipients = [u.email for u in announcement.recipients.all() if u.email]
    if not recipients:
        return

    context = {
        "announcement": announcement,
        "project": announcement.project,
    }
    for email in recipients:
        send_templated_email(
            subject_template="emails/announcement/subject.txt",
            html_template="emails/announcement/body.html",
            text_template="emails/announcement/body.txt",
            context=context,
            to=[email],
        )


# Convenience export: cron code uses these frequency strings.
DAILY = EmailFrequency.DAILY
WEEKLY = EmailFrequency.WEEKLY
