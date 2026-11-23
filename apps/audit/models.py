"""AuditLog: an append-only record of security- and governance-relevant events.

Per Ultra Plan §7:

    Append-only ``AuditLog`` table: ``(id, actor_id, action, entity_type,
    entity_id, timestamp, metadata_json)``. Log: login, logout, account
    creation, role change, project creation/deletion, task lifecycle
    transitions, member add/remove, admin actions. Admin can read audit
    log; users can read their own audit entries (GDPR-aligned).

The model therefore:
- is append-only (``save`` refuses updates, ``delete`` raises);
- uses ``SET_NULL`` on ``actor`` / ``project`` so the log survives if those
  rows are ever hard-deleted — an audit log that disappears when an account
  is removed is useless in exactly the cases it matters most;
- stores ``entity_type`` / ``entity_id`` as plain fields (not GenericFK) to
  keep queries cheap; traversal from AuditLog → entity is rare.
"""

from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError, models


class AuditAction(models.TextChoices):
    # ---- Auth ---------------------------------------------------------------
    USER_SIGNUP = "user.signup", "User signed up"
    SIGNUP_ATTEMPTED = "user.signup_attempted", "Signup attempted"
    USER_LOGIN = "user.login", "User logged in"
    USER_LOGIN_FAILED = "user.login_failed", "Login failed"
    USER_LOGOUT = "user.logout", "User logged out"
    PASSWORD_RESET_REQUESTED = "user.password_reset_requested", "Password reset requested"
    PASSWORD_RESET_ATTEMPTED = "user.password_reset_attempted", "Password reset attempted"
    PASSWORD_RESET_COMPLETED = "user.password_reset_completed", "Password reset completed"
    PASSWORD_CHANGED = "user.password_changed", "Password changed"
    PROFILE_UPDATED = "user.profile_updated", "Profile updated"
    EMAIL_CONFIRMED = "user.email_confirmed", "Email address confirmed"
    EMAIL_CONFIRMATION_RESEND_ATTEMPTED = (
        "user.email_confirmation_resend_attempted",
        "Email confirmation resend attempted",
    )

    # ---- Projects -----------------------------------------------------------
    PROJECT_CREATED = "project.created", "Project created"
    PROJECT_UPDATED = "project.updated", "Project updated"
    PROJECT_DELETED = "project.deleted", "Project deleted"
    MAILING_LIST_MESSAGE_SENT = (
        "project.mailing_list_message_sent",
        "Mailing-list message sent",
    )

    # ---- Membership / invitations ------------------------------------------
    INVITATION_SENT = "invitation.sent", "Invitation sent"
    INVITATION_RESENT = "invitation.resent", "Invitation resent"
    INVITATION_ACCEPTED = "invitation.accepted", "Invitation accepted"
    INVITATION_DECLINED = "invitation.declined", "Invitation declined"
    JOIN_REQUEST_CREATED = "join_request.created", "Join request submitted"
    JOIN_REQUEST_APPROVED = "join_request.approved", "Join request approved"
    JOIN_REQUEST_DECLINED = "join_request.declined", "Join request declined"
    JOIN_REQUEST_CANCELLED = "join_request.cancelled", "Join request cancelled"
    MEMBER_ADDED = "member.added", "Member added"
    MEMBER_REMOVED = "member.removed", "Member removed"
    MEMBER_PRIVILEGE_CHANGED = "member.privilege_changed", "Member privilege changed"

    # ---- Roles --------------------------------------------------------------
    ROLE_CREATED = "role.created", "Role created"
    ROLE_DELETED = "role.deleted", "Role deleted"
    ROLE_ASSIGNED = "role.assigned", "Role assigned to member"
    ROLE_UNASSIGNED = "role.unassigned", "Role unassigned from member"

    # ---- Tasks --------------------------------------------------------------
    TASK_CREATED = "task.created", "Task created"
    TASK_UPDATED = "task.updated", "Task updated"
    TASK_DELETED = "task.deleted", "Task deleted"
    TASK_COMPLETED = "task.completed", "Task marked complete"
    TASK_APPROVED = "task.approved", "Task completion approved"
    TASK_REJECTED = "task.rejected", "Task completion rejected"
    TASK_REVIEWED = "task.reviewed", "Task submission reviewed"
    TASK_REVIEW_CLEARED = "task.review_cleared", "Task submission review cleared"
    TASK_PART_DONE = "task.part_done", "Task part marked done"
    TASK_PART_UNDONE = "task.part_undone", "Task part done mark undone"
    TASK_PART_VERIFIED = "task.part_verified", "Task contribution verified"
    TASK_PART_UNVERIFIED = "task.part_unverified", "Task contribution verification withdrawn"
    TASK_DEADLINE_SET = "task.deadline_set", "Individual task dates set"
    TASK_DEADLINE_CLEARED = "task.deadline_cleared", "Individual task dates removed"
    TASK_MEMBER_TYPE_SET = (
        "task.member_type_set",
        "Individual contribution type set",
    )
    TASK_MEMBER_TYPE_CLEARED = (
        "task.member_type_cleared",
        "Individual contribution type removed",
    )

    # ---- Wiki ---------------------------------------------------------------
    WIKI_PAGE_CREATED = "wiki.page_created", "Wiki page created"
    WIKI_PAGE_UPDATED = "wiki.page_updated", "Wiki page edited"
    WIKI_PAGE_DELETED = "wiki.page_deleted", "Wiki page deleted"
    WIKI_PAGE_RESTORED = "wiki.page_restored", "Wiki page revision restored"

    # ---- Contributions ------------------------------------------------------
    CONTRIBUTION_ADDED = "contribution.added", "Contribution added by hand"
    CONTRIBUTION_UPDATED = "contribution.updated", "Contribution corrected"
    CONTRIBUTION_REMOVED = "contribution.removed", "Contribution withdrawn"

    # ---- Admin --------------------------------------------------------------
    ACCOUNT_SUSPENDED = "admin.account_suspended", "Account suspended"
    ACCOUNT_REACTIVATED = "admin.account_reactivated", "Account reactivated"

    # ---- Automated lockout --------------------------------------------------
    ACCOUNT_LOCKED_OUT = "security.account_locked_out", "Account locked out (too many failed logins)"
    ACCOUNT_UNLOCKED = "security.account_unlocked", "Account unlocked (email confirmation)"


class AuditLog(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_actions",
    )
    action = models.CharField(max_length=64, choices=AuditAction.choices)
    entity_type = models.CharField(max_length=64, blank=True, default="")
    entity_id = models.PositiveIntegerField(null=True, blank=True)
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_log",
    )
    metadata = models.JSONField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp", "-id"]
        indexes = [
            models.Index(fields=["actor"]),
            models.Index(fields=["action"]),
            models.Index(fields=["entity_type", "entity_id"]),
            models.Index(fields=["project"]),
            models.Index(fields=["-timestamp"]),
        ]

    def __str__(self) -> str:
        return f"AuditLog #{self.pk} ({self.action})"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise IntegrityError("AuditLog is append-only; existing rows cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise IntegrityError("AuditLog is append-only; rows cannot be deleted.")
