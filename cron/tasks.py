"""Scheduled (beat-driven) Celery tasks for i-connects.

These are fan-out / maintenance jobs. Each one is thin: it iterates the
relevant queryset and enqueues per-user work through ``.delay()`` so slow
email sends don't block the beat schedule.

Schedules are configured by ``apps/core/management/commands/setup_cron.py``
which writes rows into django-celery-beat.
"""

from __future__ import annotations

import logging
import shutil
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.core.enums import EmailFrequency, TaskStatus

logger = logging.getLogger(__name__)


def _users_with_frequency(field: str, frequency: str):
    User = get_user_model()
    return User.objects.filter(**{field: frequency, "is_active": True})


@shared_task(name="cron.daily_digest")
def daily_digest_cron() -> int:
    """Fan out :func:`send_digest_email_task` to every daily-digest user."""
    from apps.notifications.tasks import send_digest_email_task

    users = _users_with_frequency(
        "email_project_activity_frequency", EmailFrequency.DAILY
    )
    count = 0
    for user in users.only("id"):
        send_digest_email_task.delay(user.id, "daily")
        count += 1
    logger.info("daily_digest_cron: enqueued %s users", count)
    return count


@shared_task(name="cron.weekly_digest")
def weekly_digest_cron() -> int:
    """Fan out :func:`send_digest_email_task` to every weekly-digest user."""
    from apps.notifications.tasks import send_digest_email_task

    users = _users_with_frequency(
        "email_project_activity_frequency", EmailFrequency.WEEKLY
    )
    count = 0
    for user in users.only("id"):
        send_digest_email_task.delay(user.id, "weekly")
        count += 1
    logger.info("weekly_digest_cron: enqueued %s users", count)
    return count


@shared_task(name="cron.overdue_task_reminder")
def overdue_task_reminder_cron() -> int:
    """Email reminders for overdue tasks.

    Finds every assignment on a ``NOT_COMPLETED`` task whose member is past
    *their* deadline -- their individual one where a Lead set it, else the
    task's -- collects those members' users, and enqueues a digest-style
    email for each user whose ``email_overdue_task_frequency`` is not
    ``NEVER``. A member whose individual deadline is still to come is not
    reminded, though the task's own deadline has passed. The digest task does
    the actual rendering/sending.
    """
    from django.db.models import OuterRef, Subquery
    from django.db.models.functions import Coalesce

    from apps.notifications.tasks import send_digest_email_task
    from apps.tasks.models import AssigneeDeadline, Task

    User = get_user_model()
    now = timezone.now()

    personal = AssigneeDeadline.objects.filter(
        task_id=OuterRef("task_id"), member_id=OuterRef("projectmember_id")
    ).values("deadline")[:1]
    overdue_member_ids = (
        Task.assigned_members.through.objects.exclude(
            task__status=TaskStatus.COMPLETED
        )
        .annotate(due=Coalesce(Subquery(personal), "task__deadline"))
        .filter(due__lt=now)
        .values("projectmember_id")
    )
    user_ids = (
        User.objects.filter(project_memberships__in=overdue_member_ids)
        .exclude(email_overdue_task_frequency=EmailFrequency.NEVER)
        .filter(is_active=True)
        .values_list("id", flat=True)
        .distinct()
    )

    count = 0
    for uid in user_ids:
        # Reuse the digest task — it already filters for this user's overdue
        # tasks and will no-op if none are visible.
        send_digest_email_task.delay(uid, "daily")
        count += 1
    logger.info("overdue_task_reminder_cron: enqueued %s users", count)
    return count


@shared_task(name="cron.cleanup_tmp_files")
def cleanup_tmp_files_cron(max_age_hours: int = 24) -> int:
    """Delete files under ``MEDIA_ROOT/tmp/`` older than ``max_age_hours``."""
    tmp_root = Path(settings.MEDIA_ROOT) / "tmp"
    if not tmp_root.exists():
        return 0

    cutoff = timezone.now().timestamp() - (max_age_hours * 3600)
    removed = 0
    for entry in tmp_root.iterdir():
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            logger.warning("cleanup_tmp_files_cron: could not remove %s: %s", entry, exc)
    logger.info("cleanup_tmp_files_cron: removed %s entries", removed)
    return removed
