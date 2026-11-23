"""Projects app: Project and its membership / invitation / widget scaffolding."""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.enums import JoinRequestStatus, MemberPrivileges, ProjectStatus, WidgetSize


class ContributionTracking(models.TextChoices):
    NONE = "NONE", "None"
    CREDIT = "CREDIT", "Contributor Role Taxonomy (CRediT)"
    DKP = "DKP", "Dragon Kill Points"


class Project(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Metadata
    name = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    abstract = models.TextField(null=True, blank=True)
    keywords = models.TextField(null=True, blank=True)
    citation = models.TextField(null=True, blank=True)
    publisher = models.CharField(max_length=255, null=True, blank=True)
    identifier = models.CharField(max_length=255, null=True, blank=True)

    metadata = models.JSONField(null=True, blank=True)

    # Project form template (optional)
    form_version = models.ForeignKey(
        "forms_builder.FormVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="project_defaults",
    )

    status = models.CharField(
        max_length=32,
        choices=ProjectStatus.choices,
        default=ProjectStatus.PLANNING,
    )
    is_public = models.BooleanField(default=True)
    contribution_tracking = models.CharField(
        max_length=32,
        choices=ContributionTracking.choices,
        default=ContributionTracking.NONE,
    )
    public_summary = models.TextField(null=True, blank=True)

    discord_webhook_url = models.URLField(
        max_length=500,
        blank=True,
        default="",
        help_text="Optional Discord webhook URL. Task approvals will post to this channel.",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status"])]

    def __str__(self) -> str:
        return self.name

    @property
    def tracks_credit(self) -> bool:
        """True when this project attributes contributions using CRediT."""
        return self.contribution_tracking == ContributionTracking.CREDIT


class ProjectMember(models.Model):
    """A membership (individual or team-shaped) on a project."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="project_members",
    )
    name = models.CharField(max_length=255, null=True, blank=True)
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="project_memberships",
        blank=True,
    )
    deleted = models.BooleanField(default=False)
    tags = models.JSONField(null=True, blank=True)
    former_team_ids = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["project"])]

    def __str__(self) -> str:
        return self.name or f"ProjectMember #{self.pk}"

    @property
    def display_name(self) -> str:
        """Human-friendly name as ``"First Name Last Name"``.

        Derived from the linked user's ``given_name``/``family_name``,
        falling back to the user's ``name``/email and finally to the
        membership's own ``name``.
        """

        user = self.users.order_by("given_name", "family_name").first()
        if user is not None:
            full = f"{(user.given_name or '').strip()} {(user.family_name or '').strip()}".strip()
            if full:
                return full
            if user.name:
                return user.name
            return user.email
        return self.name or f"ProjectMember #{self.pk}"


class ProjectPrivilege(models.Model):
    """Per-user privilege assignment for a project."""

    created_at = models.DateTimeField(auto_now_add=True)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="project_privileges",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="privileges",
    )
    privilege = models.CharField(
        max_length=32,
        choices=MemberPrivileges.choices,
        default=MemberPrivileges.PROJECT_MANAGER,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["project"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} / {self.project_id} ({self.privilege})"


class InvitationQuerySet(models.QuerySet):
    def addressed_to(self, user):
        """Invitations meant for ``user`` (see ``Invitation.is_addressed_to``)."""
        q = models.Q(invited_user_id=user.pk)
        email = (getattr(user, "email", "") or "").strip()
        if email:
            q |= models.Q(invited_user__isnull=True, email__iexact=email)
        return self.filter(q)


class Invitation(models.Model):
    """Outstanding invitation to join a project."""

    created_at = models.DateTimeField(auto_now_add=True)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    invitation_code = models.CharField(max_length=255)
    roles = models.ManyToManyField(
        "roles.Role",
        blank=True,
        related_name="invitations",
    )
    email = models.EmailField(max_length=254)
    # Pre-resolved invitee, when a PM picks an existing user rather than
    # typing a free-text address. ``email`` is still populated (from the
    # user's account) so the accept/decline flow keeps working unchanged.
    invited_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_invitations",
    )
    privilege = models.CharField(
        max_length=32,
        choices=MemberPrivileges.choices,
        default=MemberPrivileges.CONTRIBUTOR,
    )
    added_by = models.CharField(max_length=255)
    tags = models.JSONField(null=True, blank=True)

    # When reassigning a former member's workload to a new invitee.
    reassignment_for = models.ForeignKey(
        ProjectMember,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reassignment_invitations",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project"]),
            models.Index(fields=["email"]),
        ]

    objects = InvitationQuerySet.as_manager()

    def __str__(self) -> str:
        return f"Invitation to {self.email} for project {self.project_id}"

    def is_addressed_to(self, user) -> bool:
        """Whether ``user`` is the person this invitation was sent to.

        An invitation to a picked account belongs to that account only, even
        if its email address later changes or is reused by someone else. An
        invitation typed as an email address belongs to whoever holds that
        address (case-insensitive).
        """
        if not getattr(user, "is_authenticated", False):
            return False
        if self.invited_user_id is not None:
            return self.invited_user_id == user.pk
        email = (user.email or "").strip().lower()
        return bool(email) and (self.email or "").lower() == email


class Element(models.Model):
    """A structural element within a project (e.g. a sub-study or component)."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="elements",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)

    # Prisma models Element parent/child as a many-to-many self relation.
    parents = models.ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="children",
    )

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["project"])]

    def __str__(self) -> str:
        return self.name


class ProjectWidget(models.Model):
    """Per-user, per-project dashboard widget configuration."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="project_widgets",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="widgets",
    )
    type = models.CharField(max_length=64)
    show = models.BooleanField(default=True)
    position = models.IntegerField()
    size = models.CharField(
        max_length=16,
        choices=WidgetSize.choices,
        default=WidgetSize.MEDIUM,
    )
    # Prisma declares this as MemberPrivileges[]; store as JSON list of enum values.
    privilege = models.JSONField(
        default=list,
        help_text="List of MemberPrivileges values that can see this widget.",
    )

    class Meta:
        ordering = ["position"]
        unique_together = [["user", "project", "type"]]
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["project"]),
        ]

    def __str__(self) -> str:
        return f"{self.type} widget (u={self.user_id}, p={self.project_id})"


class JoinRequest(models.Model):
    """A user's request to join a project at a specific privilege level."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="join_requests",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="project_join_requests",
    )
    requested_privilege = models.CharField(
        max_length=32,
        choices=MemberPrivileges.choices,
        default=MemberPrivileges.CONTRIBUTOR,
    )
    message = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=JoinRequestStatus.choices,
        default=JoinRequestStatus.PENDING,
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decided_join_requests",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "user"],
                condition=models.Q(status="PENDING"),
                name="uniq_pending_join_request_per_user_project",
            ),
        ]
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["user", "status"]),
        ]

    def __str__(self) -> str:
        return f"JoinRequest #{self.pk} ({self.user_id} → {self.project_id}, {self.status})"
