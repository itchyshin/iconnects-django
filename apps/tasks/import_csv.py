"""Bulk CSV task import.

Parses a user-supplied CSV and creates :class:`Task` rows on a project.
Designed for the "50+ tasks at once" case (Ultra Plan §3, persona #3): large
consortia seeding a project board without clicking the task form 50 times.

The module is deliberately permissive at the edges (unknown contribution
types fall back to ``OTHER``; unknown assignee emails are skipped with a
warning) and strict on the core requirement (``name`` is mandatory). Any
unexpected exception aborts the whole import via ``transaction.atomic`` so a
partial load is never left behind.
"""

from __future__ import annotations

import csv
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max
from django.utils.dateparse import parse_date, parse_datetime

from apps.audit.services import AuditAction, log_action
from apps.core.enums import CompletedAs, ContributionType
from apps.projects.models import ProjectMember
from apps.tasks.models import KanbanBoard, Task, TaskLog
from apps.tasks.services import notify_new_assignees
from apps.tasks.timezones import canonical, default_zone_name, from_wall


REQUIRED_COLUMNS = ["name"]
OPTIONAL_COLUMNS = [
    "description",
    "deadline",
    "contribution_type",
    "column",
    "assignee_emails",
    "priority",
    "timezone",
]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_csv(file_like) -> tuple[list[dict], list[str]]:
    """Parse a CSV file-like object into row dicts.

    Returns ``(rows, errors)``. ``rows`` is a list of dicts keyed by the
    (stripped, lowercased) header names. ``errors`` is a list of human
    readable strings; an empty list means the header was accepted. Per-row
    validation errors (e.g. malformed dates) are NOT collected here — they
    are raised by ``import_rows`` where they have access to the row index
    and the domain context.

    Never raises.
    """

    errors: list[str] = []
    rows: list[dict] = []

    try:
        reader = csv.reader(file_like)
    except Exception as exc:  # pragma: no cover - csv.reader itself is lazy
        return [], [f"could not read csv: {exc}"]

    try:
        raw_header = next(reader)
    except StopIteration:
        return [], ["csv is empty"]
    except Exception as exc:
        return [], [f"could not read csv header: {exc}"]

    header = [(h or "").strip().lower() for h in raw_header]
    missing = [col for col in REQUIRED_COLUMNS if col not in header]
    if missing:
        errors.append(
            "missing required column(s): " + ", ".join(missing)
        )
        return [], errors

    try:
        for raw in reader:
            row: dict[str, str] = {}
            for idx, key in enumerate(header):
                if not key:
                    continue
                value = raw[idx] if idx < len(raw) else ""
                row[key] = (value or "").strip()
            rows.append(row)
    except Exception as exc:
        errors.append(f"could not read csv body: {exc}")
        return rows, errors

    return rows, errors


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_rows(
    project,
    rows: list[dict],
    *,
    user,
    timezone: str | None = None,
) -> tuple[int, list[str]]:
    """Create Tasks for each row. Returns ``(created_count, errors)``.

    Each task's zone is the row's ``timezone`` column where it names one,
    else ``timezone`` (the zone picked on the import form, which starts on
    the Lead's system timezone), else the server's. A row's deadline is
    read in its task's zone; a bare date is due at 5:00 PM there.

    All database work runs inside a single ``transaction.atomic`` block; if
    any row raises an unexpected exception the whole import is rolled back
    and ``(0, ["import failed: <exc>"])`` is returned. Row-level issues that
    are expected (blank name, unknown assignee email, unknown contribution
    type) produce warnings in the returned list but do NOT abort the import.
    """

    creator_member = _resolve_creator_member(user, project)
    if creator_member is None:
        return 0, ["you are not a member of this project"]

    # Pre-compute the contribution-type lookup once: both value and label
    # (case-insensitive) are acceptable in the CSV.
    contrib_by_value = {c.value.lower(): c.value for c in ContributionType}
    contrib_by_label = {c.label.lower(): c.value for c in ContributionType}

    errors: list[str] = []
    created = 0
    assignments: list[tuple[Task, set[int]]] = []

    try:
        with transaction.atomic():
            default_column = _ensure_default_column(project)

            for offset, row in enumerate(rows):
                row_number = offset + 2  # +1 for header, +1 for 1-indexed

                name = (row.get("name") or "").strip()
                if not name:
                    errors.append(f"row {row_number}: name is required")
                    continue

                zone, zone_err = _parse_timezone(row.get("timezone"))
                if zone_err:
                    errors.append(f"row {row_number}: {zone_err}")
                    continue
                zone = zone or canonical(timezone) or default_zone_name()

                deadline, deadline_err = _parse_deadline(row.get("deadline"), zone)
                if deadline_err:
                    errors.append(f"row {row_number}: {deadline_err}")
                    # Skip this row — a bad deadline is a hard parse error.
                    continue

                contribution_type = _parse_contribution_type(
                    row.get("contribution_type"),
                    by_value=contrib_by_value,
                    by_label=contrib_by_label,
                )
                if contribution_type is None:
                    contribution_type = ContributionType.OTHER
                    if (row.get("contribution_type") or "").strip():
                        errors.append(
                            f"row {row_number}: unknown contribution_type "
                            f"{row.get('contribution_type')!r}, using OTHER"
                        )

                column = _resolve_column(
                    project=project,
                    name=row.get("column"),
                    default=default_column,
                )

                max_order = Task.objects.filter(container=column).aggregate(
                    m=Max("container_task_order")
                )["m"]
                task_order = 0 if max_order is None else max_order + 1

                task = Task.objects.create(
                    project=project,
                    created_by=creator_member,
                    container=column,
                    container_task_order=task_order,
                    name=name,
                    description=(row.get("description") or "") or None,
                    deadline=deadline,
                    timezone=zone,
                    contribution_type=contribution_type,
                    is_priority=_parse_priority(row.get("priority")),
                )

                assignee_members, unknown_emails = _resolve_assignees(
                    project=project,
                    raw=row.get("assignee_emails"),
                )
                for email in unknown_emails:
                    errors.append(
                        f"row {row_number}: skipped unknown assignee {email!r}"
                    )
                if assignee_members:
                    task.assigned_members.set(assignee_members)
                    assignments.append(
                        (task, {member.pk for member in assignee_members})
                    )
                    for member in assignee_members:
                        TaskLog.objects.create(
                            task=task,
                            assigned_to=member,
                            completed_as=(
                                CompletedAs.TEAM
                                if member.name
                                else CompletedAs.INDIVIDUAL
                            ),
                        )

                log_action(
                    AuditAction.TASK_CREATED,
                    actor=user,
                    entity=task,
                    project=project,
                    metadata={"name": task.name, "via": "csv_import"},
                )
                created += 1

            # One email per assignee listing all their new tasks, sent once
            # the import commits; a rolled-back import tells nobody.
            notify_new_assignees(assignments, assigned_by=user)
    except Exception as exc:  # noqa: BLE001 — we want to swallow and report
        return 0, [f"import failed: {exc}"]

    return created, errors


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_creator_member(user, project) -> ProjectMember | None:
    """Return the ProjectMember row used as ``created_by`` for new tasks."""
    member = (
        ProjectMember.objects.filter(project=project, users=user, deleted=False)
        .order_by("id")
        .first()
    )
    if member is not None:
        return member
    # PMs without an explicit ProjectMember row — mirror TaskCreateView's
    # fallback so the FK is satisfied.
    member = ProjectMember.objects.create(project=project, name=None)
    member.users.add(user)
    return member


def _ensure_default_column(project) -> KanbanBoard:
    """Return the lowest-ordered column, creating 'Todo' if none exist."""
    column = (
        KanbanBoard.objects.filter(project=project)
        .order_by("container_order")
        .first()
    )
    if column is not None:
        return column
    return KanbanBoard.objects.create(
        project=project, name="Todo", container_order=0
    )


def _resolve_column(
    *,
    project,
    name: str | None,
    default: KanbanBoard,
) -> KanbanBoard:
    """Resolve (or create) a KanbanBoard for ``name`` within ``project``."""
    cleaned = (name or "").strip()
    if not cleaned:
        return default
    existing = KanbanBoard.objects.filter(
        project=project, name__iexact=cleaned
    ).first()
    if existing is not None:
        return existing
    max_order = KanbanBoard.objects.filter(project=project).aggregate(
        m=Max("container_order")
    )["m"]
    return KanbanBoard.objects.create(
        project=project,
        name=cleaned,
        container_order=0 if max_order is None else max_order + 1,
    )


def _parse_timezone(raw: Any) -> tuple[str | None, str | None]:
    """Return ``(zone, error)``. Empty → ``(None, None)``."""
    text = (raw or "").strip() if isinstance(raw, str) else ""
    if not text:
        return None, None
    zone = canonical(text)
    if zone is None:
        return None, f"unknown timezone {text!r}"
    return zone, None


def _parse_deadline(raw: Any, zone: str | None = None) -> tuple[Any, str | None]:
    """Return ``(value, error)``. Empty string → ``(None, None)``.

    Clock times are read in ``zone`` (the server's when None); a bare date
    is due at 5:00 PM there, as on the task form. A value carrying its own
    offset keeps it.
    """
    from datetime import datetime

    from apps.tasks.timezones import DEFAULT_DUE_TIME

    text = (raw or "").strip() if isinstance(raw, str) else raw
    if not text:
        return None, None
    zone = zone or default_zone_name()
    # A bare ``YYYY-MM-DD`` first: parse_datetime would take it as midnight.
    d = parse_date(text)
    if d is not None:
        return from_wall(datetime.combine(d, DEFAULT_DUE_TIME), zone), None
    # Then ``YYYY-MM-DD HH:MM`` / ``YYYY-MM-DDTHH:MM``.
    dt = parse_datetime(text)
    if dt is not None:
        return from_wall(dt, zone), None
    return None, f"invalid deadline {text!r}"


def _parse_contribution_type(
    raw: Any,
    *,
    by_value: dict[str, str],
    by_label: dict[str, str],
) -> str | None:
    """Return a ContributionType value or ``None`` if unknown/blank."""
    text = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if not text:
        return ContributionType.OTHER
    if text in by_value:
        return by_value[text]
    if text in by_label:
        return by_label[text]
    return None


def _parse_priority(raw: Any) -> bool:
    """Return ``True`` for CSV priority values like ``yes``/``true``/``1``."""
    text = (raw or "").strip().lower() if isinstance(raw, str) else ""
    return text in ("1", "true", "yes", "y", "high", "priority")


def _resolve_assignees(
    *,
    project,
    raw: Any,
) -> tuple[list[ProjectMember], list[str]]:
    """Split ``raw`` on comma/semicolon and resolve each email to a member."""
    text = (raw or "").strip() if isinstance(raw, str) else ""
    if not text:
        return [], []

    emails: list[str] = []
    for chunk in text.replace(";", ",").split(","):
        email = chunk.strip()
        if email:
            emails.append(email)

    if not emails:
        return [], []

    User = get_user_model()
    resolved: list[ProjectMember] = []
    unknown: list[str] = []
    seen: set[int] = set()

    for email in emails:
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            unknown.append(email)
            continue
        member = (
            ProjectMember.objects.filter(
                project=project, users=user, deleted=False
            )
            .order_by("id")
            .first()
        )
        if member is None:
            unknown.append(email)
            continue
        if member.pk in seen:
            continue
        seen.add(member.pk)
        resolved.append(member)

    return resolved, unknown
