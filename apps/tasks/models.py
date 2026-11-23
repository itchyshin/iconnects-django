"""Tasks app: KanbanBoard, Milestone, Task and TaskLog (audit trail)."""

from __future__ import annotations

from django.db import models
from django.db.models import Exists, OuterRef, Q, Subquery
from django.utils import timezone

from apps.core.enums import AutoAssignNew, CompletedAs, ContributionType, TaskStatus
from apps.tasks.timezones import default_zone_name, get_zone, validate_timezone


def default_task_timezone() -> str:
    """A task's zone when none is chosen: the server's ``TIME_ZONE``. The
    task form preselects the Lead's system timezone instead."""
    return default_zone_name()


class KanbanBoard(models.Model):
    """A Kanban column (container) for a project's tasks."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    name = models.CharField(max_length=255, default="To Do")
    container_order = models.IntegerField()
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="containers",
    )

    class Meta:
        ordering = ["container_order"]
        indexes = [models.Index(fields=["project"])]

    def __str__(self) -> str:
        return f"{self.name} (project {self.project_id})"


class Milestone(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="milestones",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    tags = models.JSONField(null=True, blank=True)
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)

    # Prisma models Milestone parent/child as a many-to-many self relation.
    parents = models.ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="children",
    )

    class Meta:
        ordering = ["start_date", "id"]
        indexes = [models.Index(fields=["project"])]

    def __str__(self) -> str:
        return self.name


class TaskQuerySet(models.QuerySet):
    """Deadline states as queries, matching ``Task.is_overdue`` / ``is_extended``.

    A task past its deadline and still open is *extended* when every assignee
    who has not marked their part done holds an individual deadline that is
    still to come (see :class:`AssigneeDeadline`); otherwise it is *overdue*.
    """

    def past_due(self, now=None):
        """Open tasks whose task-level deadline has passed (overdue or extended)."""
        now = now or timezone.now()
        return self.filter(deadline__lt=now).exclude(status=TaskStatus.COMPLETED)

    @staticmethod
    def _deadline_states(now):
        pending = pending_assignments().filter(task_id=OuterRef("pk"))
        uncovered = pending.filter(
            ~Exists(
                AssigneeDeadline.objects.filter(
                    task_id=OuterRef("task_id"),
                    member_id=OuterRef("projectmember_id"),
                    deadline__gte=now,
                )
            )
        )
        return Exists(pending), Exists(uncovered)

    def overdue(self, now=None):
        now = now or timezone.now()
        has_pending, has_uncovered = self._deadline_states(now)
        return self.past_due(now).filter(~has_pending | has_uncovered)

    def extended(self, now=None):
        now = now or timezone.now()
        has_pending, has_uncovered = self._deadline_states(now)
        return self.past_due(now).filter(has_pending & ~has_uncovered)

    def with_my_deadline(self, user):
        """Annotate ``my_deadline``: ``user``'s individual deadline on each
        task where a Lead set one, else the task's own deadline — and
        ``my_deadline_tz``, the zone that governs it: the member's own zone
        where they have one with their own deadline, else the task's."""
        from django.db.models import F, Value
        from django.db.models.functions import Coalesce, NullIf

        personal = AssigneeDeadline.objects.filter(
            task_id=OuterRef("pk"), member__users=user, deadline__isnull=False
        ).order_by("deadline")
        return self.annotate(
            my_deadline=Coalesce(
                Subquery(personal.values("deadline")[:1]), F("deadline")
            ),
            my_deadline_tz=Coalesce(
                NullIf(Subquery(personal.values("timezone")[:1]), Value("")),
                F("timezone"),
            ),
        )

    def overdue_for(self, user, now=None):
        """Open tasks past ``user``'s own deadline on them. Annotates
        ``my_deadline`` (see :meth:`with_my_deadline`)."""
        now = now or timezone.now()
        return (
            self.with_my_deadline(user)
            .filter(my_deadline__lt=now)
            .exclude(status=TaskStatus.COMPLETED)
        )


def pending_assignments():
    """Assignment rows (``Task.assigned_members.through``) whose member has not
    marked their part done: their current TaskLog is missing or not COMPLETED."""
    through = Task.assigned_members.through
    current_status = (
        TaskLog.objects.filter(
            task_id=OuterRef("task_id"), assigned_to_id=OuterRef("projectmember_id")
        )
        .order_by("-created_at", "-id")
        .values("status")[:1]
    )
    return (
        through.objects.annotate(current_status=Subquery(current_status))
        .filter(Q(current_status__isnull=True) | ~Q(current_status=TaskStatus.COMPLETED))
    )


class Task(models.Model):
    objects = TaskQuerySet.as_manager()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    created_by = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.CASCADE,
        related_name="created_tasks",
    )

    # Task metadata
    deadline = models.DateTimeField(null=True, blank=True)
    start_date = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(
        max_length=64,
        default=default_task_timezone,
        validators=[validate_timezone],
        help_text=(
            "IANA timezone governing the task's start and due dates: the "
            "Lead's clock time is read in it, and the dates are shown in it. "
            "An assignee's own dates may carry a zone of their own "
            "(``AssigneeDeadline.timezone``)."
        ),
    )
    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    tags = models.JSONField(null=True, blank=True)

    # Kanban coding
    container_task_order = models.IntegerField()
    container = models.ForeignKey(
        KanbanBoard,
        on_delete=models.CASCADE,
        related_name="tasks",
    )

    # Task relations
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="tasks",
    )
    milestone = models.ForeignKey(
        Milestone,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
    )
    form_version = models.ForeignKey(
        "forms_builder.FormVersion",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tasks",
    )
    element = models.ForeignKey(
        "projects.Element",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
    )

    status = models.CharField(
        max_length=32,
        choices=TaskStatus.choices,
        default=TaskStatus.NOT_COMPLETED,
    )

    assigned_members = models.ManyToManyField(
        "projects.ProjectMember",
        blank=True,
        related_name="assigned_tasks",
    )
    reviewers = models.ManyToManyField(
        "projects.ProjectMember",
        blank=True,
        related_name="reviewed_tasks",
    )
    watchers = models.ManyToManyField(
        "projects.ProjectMember",
        blank=True,
        related_name="watched_tasks",
    )
    roles = models.ManyToManyField(
        "roles.Role",
        blank=True,
        related_name="tasks",
    )

    assign_by_role = models.BooleanField(default=False)

    closed_by = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_tasks",
        help_text=(
            "Who marked this task completed — the Lead who closed it, or the "
            "reviewer whose sign-off was the last one outstanding. Kept as "
            "written even after they leave the task, and cleared when the "
            "task is reopened. Nulled only if their membership row is deleted "
            "outright, which the page reports as a former member."
        ),
    )
    closed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the task was marked completed. Cleared on reopening, so it "
            "always describes the completion the task currently stands on."
        ),
    )

    requires_review = models.BooleanField(
        default=False,
        help_text=(
            "When set, each assignee's submission has to be signed off by one "
            "of the task's reviewers before the task counts as done. The form "
            "keeps this in step with ``reviewers``, which is what the review "
            "rules actually read — see ``needs_review``."
        ),
    )

    auto_assign_new = models.CharField(
        max_length=16,
        choices=AutoAssignNew.choices,
        default=AutoAssignNew.NONE,
    )
    anonymous = models.BooleanField(default=False)

    contribution_type = models.CharField(
        max_length=32,
        choices=ContributionType.choices,
        default=ContributionType.OTHER,
        help_text=(
            "CRediT-aligned contribution type an assignee's credit is "
            "recorded under when a Lead verifies their part — unless a Lead "
            "gave that assignee a type of their own "
            "(``AssigneeContributionType``)."
        ),
    )

    github_url = models.URLField(
        max_length=500,
        blank=True,
        default="",
        help_text="Optional link to a GitHub issue or pull request for this task.",
    )

    is_priority = models.BooleanField(
        default=False,
        help_text="Flags this task as high priority (shown with a colored indicator).",
    )

    class Meta:
        ordering = ["container_task_order", "id"]
        indexes = [
            models.Index(fields=["project"]),
            models.Index(fields=["container"]),
            models.Index(fields=["milestone"]),
            models.Index(fields=["status"]),
            models.Index(fields=["is_priority"]),
        ]

    def __str__(self) -> str:
        return self.name

    # -- Completion helpers --------------------------------------------------
    def _current_logs_by_member(self) -> dict[int, "TaskLog"]:
        """Map each assignee id to their most-recent TaskLog (the current one).

        ``task_logs`` is ordered newest-first, so the first log seen per
        member is the current one.
        """
        current: dict[int, TaskLog] = {}
        for log in self.task_logs.all():
            current.setdefault(log.assigned_to_id, log)
        return current

    @property
    def is_past_due(self) -> bool:
        """Still open past the task-level deadline — overdue or extended."""
        return (
            self.deadline is not None
            and self.status != TaskStatus.COMPLETED
            and self.deadline < timezone.now()
        )

    def _deadline_state(self) -> str | None:
        """``"overdue"``, ``"extended"`` or None — see :class:`TaskQuerySet`.

        Only a task already past its deadline costs any queries, and those
        read the prefetched ``task_logs`` / ``assignee_deadlines`` when a
        view has them. Not cached: a view may complete or reopen the task on
        the same instance it then renders.
        """
        if not self.is_past_due:
            return None
        now = timezone.now()
        current = self._current_logs_by_member()
        personal = self.personal_deadlines()
        pending = [
            m.id
            for m in self.assigned_members.all()
            if (log := current.get(m.id)) is None or log.status != TaskStatus.COMPLETED
        ]
        extended = bool(pending) and all(
            (d := personal.get(mid)) is not None and d >= now for mid in pending
        )
        return "extended" if extended else "overdue"

    @property
    def is_overdue(self) -> bool:
        """Still open past its deadline, and not carried by extensions.

        A finished task is never late. A task past its deadline whose every
        assignee still working holds an individual deadline yet to come is
        *extended* instead (:attr:`is_extended`). The same rule as
        ``Task.objects.overdue()``, which the dashboard counts, the board's
        Overdue filter and the project overview use; the browsing views flag
        it with ``_components/overdue_badge.html``.
        """
        return self._deadline_state() == "overdue"

    @property
    def is_extended(self) -> bool:
        """Past the task deadline, but every assignee not yet done has an
        individual deadline still to come — see :attr:`is_overdue`."""
        return self._deadline_state() == "extended"

    def personal_deadlines(self) -> dict:
        """Each member's individual deadline on this task, keyed by member id
        (members with only an individual start date are left out)."""
        return {
            d.member_id: d.deadline
            for d in self.assignee_deadlines.all()
            if d.deadline is not None
        }

    def personal_start_dates(self) -> dict:
        """Each member's individual start date on this task, keyed by member id."""
        return {
            d.member_id: d.start_date
            for d in self.assignee_deadlines.all()
            if d.start_date is not None
        }

    @property
    def tz(self):
        """The ``ZoneInfo`` governing the task's own dates."""
        return get_zone(self.timezone)

    def _own_entry(self, member):
        if member is None or member.pk is None:
            return None
        for entry in self.assignee_deadlines.all():
            if entry.member_id == member.pk:
                return entry
        return None

    def start_zone_for(self, member):
        """The zone governing ``member``'s start date: their own zone when
        they have their own start date, else the task's."""
        entry = self._own_entry(member)
        if entry is not None and entry.start_date is not None:
            return entry.tz
        return self.tz

    def deadline_zone_for(self, member):
        """The zone governing ``member``'s deadline: their own zone when they
        have their own deadline, else the task's."""
        entry = self._own_entry(member)
        if entry is not None and entry.deadline is not None:
            return entry.tz
        return self.tz

    def start_date_for(self, member):
        """``member``'s effective start date: their individual one, if a Lead
        set it, else the task's. None when neither exists."""
        if member is not None and member.pk is not None:
            personal = self.personal_start_dates().get(member.pk)
            if personal is not None:
                return personal
        return self.start_date

    def deadline_for(self, member):
        """``member``'s effective deadline: their individual one, if a Lead set
        it, else the task's. None when neither exists."""
        if member is not None and member.pk is not None:
            personal = self.personal_deadlines().get(member.pk)
            if personal is not None:
                return personal
        return self.deadline

    def personal_contribution_types(self) -> dict:
        """Each member's own contribution type on this task, keyed by member
        id — only members a Lead gave one (``AssigneeContributionType``)."""
        return {
            o.member_id: o.contribution_type
            for o in self.assignee_contribution_types.all()
        }

    def contribution_type_for(self, member) -> str:
        """``member``'s effective contribution type: their own, if a Lead set
        one, else the task's. This is what their credit is recorded under."""
        if member is not None and member.pk is not None:
            own = self.personal_contribution_types().get(member.pk)
            if own:
                return own
        return self.contribution_type

    @property
    def assignee_count(self) -> int:
        return self.assigned_members.count()

    @property
    def assignees_done_count(self) -> int:
        """Number of assignees whose current TaskLog is COMPLETED."""
        from apps.core.enums import TaskStatus

        current = self._current_logs_by_member()
        return sum(
            1
            for member in self.assigned_members.all()
            if (log := current.get(member.id)) is not None
            and log.status == TaskStatus.COMPLETED
        )

    @property
    def all_assignees_done(self) -> bool:
        """True when every assignee's current TaskLog is COMPLETED.

        Vacuously true when the task has no assignees.
        """
        return self.assignees_done_count >= self.assignee_count

    @property
    def assignees_reviewed_count(self) -> int:
        """Number of assignees whose current TaskLog carries a review."""
        current = self._current_logs_by_member()
        return sum(
            1
            for member in self.assigned_members.all()
            if (log := current.get(member.id)) is not None and log.is_reviewed
        )

    # -- Review helpers -----------------------------------------------------
    @property
    def needs_review(self) -> bool:
        """True when submissions on this task have to be signed off.

        Read off the reviewer set, because that is the fact of the matter:
        naming a reviewer is what puts a task under review, and taking the
        last one off lifts it again. A task can therefore never end up
        requiring a sign-off with nobody able to give it — which would be a
        task nobody could finish.

        ``requires_review`` is how the form says the same thing: ticking it
        reveals the reviewers field and asks for at least one, unticking it
        clears them. The flag is the intent; the reviewers are the rule.
        """
        return self.reviewers.exists()

    def is_reviewer(self, member) -> bool:
        """True when ``member`` is currently a reviewer on this task.

        Asked of the live ``reviewers`` set on every render rather than
        cached anywhere, so adding or removing a reviewer takes effect
        immediately — including for a member who is also an assignee.
        """
        if member is None or member.pk is None:
            return False
        return self.reviewers.filter(pk=member.pk).exists()


class TaskLog(models.Model):
    """Audit trail of task assignment and completion events."""

    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=32,
        choices=TaskStatus.choices,
        default=TaskStatus.NOT_COMPLETED,
    )
    metadata = models.JSONField(null=True, blank=True)
    completed_as = models.CharField(
        max_length=16,
        choices=CompletedAs.choices,
        default=CompletedAs.INDIVIDUAL,
    )
    assigned_to = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.CASCADE,
        related_name="task_logs_assigned",
    )
    completed_by = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="task_logs_completed",
    )
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="task_logs",
    )
    approved = models.BooleanField(null=True, blank=True)
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When this entry was marked COMPLETED. Distinct from created_at, "
            "which is when the entry was seeded on assignment. Null on entries "
            "that are not complete, and on ones completed before this field "
            "existed."
        ),
    )
    reviewed_by = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="task_logs_reviewed",
        help_text=(
            "The reviewer who signed this submission off. Kept as written "
            "even if they later stop being a reviewer on the task, so the "
            "record still names who reviewed it; nulled only if their "
            "membership row is deleted outright."
        ),
    )
    reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When a reviewer signed this submission off. Null means not "
            "reviewed: the pair is cleared when the reviewer undoes their "
            "review, and when the assignee reverts their own done mark."
        ),
    )
    verified_by = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="task_logs_verified",
        help_text=(
            "The Lead who verified this member's contribution, which is what "
            "credits them for it. Nulled only if the Lead's membership row is "
            "deleted outright; the credit they wrote stands."
        ),
    )
    verified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When a Lead verified this contribution. Null means not verified: "
            "the pair is cleared when the Lead withdraws the verification, and "
            "when the assignee reverts their own done mark."
        ),
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["task"]),
            models.Index(fields=["assigned_to"]),
            models.Index(fields=["completed_by"]),
            models.Index(fields=["reviewed_by"]),
            models.Index(fields=["verified_by"]),
        ]

    def __str__(self) -> str:
        return f"TaskLog #{self.pk} (task {self.task_id})"

    @property
    def is_reviewed(self) -> bool:
        """True when a reviewer has signed this submission off.

        ``reviewed_at`` is the flag: it is the field the review flow sets and
        clears together with ``reviewed_by``. Distinct from ``approved``,
        which is the Lead's ledger-writing verdict on the full detail page.
        """
        return self.reviewed_at is not None

    @property
    def is_verified(self) -> bool:
        """True when a Lead has verified this contribution.

        ``verified_at`` is the flag, set and cleared together with
        ``verified_by``, as ``reviewed_at`` is for reviews.
        """
        return self.verified_at is not None

    @property
    def is_reviewable(self) -> bool:
        """True when there is a submission here for a reviewer to act on."""
        return self.status == TaskStatus.COMPLETED


class AssigneeDeadline(models.Model):
    """Individual dates a Lead set for one assignee on a task: their own
    start date, their own deadline, or both.

    The task's own ``start_date`` and ``deadline`` still hold for the task and
    for every other assignee; each date set here replaces the task's for
    ``member`` alone, and may be earlier or later. A date left empty follows
    the task's. The row exists only while at least one of the two is set
    (``services.set_member_dates`` deletes it otherwise). It belongs to the
    assignment: taking the member off the task deletes it
    (``apps.tasks.signals``).
    """

    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, related_name="assignee_deadlines"
    )
    member = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.CASCADE,
        related_name="individual_deadlines",
    )
    start_date = models.DateTimeField(null=True, blank=True)
    deadline = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(
        max_length=64,
        blank=True,
        default="",
        validators=[validate_timezone],
        help_text=(
            "IANA timezone governing this member's own dates. Empty follows "
            "the task's zone. A date the member follows from the task stays "
            "in the task's zone."
        ),
    )
    reason = models.CharField(max_length=500, blank=True, default="")
    set_by = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The Lead who last set these dates.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task", "member"], name="unique_assignee_deadline"
            )
        ]
        indexes = [models.Index(fields=["member", "deadline"])]

    def __str__(self) -> str:
        return f"Individual dates for member {self.member_id} on task {self.task_id}"

    @property
    def tz(self):
        """The ``ZoneInfo`` governing this member's own dates: their own
        zone, else the task's."""
        return get_zone(self.timezone or self.task.timezone)


class AssigneeContributionType(models.Model):
    """A contribution type a Lead set for one assignee on a task.

    The task's own ``contribution_type`` still holds for the task and for
    every other assignee; the type set here replaces it for ``member`` alone,
    so their credit is recorded under it when their part is verified
    (``Task.contribution_type_for``). The row exists only while the member has
    a type of their own: putting them back on the task's deletes it
    (``services.set_member_contribution_type``). It belongs to the
    assignment: taking the member off the task deletes it
    (``apps.tasks.signals``).
    """

    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, related_name="assignee_contribution_types"
    )
    member = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.CASCADE,
        related_name="individual_contribution_types",
    )
    contribution_type = models.CharField(
        max_length=32,
        choices=ContributionType.choices,
    )
    set_by = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The Lead who last set this type.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task", "member"],
                name="unique_assignee_contribution_type",
            )
        ]

    def __str__(self) -> str:
        return (
            f"Contribution type {self.contribution_type} for member "
            f"{self.member_id} on task {self.task_id}"
        )


class ActivityEvent(models.TextChoices):
    CREATED = "CREATED", "Created"
    MEMBER_ADDED = "MEMBER_ADDED", "Member added"
    MEMBER_DONE = "MEMBER_DONE", "Member marked done"
    MEMBER_UNDONE = "MEMBER_UNDONE", "Member undid completion"


class TaskActivity(models.Model):
    """Chronological activity feed for a task."""

    created_at = models.DateTimeField(auto_now_add=True)
    event = models.CharField(max_length=32, choices=ActivityEvent.choices)
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    actor = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="task_activities_acted",
    )
    member = models.ForeignKey(
        "projects.ProjectMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="task_activities_affected",
    )

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["task"])]

    def __str__(self) -> str:
        return f"TaskActivity #{self.pk} ({self.event}) for task {self.task_id}"
