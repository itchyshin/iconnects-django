"""Comments app: Comment and per-member CommentReadStatus."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils.functional import cached_property


class Comment(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    content = models.TextField()

    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.CASCADE,
        related_name="comments",
        null=True,
        blank=True,
    )
    task_log = models.ForeignKey(
        "tasks.TaskLog",
        on_delete=models.CASCADE,
        related_name="comments",
        null=True,
        blank=True,
    )
    author = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.CASCADE,
        related_name="comments",
    )

    class Meta:
        # Newest first: the box that posts a comment sits at the top of the
        # thread on both task surfaces, so what you just wrote appears where
        # you wrote it rather than at the far end of a long list.
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["task"]),
            models.Index(fields=["task_log"]),
            models.Index(fields=["author"]),
        ]

    def __str__(self) -> str:
        return f"Comment #{self.pk} by {self.author_id}"

    # ``auto_now_add`` and ``auto_now`` each call ``timezone.now()`` while the
    # row is being inserted, so a brand-new comment's two timestamps land
    # microseconds apart rather than equal. Comparing them for equality — as
    # the template used to — therefore called *every* comment edited. A real
    # edit arrives in a later request, so anything under a second apart is the
    # insert, not a person.
    EDIT_TOLERANCE = timedelta(seconds=1)

    @cached_property
    def author_user(self):
        """The person who wrote this, for linking their name to their profile.

        ``author`` is a membership; a comment's is always an individual one
        (``CommentCreateView`` posts as the writer's own), whose user this
        is — chosen the way ``ProjectMember.display_name`` chooses, so the
        link and the name shown agree. ``None`` for a membership with no
        users left on it.
        """
        return self.author.users.order_by("given_name", "family_name").first()

    @property
    def is_edited(self) -> bool:
        """True when this comment was changed after it was first posted."""
        if self.created_at is None or self.updated_at is None:
            return False  # unsaved: no timestamps to compare yet
        return self.updated_at - self.created_at > self.EDIT_TOLERANCE


class CommentReadStatus(models.Model):
    """Tracks whether a given project member has read a given comment."""

    created_at = models.DateTimeField(auto_now_add=True)
    comment = models.ForeignKey(
        Comment,
        on_delete=models.CASCADE,
        related_name="read_statuses",
    )
    project_member = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.CASCADE,
        related_name="comment_read_statuses",
    )
    read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [["comment", "project_member"]]
        indexes = [
            models.Index(fields=["comment"]),
            models.Index(fields=["project_member"]),
        ]

    def __str__(self) -> str:
        return f"read={self.read} for comment {self.comment_id} by {self.project_member_id}"


class CommentMention(models.Model):
    """A person tagged in a comment with ``@Name``.

    The comment's text keeps the name as it was typed, so it reads the same in
    an email, an export or the admin; this row is what says *who* that name
    was, since two people can share one. Tagging is what sends the email (see
    :func:`apps.comments.services.notify_mentioned_users`).
    """

    created_at = models.DateTimeField(auto_now_add=True)
    comment = models.ForeignKey(
        Comment,
        on_delete=models.CASCADE,
        related_name="mentions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="comment_mentions",
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["comment", "user"], name="unique_comment_mention"
            )
        ]

    def __str__(self) -> str:
        return f"user {self.user_id} in comment {self.comment_id}"
