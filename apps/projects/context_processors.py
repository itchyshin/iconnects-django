from __future__ import annotations

from apps.core.enums import MemberPrivileges


def sidebar_counts(request):
    if not request.user.is_authenticated:
        return {}

    from django.db.models import Exists, OuterRef, Q, Subquery

    from .models import Invitation, JoinRequest, ProjectMember, ProjectPrivilege
    from apps.core.enums import JoinRequestStatus, TaskStatus
    from apps.tasks.models import Task, TaskLog

    pending_invitations_count = Invitation.objects.addressed_to(request.user).count()

    pm_project_ids = ProjectPrivilege.objects.filter(
        user=request.user,
        privilege=MemberPrivileges.PROJECT_MANAGER,
    ).values_list("project_id", flat=True)

    incoming_join_requests_count = JoinRequest.objects.filter(
        project_id__in=pm_project_ids,
        status=JoinRequestStatus.PENDING,
    ).count()

    my_join_requests_count = JoinRequest.objects.filter(
        user=request.user,
        status=JoinRequestStatus.PENDING,
    ).count()

    # Requests sent *to* the user (invitations, incoming join requests) --
    # excludes join requests the user sent out themselves.
    requests_total = pending_invitations_count + incoming_join_requests_count

    # Active (not-completed) task count -- same definition as the Tasks
    # page's default (untoggled) total: tasks assigned to or reviewed by the
    # user, counted once each even if both (matches the dashboard's
    # active_task_count + review_task_count, minus that double-count).
    user_member_qs = ProjectMember.objects.filter(users=request.user)
    latest_log_status = Subquery(
        TaskLog.objects.filter(task=OuterRef("pk"), assigned_to__in=user_member_qs)
        .order_by("-created_at")
        .values("status")[:1]
    )
    active_tasks_total = (
        Task.objects.filter(
            Q(assigned_members__users=request.user) | Q(reviewers__users=request.user),
            status=TaskStatus.NOT_COMPLETED,
        )
        .annotate(
            _user_log_status=latest_log_status,
            _is_assigned=Exists(user_member_qs.filter(assigned_tasks=OuterRef("pk"))),
        )
        # NULL-safe: a task with no TaskLog yet has _user_log_status=NULL, and
        # `.exclude(_user_log_status=COMPLETED)` would silently drop it too.
        # Only applies to tasks the user is assigned to; a review-only task
        # is judged by the task's own status alone.
        .filter(
            Q(_is_assigned=False)
            | Q(_user_log_status__isnull=True)
            | ~Q(_user_log_status=TaskStatus.COMPLETED)
        )
        .distinct()
        .count()
    )

    return {
        "sidebar_pending_invitations": pending_invitations_count,
        "sidebar_incoming_join_requests": incoming_join_requests_count,
        "sidebar_my_join_requests": my_join_requests_count,
        "sidebar_requests_total": requests_total,
        "sidebar_active_tasks_total": active_tasks_total,
    }
