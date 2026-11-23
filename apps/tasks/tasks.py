"""Celery tasks for the tasks app."""

from __future__ import annotations

import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.core.emails import absolute_url, send_templated_email

logger = logging.getLogger(__name__)


@shared_task(name="tasks.send_task_assigned_email")
def send_task_assigned_email_task(
    user_id: int, task_ids: list[int], assigned_by_id: int | None = None
) -> None:
    """Email ``user_id`` the tasks in ``task_ids`` they were just assigned.

    One email however many tasks, so a CSV import that hands someone ten
    tasks sends one message listing them all. Queued by
    :func:`apps.tasks.services.notify_new_assignees`.

    Everything is read again at send time: the recipient's
    ``email_task_assigned`` preference, and whether they are still assigned —
    a task unassigned (or deleted) before the worker got to it drops out of
    the email, and an email left with no tasks is not sent.
    """
    from .models import Task

    User = get_user_model()
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        logger.warning("send_task_assigned_email_task: user %s not found", user_id)
        return
    if not (user.is_active and user.email and user.email_task_assigned):
        return

    tasks = list(
        Task.objects.filter(pk__in=task_ids, assigned_members__users=user)
        .select_related("project")
        .distinct()
        .order_by("id")
    )
    if not tasks:
        return

    entries = [
        {
            "task": task,
            "url": absolute_url(
                reverse(
                    "tasks:detail",
                    kwargs={"project_pk": task.project_id, "pk": task.pk},
                )
            ),
        }
        for task in tasks
    ]
    assigned_by = (
        User.objects.filter(pk=assigned_by_id).first() if assigned_by_id else None
    )
    context = {
        "user": user,
        "assigned_by": assigned_by,
        "entries": entries,
    }
    send_templated_email(
        subject_template="emails/task_assigned/subject.txt",
        html_template="emails/task_assigned/body.html",
        text_template="emails/task_assigned/body.txt",
        context=context,
        to=[user.email],
    )
