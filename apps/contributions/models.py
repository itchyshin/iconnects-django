"""ContributionRecord: the ledger of credit a Lead has verified.

On a project that tracks contributions as CRediT, a record is written when
a Lead verifies an assignee's part, one per user behind that assignee. The
part must be marked done; a review is not required. Withdrawing the
verification, or the assignee undoing their done mark, takes it back. The
task being closed or reopened plays no part.

The rows follow task state rather than standing apart from it, so every FK
cascades: deleting a task, a project or a user takes the credit recorded
against it. Leads can also correct a record by hand — credit is a claim
about who did what, and a wrong claim has to be fixable. A row a lead
entered themselves is marked ``added_by_hand``, and is left alone by the
automatic writer and its withdrawal.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.enums import ContributionType


class ContributionRecord(models.Model):
    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.CASCADE,
        related_name="contributions",
    )
    task_log = models.ForeignKey(
        "tasks.TaskLog",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="contributions",
        help_text=(
            "The submission this credit was earned on. Null on a record a "
            "lead entered by hand, which stands on their say-so rather than "
            "on a mark someone made."
        ),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="contributions",
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="contributions",
    )
    contribution_type = models.CharField(
        max_length=32,
        choices=ContributionType.choices,
        default=ContributionType.OTHER,
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="contributions_verified",
    )
    closed_at = models.DateTimeField()
    added_by_hand = models.BooleanField(
        default=False,
        help_text=(
            "True when a lead entered this record themselves rather than it "
            "being written by a task closing."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-closed_at", "-id"]
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["project"]),
            models.Index(fields=["user", "project"]),
            models.Index(fields=["contribution_type"]),
        ]
        constraints = [
            # One credit per person per task: the role comes from the task,
            # so a second row would only ever double-count the same work.
            # Keyed on the task rather than the submission because a
            # hand-entered record has no submission behind it.
            models.UniqueConstraint(
                fields=["task", "user"],
                name="contribution_unique_per_task_user",
            ),
        ]

    def __str__(self) -> str:
        return f"Contribution #{self.pk} ({self.user_id} / {self.contribution_type})"
