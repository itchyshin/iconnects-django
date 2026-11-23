"""Server-rendered views for the projects app (Phase 3B)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)

from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET

from apps.audit.services import AuditAction, log_action
from apps.contributions.services import (
    get_project_contributions,
    get_project_contributions_matrix,
)
from apps.core.enums import MemberPrivileges


_SORT_LABELS = {
    "newest": None,
    "oldest": "oldest first",
    "name_asc": "name A–Z",
    "name_desc": "name Z–A",
}

_STATUS_LABELS = {
    "PLANNING": "planning",
    "ACTIVE": "active",
    "PAUSED": "paused",
    "COMPLETED": "completed",
    "INACTIVE": "inactive",
}

_ROLE_LABELS = {
    "PROJECT_MANAGER": "lead",
    "CONTRIBUTOR": "member",
}

def _build_results_summary(count, q, status, sort, visibility=None, role=None):
    """Return a human-readable results summary string, or None if no filters active."""
    parts = []
    if role:
        parts.append(_ROLE_LABELS.get(role, role.lower()))
    if visibility:
        parts.append(visibility)
    if status:
        parts.append(_STATUS_LABELS.get(status, status.lower()))
    if parts:
        desc = " ".join(parts) + " projects"
    else:
        desc = "projects"
    if q:
        desc += f' matching “{q}”'
    sort_label = _SORT_LABELS.get(sort)
    has_filters = bool(q or status or visibility or role or sort_label)
    if not has_filters:
        return None, count
    n = f"{count} result{'s' if count != 1 else ''}"
    summary = f"{n} for {desc}"
    if sort_label:
        summary += f", sorted by {sort_label}"
    return summary, count

from apps.core.enums import JoinRequestStatus

from .forms import (
    InvitationForm,
    JoinRequestDeclineForm,
    JoinRequestForm,
    MemberPrivilegeForm,
    ProjectForm,
)
from .mixins import (
    ProjectContextMixin,
    ProjectManagerRequiredMixin,
    ProjectMemberRequiredMixin,
)
from .models import (
    Invitation,
    JoinRequest,
    Project,
    ProjectMember,
    ProjectPrivilege,
)
from .services import (
    JoinRequestError,
    LastProjectManagerError,
    accept_invitation,
    approve_join_request,
    assert_pm_self_action_allowed,
    cancel_join_request,
    create_invitation,
    create_join_request,
    create_project_with_owner,
    decline_invitation,
    decline_join_request,
    is_last_project_manager,
    resend_invitation,
)


# ---------------------------------------------------------------------------
# Project list / detail / CRUD
# ---------------------------------------------------------------------------


class DashboardView(LoginRequiredMixin, TemplateView):
    """Personal dashboard — active tasks, overdue, pending approvals, stats."""

    template_name = "dashboard.html"

    # The dashboard shows a preview of each list, not the whole thing — the
    # Tasks and Requests pages are where the full, filterable lists live.
    ACTIVE_TASK_LIMIT = 5
    REQUEST_LIMIT = 5

    # Controls on the "Active tasks and requests" section. "" is no type
    # filter (both groups); "date" sort is the default reading order --
    # tasks by how soon they are due, requests newest first.
    #
    # Each type narrows to one group and, for the finer ones, to a slice of
    # it. Which map a type appears in is what decides whether that group
    # renders at all; the value is the noun its "All N ..." link and its
    # empty line use, so both say what was actually filtered to.
    _TASK_TYPE_LABELS = {
        "": "tasks",
        "tasks": "tasks",
        "overdue": "overdue tasks",
        "review": "tasks to review",
    }
    _REQUEST_TYPE_LABELS = {
        "": "requests",
        "requests": "requests",
        "join_requests": "join requests",
        "invitations": "invitations",
    }
    _TYPES = tuple(
        t for t in (*_TASK_TYPE_LABELS, *_REQUEST_TYPE_LABELS) if t
    )
    # Both sorts key off the date each row already shows -- a task's
    # deadline, a request's age -- so the order always matches what is on
    # screen. A task with no deadline has nothing to compare, so it sorts
    # last either way rather than leading the "oldest" list.
    #
    # "oldest" is the default: this list is capped at a handful of rows, and
    # newest-first pushes the overdue and the imminent off the end of it --
    # exactly the rows the dashboard exists to surface.
    _SORTS = ("oldest", "newest")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        from django.db.models import Count, Exists, F, OuterRef, Subquery, Q
        from django.utils import timezone
        from apps.core.enums import TaskStatus, MemberPrivileges
        from apps.tasks.models import Task, TaskLog
        from apps.notifications.models import Notification

        # --- "Active tasks and requests" controls --------------------------
        current_type = self.request.GET.get("type", "")
        if current_type not in self._TYPES:
            current_type = ""
        current_sort = self.request.GET.get("sort", "")
        if current_sort not in self._SORTS:
            current_sort = "oldest"
        context["current_type"] = current_type
        context["current_sort"] = current_sort
        shows_tasks = current_type in self._TASK_TYPE_LABELS
        shows_requests = current_type in self._REQUEST_TYPE_LABELS
        context["shows_tasks"] = shows_tasks
        context["shows_requests"] = shows_requests
        context["active_tasks_label"] = self._TASK_TYPE_LABELS.get(current_type, "tasks")
        context["incoming_requests_label"] = self._REQUEST_TYPE_LABELS.get(
            current_type, "requests"
        )

        # --- Privilege map -------------------------------------------------
        privs = ProjectPrivilege.objects.filter(user=user).select_related("project")
        privs_map = {p.project_id: p.privilege for p in privs}
        pm_project_ids = [
            pid for pid, priv in privs_map.items()
            if priv == MemberPrivileges.PROJECT_MANAGER
        ]

        # --- Projects ------------------------------------------------------
        user_projects = (
            Project.objects.filter(privileges__user=user)
            .distinct()
            .order_by("-updated_at")
        )
        context["project_count"] = user_projects.count()
        context["recent_projects"] = list(user_projects)

        # --- Task querysets ------------------------------------------------
        user_member_qs = ProjectMember.objects.filter(users=user)

        latest_log_status = Subquery(
            TaskLog.objects.filter(
                task=OuterRef("pk"),
                assigned_to__in=user_member_qs,
            )
            .order_by("-created_at")
            .values("status")[:1]
        )

        assigned_tasks = (
            Task.objects.filter(
                assigned_members__users=user,
                status=TaskStatus.NOT_COMPLETED,
            )
            .annotate(_user_log_status=latest_log_status)
            # NULL-safe: a task with no TaskLog yet has _user_log_status=NULL,
            # and `exclude(_user_log_status=COMPLETED)` would silently drop it
            # too (NOT (NULL = COMPLETED) is NULL/false in SQL), so it must be
            # explicitly kept.
            .filter(Q(_user_log_status__isnull=True) | ~Q(_user_log_status=TaskStatus.COMPLETED))
            .select_related("project", "container")
            .order_by("deadline", "id")
            .distinct()
        )

        context["active_task_count"] = assigned_tasks.count()

        # --- Tasks assigned to the user to review --------------------------
        context["review_task_count"] = (
            Task.objects.filter(reviewers__users=user, status=TaskStatus.NOT_COMPLETED)
            .distinct()
            .count()
        )

        # Every task still open for the user, whether they are assigned to it
        # or reviewing it -- the source of both the "Tasks" tile count and the
        # active-task list below it. A task the user is both assigned to and
        # reviewing appears once, matching the Tasks page's and sidebar's
        # merged total (summing active_task_count + review_task_count would
        # double-count those).
        active_tasks_qs = (
            Task.objects.filter(
                Q(assigned_members__users=user) | Q(reviewers__users=user),
                status=TaskStatus.NOT_COMPLETED,
            )
            # my_deadline: the user's individual deadline where a Lead set
            # one, else the task's -- what "overdue" and the date on each
            # row mean for this user.
            .with_my_deadline(user)
            .annotate(
                user_log_status=latest_log_status,
                is_assigned=Exists(user_member_qs.filter(assigned_tasks=OuterRef("pk"))),
                is_reviewing=Exists(user_member_qs.filter(reviewed_tasks=OuterRef("pk"))),
            )
            .filter(
                Q(is_assigned=False)
                | Q(user_log_status__isnull=True)
                | ~Q(user_log_status=TaskStatus.COMPLETED)
            )
            .distinct()
        )

        context["active_tasks_total"] = active_tasks_qs.count()
        now = timezone.now()
        context["now"] = now

        # The finer task types narrow the same queryset; the count follows,
        # so the section's badge and the "All N ..." link below the rows
        # both describe the filtered set rather than the whole one.
        if current_type == "overdue":
            matching_tasks_qs = active_tasks_qs.filter(my_deadline__lt=now)
        elif current_type == "review":
            matching_tasks_qs = active_tasks_qs.filter(is_reviewing=True)
        else:
            matching_tasks_qs = active_tasks_qs

        # nulls_last keeps undated tasks at the end in both directions
        # (SQLite orders NULLs before everything by default, Postgres after).
        if current_sort == "newest":
            task_order = (F("my_deadline").desc(nulls_last=True), "-created_at", "id")
        else:
            task_order = (F("my_deadline").asc(nulls_last=True), "created_at", "id")

        if not shows_tasks:
            context["active_tasks"] = []
            context["active_tasks_match_count"] = 0
        else:
            context["active_tasks"] = list(
                matching_tasks_qs.select_related("project", "container")
                .prefetch_related("assigned_members")
                .order_by(*task_order)[: self.ACTIVE_TASK_LIMIT]
            )
            context["active_tasks_match_count"] = (
                matching_tasks_qs.count()
                if matching_tasks_qs is not active_tasks_qs
                else context["active_tasks_total"]
            )

        # --- Pending approvals (PM only) -----------------------------------
        if pm_project_ids:
            pending_logs = list(
                TaskLog.objects.filter(
                    status=TaskStatus.COMPLETED,
                    approved__isnull=True,
                    task__project_id__in=pm_project_ids,
                )
                .select_related("task", "task__project", "assigned_to")
                .order_by("-created_at")
            )
        else:
            pending_logs = []
        context["pending_logs"] = pending_logs
        context["pending_count"] = len(pending_logs)
        context["is_pm_anywhere"] = bool(pm_project_ids)

        # --- Pending join requests (PM only) ----------------------------------
        if pm_project_ids:
            pending_join_requests = list(
                JoinRequest.objects.filter(
                    project_id__in=pm_project_ids,
                    status=JoinRequestStatus.PENDING,
                )
                .select_related("user", "project")
                .order_by("-created_at")
            )
        else:
            pending_join_requests = []
        context["pending_join_requests"] = pending_join_requests
        context["pending_join_request_count"] = len(pending_join_requests)

        # --- Invitation requests (invitations addressed to the current user) ---
        invitation_requests = list(
            Invitation.objects.addressed_to(user)
            .select_related("project")
            .order_by("-created_at")
        )
        context["invitation_requests"] = invitation_requests
        context["invitation_request_count"] = len(invitation_requests)

        # --- Incoming requests list -------------------------------------------
        # Join requests awaiting the user's review plus invitations addressed
        # to them, merged and tagged by kind so a single list can render both
        # -- the same item shape the Requests page's "Incoming" tab uses.
        incoming_requests = [
            {"kind": "join_request", "obj": jr} for jr in pending_join_requests
        ] + [{"kind": "invitation", "obj": inv} for inv in invitation_requests]
        incoming_requests.sort(
            key=lambda item: item["obj"].created_at,
            reverse=current_sort == "newest",
        )
        context["incoming_request_count"] = len(incoming_requests)

        if current_type == "join_requests":
            matching_requests = [i for i in incoming_requests if i["kind"] == "join_request"]
        elif current_type == "invitations":
            matching_requests = [i for i in incoming_requests if i["kind"] == "invitation"]
        else:
            matching_requests = incoming_requests

        if not shows_requests:
            matching_requests = []
        context["incoming_request_match_count"] = len(matching_requests)
        context["incoming_requests"] = matching_requests[: self.REQUEST_LIMIT]

        # --- Notifications -------------------------------------------------
        context["recent_notifications"] = list(
            Notification.objects.unread_for_user(user)
            .select_related("project")
            .order_by("-created_at")[:8]
        )
        context["unread_notification_count"] = (
            Notification.objects.unread_for_user(user).count()
        )

        return context


class PublicProjectListView(ListView):
    """Grid of public projects -- accessible to anonymous visitors."""

    model = Project
    template_name = "projects/list.html"
    context_object_name = "projects"
    paginate_by = 12

    _SORT_MAP = {
        "newest": "-created_at",
        "oldest": "created_at",
        "name_asc": "name",
        "name_desc": "-name",
    }

    def get_queryset(self):
        from django.db.models import Q
        qs = Project.objects.filter(is_public=True)
        status = self.request.GET.get("status", "")
        if status:
            qs = qs.filter(status=status)
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q) | Q(abstract__icontains=q))
        sort = self.request.GET.get("sort", "newest")
        order = self._SORT_MAP.get(sort, "-created_at")
        return qs.order_by(order)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_q"] = self.request.GET.get("q", "")
        context["current_sort"] = self.request.GET.get("sort", "newest")
        context["current_status"] = self.request.GET.get("status", "")
        from apps.core.enums import ProjectStatus
        _STATUS_ORDER = ["PLANNING", "ACTIVE", "PAUSED", "COMPLETED", "INACTIVE"]
        context["project_statuses"] = [
            c for key in _STATUS_ORDER for c in ProjectStatus.choices if c[0] == key
        ]
        params = self.request.GET.copy()
        params.pop("page", None)
        context["filter_params"] = params.urlencode()

        total = context["paginator"].count
        context["results_summary"], _ = _build_results_summary(
            total,
            context["current_q"],
            context["current_status"],
            context["current_sort"],
        )

        # Build project_rows with member privileges for member name display
        pm = MemberPrivileges.PROJECT_MANAGER.value
        project_ids = [p.id for p in context["projects"]]
        all_member_privs = (
            ProjectPrivilege.objects
            .filter(project_id__in=project_ids)
            .select_related("user")
        )
        members_by_project: dict[int, list] = {}
        for mp in all_member_privs:
            members_by_project.setdefault(mp.project_id, []).append(mp)
        for pid in members_by_project:
            members_by_project[pid].sort(key=lambda p: (0 if p.privilege == pm else 1))

        # The viewer's own standing on each project, read out of the rows
        # already fetched above rather than a query of its own. Anonymous
        # visitors reach this page too, and belong to nothing.
        my_privilege_by_project: dict[int, str] = {}
        if self.request.user.is_authenticated:
            for mp in all_member_privs:
                if mp.user_id == self.request.user.pk:
                    # ProjectPrivilege orders newest first, so the first row
                    # seen for a project is the one _get_user_privilege would
                    # have picked.
                    my_privilege_by_project.setdefault(mp.project_id, mp.privilege)

        context["project_rows"] = [
            {
                "project": p,
                "members": members_by_project.get(p.id, []),
                "my_privilege": my_privilege_by_project.get(p.id),
            }
            for p in context["projects"]
        ]
        context["total_project_count"] = Project.objects.filter(is_public=True).count()
        return context


class MyProjectListView(LoginRequiredMixin, ListView):
    """Projects the current user is a member of."""

    model = Project
    template_name = "projects/my_list.html"
    context_object_name = "projects"
    paginate_by = 12

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["projects/partials/my_list_results.html"]
        return [self.template_name]

    _SORT_MAP = {
        "newest": "-created_at",
        "oldest": "created_at",
        "name_asc": "name",
        "name_desc": "-name",
    }
    _VALID_PER_PAGE = {6, 12, 24, 48}

    def get_paginate_by(self, queryset):
        try:
            per_page = int(self.request.GET.get("per_page", self.paginate_by))
        except (TypeError, ValueError):
            per_page = self.paginate_by
        return per_page if per_page in self._VALID_PER_PAGE else self.paginate_by

    def get_queryset(self):
        from django.db.models import Q
        user = self.request.user
        role = self.request.GET.get("role", "")
        privilege_filter = {"privileges__user": user}
        if role in MemberPrivileges.values:
            privilege_filter["privileges__privilege"] = role
        qs = Project.objects.filter(**privilege_filter).distinct()
        status = self.request.GET.get("status", "")
        if status:
            qs = qs.filter(status=status)
        visibility = self.request.GET.get("visibility", "")
        if visibility == "public":
            qs = qs.filter(is_public=True)
        elif visibility == "private":
            qs = qs.filter(is_public=False)
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(description__icontains=q)
                | Q(abstract__icontains=q)
                | Q(privileges__user__given_name__icontains=q)
                | Q(privileges__user__family_name__icontains=q)
                | Q(privileges__user__email__icontains=q)
            ).distinct()
        sort = self.request.GET.get("sort", "newest")
        order = self._SORT_MAP.get(sort, "-created_at")
        return qs.order_by(order)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        # Fetch all member privileges for visible projects in one query.
        project_ids = [p.id for p in context["projects"]]
        all_member_privs = (
            ProjectPrivilege.objects
            .filter(project_id__in=project_ids)
            .select_related("user")
            # Leads (PROJECT_MANAGER) first, then contributors
            .order_by("project_id", "privilege")
        )
        members_by_project: dict[int, list] = {}
        for mp in all_member_privs:
            members_by_project.setdefault(mp.project_id, []).append(mp)
        # Re-sort so PROJECT_MANAGER always comes before CONTRIBUTOR
        pm = MemberPrivileges.PROJECT_MANAGER.value
        for pid in members_by_project:
            members_by_project[pid].sort(
                key=lambda p: (0 if p.privilege == pm else 1)
            )

        # The current user's own privilege on each project, for the "Role" pill.
        my_privileges = dict(
            ProjectPrivilege.objects.filter(project_id__in=project_ids, user=user)
            .values_list("project_id", "privilege")
        )

        # Templates can't do dict lookups by variable key, so pre-zip here.
        context["project_rows"] = [
            {
                "project": p,
                "members": members_by_project.get(p.id, []),
                "my_privilege": my_privileges.get(p.id),
            }
            for p in context["projects"]
        ]

        # Filter / sort context
        context["current_q"] = self.request.GET.get("q", "")
        context["current_sort"] = self.request.GET.get("sort", "newest")
        context["current_visibility"] = self.request.GET.get("visibility", "")
        context["current_status"] = self.request.GET.get("status", "")
        context["current_role"] = self.request.GET.get("role", "")
        context["current_per_page"] = self.get_paginate_by(self.get_queryset())
        context["valid_per_page"] = sorted(self._VALID_PER_PAGE)
        context["create_url"] = reverse("projects:create")
        params = self.request.GET.copy()
        params.pop("page", None)
        context["filter_params"] = params.urlencode()

        from apps.core.enums import ProjectStatus
        _STATUS_ORDER = ["PLANNING", "ACTIVE", "PAUSED", "COMPLETED", "INACTIVE"]
        context["project_statuses"] = [
            c for key in _STATUS_ORDER for c in ProjectStatus.choices if c[0] == key
        ]

        total = context["paginator"].count
        context["total_project_count"] = (
            Project.objects.filter(privileges__user=user).distinct().count()
        )
        context["results_summary"], _ = _build_results_summary(
            total,
            context["current_q"],
            context["current_status"],
            context["current_sort"],
            context["current_visibility"],
            context["current_role"],
        )

        return context


def _project_summary(project, now, *, is_lead: bool) -> dict:
    """Everything the Project Overview tab reports to a member, beyond its
    number cards.

    The tab answers two questions, one card each:

    - Recent activity: who did what lately, across tasks and the wiki.
    - Wiki: the pages a lead put on the Overview, settled ones first.

    Wiki pages follow their "Who can see it": a leads-only page is neither
    listed nor named in the activity for anyone but a lead. Each list is
    capped; the tabs hold the rest.
    """
    from django.utils.timesince import timesince

    from apps.core.enums import TaskStatus
    from apps.tasks.models import Task, TaskLog
    from apps.wiki.models import WikiPage, WikiRevision

    tasks = Task.objects.filter(project=project)
    readable_pages = WikiPage.objects.filter(project=project).visible_to(
        is_lead=is_lead, is_member=True
    )

    # ── Recent activity ──
    # Drawn from what the app records today: a task's creation and its
    # completion live on the task, each assignee's done mark, its review and
    # a Lead's verification on their current TaskLog, and every wiki save is a
    # revision.
    events = []
    for t in tasks.select_related("created_by").order_by("-created_at")[:8]:
        events.append({
            "when": t.created_at,
            "who": t.created_by.display_name if t.created_by else "Someone",
            "verb": "created", "kind": "task", "task": t,
        })
    for log in (
        TaskLog.objects.filter(
            task__project=project,
            status=TaskStatus.COMPLETED,
            completed_at__isnull=False,
        )
        .select_related("task", "assigned_to")
        .order_by("-completed_at")[:8]
    ):
        events.append({
            "when": log.completed_at,
            "who": log.assigned_to.display_name,
            "verb": "marked their part done on", "kind": "task", "task": log.task,
        })
    for log in (
        TaskLog.objects.filter(task__project=project, reviewed_at__isnull=False)
        .select_related("task", "assigned_to", "reviewed_by")
        .order_by("-reviewed_at")[:8]
    ):
        own = log.reviewed_by_id == log.assigned_to_id
        part = "their own part" if own else f"{log.assigned_to.display_name}'s part"
        events.append({
            "when": log.reviewed_at,
            "who": log.reviewed_by.display_name if log.reviewed_by else "Someone",
            "verb": f"reviewed {part} on", "kind": "task", "task": log.task,
        })
    for log in (
        TaskLog.objects.filter(task__project=project, verified_at__isnull=False)
        .select_related("task", "assigned_to", "verified_by")
        .order_by("-verified_at")[:8]
    ):
        own = log.verified_by_id == log.assigned_to_id
        part = "their own part" if own else f"{log.assigned_to.display_name}'s part"
        events.append({
            "when": log.verified_at,
            "who": log.verified_by.display_name if log.verified_by else "Someone",
            "verb": f"verified {part} on", "kind": "task", "task": log.task,
        })
    for t in (
        tasks.filter(closed_at__isnull=False)
        .select_related("closed_by")
        .order_by("-closed_at")[:8]
    ):
        # Only a Lead completes a task, so the line names whoever did.
        events.append({
            "when": t.closed_at,
            "who": t.closed_by.display_name if t.closed_by else "A lead",
            "verb": "completed", "kind": "task", "task": t,
        })
    for r in (
        WikiRevision.objects.filter(page__in=readable_pages)
        .select_related("page", "editor")
        .order_by("-created_at")[:8]
    ):
        events.append({
            "when": r.created_at,
            "who": r.editor.display_name if r.editor else "Someone",
            "verb": "created the wiki page" if r.number == 1 else "edited the wiki page",
            "kind": "wiki", "page": r.page,
        })
    events.sort(key=lambda e: e["when"], reverse=True)
    for e in events:
        # One unit is enough for a feed ("4 hours ago", not "4 hours, 39
        # minutes ago"); the last minute reads "just now", not "0 minutes".
        e["ago"] = (
            "just now"
            if (now - e["when"]).total_seconds() < 60
            else f"{timesince(e['when'], now, depth=1)} ago"
        )

    # ── Wiki ──
    # Every page a lead has put on the Overview (that the viewer may read),
    # the most recently updated first.
    wiki = {
        "pages": list(
            readable_pages.filter(show_on_overview=True).order_by("-updated_at", "-id")
        )
    }

    return {
        "activity": events[:8],
        "wiki": wiki,
    }


class ProjectDetailView(DetailView):
    """Project overview page.

    Public projects are visible to anyone. Private projects require the
    viewer to be a member.
    """

    model = Project
    template_name = "projects/detail.html"
    context_object_name = "project"

    def get_object(self, queryset=None):
        project = super().get_object(queryset)
        if not project.is_public:
            if not self.request.user.is_authenticated:
                raise PermissionDenied("This project is private.")
            is_member = ProjectPrivilege.objects.filter(
                user=self.request.user, project=project
            ).exists()
            if not is_member:
                raise PermissionDenied("This project is private.")
        return project

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project = self.object
        all_members = list(
            ProjectMember.objects.filter(project=project, deleted=False)
            .prefetch_related("users", "roles")
            .order_by("id")
        )
        lead_user_ids = set(
            ProjectPrivilege.objects.filter(
                project=project,
                privilege=MemberPrivileges.PROJECT_MANAGER,
            ).values_list("user_id", flat=True)
        )
        leads, regulars = [], []
        for m in all_members:
            if any(u.id in lead_user_ids for u in m.users.all()):
                leads.append(m)
            else:
                regulars.append(m)
        context["members"] = all_members
        context["project_leads"] = leads
        context["regular_members"] = regulars
        if self.request.user.is_authenticated:
            user_priv = (
                ProjectPrivilege.objects.filter(
                    user=self.request.user, project=project
                )
                .order_by("-created_at")
                .first()
            )
            context["user_privilege"] = user_priv.privilege if user_priv else None
            context["pending_join_request"] = (
                JoinRequest.objects.filter(
                    project=project,
                    user=self.request.user,
                    status=JoinRequestStatus.PENDING,
                ).first()
            )
            context["pending_invitation"] = (
                Invitation.objects.addressed_to(self.request.user)
                .filter(project=project)
                .first()
            )
            if context["user_privilege"] == MemberPrivileges.PROJECT_MANAGER:
                context["pending_join_request_count"] = JoinRequest.objects.filter(
                    project=project, status=JoinRequestStatus.PENDING
                ).count()
                context["pending_invitation_count"] = Invitation.objects.filter(
                    project=project
                ).count()
            else:
                context["pending_join_request_count"] = 0
                context["pending_invitation_count"] = 0
        else:
            context["user_privilege"] = None
            context["pending_join_request"] = None
            context["pending_invitation"] = None

        context["is_project_manager"] = (
            context["user_privilege"] == MemberPrivileges.PROJECT_MANAGER
        )
        # Drives the project section tabs in the top bar (members only).
        context["is_project_member"] = context["user_privilege"] is not None

        # The number cards are cheap enough to count for any viewer who can
        # see the project; the summary's cards are drawn for members only.
        from apps.core.enums import TaskStatus
        from apps.tasks.models import Task
        from apps.wiki.models import WikiPage
        from django.utils import timezone

        context["today"] = timezone.now()
        task_qs = Task.objects.filter(project=project)
        context["task_count"] = task_qs.count()
        # Overdue means still open past its deadline; a finished task whose
        # deadline has passed is not late, and nor is one Extended: every
        # assignee still working on it has a later individual deadline
        # (Task.is_overdue).
        context["overdue_count"] = task_qs.overdue(context["today"]).count()
        context["contributor_count"] = ProjectPrivilege.objects.filter(
            project=project
        ).values("user").distinct().count()
        # Wiki pages count and show as the viewer may read them. Anyone but a
        # member only gets this far on a public project, where the pages a
        # lead made public and put on the Overview are listed for them.
        readable_pages = WikiPage.objects.filter(project=project).visible_to(
            is_lead=context["is_project_manager"],
            is_member=context["is_project_member"],
        )
        context["wiki_page_count"] = readable_pages.count()
        if context["is_project_member"]:
            context["summary"] = _project_summary(
                project, context["today"], is_lead=context["is_project_manager"]
            )
        else:
            context["public_wiki_pages"] = list(
                readable_pages.filter(show_on_overview=True).order_by(
                    "-updated_at", "-id"
                )
            )
        return context


class ProjectCreateView(LoginRequiredMixin, CreateView):
    model = Project
    form_class = ProjectForm
    template_name = "projects/form.html"

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["projects/partials/create_form.html"]
        return ["projects/form.html"]

    def form_valid(self, form):
        project = create_project_with_owner(
            user=self.request.user, form_data=form.cleaned_data
        )
        if self.request.headers.get("HX-Request"):
            import json
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps(
                {"project-created": {"url": reverse("projects:detail", kwargs={"pk": project.pk})}}
            )
            return response
        messages.success(self.request, f"Project '{project.name}' created.")
        return HttpResponseRedirect(
            reverse("projects:detail", kwargs={"pk": project.pk})
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = "New project"
        context["submit_label"] = "Create project"
        return context


class ProjectUpdateView(ProjectManagerRequiredMixin, ProjectContextMixin, UpdateView):
    model = Project
    form_class = ProjectForm
    template_name = "projects/form.html"

    def get_success_url(self):
        return reverse("projects:detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(
            AuditAction.PROJECT_UPDATED,
            actor=self.request.user,
            entity=self.object,
            project=self.object,
            metadata={"name": self.object.name},
        )
        messages.success(self.request, "Project updated.")
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = "Edit project"
        context["submit_label"] = "Save changes"
        return context


class ProjectDeleteView(ProjectManagerRequiredMixin, DeleteView):
    model = Project
    template_name = "projects/confirm_delete.html"
    success_url = reverse_lazy("projects:my_list")

    def form_valid(self, form):
        project = self.object
        project_name = project.name
        log_action(
            AuditAction.PROJECT_DELETED,
            actor=self.request.user,
            entity=project,
            project=project,
            metadata={"name": project_name},
        )
        messages.success(self.request, f"Project '{project_name}' deleted.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


def _member_rows(project, *, viewer=None):
    """Collect (member, user, privilege) dicts for a project.

    When ``viewer`` is supplied, each row is annotated with ``is_self`` so
    the template can disable PM-mutating controls on the viewer's own row
    when the last-PM invariant would otherwise be broken.
    """
    members = (
        ProjectMember.objects.filter(project=project, deleted=False)
        .prefetch_related("users", "roles")
        .order_by("id")
    )
    privileges = {
        p.user_id: p.privilege
        for p in ProjectPrivilege.objects.filter(project=project)
    }
    viewer_id = getattr(viewer, "id", None) if viewer is not None else None
    rows = []
    for m in members:
        primary_user = m.users.first()
        effective = (
            privileges.get(primary_user.id)
            if primary_user
            else MemberPrivileges.CONTRIBUTOR
        )
        rows.append(
            {
                "member": m,
                "user": primary_user,
                "privilege": effective,
                "is_self": (
                    primary_user is not None and primary_user.id == viewer_id
                ),
            }
        )
    return rows


def _render_member_row(request, project, member, *, oob=False):
    """One Contributors table row, as the members page draws it for ``request.user``.

    ``oob`` marks it as an htmx out-of-band swap, so it can ride along with
    another response and replace the row of the same id in place.
    """
    primary_user = member.users.first()
    privilege = (
        ProjectPrivilege.objects.filter(project=project, user=primary_user)
        .values_list("privilege", flat=True)
        .first()
        if primary_user
        else MemberPrivileges.CONTRIBUTOR
    )
    row = {
        "member": member,
        "user": primary_user,
        "privilege": privilege,
        "is_self": primary_user is not None and primary_user.id == request.user.id,
    }
    return render_to_string(
        "projects/member_row.html",
        {
            "row": row,
            "project": project,
            "user_privilege": MemberPrivileges.PROJECT_MANAGER,
            "viewer_is_last_pm": is_last_project_manager(
                project=project, user=request.user
            ),
            "oob": oob,
        },
        request=request,
    )


def _viewer_row_oob(request, project, *, changed_member):
    """The viewer's own row, out of band, after a change to someone else's.

    Promoting, demoting or removing another member can make the viewer the
    last Lead or stop them being it, which disables or enables their own
    Modify privilege and Remove. Their row is redrawn with the change so it
    does not wait for a reload. Empty when the change was to their own row,
    which the response redraws anyway.
    """
    viewer_member = (
        ProjectMember.objects.filter(project=project, users=request.user, deleted=False)
        .exclude(pk=changed_member.pk)
        .first()
    )
    if viewer_member is None:
        return ""
    return _render_member_row(request, project, viewer_member, oob=True)


class ProjectMemberListView(
    ProjectMemberRequiredMixin, ProjectContextMixin, TemplateView
):
    template_name = "projects/members.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["rows"] = _member_rows(self.project, viewer=self.request.user)
        context["viewer_is_last_pm"] = is_last_project_manager(
            project=self.project, user=self.request.user
        )
        if context["user_privilege"] == MemberPrivileges.PROJECT_MANAGER:
            # The Modify privilege modal's choices.
            context["privilege_choices"] = MemberPrivileges.choices
            context["pending_join_request_count"] = JoinRequest.objects.filter(
                project=self.project, status=JoinRequestStatus.PENDING
            ).count()
            context["pending_invitation_count"] = Invitation.objects.filter(
                project=self.project
            ).count()
        return context


class ContributionTrackingMixin(ProjectMemberRequiredMixin, ProjectContextMixin):
    """Shared context for the Contribution Tracking section and its views.

    Open to every project member — a contribution matrix that only its
    leads could read would be no use to the people in it.
    """

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_project_manager"] = (
            context.get("user_privilege") == MemberPrivileges.PROJECT_MANAGER
        )
        if self.project.tracks_credit:
            # ``?used=1`` trims the grid to roles and members with something
            # in them; the default is the whole taxonomy and every member.
            # ``?sort=`` names a column and ``?dir=`` its direction — both
            # fall back to the default rather than erroring on a stale link.
            only_used = self.request.GET.get("used") == "1"
            context["only_used"] = only_used
            context["matrix"] = get_project_contributions_matrix(
                self.project,
                only_used=only_used,
                sort=self.request.GET.get("sort"),
                direction=self.request.GET.get("dir"),
            )
            context["records"] = get_project_contributions(self.project).order_by(
                "-closed_at", "-id"
            )
        return context


class ContributionTrackingView(ContributionTrackingMixin, TemplateView):
    """The project's credit ledger, in two views the section switches between.

    Both ship in the first render, the way the board ships its three, so the
    switcher is instant; the partials below re-fetch whichever one is being
    switched to, so it is never stale.
    """

    template_name = "projects/contribution_tracking.html"


class ContributionsByMemberPartialView(ContributionTrackingMixin, TemplateView):
    """HTMX partial: the members x CRediT-role matrix, freshly counted."""

    template_name = "projects/partials/contributions_by_member.html"


class EveryContributionPartialView(ContributionTrackingMixin, TemplateView):
    """HTMX partial: every contribution on the project, one row each."""

    template_name = "projects/partials/every_contribution.html"


class ProjectFormsView(
    ProjectMemberRequiredMixin, ProjectContextMixin, TemplateView
):
    template_name = "projects/forms.html"


class ProjectInvitationListView(
    ProjectManagerRequiredMixin, ProjectContextMixin, ListView
):
    """PM-facing list of pending (unsent/unanswered) invitations for a project."""

    template_name = "projects/invitation_requests.html"
    context_object_name = "invitations"

    def get_queryset(self):
        return (
            Invitation.objects.filter(project=self.project)
            .select_related("invited_user")
            .order_by("-created_at")
        )


class CancelInvitationView(ProjectManagerRequiredMixin, ProjectContextMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk, invitation_pk):
        invitation = get_object_or_404(Invitation, pk=invitation_pk, project=self.project)
        invitation.delete()
        messages.success(request, "Invitation cancelled.")
        return redirect("projects:project_invitations", pk=self.project.pk)


class ResendInvitationView(ProjectManagerRequiredMixin, ProjectContextMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk, invitation_pk):
        invitation = get_object_or_404(Invitation, pk=invitation_pk, project=self.project)
        resend_invitation(invitation=invitation, resender=request.user)
        messages.success(request, f"Invitation resent to {invitation.email}.")
        return redirect("projects:project_invitations", pk=self.project.pk)


class UpdateMemberPrivilegeView(
    ProjectManagerRequiredMixin, ProjectContextMixin, View
):
    """HTMX POST target -- updates the privilege of a member's primary user."""

    http_method_names = ["post"]

    def post(self, request, pk, member_pk):
        member = get_object_or_404(ProjectMember, pk=member_pk, project=self.project)
        form = MemberPrivilegeForm(request.POST)
        if not form.is_valid():
            return HttpResponseBadRequest("Invalid privilege value.")

        new_privilege = form.cleaned_data["privilege"]
        primary_user = member.users.first()
        if primary_user is None:
            return HttpResponseBadRequest("Member has no linked user.")

        try:
            assert_pm_self_action_allowed(
                project=self.project,
                actor=request.user,
                target_user=primary_user,
                new_privilege=new_privilege,
            )
        except LastProjectManagerError as exc:
            messages.error(request, str(exc))
            if request.headers.get("HX-Request"):
                return HttpResponse(str(exc), status=400)
            return HttpResponseRedirect(
                reverse("projects:members", kwargs={"pk": self.project.pk})
            )

        ProjectPrivilege.objects.filter(
            user=primary_user, project=self.project
        ).delete()
        ProjectPrivilege.objects.create(
            user=primary_user, project=self.project, privilege=new_privilege
        )
        log_action(
            AuditAction.MEMBER_PRIVILEGE_CHANGED,
            actor=request.user,
            project=self.project,
            metadata={
                "target_user_id": primary_user.id,
                "to": new_privilege,
            },
        )

        if (
            primary_user.id == request.user.id
            and new_privilege != MemberPrivileges.PROJECT_MANAGER
        ):
            # A Lead who steps down no longer sees the page a Lead does: no
            # actions in the table, no Invite member, no Settings tab. Reload
            # it as they now see it rather than patching one row.
            messages.success(
                request,
                f"You are now a {MemberPrivileges(new_privilege).label} of this project.",
            )
            response = HttpResponse("")
            response["HX-Refresh"] = "true"
            return response

        return HttpResponse(
            _render_member_row(request, self.project, member)
            + _viewer_row_oob(request, self.project, changed_member=member)
        )


class RemoveMemberView(ProjectManagerRequiredMixin, ProjectContextMixin, View):
    http_method_names = ["post", "delete"]

    def post(self, request, pk, member_pk):
        return self._remove(request, member_pk)

    def delete(self, request, pk, member_pk):
        return self._remove(request, member_pk)

    def _remove(self, request, member_pk):
        member = get_object_or_404(ProjectMember, pk=member_pk, project=self.project)
        primary_user = member.users.first()

        if primary_user is not None:
            try:
                assert_pm_self_action_allowed(
                    project=self.project,
                    actor=request.user,
                    target_user=primary_user,
                    new_privilege=None,
                )
            except LastProjectManagerError as exc:
                messages.error(request, str(exc))
                if request.headers.get("HX-Request"):
                    return HttpResponse(str(exc), status=400)
                return HttpResponseRedirect(
                    reverse("projects:members", kwargs={"pk": self.project.pk})
                )

        member.deleted = True
        member.save(update_fields=["deleted", "updated_at"])
        if primary_user is not None:
            ProjectPrivilege.objects.filter(
                user=primary_user, project=self.project
            ).delete()
        log_action(
            AuditAction.MEMBER_REMOVED,
            actor=request.user,
            project=self.project,
            metadata={
                "target_user_id": primary_user.id if primary_user else None
            },
        )
        if request.headers.get("HX-Request"):
            # Empty main content removes the row via hx-swap="outerHTML";
            # the viewer's own row rides along out of band.
            return HttpResponse(
                _viewer_row_oob(request, self.project, changed_member=member)
            )
        return HttpResponseRedirect(
            reverse("projects:members", kwargs={"pk": self.project.pk})
        )


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


@login_required
@require_GET
def user_invite_search(request, pk):
    """Return an HTML fragment of users matching ``?q=`` for the invite modal search."""
    from django.contrib.auth import get_user_model
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email
    from django.db.models import Q

    project = get_object_or_404(Project, pk=pk)
    if not ProjectPrivilege.objects.filter(
        project=project,
        user=request.user,
        privilege=MemberPrivileges.PROJECT_MANAGER,
    ).exists():
        return HttpResponse(status=403)

    q = request.GET.get("q", "").strip()

    def is_email(value):
        try:
            validate_email(value)
        except ValidationError:
            return False
        return True

    if not q:
        return render(
            request,
            "projects/partials/user_search_results.html",
            {"users_data": [], "q": q, "q_is_email": False},
        )

    User = get_user_model()
    existing_member_ids = list(
        ProjectPrivilege.objects.filter(project=project).values_list("user_id", flat=True)
    )
    already_invited_ids = list(
        Invitation.objects.filter(project=project, invited_user__isnull=False)
        .values_list("invited_user_id", flat=True)
    )
    qs = (
        User.objects.exclude(pk__in=existing_member_ids)
        .exclude(pk__in=already_invited_ids)
        .filter(
            Q(name__icontains=q)
            | Q(given_name__icontains=q)
            | Q(family_name__icontains=q)
            | Q(email__icontains=q)
        )
        .order_by("name", "email")[:10]
    )

    users_data = []
    for u in qs:
        if u.given_name or u.family_name:
            full_name = f"{u.given_name} {u.family_name}".strip()
        else:
            full_name = u.name
        label = f"{full_name} ({u.email})" if full_name else u.email
        users_data.append({"user": u, "label": label})

    return render(
        request,
        "projects/partials/user_search_results.html",
        {"users_data": users_data, "q": q, "q_is_email": is_email(q)},
    )


class InviteCreateView(ProjectManagerRequiredMixin, ProjectContextMixin, View):
    """GET renders the invite modal fragment; POST sends the invitation."""

    template_name = "projects/invite_form.html"

    def get(self, request, pk):
        form = InvitationForm(project=self.project)
        return render(
            request,
            self.template_name,
            {"form": form, "project": self.project},
        )

    def post(self, request, pk):
        form = InvitationForm(request.POST, project=self.project)
        if not form.is_valid():
            selected_user_label = ""
            user_pk = request.POST.get("invited_user")
            if user_pk:
                from django.contrib.auth import get_user_model
                try:
                    u = get_user_model().objects.get(pk=user_pk)
                    if u.given_name or u.family_name:
                        full_name = f"{u.given_name} {u.family_name}".strip()
                    else:
                        full_name = u.name
                    selected_user_label = f"{full_name} ({u.email})" if full_name else u.email
                except get_user_model().DoesNotExist:
                    pass
            if not selected_user_label:
                selected_user_label = request.POST.get("email", "").strip()
            return render(
                request,
                self.template_name,
                {
                    "form": form,
                    "project": self.project,
                    "selected_user_label": selected_user_label,
                },
            )
        added_by = (
            request.user.get_full_name() or request.user.name or request.user.email
        )
        create_invitation(
            project=self.project,
            email=form.cleaned_data["email"],
            privilege=form.cleaned_data["privilege"],
            added_by=added_by,
            roles=form.cleaned_data.get("roles"),
            inviter=request.user,
            invited_user=form.cleaned_data.get("invited_user"),
        )
        recipient_label = (
            getattr(form.cleaned_data.get("invited_user"), "email", None)
            or form.cleaned_data["email"]
        )
        messages.success(request, f"Invitation sent to {recipient_label}.")
        if request.headers.get("HX-Request"):
            response = HttpResponse()
            response["HX-Redirect"] = reverse(
                "projects:members", kwargs={"pk": self.project.pk}
            )
            return response
        return HttpResponseRedirect(
            reverse("projects:members", kwargs={"pk": self.project.pk})
        )


class MyInvitationsView(LoginRequiredMixin, ListView):
    """Pending invitations addressed to the current user's email."""

    model = Invitation
    template_name = "invites/list.html"
    context_object_name = "invitations"

    def get_queryset(self):
        return (
            Invitation.objects.addressed_to(self.request.user)
            .select_related("project")
            .order_by("-created_at")
        )


class AcceptOrDeclineInviteView(View):
    """Single URL that handles showing, accepting, and declining an invite.

    Only the invitee may see or act on an invitation. Anyone else who is
    signed in -- say, on a shared computer -- gets a page saying the link
    belongs to another account, with nothing about the project, and a way to
    sign out and come back to the link as the right person.
    """

    template_name = "invites/accept.html"
    wrong_account_template = "invites/wrong_account.html"

    def _invitation_or_response(self, request, code):
        """Return ``(invitation, None)`` for the invitee, else ``(None, response)``."""
        # Sign-in comes first, so a signed-out visitor learns nothing about
        # whether the code exists.
        if not request.user.is_authenticated:
            return None, redirect_to_login(request.path)
        invitation = get_object_or_404(
            Invitation.objects.select_related("project"), invitation_code=code
        )
        if not invitation.is_addressed_to(request.user):
            return None, render(
                request,
                self.wrong_account_template,
                {"invitation_path": request.path},
                status=403,
            )
        return invitation, None

    def get(self, request, code):
        invitation, response = self._invitation_or_response(request, code)
        if response is not None:
            return response
        return render(
            request,
            self.template_name,
            {"invitation": invitation, "project": invitation.project},
        )

    def post(self, request, code):
        invitation, response = self._invitation_or_response(request, code)
        if response is not None:
            return response
        action = request.POST.get("action", "accept")

        if action == "decline":
            decline_invitation(invitation=invitation, user=request.user)
            messages.info(request, "Invitation declined.")
            return redirect("projects:my_invitations")

        project = accept_invitation(invitation=invitation, user=request.user)
        messages.success(request, f"Joined '{project.name}'.")
        return redirect("projects:detail", pk=project.pk)


# ---------------------------------------------------------------------------
# Join requests
# ---------------------------------------------------------------------------


def _normalize_requested_privilege(raw: str | None) -> str:
    if raw == MemberPrivileges.PROJECT_MANAGER:
        return MemberPrivileges.PROJECT_MANAGER
    return MemberPrivileges.CONTRIBUTOR


class RequestToJoinView(LoginRequiredMixin, View):
    """Show the join-request form (GET) and submit it (POST).

    Accessible to any logged-in non-member. For private projects, the user
    must have somehow reached the URL — they cannot view detail without
    membership, so this endpoint can only be hit if they already had the
    link or the project is public.
    """

    template_name = "projects/join_request_form.html"

    def _get_project(self, pk: int) -> Project:
        project = get_object_or_404(Project, pk=pk)
        if not project.is_public and not ProjectPrivilege.objects.filter(
            user=self.request.user, project=project
        ).exists():
            # Private projects: only members can interact at all. The
            # detail view would already block this, but be explicit.
            raise PermissionDenied("This project is private.")
        return project

    def _already_member(self, project: Project) -> bool:
        return ProjectPrivilege.objects.filter(
            user=self.request.user, project=project
        ).exists()

    _modal_template = "projects/partials/join_request_form_modal.html"

    def get(self, request, pk):
        project = self._get_project(pk)
        if self._already_member(project):
            messages.info(request, "You are already a member of this project.")
            return redirect("projects:detail", pk=project.pk)

        pending = JoinRequest.objects.filter(
            project=project,
            user=request.user,
            status=JoinRequestStatus.PENDING,
        ).first()
        if pending is not None:
            messages.info(request, "You already have a pending request for this project.")
            return redirect("projects:detail", pk=project.pk)

        initial_privilege = _normalize_requested_privilege(
            request.GET.get("privilege")
        )
        form = JoinRequestForm(initial_privilege=initial_privilege)
        template = self._modal_template if request.headers.get("HX-Request") else self.template_name
        return render(request, template, {"form": form, "project": project})

    def post(self, request, pk):
        project = self._get_project(pk)
        is_htmx = bool(request.headers.get("HX-Request"))
        form = JoinRequestForm(request.POST)
        if not form.is_valid():
            template = self._modal_template if is_htmx else self.template_name
            return render(request, template, {"form": form, "project": project})

        try:
            create_join_request(
                project=project,
                user=request.user,
                requested_privilege=form.cleaned_data["requested_privilege"],
                message=form.cleaned_data.get("message", ""),
            )
        except JoinRequestError as exc:
            messages.error(request, str(exc))
            redirect_url = reverse("projects:detail", pk=project.pk)
            if is_htmx:
                response = HttpResponse(status=204)
                response["HX-Redirect"] = redirect_url
                return response
            return redirect(redirect_url)

        messages.success(
            request,
            f"Your request to join '{project.name}' was sent to the project leads.",
        )

        if is_htmx:
            pending = JoinRequest.objects.filter(
                project=project,
                user=request.user,
                status=JoinRequestStatus.PENDING,
            ).first()
            response = render(
                request,
                "projects/partials/join_action.html",
                {"pending_join_request": pending, "project": project},
            )
            response["HX-Retarget"] = "#join-action"
            response["HX-Reswap"] = "innerHTML"
            response["HX-Trigger"] = "closejoinmodal"
            return response

        return redirect("projects:detail", pk=project.pk)


class ReviewJoinRequestView(ProjectManagerRequiredMixin, ProjectContextMixin, View):
    """GET — show approve/decline review page for a single join request."""

    template_name = "projects/review_join_request.html"

    def get(self, request, pk, join_request_pk):
        join_request = get_object_or_404(
            JoinRequest,
            pk=join_request_pk,
            project=self.project,
            status=JoinRequestStatus.PENDING,
        )
        template = (
            "projects/partials/review_join_request_modal.html"
            if request.headers.get("HX-Request")
            else self.template_name
        )
        return render(
            request,
            template,
            {
                "join_request": join_request,
                "project": self.project,
                "source": request.GET.get("source", ""),
            },
        )


class ProjectJoinRequestListView(
    ProjectManagerRequiredMixin, ProjectContextMixin, ListView
):
    """PM-facing list of pending join requests for a project."""

    template_name = "projects/join_requests.html"
    context_object_name = "join_requests"

    def get_queryset(self):
        return (
            JoinRequest.objects.filter(
                project=self.project, status=JoinRequestStatus.PENDING
            )
            .select_related("user")
            .order_by("created_at")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["decline_form"] = JoinRequestDeclineForm()
        return context


class ApproveJoinRequestView(
    ProjectManagerRequiredMixin, ProjectContextMixin, View
):
    http_method_names = ["post"]

    def post(self, request, pk, join_request_pk):
        join_request = get_object_or_404(
            JoinRequest, pk=join_request_pk, project=self.project
        )
        try:
            approve_join_request(join_request=join_request, approver=request.user)
        except JoinRequestError as exc:
            messages.error(request, str(exc))
            return redirect("projects:join_requests", pk=self.project.pk)

        messages.success(request, "Join request approved.")
        if request.headers.get("HX-Request"):
            return HttpResponse("")
        if request.POST.get("next") == "incoming":
            return redirect("projects:incoming_join_requests")
        return redirect("projects:join_requests", pk=self.project.pk)


class DeclineJoinRequestView(
    ProjectManagerRequiredMixin, ProjectContextMixin, View
):
    http_method_names = ["post"]

    def post(self, request, pk, join_request_pk):
        join_request = get_object_or_404(
            JoinRequest, pk=join_request_pk, project=self.project
        )
        form = JoinRequestDeclineForm(request.POST)
        reason = form.cleaned_data.get("reason", "") if form.is_valid() else ""
        try:
            decline_join_request(
                join_request=join_request,
                approver=request.user,
                reason=reason,
            )
        except JoinRequestError as exc:
            messages.error(request, str(exc))
            return redirect("projects:join_requests", pk=self.project.pk)

        messages.info(request, "Join request declined.")
        if request.headers.get("HX-Request"):
            return HttpResponse("")
        if request.POST.get("next") == "incoming":
            return redirect("projects:incoming_join_requests")
        return redirect("projects:join_requests", pk=self.project.pk)


class CancelJoinRequestView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, join_request_pk):
        join_request = get_object_or_404(
            JoinRequest, pk=join_request_pk, user=request.user
        )
        project = join_request.project
        is_htmx = bool(request.headers.get("HX-Request"))

        try:
            cancel_join_request(join_request=join_request, user=request.user)
        except JoinRequestError as exc:
            messages.error(request, str(exc))
            if is_htmx:
                pending = JoinRequest.objects.filter(
                    project=project,
                    user=request.user,
                    status=JoinRequestStatus.PENDING,
                ).first()
                return render(
                    request,
                    "projects/partials/join_action.html",
                    {"pending_join_request": pending, "project": project},
                )
            return redirect("projects:detail", pk=project.pk)

        if is_htmx:
            return render(
                request,
                "projects/partials/join_action.html",
                {"pending_join_request": None, "project": project},
            )

        messages.info(request, "Join request cancelled.")
        return redirect("projects:detail", pk=project.pk)


class MyJoinRequestsView(LoginRequiredMixin, ListView):
    template_name = "projects/my_join_requests.html"
    context_object_name = "join_requests"

    def get_queryset(self):
        return (
            JoinRequest.objects.filter(user=self.request.user)
            .select_related("project")
            .order_by("-created_at")
        )


class IncomingJoinRequestsView(LoginRequiredMixin, ListView):
    template_name = "projects/incoming_join_requests.html"
    context_object_name = "join_requests"

    def get_queryset(self):
        from apps.core.enums import MemberPrivileges

        pm_project_ids = ProjectPrivilege.objects.filter(
            user=self.request.user,
            privilege=MemberPrivileges.PROJECT_MANAGER,
        ).values_list("project_id", flat=True)

        return (
            JoinRequest.objects.filter(
                project_id__in=pm_project_ids,
                status=JoinRequestStatus.PENDING,
            )
            .select_related("user", "project")
            .order_by("created_at")
        )


class RequestsView(LoginRequiredMixin, TemplateView):
    """Consolidated view of all incoming and outgoing requests.

    Incoming: join requests awaiting your review (as a project manager)
    and invitations you've received, merged into one list tagged by type
    so the "type" pill can distinguish them. Outgoing: join requests
    you've sent and invitations you've sent out (for projects you
    manage), merged the same way.

    A shared search box and Type/Status/Sort filters apply to both tabs.
    Invitations have no "completed" state of their own -- they're
    deleted outright once accepted or declined -- so the "Completed"
    status only ever matches join requests; "Active" (the default)
    matches pending join requests plus every invitation, since an
    invitation still on file is by definition not yet resolved.
    """

    template_name = "projects/requests.html"

    _STATUSES = ("active", "completed", "all")
    _SORTS = ("newest", "oldest", "name_asc", "name_desc")
    _SORT_LABELS = {
        "newest": None,
        "oldest": "oldest first",
        "name_asc": "project A–Z",
        "name_desc": "project Z–A",
    }
    _TYPE_LABELS = {
        "join_request": "join request",
        "invitation": "invitation",
    }

    @classmethod
    def _build_summary(cls, count, current_q, current_type, current_status, current_sort):
        """Human-readable results summary, or None if no filters are active."""
        parts = []
        if current_type:
            parts.append(cls._TYPE_LABELS.get(current_type, current_type))
        if current_status == "completed":
            parts.append("completed")
        desc = (" ".join(parts) + " requests") if parts else "requests"
        if current_status == "all":
            desc += " (including completed)"
        if current_q:
            desc += f' matching "{current_q}"'
        sort_label = cls._SORT_LABELS.get(current_sort)
        has_filters = bool(current_q or current_type or current_status != "active" or sort_label)
        if not has_filters:
            return None
        n = f"{count} result{'s' if count != 1 else ''}"
        summary = f"{n} for {desc}"
        if sort_label:
            summary += f", sorted by {sort_label}"
        return summary

    @staticmethod
    def _merge_and_sort(join_requests, invitations, sort):
        items = [{"kind": "join_request", "obj": jr} for jr in join_requests]
        items += [{"kind": "invitation", "obj": inv} for inv in invitations]
        if sort in ("name_asc", "name_desc"):
            items.sort(key=lambda item: item["obj"].project.name.lower(), reverse=sort == "name_desc")
        else:
            items.sort(key=lambda item: item["obj"].created_at, reverse=sort != "oldest")
        return items

    def get_context_data(self, **kwargs):
        from django.db.models import Q

        from apps.core.enums import MemberPrivileges

        context = super().get_context_data(**kwargs)
        user = self.request.user

        current_q = self.request.GET.get("q", "").strip()
        current_status = self.request.GET.get("status", "")
        if current_status not in self._STATUSES:
            current_status = "active"
        current_sort = self.request.GET.get("sort", "")
        if current_sort not in self._SORTS:
            current_sort = "newest"
        current_tab = self.request.GET.get("tab", "")
        if current_tab not in ("incoming", "outgoing"):
            current_tab = "incoming"
        current_type = self.request.GET.get("type", "")
        if current_type not in ("join_request", "invitation"):
            current_type = ""

        pm_project_ids = ProjectPrivilege.objects.filter(
            user=user,
            privilege=MemberPrivileges.PROJECT_MANAGER,
        ).values_list("project_id", flat=True)

        incoming_jr_qs = JoinRequest.objects.filter(project_id__in=pm_project_ids)
        incoming_inv_qs = Invitation.objects.addressed_to(user)
        outgoing_jr_qs = JoinRequest.objects.filter(user=user)
        # Invitations sent out to others, for projects the user manages.
        outgoing_inv_qs = Invitation.objects.filter(project_id__in=pm_project_ids)

        if current_status == "active":
            incoming_jr_qs = incoming_jr_qs.filter(status=JoinRequestStatus.PENDING)
            outgoing_jr_qs = outgoing_jr_qs.filter(status=JoinRequestStatus.PENDING)
        elif current_status == "completed":
            incoming_jr_qs = incoming_jr_qs.exclude(status=JoinRequestStatus.PENDING)
            outgoing_jr_qs = outgoing_jr_qs.exclude(status=JoinRequestStatus.PENDING)
            incoming_inv_qs = incoming_inv_qs.none()
            outgoing_inv_qs = outgoing_inv_qs.none()
        # "all": no status filter on either side.

        if current_type == "join_request":
            incoming_inv_qs = incoming_inv_qs.none()
            outgoing_inv_qs = outgoing_inv_qs.none()
        elif current_type == "invitation":
            incoming_jr_qs = incoming_jr_qs.none()
            outgoing_jr_qs = outgoing_jr_qs.none()

        if current_q:
            jr_q = (
                Q(user__name__icontains=current_q)
                | Q(user__given_name__icontains=current_q)
                | Q(user__family_name__icontains=current_q)
                | Q(user__email__icontains=current_q)
                | Q(project__name__icontains=current_q)
            )
            incoming_jr_qs = incoming_jr_qs.filter(jr_q)
            outgoing_jr_qs = outgoing_jr_qs.filter(jr_q)

            inv_q = (
                Q(email__icontains=current_q)
                | Q(invited_user__name__icontains=current_q)
                | Q(invited_user__given_name__icontains=current_q)
                | Q(invited_user__family_name__icontains=current_q)
                | Q(added_by__icontains=current_q)
                | Q(project__name__icontains=current_q)
            )
            incoming_inv_qs = incoming_inv_qs.filter(inv_q)
            outgoing_inv_qs = outgoing_inv_qs.filter(inv_q)

        incoming_join_requests = list(
            incoming_jr_qs.select_related("user", "project").order_by("created_at")
        )
        incoming_invitations = list(
            incoming_inv_qs.select_related("project").order_by("-created_at")
        )
        outgoing_join_requests = list(
            outgoing_jr_qs.select_related("project").order_by("-created_at")
        )
        outgoing_invitations = list(
            outgoing_inv_qs.select_related("project").order_by("-created_at")
        )

        context["incoming_items"] = self._merge_and_sort(
            incoming_join_requests, incoming_invitations, current_sort
        )
        context["outgoing_items"] = self._merge_and_sort(
            outgoing_join_requests, outgoing_invitations, current_sort
        )

        context["current_q"] = current_q
        context["current_status"] = current_status
        context["current_sort"] = current_sort
        context["current_tab"] = current_tab
        context["current_type"] = current_type

        context["incoming_total"] = len(context["incoming_items"])
        context["requests_total"] = context["incoming_total"]

        context["incoming_results_summary"] = self._build_summary(
            context["incoming_total"], current_q, current_type, current_status, current_sort
        )
        context["outgoing_results_summary"] = self._build_summary(
            len(context["outgoing_items"]), current_q, current_type, current_status, current_sort
        )
        return context


@login_required
@require_GET
def my_projects_badge(request):
    count = Project.objects.filter(privileges__user=request.user).distinct().count()
    return HttpResponse(str(count) if count else "")


