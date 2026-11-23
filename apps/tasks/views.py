"""Tasks app views: kanban board, task CRUD, milestones, and HTMX endpoints.

All views require authentication; project-scoped views additionally require
project membership (or PROJECT_MANAGER privilege) via ``ProjectContextMixin``
/ ``TaskMemberAccessMixin``.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, time

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Count, Exists, F, Max, Min, OuterRef, Prefetch, ProtectedError, Q, Subquery
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import resolve, reverse, Resolver404
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify
from urllib.parse import urlparse
from django.views.generic import (
    CreateView,
    DeleteView,
    FormView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)

from apps.audit.services import AuditAction, log_action
from apps.comments.forms import CommentForm
from apps.comments.views import comment_box_context
from apps.core.enums import (
    CompletedAs,
    ContributionType,
    TaskStatus,
    credit_role_choices,
)
from apps.projects.models import Project, ProjectMember
from apps.tasks.calendar import (
    VIEWS as CALENDAR_VIEWS,
    build_day_tasks,
    build_month_grid,
    build_week_grid,
    build_year_grid,
    parse_view_date,
    shift_date,
)
from apps.tasks.forms import (
    AssigneeDatesForm,
    KanbanBoardForm,
    MilestoneForm,
    TaskAssignForm,
    TaskForm,
    TaskMilestoneForm,
)
from apps.tasks.ics import build_ics
from apps.tasks.timezones import (
    DEFAULT_DUE_TIME,
    DEFAULT_START_TIME,
    canonical,
    picker_value,
    short_zone_label,
)
from apps.tasks.import_csv import import_rows, parse_csv
from apps.tasks.import_form import TaskImportForm
from apps.tasks.mixins import (
    ProjectContextMixin,
    ProjectManagerRequiredMixin,
    TaskMemberAccessMixin,
)
from apps.tasks.models import ActivityEvent, KanbanBoard, Milestone, Task, TaskActivity, TaskLog
from apps.tasks.services import (
    UNSET,
    apply_task_date_change,
    rezone_following_members,
    approve_completion,
    clear_member_dates,
    clear_review,
    close_task,
    mark_own_done,
    mark_reviewed,
    move_task,
    notify_new_assignees,
    pending_members,
    reopen_own,
    reopen_task,
    review_closed,
    review_locked,
    set_member_contribution_type,
    set_member_dates,
    set_pending_dates,
    unverify_part,
    verify_part,
    verify_parts,
    VerifyScope,
)


# ---------------------------------------------------------------------------
# All tasks (cross-project)
# ---------------------------------------------------------------------------


class MyTaskListView(LoginRequiredMixin, ListView):
    """Tasks touching the current user, across all of their projects: those
    assigned to the user and those where the user is a reviewer, merged into
    a single list. The "Type" filter narrows that down to just tasks
    assigned to the user, or just tasks where the user is a reviewer.

    By default only active ("Not completed") tasks are shown, matching the
    dashboard's task counts; the "Status" filter can switch to "Completed"
    only or "All" regardless of status. Excludes tasks the user merely
    created or that are assigned to others. The one place tasks can be
    filtered and browsed across projects; paginated so a heavy task load
    never gets fetched at once.
    """

    model = Task
    template_name = "tasks/my_list.html"
    context_object_name = "tasks"
    paginate_by = 20

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["tasks/partials/my_list_results.html"]
        return [self.template_name]

    _SORT_MAP = {
        "newest": "-created_at",
        "oldest": "created_at",
        # The user's own deadline: their individual one where a Lead set
        # it, else the task's (``TaskQuerySet.with_my_deadline``).
        "deadline_asc": "my_deadline",
        "deadline_desc": "-my_deadline",
        "name_asc": "name",
        "name_desc": "-name",
    }

    _TYPES = ("assigned", "review")
    _STATUSES = ("not_completed", "completed", "all")

    def _current_status(self):
        status = self.request.GET.get("status", "")
        return status if status in self._STATUSES else "not_completed"

    def _current_type(self):
        task_type = self.request.GET.get("type", "")
        return task_type if task_type in self._TYPES else ""

    def _base_queryset(self):
        user = self.request.user
        user_member_qs = ProjectMember.objects.filter(users=user)
        latest_log_status = Subquery(
            TaskLog.objects.filter(task=OuterRef("pk"), assigned_to__in=user_member_qs)
            .order_by("-created_at")
            .values("status")[:1]
        )
        return Task.objects.filter(
            Q(assigned_members__users=user) | Q(reviewers__users=user)
        ).with_my_deadline(user).annotate(
            user_log_status=latest_log_status,
            is_assigned=Exists(user_member_qs.filter(assigned_tasks=OuterRef("pk"))),
            is_reviewing=Exists(user_member_qs.filter(reviewed_tasks=OuterRef("pk"))),
        )

    def _apply_status_filter(self, qs, status_filter):
        """Restrict by completion status. "all" applies no filter; "completed"
        shows only fully-completed tasks; the default "not_completed" matches
        the dashboard's task counts."""
        if status_filter == "all":
            return qs
        if status_filter == "completed":
            return qs.filter(status=TaskStatus.COMPLETED)
        # NULL-safe: a task with no TaskLog yet has user_log_status=NULL, and
        # `.exclude(user_log_status=COMPLETED)` would silently drop it too
        # (NOT (NULL = COMPLETED) is NULL/false in SQL), so it must be kept
        # explicitly — matching the dashboard's active_task_count logic. Only
        # applies to tasks the user is assigned to; a review-only task is
        # judged by the task's own status alone.
        return qs.filter(status=TaskStatus.NOT_COMPLETED).filter(
            Q(is_assigned=False)
            | Q(user_log_status__isnull=True)
            | ~Q(user_log_status=TaskStatus.COMPLETED)
        )

    def _apply_type_filter(self, qs, task_type):
        if task_type == "assigned":
            return qs.filter(is_assigned=True)
        if task_type == "review":
            return qs.filter(is_reviewing=True)
        return qs

    def _apply_common_filters(self, qs):
        """Search/project/priority filters (not sort, not the type
        filter)."""
        project_id = self.request.GET.get("project", "")
        if project_id:
            qs = qs.filter(project_id=project_id)

        if self.request.GET.get("priority", "") == "high":
            qs = qs.filter(is_priority=True)

        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(description__icontains=q)
                | Q(project__name__icontains=q)
                | Q(assigned_members__name__icontains=q)
                | Q(assigned_members__users__given_name__icontains=q)
                | Q(assigned_members__users__family_name__icontains=q)
                | Q(assigned_members__users__name__icontains=q)
                | Q(assigned_members__users__email__icontains=q)
            )
        return qs.distinct()

    def get_queryset(self):
        qs = (
            self._base_queryset()
            .select_related("project", "container")
            .prefetch_related("assigned_members")
        )
        qs = self._apply_common_filters(qs)
        qs = self._apply_type_filter(qs, self._current_type())
        qs = self._apply_status_filter(qs, self._current_status())

        sort = self.request.GET.get("sort", "newest")
        if sort in ("deadline_asc", "deadline_desc"):
            qs = qs.filter(my_deadline__isnull=False)
        order = self._SORT_MAP.get(sort, "-created_at")
        return qs.order_by(order, "id")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        context["is_htmx_request"] = bool(self.request.headers.get("HX-Request"))
        current_q = self.request.GET.get("q", "").strip()
        current_sort = self.request.GET.get("sort", "newest")
        current_project = self.request.GET.get("project", "")
        current_priority = self.request.GET.get("priority", "")
        current_status = self._current_status()
        current_type = self._current_type()

        context["current_q"] = current_q
        context["current_sort"] = current_sort
        context["current_project"] = current_project
        context["current_priority"] = current_priority
        context["current_status"] = current_status
        context["current_type"] = current_type

        context["my_projects"] = (
            Project.objects.filter(privileges__user=user)
            .distinct()
            .order_by("name")
        )

        params = self.request.GET.copy()
        params.pop("page", None)
        context["filter_params"] = params.urlencode()

        context["total_task_count"] = self._apply_status_filter(
            self._apply_common_filters(self._base_queryset()), current_status
        ).count()
        context["now"] = timezone.now()

        has_filters = bool(
            current_q or current_project or current_priority or current_type
            or current_status != "not_completed"
        )
        if has_filters:
            parts = []
            if current_priority == "high":
                parts.append("high-priority")
            if current_type == "assigned":
                parts.append("assigned")
            elif current_type == "review":
                parts.append("review")
            desc = (" ".join(parts) + " tasks") if parts else "tasks"
            if current_status == "all":
                desc += " (including completed)"
            elif current_status == "completed":
                desc += " (completed only)"
            if current_project:
                project_obj = next(
                    (p for p in context["my_projects"] if str(p.pk) == current_project),
                    None,
                )
                if project_obj:
                    desc += f" in {project_obj.name}"
            if current_q:
                desc += f' matching "{current_q}"'

            total = context["paginator"].count
            n = f"{total} result{'s' if total != 1 else ''}"
            context["results_summary"] = f"{n} for {desc}"

        return context


# ---------------------------------------------------------------------------
# Kanban board
# ---------------------------------------------------------------------------


def _fmt_short(d) -> str:
    return f"{d.strftime('%b')} {d.day}"


# --- Board filter: ?filter= narrows every board view -----------------------

#: The board's Filter menu, in order: value -> label. "" shows every task.
BOARD_FILTERS = (
    ("", "All"),
    ("overdue", "Overdue"),
    ("extended", "Extended"),
    ("high_priority", "High Priority"),
    ("completed", "Completed"),
    ("not_completed", "Not Completed"),
)


def _board_filter(request) -> str:
    """The board's filter (``?filter=``), or "" for every task.

    The board's Filter menu sets it, and the Project Overview's Overdue
    number links to ``?filter=overdue``. Kanban, List, Table and Calendar
    all honour it; board.html adds it to each view's HTMX requests while
    it is on. Anything unknown means every task.
    """
    value = request.GET.get("filter", "")
    return value if value in dict(BOARD_FILTERS) else ""


def _apply_board_filter(tasks, value: str):
    """Narrow ``tasks`` to the board filter ``value``. Overdue and Extended
    are ``Task.is_overdue`` / ``Task.is_extended`` as queries: still open
    past the task deadline, and Extended when every assignee not yet done
    has an individual deadline still to come."""
    if value == "overdue":
        return tasks.overdue()
    if value == "extended":
        return tasks.extended()
    if value == "high_priority":
        return tasks.filter(is_priority=True)
    if value == "completed":
        return tasks.filter(status=TaskStatus.COMPLETED)
    if value == "not_completed":
        return tasks.exclude(status=TaskStatus.COMPLETED)
    return tasks


def _kanban_columns(request, project):
    """The board's columns, each with its tasks prefetched (narrowed by the
    board filter, so a column's count matches its cards)."""
    tasks = _apply_board_filter(
        Task.objects.prefetch_related("assigned_members", "reviewers", "assignee_deadlines", "task_logs"),
        _board_filter(request),
    )
    return (
        KanbanBoard.objects.filter(project=project)
        .prefetch_related(Prefetch("tasks", queryset=tasks))
        .order_by("container_order")
    )


def _calendar_context(request, project) -> dict:
    view = request.GET.get("view", "month")
    if view not in CALENDAR_VIEWS:
        view = "month"
    anchor = parse_view_date(request.GET.get("date"))
    tasks = (
        Task.objects.filter(project=project)
        .filter(Q(deadline__isnull=False) | Q(start_date__isnull=False))
        .prefetch_related("assigned_members", "reviewers", "assignee_deadlines", "task_logs")
    )
    tasks = _apply_board_filter(tasks, _board_filter(request))

    context = {
        "calendar_view": view,
        "calendar_date": anchor.isoformat(),
        "calendar_prev_date": shift_date(view, anchor, -1).isoformat(),
        "calendar_next_date": shift_date(view, anchor, 1).isoformat(),
        "calendar_today_date": timezone.localdate().isoformat(),
    }

    if view == "year":
        months = build_year_grid(anchor.year, tasks)
        context["calendar_year_months"] = months
        context["calendar_label"] = str(anchor.year)
    elif view == "week":
        week = build_week_grid(anchor, tasks)
        context["calendar_week"] = week
        start, end = week["days"][0].date, week["days"][-1].date
        if start.month == end.month:
            context["calendar_label"] = f"{_fmt_short(start)}–{end.day}, {end.year}"
        else:
            context["calendar_label"] = f"{_fmt_short(start)} – {_fmt_short(end)}, {end.year}"
    elif view == "day":
        day_tasks = build_day_tasks(anchor, tasks)
        context["calendar_day_tasks"] = day_tasks
        context["calendar_day_date"] = anchor
        context["calendar_label"] = f"{anchor.strftime('%A, %B')} {anchor.day}, {anchor.year}"
    else:
        weeks = build_month_grid(anchor.year, anchor.month, tasks)
        context["calendar_weeks"] = weeks
        context["calendar_label"] = anchor.strftime("%B %Y")

    return context


# --- List view: search / filter / sort -------------------------------------

LIST_SORT_CHOICES = (
    ("manual", "Board order"),
    ("name_asc", "Name A–Z"),
    ("name_desc", "Name Z–A"),
    ("deadline_asc", "Due date: soonest"),
    ("deadline_desc", "Due date: latest"),
    ("start_asc", "Start date: soonest"),
    ("newest", "Newest first"),
    ("oldest", "Oldest first"),
)

LIST_DONE_CHOICES = (
    ("", "Any completion"),
    ("complete", "Complete"),
    ("not_complete", "Not complete"),
)

LIST_PRIORITY_CHOICES = (
    ("", "Any priority"),
    ("high", "High priority"),
)


def _list_order_by(sort: str):
    """Ordering for a List view sort key.

    Tasks with no date sort last on the date keys either way, so switching
    between soonest/latest never buries the dated tasks under the undated
    ones.
    """
    if sort == "deadline_asc":
        return (F("deadline").asc(nulls_last=True), "id")
    if sort == "deadline_desc":
        return (F("deadline").desc(nulls_last=True), "id")
    if sort == "start_asc":
        return (F("start_date").asc(nulls_last=True), "id")
    return {
        "name_asc": ("name", "id"),
        "name_desc": ("-name", "id"),
        "newest": ("-created_at", "id"),
        "oldest": ("created_at", "id"),
    }.get(sort, ("container_task_order", "id"))


def _list_view_context(request, project, columns) -> dict:
    """Search / filter / sort state for the task List view.

    Groups the matching tasks by column so the template can render one
    section per column, and echoes the control values back so the full board
    page and the HTMX partial stay in step. ``columns`` is the same queryset
    the Kanban view renders, so no extra column query is needed.
    """
    q = request.GET.get("q", "").strip()
    column_filter = request.GET.get("col", "")
    done = request.GET.get("done", "")
    priority = request.GET.get("priority", "")
    assignee = request.GET.get("assignee", "")
    sort = request.GET.get("sort", "")
    board_filter = _board_filter(request)

    if sort not in dict(LIST_SORT_CHOICES):
        sort = "manual"
    if done not in dict(LIST_DONE_CHOICES):
        done = ""
    if priority not in dict(LIST_PRIORITY_CHOICES):
        priority = ""

    members = (
        ProjectMember.objects.filter(project=project, deleted=False)
        .annotate(sort_name=Min("users__given_name"))
        .order_by("sort_name", "id")
    )

    column_pks = {str(column.pk) for column in columns}
    if column_filter not in column_pks:
        column_filter = ""
    member_pks = {str(member.pk) for member in members}
    if assignee not in member_pks and assignee != "none":
        assignee = ""

    tasks = Task.objects.filter(project=project).prefetch_related(
        # The last two let a past-due task tell Overdue from Extended
        # (``Task.is_overdue``) without a query of its own.
        "assigned_members", "reviewers", "assignee_deadlines", "task_logs"
    )

    if q:
        tasks = tasks.filter(
            Q(name__icontains=q)
            | Q(description__icontains=q)
            | Q(assigned_members__name__icontains=q)
            | Q(assigned_members__users__given_name__icontains=q)
            | Q(assigned_members__users__family_name__icontains=q)
            | Q(assigned_members__users__name__icontains=q)
        )
    if column_filter:
        tasks = tasks.filter(container_id=column_filter)
    if done == "complete":
        tasks = tasks.filter(status=TaskStatus.COMPLETED)
    elif done == "not_complete":
        tasks = tasks.exclude(status=TaskStatus.COMPLETED)
    if priority == "high":
        tasks = tasks.filter(is_priority=True)
    if assignee == "none":
        tasks = tasks.filter(assigned_members__isnull=True)
    elif assignee:
        tasks = tasks.filter(assigned_members__pk=assignee)
    tasks = _apply_board_filter(tasks, board_filter)

    tasks = tasks.distinct().order_by(*_list_order_by(sort))

    by_column: dict[int, list] = {}
    for task in tasks:
        by_column.setdefault(task.container_id, []).append(task)

    is_filtered = bool(q or column_filter or done or priority or assignee or board_filter)
    groups = []
    for column in columns:
        column_tasks = by_column.get(column.pk, [])
        # An unfiltered list keeps empty columns so a Lead can drop a task
        # into one; while filtering they would just be dead sections.
        if not column_tasks and is_filtered:
            continue
        groups.append({"column": column, "tasks": column_tasks})

    return {
        "list_groups": groups,
        "list_match_count": sum(len(group["tasks"]) for group in groups),
        "list_is_filtered": is_filtered,
        # Dropping a row rewrites container_task_order, which only lines up
        # with what is on screen while the list shows every task in board
        # order.
        "list_reorderable": sort == "manual" and not is_filtered,
        "list_q": q,
        "list_col": column_filter,
        "list_done": done,
        "list_priority": priority,
        "list_assignee": assignee,
        "list_sort": sort,
        "list_sort_choices": LIST_SORT_CHOICES,
        "list_done_choices": LIST_DONE_CHOICES,
        "list_priority_choices": LIST_PRIORITY_CHOICES,
        "list_members": members,
    }


# --- Table view: one flat, sortable table of every task -------------------

# Header key -> ordering. Each header toggles between its key and "-key";
# dated columns keep undated tasks last in both directions, as the List does.
_BOARD_ORDER = ("container__container_order", "container_task_order", "id")

TABLE_SORT_FIELDS = {
    "name": (("name", "id"), ("-name", "id")),
    # Board order: by column, then position in it. The default sort; the
    # Table has no Column header of its own.
    "column": (
        _BOARD_ORDER,
        ("-container__container_order", "container_task_order", "id"),
    ),
    # How many assignees / reviewers a task has (annotated in
    # _table_view_context), board order breaking ties.
    "assignees": (("n_assignees", *_BOARD_ORDER), ("-n_assignees", *_BOARD_ORDER)),
    "reviewers": (("n_reviewers", *_BOARD_ORDER), ("-n_reviewers", *_BOARD_ORDER)),
    "start": (
        (F("start_date").asc(nulls_last=True), "id"),
        (F("start_date").desc(nulls_last=True), "id"),
    ),
    "due": (
        (F("deadline").asc(nulls_last=True), "id"),
        (F("deadline").desc(nulls_last=True), "id"),
    ),
    "priority": (("-is_priority", "name", "id"), ("is_priority", "name", "id")),
    "status": (("status", "name", "id"), ("-status", "name", "id")),
}


def _table_view_context(request, project) -> dict:
    """Every task in the project as one flat table, sorted by ``?table_sort=``.

    The value is a header key, optionally prefixed with ``-`` for descending;
    anything else falls back to board order (by column, then position in it).
    """
    raw = request.GET.get("table_sort", "")
    key = raw.lstrip("-")
    if key not in TABLE_SORT_FIELDS:
        raw, key = "column", "column"
    descending = raw.startswith("-")
    ordering = TABLE_SORT_FIELDS[key][1 if descending else 0]

    tasks = (
        Task.objects.filter(project=project)
        .select_related("container")
        .prefetch_related("assigned_members", "reviewers", "assignee_deadlines", "task_logs")
        .annotate(
            n_assignees=Count("assigned_members", distinct=True),
            n_reviewers=Count("reviewers", distinct=True),
        )
        .order_by(*ordering)
    )
    tasks = _apply_board_filter(tasks, _board_filter(request))
    return {
        "table_tasks": tasks,
        "table_sort_key": key,
        "table_sort_desc": descending,
    }


class BoardView(LoginRequiredMixin, ProjectContextMixin, TemplateView):
    """Main kanban board for a project."""

    template_name = "tasks/board.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        columns = _kanban_columns(self.request, self.project)
        context["columns"] = columns
        context["board_filter"] = _board_filter(self.request)
        context["board_filters"] = BOARD_FILTERS
        context.update(_list_view_context(self.request, self.project, columns))
        context.update(_calendar_context(self.request, self.project))
        context.update(_table_view_context(self.request, self.project))
        return context


class BoardListPartialView(LoginRequiredMixin, ProjectContextMixin, TemplateView):
    """HTMX partial that renders the list view section with fresh data.

    ``?results=1`` renders the grouped rows alone, which is what the
    search / filter / sort controls swap; they sit outside that target so
    typing in the search box never loses focus.
    """

    template_name = "tasks/task_list_view.html"

    def get_template_names(self):
        if self.request.GET.get("results") == "1":
            return ["tasks/partials/list_results.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        columns = (
            KanbanBoard.objects.filter(project=self.project)
            .order_by("container_order")
        )
        context["columns"] = columns
        context.update(_list_view_context(self.request, self.project, columns))
        return context


class BoardKanbanPartialView(LoginRequiredMixin, ProjectContextMixin, TemplateView):
    """HTMX partial that renders the kanban board section with fresh data."""

    template_name = "tasks/task_board_partial.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["columns"] = _kanban_columns(self.request, self.project)
        return context


class BoardCalendarPartialView(LoginRequiredMixin, ProjectContextMixin, TemplateView):
    """HTMX partial that renders the calendar view section for a given month.

    Only tasks with ``deadline`` and/or ``start_date`` set are included —
    tasks missing both calendar fields never reach the template.
    """

    template_name = "tasks/task_calendar_view.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_calendar_context(self.request, self.project))
        return context


class BoardTablePartialView(LoginRequiredMixin, ProjectContextMixin, TemplateView):
    """HTMX partial that renders the table view, sorted by ``?table_sort=``."""

    template_name = "tasks/task_table_view.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_table_view_context(self.request, self.project))
        return context


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


_COMPLETED_MESSAGE = (
    "This task is marked completed. A Lead has to mark it not complete before "
    "it can be changed."
)


def _completion_locked(task) -> bool:
    """True while the task stands completed, freezing what the task *is*.

    Editing it, deleting it, changing its assignees and moving it to another
    milestone all stop there. A completed task is a record of work that has
    been judged finished — and, on a project tracking contributions, work its
    contributors have already been credited for under the task's type. Letting
    that be rewritten in place would rewrite the record behind the credit.

    A lock rather than a wall: completion is reversible, and Mark Not Complete
    is the action a completed task still offers its Lead.

    Board position is deliberately left out. Which column a task sits in is
    where it is, not what it is, and dragging a finished task into Done is the
    board doing its job.
    """
    return task.status == TaskStatus.COMPLETED


class CompletedTaskLockMixin:
    """Refuse a change to a task that stands completed.

    Compose it *after* whichever mixin resolves the task, so the task is in
    hand by the time this runs: ``TaskMemberAccessMixin`` sets ``self.task``
    before calling on, and a generic editing view is asked for its own object.
    """

    def dispatch(self, request, *args, **kwargs):  # type: ignore[override]
        task = getattr(self, "task", None)
        if task is None and hasattr(self, "get_object"):
            task = self.get_object()
        if task is not None and _completion_locked(task):
            return self._completion_locked_response(request, task)
        return super().dispatch(request, *args, **kwargs)

    def _completion_locked_response(self, request, task):
        if request.headers.get("HX-Request"):
            return HttpResponse(_COMPLETED_MESSAGE, status=409)
        messages.error(request, _COMPLETED_MESSAGE)
        return redirect(
            "tasks:detail", project_pk=task.project_id, pk=task.pk
        )


class TaskCreateView(
    LoginRequiredMixin, ProjectManagerRequiredMixin, ProjectContextMixin, CreateView
):
    template_name = "tasks/task_form.html"
    form_class = TaskForm
    model = Task

    def get_template_names(self):
        # HTMX requests (kanban modal) get the bare partial; direct page
        # visits get the full base-layout wrapper.
        if self.request.headers.get("HX-Request"):
            return ["tasks/task_form.html"]
        return ["tasks/task_form_page.html"]

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["project"] = self.project
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        container_id = self.request.GET.get("container")
        if container_id:
            initial["container"] = container_id
        else:
            first = KanbanBoard.objects.filter(project=self.project).order_by("container_order").first()
            if first:
                initial["container"] = first.pk

        # A date range picked on the calendar view (?start_date=&deadline=,
        # both YYYY-MM-DD) pre-fills the new task's span; the user still
        # fills in the rest and saves — nothing is created automatically.
        # Clock times with no zone attached: they are read in whichever zone
        # the Lead saves the task in (their system timezone by default). The
        # times are the form's defaults: 9:00 AM to start, 5:00 PM to be due.
        start_date_param = parse_date(self.request.GET.get("start_date") or "")
        if start_date_param:
            initial["start_date"] = datetime.combine(start_date_param, DEFAULT_START_TIME)
        deadline_param = parse_date(self.request.GET.get("deadline") or "")
        if deadline_param:
            initial["deadline"] = datetime.combine(deadline_param, DEFAULT_DUE_TIME)
        return initial

    def form_valid(self, form):
        task: Task = form.save(commit=False)
        task.project = self.project
        task.created_by = self.project_member or _fallback_member(
            self.request.user, self.project
        )
        # container_task_order — place at the top of the column so a newly
        # created task surfaces first (Task ordering is ascending).
        min_order = Task.objects.filter(container=task.container).aggregate(
            m=Min("container_task_order")
        )["m"]
        task.container_task_order = 0 if min_order is None else min_order - 1
        task.save()
        form.save_m2m()
        # Seed a TaskLog per assignee (mirrors the Blitz createTask behaviour).
        _seed_assignee_logs(task)
        notify_new_assignees(
            [(task, _assignee_ids(task))], assigned_by=self.request.user
        )
        log_action(
            AuditAction.TASK_CREATED,
            actor=self.request.user,
            entity=task,
            project=task.project,
            metadata={"name": task.name},
        )
        if self.request.htmx if hasattr(self.request, "htmx") else self.request.headers.get(
            "HX-Request"
        ):
            # Swap the new card onto the board.
            response = render(
                self.request,
                "tasks/task_card.html",
                {"task": task, "project": self.project},
            )
            response["HX-Trigger"] = json.dumps({"task-created": {"id": task.id}})
            if self.request.POST.get("autosave") == "1":
                # Saved because its editor was closed by accident: the board
                # reloads on task-created, and this says why the task is there.
                messages.success(
                    self.request,
                    f"Task '{task.name}' was saved when its editor closed.",
                )
            return response
        messages.success(self.request, f"Task '{task.name}' created.")
        next_url = self.request.POST.get("next")
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return redirect(next_url)
        return redirect(self._detail_url(task))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_action"] = reverse(
            "tasks:create", kwargs={"project_pk": self.project.pk}
        )
        context["form_title"] = "Create a new task"
        next_url = self.request.GET.get("next") or self.request.POST.get("next")
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            context["next_url"] = next_url
        return context

    def _detail_url(self, task: Task) -> str:
        return reverse(
            "tasks:detail",
            kwargs={"project_pk": self.project.pk, "pk": task.pk},
        )


class TaskDetailPaneView(LoginRequiredMixin, ProjectContextMixin, TemplateView):
    """HTMX side-pane partial — board peek view, no base layout."""

    template_name = "tasks/task_detail_pane.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        task = get_object_or_404(
            Task.objects.select_related("container", "milestone", "created_by")
            # The pane still lists no reviewers of its own; it reports review
            # state per assignee from task_logs. ``reviewers`` is here because
            # it decides who sees the review buttons, and the chained
            # ``task_logs__reviewed_by`` / ``__completed_by`` keep the matrix
            # from firing a query per row to name who reviewed and who marked.
            .prefetch_related(
                "assigned_members",
                "reviewers",
                "task_logs__reviewed_by",
                "assignee_deadlines",
                "assignee_contribution_types",
                "task_logs__completed_by",
                "comments__author",
                "comments__mentions__user",
            ),
            project=self.project,
            pk=kwargs["pk"],
        )
        context["task"] = task
        context["comment_form"] = CommentForm()
        context.update(comment_box_context(task, self.request.user))
        context["status_block"] = _task_status_context(
            task,
            project_member=self.project_member,
            is_project_manager=self.is_project_manager,
        )
        from_pane = "board"
        referer = self.request.META.get("HTTP_REFERER", "")
        if referer:
            parsed = urlparse(referer)
            try:
                match = resolve(parsed.path)
                view_name = f"{match.namespace}:{match.url_name}" if match.namespace else match.url_name
                if view_name == "projects:detail" and match.kwargs.get("pk") == self.project.pk:
                    from_pane = "project"
            except Resolver404:
                pass
        detail_url = reverse("tasks:detail", kwargs={"project_pk": self.project.pk, "pk": kwargs["pk"]})
        context["full_page_url"] = f"{detail_url}?from_pane={from_pane}"
        return context


class TaskDetailView(LoginRequiredMixin, ProjectContextMixin, TemplateView):
    template_name = "tasks/task_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        task = get_object_or_404(
            Task.objects.select_related("container", "milestone", "created_by")
            .prefetch_related("assigned_members", "reviewers", "task_logs__comments__author", "task_logs__reviewed_by", "assignee_deadlines", "assignee_contribution_types", "task_logs__completed_by", "comments__author", "comments__mentions__user", "activities__actor", "activities__member")
            .filter(project=self.project),
            pk=kwargs["pk"],
        )
        context["task"] = task
        context["task_activities"] = task.activities.select_related("actor", "member")
        context["comment_form"] = CommentForm()
        context.update(comment_box_context(task, self.request.user))
        context["assign_form"] = TaskAssignForm(
            project=self.project,
            initial={"assigned_members": task.assigned_members.all()},
        )
        context["milestone_form"] = TaskMilestoneForm(
            project=self.project, initial={"milestone": task.milestone}
        )
        context["status_block"] = _task_status_context(
            task,
            project_member=self.project_member,
            is_project_manager=self.is_project_manager,
        )
        origin = self._resolve_origin()
        context["back_url"] = origin["url"]
        context["back_label"] = origin["label"]
        context["delete_redirect_url"] = origin["url"]
        return context

    def _resolve_origin(self) -> dict:
        """Identify the page the task was opened from.

        Returns ``{"url": ..., "label": ...}`` for either the originating
        Project view or Kanban board. Falls back to the Kanban board when
        the Referer is missing, off-host, or points elsewhere.

        When ``?from_pane=board`` or ``?from_pane=project`` is present the
        task was opened from the side pane, so append ``?open_task=<pk>`` to
        the return URL so the originating page can re-open the pane.
        """
        task_pk = self.kwargs["pk"]
        board_url = reverse("tasks:board", kwargs={"project_pk": self.project.pk})
        project_url = reverse("projects:detail", kwargs={"pk": self.project.pk})

        from_pane = self.request.GET.get("from_pane")
        if from_pane == "board":
            return {"url": f"{board_url}?open_task={task_pk}", "label": "All tasks"}
        if from_pane == "project":
            return {"url": f"{project_url}?open_task={task_pk}", "label": "Project Overview"}

        board = {"url": board_url, "label": "All tasks"}
        project = {"url": project_url, "label": "Project Overview"}
        referer = self.request.META.get("HTTP_REFERER", "")
        if not referer:
            return board
        parsed = urlparse(referer)
        if parsed.netloc and parsed.netloc != self.request.get_host():
            return board
        try:
            match = resolve(parsed.path)
        except Resolver404:
            return board
        view_name = f"{match.namespace}:{match.url_name}" if match.namespace else match.url_name
        if view_name == "projects:detail" and match.kwargs.get("pk") == self.project.pk:
            return project
        return board


class TaskUpdateView(
    LoginRequiredMixin,
    ProjectManagerRequiredMixin,
    ProjectContextMixin,
    CompletedTaskLockMixin,
    UpdateView,
):
    """Edit a task. Refused while the task stands completed — see
    :class:`CompletedTaskLockMixin`.

    Credit can be written while a task is open (a Lead verifies parts as they
    come in), so a change of contribution type is carried onto that credit.
    """

    template_name = "tasks/task_form.html"
    form_class = TaskForm
    model = Task

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["tasks/task_form.html"]
        return ["tasks/task_form_page.html"]

    def get_queryset(self):
        return Task.objects.filter(project=self.project)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["project"] = self.project
        return kwargs

    def form_valid(self, form):
        old_type = form.initial.get("contribution_type")
        # Read before saving: only members this edit adds hear about it.
        previous_ids = _assignee_ids(self.object)
        task = form.save()
        actor = self.project_member or _fallback_member(self.request.user, self.project)
        # A new timezone: members whose own dates follow the task's zone keep
        # their clock times in it, as the task's dates do.
        if form.zone_changed():
            rezone_following_members(
                task=task,
                old_zone=form.original_zone_name,
                new_zone=task.timezone,
                actor=actor,
            )
        # A moved start or due date asks what happens to members' own dates
        # of that kind (TaskForm.individual_*): shift, keep or remove them.
        for date_field, choice in form.changed_individual_dates():
            apply_task_date_change(
                task=task,
                field=date_field,
                old_value=form.original_dates[date_field],
                new_value=getattr(task, date_field),
                choice=choice,
                actor=actor,
                old_zone=form.original_zone_name,
            )
        if "contribution_type" in form.changed_data and old_type:
            from apps.contributions.services import sync_contribution_type

            sync_contribution_type(task, old_type=old_type)
        # Seed Activity & comments entries for any newly-assigned members,
        # mirroring the Assignees ("Change assignees") flow.
        _seed_assignee_logs(task)
        notify_new_assignees(
            [(task, _assignee_ids(task) - previous_ids)],
            assigned_by=self.request.user,
        )
        log_action(
            AuditAction.TASK_UPDATED,
            actor=self.request.user,
            entity=task,
            project=task.project,
            metadata={
                "name": task.name,
                **(
                    {"timezone": task.timezone, "previous_timezone": form.original_zone_name}
                    if form.zone_changed()
                    else {}
                ),
            },
        )
        if self.request.headers.get("HX-Request"):
            response = render(
                self.request,
                "tasks/partials/task_summary.html",
                {"task": task, "project": self.project},
            )
            # ``assignees-changed`` refreshes the task detail timeline if the
            # edit happened from there; ``task-updated`` refreshes board/project.
            response["HX-Trigger"] = json.dumps(
                {"task-updated": {"id": task.id}, "assignees-changed": True}
            )
            return response
        messages.success(self.request, f"Task '{task.name}' updated.")
        next_url = self.request.POST.get("next")
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return redirect(next_url)
        return redirect(
            "tasks:detail",
            project_pk=self.project.pk,
            pk=task.pk,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_action"] = reverse(
            "tasks:update",
            kwargs={"project_pk": self.project.pk, "pk": self.object.pk},
        )
        context["form_title"] = "Edit task"
        next_url = self.request.GET.get("next") or self.request.POST.get("next")
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            context["next_url"] = next_url
        return context


class TimezoneListView(LoginRequiredMixin, View):
    """The timezone picker's entries, as JSON: every zone with its city,
    country, offset right now and the words it is searched by, plus the old
    zone names browsers may report (``timezones.timezone_choices``,
    ``timezones.ALIASES``). Fetched once per page by the picker
    (``_components/timezone_select.html``) rather than written into every
    form, and cached for an hour since the offsets move only with daylight
    saving."""

    def get(self, request, *args, **kwargs):
        from apps.tasks.timezones import ALIASES, timezone_choices

        fields = ("value", "label", "offset", "display", "detail", "search")
        response = JsonResponse(
            {
                "options": [
                    {key: option[key] for key in fields} for option in timezone_choices()
                ],
                "aliases": ALIASES,
            }
        )
        response["Cache-Control"] = "private, max-age=3600"
        return response


class TaskImportView(
    LoginRequiredMixin, ProjectManagerRequiredMixin, ProjectContextMixin, FormView
):
    """Bulk-create tasks from a CSV upload. Project Lead only."""

    template_name = "tasks/task_import.html"
    form_class = TaskImportForm

    def form_valid(self, form):
        upload = form.cleaned_data["csv_file"]
        wrapped = io.TextIOWrapper(upload, encoding="utf-8", newline="")
        rows, parse_errors = parse_csv(wrapped)

        if parse_errors:
            for err in parse_errors:
                form.add_error(None, err)
            return self.form_invalid(form)

        created, row_errors = import_rows(
            self.project, rows, user=self.request.user,
            timezone=form.cleaned_data.get("timezone") or None,
        )

        if created == 0 and row_errors and row_errors[0].startswith("import failed"):
            # A hard rollback — surface it as a form error and re-render.
            for err in row_errors:
                form.add_error(None, err)
            return self.form_invalid(form)

        messages.success(self.request, f"{created} tasks imported.")
        for warning in row_errors:
            messages.warning(self.request, warning)
        return redirect("tasks:board", project_pk=self.project.pk)


class TaskDeleteView(
    LoginRequiredMixin,
    ProjectManagerRequiredMixin,
    ProjectContextMixin,
    CompletedTaskLockMixin,
    DeleteView,
):
    """Delete a task. Refused while the task stands completed — see
    :class:`CompletedTaskLockMixin`."""

    template_name = "tasks/task_confirm_delete.html"
    model = Task

    def get_queryset(self):
        return Task.objects.filter(project=self.project)

    def get_success_url(self):
        next_url = self.request.POST.get("next") or self.request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return next_url
        return reverse("tasks:board", kwargs={"project_pk": self.project.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["has_contributions"] = _task_has_contributions(self.object)
        return context

    def form_valid(self, form):
        task = self.get_object()
        # A reopened task can still carry credit a Lead entered by hand, which
        # the task's own cascade would take with it. Deleting the work is not
        # the same as withdrawing the credit for it, so say so rather than
        # doing both at once.
        if _task_has_contributions(task):
            return self._blocked_response(
                "This task has recorded contributions and can't be deleted."
            )
        task_pk = task.pk
        task_name = task.name
        project = task.project
        actor = self.request.user
        try:
            response = super().form_valid(form)
        except ProtectedError:
            # Defensive: catch any other protected relation we don't pre-check.
            return self._blocked_response(
                "This task is referenced by protected records and can't be deleted."
            )
        log_action(
            AuditAction.TASK_DELETED,
            actor=actor,
            entity_type="task",
            entity_id=task_pk,
            project=project,
            metadata={"name": task_name},
        )
        if self.request.headers.get("HX-Request"):
            return HttpResponse(status=204)
        return response

    def _blocked_response(self, message: str):
        if self.request.headers.get("HX-Request"):
            return HttpResponse(message, status=409)
        messages.error(self.request, message)
        return redirect(
            "tasks:detail", project_pk=self.project.pk, pk=self.object.pk
        )


# ---------------------------------------------------------------------------
# HTMX task actions: move, complete, reopen, assign, approve
# ---------------------------------------------------------------------------


class TaskMoveView(
    LoginRequiredMixin, ProjectManagerRequiredMixin, TaskMemberAccessMixin, View
):
    """HTMX endpoint: drag-drop task into a new column / position."""

    def post(self, request, *args, **kwargs):
        try:
            container_id = int(request.POST.get("container_id", ""))
            order = int(request.POST.get("order", "0"))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("container_id and order are required")
        try:
            move_task(self.task, container_id=container_id, order=order)
        except KanbanBoard.DoesNotExist:
            raise Http404("Destination column not found for this project.")
        return HttpResponse(status=204)


class TaskChangeColumnView(
    LoginRequiredMixin, ProjectManagerRequiredMixin, TaskMemberAccessMixin, View
):
    """Keyboard-accessible alternative to SortableJS drag-drop.

    Accepts a form POST with ``container_id`` and appends the task to the
    end of the target column. This closes the a11y gap flagged during the
    WCAG pass: users who can't use a mouse can reach the task detail page
    (which is just a link) and move the task via the same endpoint the
    drag-drop uses, without needing pointer coordination.
    """

    def post(self, request, *args, **kwargs):
        try:
            container_id = int(request.POST.get("container_id", ""))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("container_id is required")

        # Place at the end of the destination column.
        try:
            dest = KanbanBoard.objects.get(
                pk=container_id, project=self.task.project
            )
        except KanbanBoard.DoesNotExist:
            raise Http404("Destination column not found for this project.")

        end_of_col = (
            Task.objects.filter(container=dest).aggregate(m=Max("container_task_order"))["m"] or -1
        ) + 1
        move_task(self.task, container_id=dest.pk, order=end_of_col)
        messages.success(request, f"Moved to '{dest.name}'.")
        return redirect(
            "tasks:detail", project_pk=self.task.project_id, pk=self.task.pk
        )


_LOCKED_MESSAGE = (
    "This task is marked completed. A Lead has to mark it not complete before "
    "setting or clearing another member's done mark."
)

_REVIEW_CLOSED_MESSAGE = (
    "This part was marked done before the task was marked completed, so it "
    "can no longer be reviewed. A Lead has to mark the task not complete "
    "first."
)

_REVIEW_LOCKED_MESSAGE = (
    "This part was reviewed before the task was marked completed, so its "
    "review is locked. A Lead has to mark the task not complete before it "
    "can be withdrawn."
)

def _marks_locked(task) -> bool:
    """True once the task is marked completed, freezing the marks a Lead sets
    for others.

    Each assignee's own mark (``TaskCompleteView``, ``TaskUndoOwnView``), the
    reviewers' reviews (``TaskLogReviewView``) and the Lead's verifications
    (``TaskLogVerifyView``) stay open on a completed task: closing it is the
    Lead's call about the task, not a verdict on any one part. The exceptions
    are a review given before the task was completed, which nobody can
    withdraw (``services.review_locked``) — though the assignee undoing their
    own mark still clears it — and a part marked before the task was
    completed and left unreviewed, which can no longer be reviewed
    (``services.review_closed``); only parts marked after completion are. A Lead who needs to change either marks the
    task Not Complete first.
    """
    return task.status == TaskStatus.COMPLETED


def _task_status_context(task, *, project_member, is_project_manager):
    """Context for the completion status block (per-assignee done state).

    Each assignee state carries the three verdicts the pane's completion
    matrix shows side by side: ``done`` is the member's own mark on their
    current TaskLog, ``reviewed`` is a reviewer's sign-off on that same
    submission, and ``verified`` is a Lead's check that credits it, each
    named and dated.

    Everything here is derived from the task's *current* assignee and
    reviewer sets, so the matrix follows membership changes on its own: a
    member added to the task gets a row on the next render, one removed loses
    theirs (their TaskLog survives for the audit trail), and a reviewer added
    or removed gains or loses the review buttons with no stored permission to
    migrate.
    """
    current = {}
    for log in task.task_logs.all():  # newest-first; first seen is current
        current.setdefault(log.assigned_to_id, log)
    assignees = list(task.assigned_members.all())
    # A Lead can review as well as the task's named reviewers, and a Lead can
    # also set or clear anyone's done mark — the two per-member actions they
    # borrow from assignees and reviewers.
    # A task that asks for no review has no Reviewed column at all, so nobody
    # reviews on it — not even a Lead, who would otherwise still see the
    # buttons the endpoint would still honour.
    needs_review = task.needs_review
    viewer_reviews = bool(
        needs_review and (is_project_manager or task.is_reviewer(project_member))
    )
    locked = _marks_locked(task)
    individual_dates = {d.member_id: d for d in task.assignee_deadlines.all()}
    # Each member's own contribution type, on CRediT projects only: the type
    # means nothing anywhere else. A Lead may change it whether or not the
    # task is completed — it only decides what their credit is filed under,
    # and credit stays correctable after completion.
    tracks_credit = task.project.tracks_credit
    own_types = (
        {o.member_id: o for o in task.assignee_contribution_types.all()}
        if tracks_credit
        else {}
    )
    type_labels = dict(ContributionType.choices)
    can_set_type = bool(is_project_manager and tracks_credit)
    now = timezone.now()
    # A Lead sets members' own dates while the task is open; a completed
    # task has no dates left to keep.
    can_set_dates = is_project_manager and task.status != TaskStatus.COMPLETED

    def picker(value, zone):
        """A date as the Lead's date picker (datetime-local) takes it, in
        ``zone``."""
        return picker_value(value, zone)

    assignee_states = []
    for member in assignees:
        log = current.get(member.id)
        done = log is not None and log.status == TaskStatus.COMPLETED
        individual = individual_dates.get(member.id)
        own_type = own_types.get(member.id)
        effective_type = (
            own_type.contribution_type if own_type else task.contribution_type
        )
        own_start = individual.start_date if individual else None
        own_deadline = individual.deadline if individual else None
        effective_deadline = own_deadline or task.deadline
        # The zone governing the member's own dates: theirs, else the task's.
        own_zone = (individual.timezone if individual else "") or task.timezone
        reviewed = log is not None and log.is_reviewed
        verified = log is not None and log.is_verified
        # A review given before the task was completed stays as it was, and
        # so does the done mark it rests on.
        locked_review = review_locked(task, log)
        # A part marked before the task was completed and left unreviewed
        # takes no review now; one marked after it does.
        closed_review = review_closed(task, log)
        assignee_states.append(
            {
                "member": member,
                "review_locked": locked_review,
                "review_closed": closed_review,
                # Whether *this viewer* may act on *this row's* review. Open
                # on a completed task only for a part marked after it.
                "can_review": viewer_reviews and not closed_review,
                # Undoing is narrower than making: a reviewer may withdraw
                # their own verdict and no one else's, while a Lead can clear
                # any of them. A review whose reviewer's membership row was
                # deleted has no owner left to withdraw it, so it too is the
                # Lead's alone.
                "can_unreview": (
                    not locked_review
                    # A task that asks for no review offers no review action,
                    # withdrawal included. A verdict already given stays on
                    # show — see show_review below — it just has no buttons
                    # beside it any more.
                    and needs_review
                    and (
                        is_project_manager
                        or (
                            viewer_reviews
                            and log is not None
                            and project_member is not None
                            and log.reviewed_by_id == project_member.pk
                        )
                    )
                ),
                # A Lead setting or clearing someone's mark is the one action
                # a completed task still freezes.
                "can_mark_done": is_project_manager and not locked,
                # Verifying is the Lead's alone, open on a part marked done
                # whether or not the task is completed; withdrawing it is
                # open whenever there is one to withdraw.
                "can_verify": is_project_manager and done and not verified,
                "can_unverify": is_project_manager and verified,
                # The viewer's own row: they tick their own part done (and
                # untick it) from it, even once the task is completed.
                "is_viewer": bool(project_member and member.id == project_member.id),
                # The entry the review buttons act on. None until the member
                # has one seeded, in which case there is nothing to review.
                "log": log,
                "done": done,
                # Null on entries completed before completed_at existed, so the
                # cell shows the check with no date rather than a wrong one.
                "done_at": log.completed_at if log is not None else None,
                # Who set the mark. Null on entries that recorded no one, and
                # on one whose setter's membership row was deleted outright.
                "done_by": log.completed_by if log is not None else None,
                # A mark the assignee set themselves needs no name beside it —
                # the row already carries theirs. Only a mark set *for* them,
                # by a Lead, has something to add.
                "done_by_self": bool(
                    log is not None and log.completed_by_id == member.id
                ),
                "reviewed": reviewed,
                # Null on a review whose reviewer's membership row was deleted
                # outright; the cell then reports the review without a name.
                "reviewed_by": log.reviewed_by if log is not None else None,
                "reviewed_at": log.reviewed_at if log is not None else None,
                # Marked done and still waiting on a reviewer.
                "awaiting_review": done and not reviewed,
                "verified": verified,
                # Null on a verification whose Lead's membership row was
                # deleted outright; the cell then reports it without a name.
                "verified_by": log.verified_by if log is not None else None,
                "verified_at": log.verified_at if log is not None else None,
                # Marked done and still waiting on a Lead's verification.
                "awaiting_verification": done and not verified,
                # The member's own deadline, when a Lead set one: it replaces
                # the task's for them alone (``AssigneeDeadline``).
                # The member's own dates, when a Lead set them
                # (``AssigneeDeadline``): each replaces the task's for them
                # alone. ``start_date`` / ``deadline`` are what holds for
                # them, own or the task's.
                "individual": individual,
                "own_start": own_start,
                "own_deadline": own_deadline,
                "start_date": own_start or task.start_date,
                "deadline": effective_deadline,
                # The zone each of those is shown in: the member's own for
                # an own date, the task's for one they follow.
                "start_tz": own_zone if own_start else task.timezone,
                "deadline_tz": own_zone if own_deadline else task.timezone,
                # The zone saved with their own dates ("" follows the
                # task's), or None when they have no own dates — the dialog
                # then starts on the Lead's system timezone.
                "own_timezone": individual.timezone if individual else None,
                # What under their name differs from the task — only that
                # is shown there: an own start or deadline at another
                # moment than the task's, an own zone other than the
                # task's (as "Tokyo (UTC+9)"), an own type other than the
                # task's.
                "start_differs": own_start is not None and own_start != task.start_date,
                "deadline_differs": (
                    own_deadline is not None and own_deadline != task.deadline
                ),
                "zone_differs": bool(
                    individual
                    and individual.timezone
                    and canonical(individual.timezone) != canonical(task.timezone)
                ),
                "zone_short": short_zone_label(
                    own_zone, own_deadline or own_start or task.deadline
                ),
                "dates_differ": bool(
                    (own_start is not None and own_start != task.start_date)
                    or (own_deadline is not None and own_deadline != task.deadline)
                    or (
                        individual
                        and individual.timezone
                        and canonical(individual.timezone) != canonical(task.timezone)
                    )
                ),
                "type_differs": bool(
                    own_type and own_type.contribution_type != task.contribution_type
                ),
                # Late on their own deadline: open task, part not done.
                "overdue": bool(
                    effective_deadline is not None
                    and effective_deadline < now
                    and not done
                    and task.status != TaskStatus.COMPLETED
                ),
                # Can a Lead act on their dates?
                "can_set_dates": can_set_dates,
                # Pre-fill the Lead's dialog with the member's own dates
                # only: a date left empty there follows the task's.
                "own_start_input": picker(own_start, own_zone),
                "own_deadline_input": picker(own_deadline, own_zone),
                # The type their credit is filed under: their own, when a
                # Lead set one (``AssigneeContributionType``), else the
                # task's. ``own_type`` is the entry, None when they follow
                # the task's.
                "tracks_credit": tracks_credit,
                "own_type": own_type,
                "contribution_type": effective_type,
                "contribution_type_label": type_labels.get(
                    effective_type, effective_type
                ),
                "can_set_type": can_set_type,
            }
        )
    my_state = next(
        (s for s in assignee_states if project_member and s["member"].id == project_member.id),
        None,
    )
    return {
        "task": task,
        "project": task.project,
        "is_project_manager": is_project_manager,
        "assignee_states": assignee_states,
        "done_count": sum(1 for s in assignee_states if s["done"]),
        "reviewed_count": sum(1 for s in assignee_states if s["reviewed"]),
        "verified_count": sum(1 for s in assignee_states if s["verified"]),
        # What the Lead's two batch actions would verify, counted so each
        # button can say how many and switch off at none.
        "verify_done_count": sum(
            1 for s in assignee_states if s["done"] and not s["verified"]
        ),
        "verify_reviewed_count": sum(
            1
            for s in assignee_states
            if s["done"] and s["reviewed"] and not s["verified"]
        ),
        "assignee_count": len(assignee_states),
        # The Lead's "Set deadline for all not done" covers these members.
        # The Lead's "Set dates for all not done" covers these members.
        "pending_dates_count": sum(1 for s in assignee_states if not s["done"]),
        "can_set_dates": can_set_dates,
        # The Lead's per-member contribution type dialog, on CRediT projects.
        "can_set_type": can_set_type,
        "own_type_count": len(own_types),
        "contribution_type_choices": (
            _member_type_choices(task, own_types.values()) if can_set_type else []
        ),
        "is_assignee": my_state is not None,
        "my_done": bool(my_state and my_state["done"]),
        # Whether the viewer reviews on this task at all. The per-row
        # ``can_review`` above is what actually gates the buttons.
        "viewer_reviews": viewer_reviews,
        # A completed task freezes the marks a Lead sets for others, and the
        # reviews given before it was completed with the marks under them;
        # the rest stays live.
        "marks_locked": locked,
        # Whether review is expected of this task at all — see
        # ``Task.needs_review``. Gates who may review.
        "needs_review": needs_review,
        # Whether to draw the Reviewed column. Wider than needs_review by one
        # case: a verdict already given still shows after the last reviewer
        # comes off the task, because taking someone off the list does not
        # unsay what they signed. There are no buttons in it then — nobody
        # reviews on a task with no reviewers — just the standing record.
        "show_review": needs_review or any(s["reviewed"] for s in assignee_states),
        # Named in the pane under the matrix, so it is clear who the Reviewed
        # column is waiting on. Read live, like everything else here: a
        # reviewer added or removed shows up on the next render.
        "reviewers": list(task.reviewers.all()),
    }


def _respond_status_block(request, task, *, project_member, is_project_manager):
    """Render the status block for HTMX, or redirect to the task for full GETs."""
    if request.headers.get("HX-Request"):
        # Every caller reaches here having just mutated something, and not all
        # of it lands on this instance: the review rule closes and reopens the
        # task through ``task_log.task``, which is a separate object from the
        # one the view loaded. Re-read before rendering, or the swap shows the
        # state from before the click and the chip lags a click behind.
        task.refresh_from_db()
        pane_mode = request.POST.get("pane_mode") == "1"
        ctx = _task_status_context(
            task, project_member=project_member, is_project_manager=is_project_manager
        )
        ctx["pane_mode"] = pane_mode
        # The due hint swapped out of band speaks to the viewer's own deadline.
        ctx["project_member"] = project_member
        # The side pane sends surface=pane with every request made inside it
        # (an inherited hx-vals on its root), so pieces that are laid out
        # differently there come back in the pane's shape.
        ctx["surface"] = request.POST.get("surface", "")
        # A swap response: the chip and the two action-bar buttons sit outside
        # the block on every surface that renders it, so they come back out of
        # band with it. First renders leave this off — the markup around the
        # block already carries them.
        ctx["oob"] = True
        return render(request, "tasks/partials/task_status_block.html", ctx)
    return redirect("tasks:detail", project_pk=task.project_id, pk=task.pk)


class TaskStatusBlockView(LoginRequiredMixin, TaskMemberAccessMixin, View):
    """GET endpoint that re-renders the status block partial (e.g. after edits)."""

    def get(self, request, *args, **kwargs):
        pane_mode = request.GET.get("pane") == "1"
        ctx = _task_status_context(
            self.task,
            project_member=self.project_member,
            is_project_manager=self.is_project_manager,
        )
        ctx["pane_mode"] = pane_mode
        ctx["surface"] = request.GET.get("surface", "")  # see _respond_status_block
        ctx["oob"] = True  # a swap response — see _respond_status_block
        return render(request, "tasks/partials/task_status_block.html", ctx)


class TaskCompleteView(LoginRequiredMixin, TaskMemberAccessMixin, View):
    """An assignee marks *their own* part of an INDIVIDUAL-mode task done.

    Open on a completed task too: closing it is the Lead's call about the
    task, not a verdict on who did their part, so it does not stop the
    assignees still to tick from recording theirs.
    """

    def post(self, request, *args, **kwargs):
        if self.project_member is None:
            return HttpResponseBadRequest("No project membership.")
        if not self.task.assigned_members.filter(pk=self.project_member.pk).exists():
            return HttpResponseForbidden("Only an assignee can mark their part done.")
        mark_own_done(task=self.task, member=self.project_member)
        return _respond_status_block(
            request, self.task,
            project_member=self.project_member,
            is_project_manager=self.is_project_manager,
        )


class TaskUndoOwnView(LoginRequiredMixin, TaskMemberAccessMixin, View):
    """An assignee undoes their own INDIVIDUAL-mode completion.

    Open on a completed task, like :class:`TaskCompleteView` — even where a
    review given before the task was completed rests on the mark, which no
    one else can withdraw (``services.review_locked``). The mark is the
    assignee's own statement about their work, so they can always take it
    back; the review and the verification standing on it go with it, along
    with the credit the verification wrote. The task stays completed.
    """

    def post(self, request, *args, **kwargs):
        if self.project_member is None:
            return HttpResponseBadRequest("No project membership.")
        if not self.task.assigned_members.filter(pk=self.project_member.pk).exists():
            return HttpResponseForbidden("Only an assignee can undo their mark.")
        reopen_own(task=self.task, member=self.project_member)
        return _respond_status_block(
            request, self.task,
            project_member=self.project_member,
            is_project_manager=self.is_project_manager,
        )


class TaskCloseView(
    LoginRequiredMixin, ProjectManagerRequiredMixin, TaskMemberAccessMixin, View
):
    """A Project Lead closes the task (finalises Task.status = COMPLETED)."""

    def post(self, request, *args, **kwargs):
        closer = self.project_member or _fallback_member(request.user, self.project)
        close_task(task=self.task, closed_by=closer)
        return _respond_status_block(
            request, self.task,
            project_member=self.project_member,
            is_project_manager=self.is_project_manager,
        )


class TaskReopenView(
    LoginRequiredMixin, ProjectManagerRequiredMixin, TaskMemberAccessMixin, View
):
    """A Project Lead reopens a closed task, clearing all completion state."""

    def post(self, request, *args, **kwargs):
        reopen_task(task=self.task)
        return _respond_status_block(
            request, self.task,
            project_member=self.project_member,
            is_project_manager=self.is_project_manager,
        )


def _task_activity_context(task, *, is_project_manager):
    """Context for rendering the Activity timeline partial."""
    return {
        "task": task,
        "task_activities": task.activities.select_related("actor", "member"),
        "is_project_manager": is_project_manager,
    }


def _assignee_ids(task) -> set[int]:
    """The ``ProjectMember`` ids currently assigned to ``task``, from the DB."""
    return set(task.assigned_members.values_list("pk", flat=True))


def _seed_assignee_logs(task, actor=None) -> bool:
    """Seed a TaskLog (Activity & comments entry) for each assigned member
    that doesn't have one yet.

    Idempotent — keying off existing logs means it can run on create, on
    edit, and on assignee changes without producing duplicates. Returns
    ``True`` when at least one log was created.
    """
    logged_member_ids = set(task.task_logs.values_list("assigned_to_id", flat=True))
    new_members = [
        member
        for member in task.assigned_members.all()
        if member.pk not in logged_member_ids
    ]
    if new_members:
        TaskLog.objects.bulk_create([
            TaskLog(
                task=task,
                assigned_to=member,
                completed_as=(CompletedAs.TEAM if member.name else CompletedAs.INDIVIDUAL),
            )
            for member in new_members
        ])
    return bool(new_members)


class TaskAssignView(
    LoginRequiredMixin,
    ProjectManagerRequiredMixin,
    TaskMemberAccessMixin,
    CompletedTaskLockMixin,
    View,
):
    """Change a task's assignees. Refused while the task stands completed."""

    def post(self, request, *args, **kwargs):
        form = TaskAssignForm(request.POST, project=self.project)
        if not form.is_valid():
            return HttpResponseBadRequest("Invalid assignment data.")
        members = form.cleaned_data.get("assigned_members") or []
        previous_ids = _assignee_ids(self.task)
        self.task.assigned_members.set(members)
        notify_new_assignees(
            [(self.task, _assignee_ids(self.task) - previous_ids)],
            assigned_by=request.user,
        )
        # Ensure every assigned member has an Activity & comments entry. Keying
        # off existing logs makes this idempotent and also backfills members
        # assigned without a log.
        _seed_assignee_logs(self.task, actor=self.project_member)
        # Fallback for when htmx isn't running (e.g. the script failed to
        # load): a native form submit reloads the task page, which renders
        # the full timeline server-side — so seeded threads still appear.
        if not request.headers.get("HX-Request"):
            return redirect(
                "tasks:detail", project_pk=self.project.pk, pk=self.task.pk
            )
        response = render(
            request,
            "tasks/partials/task_assignees.html",
            {"task": self.task, "project": self.project},
        )
        # Tell the page to refresh the Activity & comments timeline so any
        # newly-seeded entries appear without a full reload.
        response["HX-Trigger"] = "assignees-changed"
        return response

    def get(self, request, *args, **kwargs):
        # Direct (non-HTMX) navigation lands here; send users to the task.
        return redirect(
            "tasks:detail", project_pk=self.project.pk, pk=self.task.pk
        )


class TaskActivityView(LoginRequiredMixin, TaskMemberAccessMixin, View):
    """Render the Activity & comments timeline (used for HTMX refreshes)."""

    def get(self, request, *args, **kwargs):
        return render(
            request,
            "tasks/partials/task_log_list.html",
            _task_activity_context(
                self.task, is_project_manager=self.is_project_manager
            ),
        )


class TaskMemberDoneView(LoginRequiredMixin, TaskMemberAccessMixin, View):
    """A Lead sets or clears one assignee's done mark from the matrix.

    The assignees' own two actions stay in the pane's top bar; this is the
    Lead borrowing them per row, so they can record a mark for someone who
    finished without clicking, or clear one that was set in error. The entry
    records the Lead as ``completed_by``, so it never reads as the assignee's
    own click.

    Deliberately *not* what Mark Completed does: closing a task still writes
    no marks on anybody's behalf. This is a separate, explicit, per-person
    action, and like every other mark it closes once the task is completed.

    Keyed by member rather than by TaskLog, unlike its review sibling: a mark
    belongs to a person and ``mark_own_done`` will seed the entry, so this
    still works for an assignee added without one. A review, by contrast, is a
    verdict on a specific submission that must already exist.

    POST ``done=0`` clears the mark; anything else sets it.

    The Lead check is inline rather than ``ProjectManagerRequiredMixin``: that
    mixin declines to resolve a project from a URL carrying ``pk`` plus a
    second key, and would refuse every caller. The sibling log views check the
    same way.
    """

    def post(self, request, *args, **kwargs):
        if not self.is_project_manager:
            return HttpResponseForbidden(
                "Only a Lead can set another member's done mark."
            )
        if _marks_locked(self.task):
            return HttpResponseForbidden(_LOCKED_MESSAGE)
        member = get_object_or_404(
            self.task.assigned_members.all(), pk=kwargs["member_pk"]
        )
        actor = self.project_member or _fallback_member(request.user, self.project)
        if request.POST.get("done") in {"0", "false", "no"}:
            reopen_own(task=self.task, member=member, actor=actor)
        else:
            mark_own_done(task=self.task, member=member, actor=actor)
        return _respond_status_block(
            request, self.task,
            project_member=self.project_member,
            is_project_manager=self.is_project_manager,
        )


class TaskLogReviewView(LoginRequiredMixin, TaskMemberAccessMixin, View):
    """A task reviewer marks one assignee's submission reviewed, or undoes it.

    Permission is read from the task's live ``reviewers`` set (plus the Lead
    privilege) on every request, so adding or removing a reviewer takes effect
    at once and a member removed from the project loses the action with their
    membership.

    On a completed task, a part marked done after it was completed can be
    reviewed, and that review withdrawn. A part already marked when the task
    was completed stays as it stood until a Lead reopens the task: unreviewed
    it takes no review (``services.review_closed``), reviewed it cannot be
    withdrawn (``services.review_locked``). Neither direction
    opens at all on a task that asks for no review: the Reviewed column is not
    drawn there, so the endpoint behind it should not answer either.
    Withdrawing is also narrower in who may: a reviewer may take back their
    own verdict and no one else's, while a Lead can clear any review on the
    task.

    POST ``reviewed=0`` withdraws the review; anything else records one.
    """

    def post(self, request, *args, **kwargs):
        log = get_object_or_404(
            TaskLog.objects.filter(task=self.task), pk=kwargs["log_pk"]
        )
        if not self.task.needs_review:
            return HttpResponseForbidden(
                "This task doesn't require review. Turn on Require review and "
                "name a reviewer first."
            )
        if not (self.is_project_manager or self.task.is_reviewer(self.project_member)):
            return HttpResponseForbidden(
                "Only a reviewer on this task can review submissions."
            )
        if request.POST.get("reviewed") in {"0", "false", "no"}:
            if not self.is_project_manager and not (
                self.project_member is not None
                and log.reviewed_by_id == self.project_member.pk
            ):
                return HttpResponseForbidden(
                    "Only the reviewer who signed this off, or a Lead, can "
                    "withdraw it."
                )
            if review_locked(self.task, log):
                return HttpResponseForbidden(_REVIEW_LOCKED_MESSAGE)
            clear_review(task_log=log, cleared_by=self.project_member)
        else:
            if not log.is_reviewable:
                return HttpResponseBadRequest(
                    "That assignee has not marked their part done yet."
                )
            if review_closed(self.task, log):
                return HttpResponseForbidden(_REVIEW_CLOSED_MESSAGE)
            # Reviewing someone who has since been taken off the task would
            # write a verdict the matrix no longer has a row for.
            if not self.task.assigned_members.filter(pk=log.assigned_to_id).exists():
                return HttpResponseBadRequest(
                    "That member is no longer assigned to this task."
                )
            reviewer = self.project_member or _fallback_member(
                request.user, self.project
            )
            mark_reviewed(task_log=log, reviewer=reviewer)

        return _respond_status_block(
            request, self.task,
            project_member=self.project_member,
            is_project_manager=self.is_project_manager,
        )


class TaskLogVerifyView(LoginRequiredMixin, TaskMemberAccessMixin, View):
    """A Lead verifies one member's contribution, or withdraws that.

    Verifying is what credits the member (``services.verify_part``). It is
    open on a completed task as much as an open one, and asks for the part to
    be marked done but not reviewed: a Lead may verify a part the reviewers
    have not reached.

    POST ``verified=0`` withdraws the verification; anything else records
    one. The Lead check is inline, as on the sibling log views.
    """

    def post(self, request, *args, **kwargs):
        log = get_object_or_404(
            TaskLog.objects.filter(task=self.task), pk=kwargs["log_pk"]
        )
        if not self.is_project_manager:
            return HttpResponseForbidden(
                "Only a Lead can verify a member's contribution."
            )
        actor = self.project_member or _fallback_member(request.user, self.project)
        if request.POST.get("verified") in {"0", "false", "no"}:
            unverify_part(task_log=log, actor=actor)
        else:
            if log.status != TaskStatus.COMPLETED:
                return HttpResponseBadRequest(
                    "That assignee has not marked their part done yet."
                )
            if not self.task.assigned_members.filter(pk=log.assigned_to_id).exists():
                return HttpResponseBadRequest(
                    "That member is no longer assigned to this task."
                )
            # Only the member's current entry carries their state; an older
            # one is history, and crediting it would skip what came after.
            current = (
                self.task.task_logs.filter(assigned_to_id=log.assigned_to_id)
                .order_by("-created_at")
                .first()
            )
            if current is None or current.pk != log.pk:
                return HttpResponseBadRequest(
                    "That entry is no longer the member's current one."
                )
            verify_part(task_log=log, lead=actor)
        return _respond_status_block(
            request, self.task,
            project_member=self.project_member,
            is_project_manager=self.is_project_manager,
        )


class TaskVerifyBatchView(LoginRequiredMixin, TaskMemberAccessMixin, View):
    """A Lead verifies several contributions at once.

    POST ``scope=done`` verifies every part marked done; ``scope=reviewed``
    every part marked done and reviewed. Parts already verified are skipped,
    so the action is safe to repeat. See ``services.verify_parts``.
    """

    def post(self, request, *args, **kwargs):
        if not self.is_project_manager:
            return HttpResponseForbidden(
                "Only a Lead can verify members' contributions."
            )
        scope = request.POST.get("scope")
        if scope not in VerifyScope.ALL:
            return HttpResponseBadRequest("Choose which parts to verify.")
        lead = self.project_member or _fallback_member(request.user, self.project)
        verify_parts(task=self.task, lead=lead, scope=scope)
        return _respond_status_block(
            request, self.task,
            project_member=self.project_member,
            is_project_manager=self.is_project_manager,
        )


class TaskMemberDatesView(LoginRequiredMixin, TaskMemberAccessMixin, View):
    """A Lead sets or removes members' own start dates and deadlines.

    POST ``member=<id>`` with ``start_date`` and/or ``deadline`` (and an
    optional ``reason``) sets that assignee's own dates; a date left empty
    follows the task's, and both empty removes them. ``action=clear`` removes
    them outright. POST ``scope=pending`` with either date gives it to every
    assignee who has not marked their part done; a date left empty is left
    as each member has it. Each date may be earlier or later than the
    task's, which still holds for the task and everyone else.

    Refused on a completed task: there are no dates left to keep.
    """

    def post(self, request, *args, **kwargs):
        if not self.is_project_manager:
            return HttpResponseForbidden(
                "Only a Lead can change a member's dates."
            )
        if self.task.status == TaskStatus.COMPLETED:
            return HttpResponseForbidden(
                "This task is marked completed. Mark it not complete before "
                "changing anyone's dates."
            )
        lead = self.project_member or _fallback_member(request.user, self.project)
        scope = "pending" if request.POST.get("scope") == "pending" else "member"
        member = current = None
        if scope == "member":
            member = self.task.assigned_members.filter(
                pk=request.POST.get("member") or 0
            ).first()
            if member is None:
                return HttpResponseBadRequest(
                    "That member is not assigned to this task."
                )
            if request.POST.get("action") == "clear":
                clear_member_dates(task=self.task, member=member, actor=lead)
                return _respond_status_block(
                    request, self.task,
                    project_member=self.project_member,
                    is_project_manager=self.is_project_manager,
                )
            current = self.task.assignee_deadlines.filter(member=member).first()
        pending = ()
        if scope == "pending":
            own = {d.member_id: d for d in self.task.assignee_deadlines.all()}
            pending = [(m, own.get(m.id)) for m in pending_members(self.task)]
        form = AssigneeDatesForm(
            request.POST, task=self.task, scope=scope, current=current,
            pending=pending, member=member,
        )
        if not form.is_valid():
            return HttpResponseBadRequest(
                " ".join(str(e) for errors in form.errors.values() for e in errors)
            )
        start_date = form.cleaned_data["start_date"]
        deadline = form.cleaned_data["deadline"]
        zone = form.cleaned_data["timezone"]
        reason = form.cleaned_data["reason"]
        if scope == "pending":
            if not set_pending_dates(
                task=self.task,
                lead=lead,
                start_date=UNSET if start_date is None else start_date,
                deadline=UNSET if deadline is None else deadline,
                timezone=zone,
                reason=reason or None,
            ):
                return HttpResponseBadRequest(
                    "Every assignee has already marked their part done."
                )
        else:
            set_member_dates(
                task=self.task, member=member, lead=lead,
                start_date=start_date, deadline=deadline, timezone=zone,
                reason=reason,
            )
        return _respond_status_block(
            request, self.task,
            project_member=self.project_member,
            is_project_manager=self.is_project_manager,
        )


def _member_type_choices(task, own_types) -> list[tuple[str, str]]:
    """The types a Lead can give one member: the CRediT roles plus "Other",
    and any type the task or a member already has, so opening the dialog
    never quietly rewrites a legacy one."""
    choices = credit_role_choices(task.contribution_type)
    values = {value for value, _label in choices}
    labels = dict(ContributionType.choices)
    for value in [ContributionType.OTHER, *(o.contribution_type for o in own_types)]:
        if value not in values and value in labels:
            choices.append((value, labels[value]))
            values.add(value)
    return choices


class TaskMemberContributionTypeView(LoginRequiredMixin, TaskMemberAccessMixin, View):
    """A Lead gives one assignee their own contribution type, or puts them
    back on the task's.

    POST ``member=<id>`` with ``contribution_type=<value>`` sets that
    assignee's own type; an empty ``contribution_type`` or ``action=clear``
    removes it, so the task's type applies to them again. Credit already
    verified for them moves with the change (see
    ``services.set_member_contribution_type``).

    Open whether or not the task is completed: the type only decides what
    the member's credit is filed under, and credit stays correctable. Only
    on projects tracking contributions as CRediT.
    """

    def post(self, request, *args, **kwargs):
        if not self.is_project_manager:
            return HttpResponseForbidden(
                "Only a Lead can change a member's contribution type."
            )
        if not self.project.tracks_credit:
            return HttpResponseBadRequest(
                "This project does not track contributions as CRediT."
            )
        member = self.task.assigned_members.filter(
            pk=request.POST.get("member") or 0
        ).first()
        if member is None:
            return HttpResponseBadRequest("That member is not assigned to this task.")
        value = (request.POST.get("contribution_type") or "").strip()
        if request.POST.get("action") == "clear":
            value = ""
        if value:
            current = self.task.assignee_contribution_types.filter(
                member=member
            ).first()
            allowed = {
                v for v, _label in _member_type_choices(
                    self.task, [current] if current else []
                )
            }
            if value not in allowed:
                return HttpResponseBadRequest("Choose a valid contribution type.")
        lead = self.project_member or _fallback_member(request.user, self.project)
        set_member_contribution_type(
            task=self.task, member=member, lead=lead,
            contribution_type=value or None,
        )
        return _respond_status_block(
            request, self.task,
            project_member=self.project_member,
            is_project_manager=self.is_project_manager,
        )


class TaskLogApproveView(LoginRequiredMixin, TaskMemberAccessMixin, View):
    """Project-manager only: approve / reject a TaskLog completion entry."""

    def post(self, request, *args, **kwargs):
        log = get_object_or_404(
            TaskLog.objects.filter(task=self.task), pk=kwargs["log_pk"]
        )
        if not self.is_project_manager:
            return HttpResponse(status=403)
        raw = request.POST.get("approved")
        if raw in {"true", "1", "yes"}:
            approved: bool | None = True
        elif raw in {"false", "0", "no"}:
            approved = False
        else:
            approved = None
        approve_completion(task_log=log, approved=approved, verifier_user=request.user)
        return render(
            request,
            "tasks/task_log_item.html",
            {
                "log": log,
                "task": self.task,
                "project": self.project,
                "is_project_manager": self.is_project_manager,
                "comment_form": CommentForm(),
            },
        )


# ---------------------------------------------------------------------------
# Kanban column management
# ---------------------------------------------------------------------------


class BoardColumnListView(LoginRequiredMixin, ProjectContextMixin, TemplateView):
    template_name = "tasks/board_columns.html"

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["tasks/partials/board_columns_modal.html"]
        return super().get_template_names()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["columns"] = KanbanBoard.objects.filter(project=self.project)
        context["form"] = KanbanBoardForm()
        return context


class BoardColumnCreateView(
    LoginRequiredMixin, ProjectManagerRequiredMixin, ProjectContextMixin, View
):
    def post(self, request, *args, **kwargs):
        form = KanbanBoardForm(request.POST)
        if not form.is_valid():
            return HttpResponseBadRequest("Invalid column data.")
        max_order = KanbanBoard.objects.filter(project=self.project).aggregate(
            m=Max("container_order")
        )["m"]
        column: KanbanBoard = form.save(commit=False)
        column.project = self.project
        column.container_order = 0 if max_order is None else max_order + 1
        column.save()
        return render(
            request,
            "tasks/partials/kanban_column_header.html",
            {"column": column, "project": self.project, "is_project_manager": True},
        )


class BoardColumnUpdateView(
    LoginRequiredMixin, ProjectManagerRequiredMixin, ProjectContextMixin, View
):
    def get(self, request, *args, **kwargs):
        column = get_object_or_404(
            KanbanBoard, pk=kwargs["pk"], project=self.project
        )
        form = KanbanBoardForm(instance=column)
        return render(
            request,
            "tasks/partials/kanban_column_edit.html",
            {"column": column, "form": form, "project": self.project},
        )

    def post(self, request, *args, **kwargs):
        column = get_object_or_404(
            KanbanBoard, pk=kwargs["pk"], project=self.project
        )
        form = KanbanBoardForm(request.POST, instance=column)
        if not form.is_valid():
            return HttpResponseBadRequest("Invalid column data.")
        form.save()
        return render(
            request,
            "tasks/partials/kanban_column_header.html",
            {"column": column, "project": self.project, "is_project_manager": True},
        )


class BoardColumnReorderView(
    LoginRequiredMixin, ProjectManagerRequiredMixin, ProjectContextMixin, View
):
    """Persist a new ordering for this project's kanban columns."""

    def post(self, request, *args, **kwargs):
        try:
            ids = [int(x) for x in request.POST.getlist("order")]
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Invalid order payload.")
        existing_ids = set(
            KanbanBoard.objects.filter(project=self.project).values_list(
                "id", flat=True
            )
        )
        if set(ids) != existing_ids or len(ids) != len(existing_ids):
            return HttpResponseBadRequest(
                "Order must include every column exactly once."
            )
        with transaction.atomic():
            for idx, col_id in enumerate(ids):
                KanbanBoard.objects.filter(pk=col_id, project=self.project).update(
                    container_order=idx
                )
        return HttpResponse(status=204)


class BoardColumnDeleteView(
    LoginRequiredMixin, ProjectManagerRequiredMixin, ProjectContextMixin, View
):
    def post(self, request, *args, **kwargs):
        column = get_object_or_404(
            KanbanBoard, pk=kwargs["pk"], project=self.project
        )
        if column.tasks.exists():
            return HttpResponseBadRequest(
                "Cannot delete a column that still contains tasks. "
                "Move or delete its tasks first."
            )
        column.delete()
        # Empty 200 (not 204) so htmx swaps and the row is removed.
        return HttpResponse("")


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------


class MilestoneListView(LoginRequiredMixin, ProjectContextMixin, ListView):
    template_name = "tasks/milestone_list.html"
    context_object_name = "milestones"

    def get_queryset(self):
        return Milestone.objects.filter(project=self.project).prefetch_related("tasks")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = MilestoneForm()
        return context


class MilestoneCreateView(
    LoginRequiredMixin, ProjectManagerRequiredMixin, ProjectContextMixin, View
):
    def post(self, request, *args, **kwargs):
        form = MilestoneForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                "tasks/milestone_form.html",
                {"form": form, "project": self.project, "form_title": "New milestone"},
                status=400,
            )
        milestone: Milestone = form.save(commit=False)
        milestone.project = self.project
        milestone.save()
        return render(
            request,
            "tasks/partials/milestone_item.html",
            {"milestone": milestone, "project": self.project, "is_project_manager": True},
        )


class MilestoneUpdateView(
    LoginRequiredMixin, ProjectManagerRequiredMixin, ProjectContextMixin, View
):
    def get(self, request, *args, **kwargs):
        milestone = get_object_or_404(
            Milestone, pk=kwargs["pk"], project=self.project
        )
        form = MilestoneForm(instance=milestone)
        return render(
            request,
            "tasks/milestone_form.html",
            {
                "form": form,
                "project": self.project,
                "milestone": milestone,
                "form_title": f"Edit milestone: {milestone.name}",
                "form_action": reverse(
                    "tasks:milestone_update",
                    kwargs={"project_pk": self.project.pk, "pk": milestone.pk},
                ),
            },
        )

    def post(self, request, *args, **kwargs):
        milestone = get_object_or_404(
            Milestone, pk=kwargs["pk"], project=self.project
        )
        form = MilestoneForm(request.POST, instance=milestone)
        if not form.is_valid():
            return render(
                request,
                "tasks/milestone_form.html",
                {
                    "form": form,
                    "project": self.project,
                    "milestone": milestone,
                    "form_title": f"Edit milestone: {milestone.name}",
                },
                status=400,
            )
        form.save()
        return render(
            request,
            "tasks/partials/milestone_item.html",
            {"milestone": milestone, "project": self.project, "is_project_manager": True},
        )


class MilestoneDeleteView(
    LoginRequiredMixin, ProjectManagerRequiredMixin, ProjectContextMixin, View
):
    def post(self, request, *args, **kwargs):
        milestone = get_object_or_404(
            Milestone, pk=kwargs["pk"], project=self.project
        )
        milestone.delete()
        return HttpResponse(status=204)


class MilestoneCalendarView(LoginRequiredMixin, ProjectContextMixin, View):
    """Serve project milestones as an ICS (iCalendar) feed.

    Researchers can subscribe to this URL from Google Calendar / Apple
    Calendar / Outlook to see milestone deadlines alongside their own
    events. Access is gated by project membership (via ``ProjectContextMixin``).
    """

    def get(self, request, *args, **kwargs):
        milestones = Milestone.objects.filter(project=self.project)
        ics_text = build_ics(
            milestones,
            calendar_name=f"{self.project.name} — milestones",
        )
        response = HttpResponse(
            ics_text, content_type="text/calendar; charset=utf-8"
        )
        filename = f"milestones-{slugify(self.project.name) or self.project.pk}.ics"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class TaskMilestoneAssignView(
    LoginRequiredMixin,
    ProjectManagerRequiredMixin,
    TaskMemberAccessMixin,
    CompletedTaskLockMixin,
    View,
):
    """HTMX endpoint: set / clear ``Task.milestone``.

    Refused while the task stands completed."""

    def post(self, request, *args, **kwargs):
        form = TaskMilestoneForm(request.POST, project=self.project)
        if not form.is_valid():
            return HttpResponseBadRequest("Invalid milestone selection.")
        self.task.milestone = form.cleaned_data.get("milestone")
        self.task.save(update_fields=["milestone", "updated_at"])
        return render(
            request,
            "tasks/partials/task_milestone.html",
            {"task": self.task, "project": self.project},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _respond_task_card(request, task: Task) -> HttpResponse:
    """Return the task card partial after an HTMX mutation, or redirect."""
    if request.headers.get("HX-Request"):
        return render(
            request,
            "tasks/task_card.html",
            {"task": task, "project": task.project},
        )
    return redirect("tasks:detail", project_pk=task.project_id, pk=task.pk)


def _task_has_contributions(task) -> bool:
    """True when the task has ContributionRecord rows standing against it.

    Deleting the task cascades them away, so the delete view refuses rather
    than destroying credit as a side effect. Imported lazily to avoid a hard
    dependency on the contributions app at module load.
    """
    from apps.contributions.models import ContributionRecord

    return ContributionRecord.objects.filter(task=task).exists()


def _fallback_member(user, project):
    """Return or create a ProjectMember for PMs who don't have an explicit row.

    Only used when a project manager without a membership row creates a task
    (rare, but makes the FK happy). Prefer to rely on the resolved
    ``project_member`` from ``ProjectContextMixin`` when it exists.
    """
    member = (
        ProjectMember.objects.filter(project=project, users=user, deleted=False)
        .order_by("id")
        .first()
    )
    if member is not None:
        return member
    member = ProjectMember.objects.create(project=project, name=None)
    member.users.add(user)
    return member
