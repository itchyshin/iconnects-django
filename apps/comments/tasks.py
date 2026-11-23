"""Celery tasks for the comments app."""

from __future__ import annotations

import logging

from celery import shared_task
from django.urls import reverse
from django.utils.text import Truncator

from apps.core.emails import absolute_url, send_templated_email

logger = logging.getLogger(__name__)

# Enough of the comment to answer from the inbox in most cases; the link has
# the rest.
EXCERPT_CHARS = 300


@shared_task(name="comments.send_comment_mention_email")
def send_comment_mention_email_task(comment_id: int, user_ids: list[int]) -> None:
    """Email each of ``user_ids`` that they were tagged in ``comment_id``.

    Queued by :func:`apps.comments.services.notify_mentioned_users`. Everything
    is read again at send time, so nothing goes out that no longer holds: a
    comment deleted, or a tag edited out, before the worker got to it sends
    nothing; nor does a recipient who turned ``email_comment_mention`` off,
    or who can no longer open the task.
    """
    from .models import Comment
    from .services import mentionable_users

    comment = (
        Comment.objects.select_related("task__project", "author")
        .filter(pk=comment_id, task__isnull=False)
        .first()
    )
    if comment is None:
        logger.info("send_comment_mention_email_task: comment %s is gone", comment_id)
        return
    task = comment.task

    still_tagged = set(
        comment.mentions.filter(user_id__in=user_ids).values_list("user_id", flat=True)
    )
    recipients = (
        mentionable_users(task.project)
        .filter(pk__in=still_tagged, email_comment_mention=True)
        .exclude(email="")
    )

    context_base = {
        "author_name": comment.author.display_name,
        "task": task,
        "project": task.project,
        "excerpt": Truncator(comment.content).chars(EXCERPT_CHARS),
        "url": absolute_url(
            reverse(
                "tasks:detail",
                kwargs={"project_pk": task.project_id, "pk": task.pk},
            )
        ),
    }
    for user in recipients:
        send_templated_email(
            subject_template="emails/comment_mention/subject.txt",
            html_template="emails/comment_mention/body.html",
            text_template="emails/comment_mention/body.txt",
            context={**context_base, "user": user},
            to=[user.email],
        )
