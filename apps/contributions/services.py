"""Service helpers for the contributions ledger.

A Lead verifying a member's part is the ledger event:
:func:`record_member_contribution` writes that member's credit and
:func:`withdraw_member_contributions` takes it back when the verification,
or the done mark under it, is withdrawn. Whether the task is open or closed
plays no part. The read side (``get_user_contributions``,
``get_project_contributions``, and the matrix builders) is kept separate so
the ledger could later be extracted or replaced.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.audit.services import AuditAction, log_action
from apps.core.enums import (
    CREDIT_ROLES,
    ContributionType,
    TaskStatus,
    sort_contribution_types,
)
from apps.projects.models import ContributionTracking, Project, ProjectMember
from apps.tasks.models import Task, TaskLog

from .models import ContributionRecord

User = get_user_model()


def eligible_contributions(task: Task) -> list[tuple[ProjectMember, TaskLog]]:
    """``(assignee, task_log)`` for everyone who has earned credit on ``task``.

    An assignee qualifies once their current TaskLog is marked COMPLETED and
    a Lead has verified it. Verification is the Lead's own check on the
    work, so a review is not asked for on top: a Lead may verify a part the
    reviewers have not reached.

    Read off the task's *live* assignee set, so a member taken off the task
    stops qualifying here (credit already written for them stands).
    """
    current: dict[int, TaskLog] = {}
    for log in task.task_logs.all():  # newest-first; first seen is current
        current.setdefault(log.assigned_to_id, log)

    eligible: list[tuple[ProjectMember, TaskLog]] = []
    for member in task.assigned_members.all():
        log = current.get(member.pk)
        if log is None or log.status != TaskStatus.COMPLETED:
            continue
        if not log.is_verified:
            continue
        eligible.append((member, log))
    return eligible


@transaction.atomic
def record_member_contribution(
    task_log: TaskLog,
    *,
    verifier_user: Optional[User] = None,
) -> list[ContributionRecord]:
    """Write the credit one verified part earned, one row per user behind it.

    Called from ``tasks.services.verify_part`` once a Lead has verified the
    part. Only projects tracking contributions as CRediT keep a ledger, so
    this is a no-op elsewhere.

    ``verifier_user`` is the Lead who verified it, stored as ``verified_by``;
    the verification time is stored as ``closed_at``, the moment the credit
    became final.

    The credit is filed under the member's effective type
    (``Task.contribution_type_for``): their own, when a Lead gave them one,
    else the task's.

    Idempotent: the ``(task, user)`` unique constraint plus ``get_or_create``
    means verifying twice writes no second row, and a record a Lead entered
    by hand for the same person is left as it is.
    """
    task = task_log.task
    if not task.project.tracks_credit:
        return []
    when = task_log.verified_at or timezone.now()
    contribution_type = task.contribution_type_for(task_log.assigned_to)
    created: list[ContributionRecord] = []
    for user in task_log.assigned_to.users.all():
        obj, was_created = ContributionRecord.objects.get_or_create(
            task=task,
            user=user,
            defaults=dict(
                task_log=task_log,
                project=task.project,
                contribution_type=contribution_type,
                verified_by=verifier_user,
                closed_at=when,
            ),
        )
        if was_created:
            created.append(obj)
    return created


@transaction.atomic
def withdraw_member_contributions(task: Task, member: ProjectMember) -> int:
    """Take back the credit one assignee earned on ``task``.

    Called when a Lead withdraws the verification, or the assignee's done
    mark under it is undone. Hand-entered records stay: they were never
    derived from the marks, so a mark changing says nothing about them.
    """
    deleted, _by_model = ContributionRecord.objects.filter(
        task=task, task_log__assigned_to=member, added_by_hand=False
    ).delete()
    return deleted


def sync_contribution_type(task: Task, *, old_type: str) -> int:
    """Carry a change of the task's contribution type onto its credit.

    Credit can be written while a task is still open and editable, so a Lead
    changing the type afterwards would otherwise leave verified records filed
    under the old one. Only records still under ``old_type`` move: one a Lead
    corrected by hand to another role keeps that correction, and
    hand-entered records keep their own. Neither does the credit of a member
    a Lead gave a type of their own: the task's type is not theirs.
    """
    if old_type == task.contribution_type:
        return 0
    own_type_members = task.assignee_contribution_types.values("member_id")
    return (
        ContributionRecord.objects.filter(
            task=task, added_by_hand=False, contribution_type=old_type
        )
        .exclude(task_log__assigned_to__in=own_type_members)
        .update(contribution_type=task.contribution_type)
    )


def sync_member_contribution_type(
    task: Task, member: ProjectMember, *, old_type: str, new_type: str
) -> int:
    """Carry a change of one member's effective type onto their credit.

    A Lead giving a member a type of their own, changing it, or putting them
    back on the task's, after their part was already verified would
    otherwise leave that credit under the old type. As with
    :func:`sync_contribution_type`, only the automatic records still under
    ``old_type`` move; hand-entered and hand-corrected ones stay.
    """
    if old_type == new_type:
        return 0
    return ContributionRecord.objects.filter(
        task=task,
        task_log__assigned_to=member,
        added_by_hand=False,
        contribution_type=old_type,
    ).update(contribution_type=new_type)


def get_user_contributions(
    user: User,
    *,
    project: Optional[Project] = None,
):
    """Return a user's contributions, optionally scoped to one project."""
    qs = ContributionRecord.objects.filter(user=user).select_related(
        "task", "project", "verified_by"
    )
    if project is not None:
        qs = qs.filter(project=project)
    return qs


def get_project_contributions(project: Project):
    """All contributions for a project, newest first."""
    return (
        ContributionRecord.objects.filter(project=project)
        .select_related("task", "user", "verified_by")
    )


def get_user_contribution_counts_by_type(
    user: User, *, project: Optional[Project] = None
) -> dict[str, int]:
    """A user's contribution counts by type, optionally scoped to one project."""
    qs = ContributionRecord.objects.filter(user=user)
    if project is not None:
        qs = qs.filter(project=project)
    counts: dict[str, int] = defaultdict(int)
    for ctype in qs.values_list("contribution_type", flat=True):
        counts[ctype] += 1
    # Return a plain dict keyed by the enum value.
    return dict(counts)


def get_contribution_tiles(
    user: User, *, project: Optional[Project] = None
) -> list[tuple[str, int]]:
    """``(label, count)`` for every contribution type, in the app's order.

    The whole taxonomy, zeros included: the card is read to see the shape
    of someone's credit, and which roles they have *not* earned is part of
    that shape. Feeds ``_components/contribution_card.html`` on both the
    researcher profile and the Contributions module, so the two show the
    same card rather than two views of the same numbers.

    The legacy ``WRITING`` type is no longer a role anyone can be credited
    with, so it is left off unless this user actually holds credit under
    it — a zero there is not part of anyone's shape.
    """
    counts = get_user_contribution_counts_by_type(user, project=project)
    labels = dict(ContributionType.choices)
    values = [
        v
        for v in sort_contribution_types(labels)
        if v != ContributionType.WRITING or counts.get(v, 0)
    ]
    return [(labels[v], counts.get(v, 0)) for v in values]


def get_user_credit_projects(user: User) -> list[Project]:
    """Projects where this user could hold credit — theirs, tracking CRediT.

    The filter's option list. Offering every project they belong to would
    list ones that can never show a row; offering only the ones they have
    already earned in would hide the answer to "have I been credited on
    this project at all?".
    """
    return list(
        Project.objects.filter(
            project_members__users=user,
            project_members__deleted=False,
            contribution_tracking=ContributionTracking.CREDIT,
        )
        .distinct()
        .order_by("name")
    )


def get_user_contributions_matrix(
    user: User,
    *,
    project: Optional[Project] = None,
    all_types: bool = False,
) -> dict:
    """Build a projects × CRediT-role matrix of one user's own credit.

    The same shape as :func:`get_project_contributions_matrix`, with
    ``rows`` keyed on ``"project"`` rather than ``"user"``. Columns are the
    roles this user has been credited with, or the whole taxonomy when
    ``all_types`` is set.
    """
    labels = dict(ContributionType.choices)
    qs = ContributionRecord.objects.filter(user=user)
    if project is not None:
        qs = qs.filter(project=project)
    records = list(
        qs.select_related("project").only(
            "project_id", "contribution_type", "project__name"
        )
    )

    used_types = {rec.contribution_type for rec in records}
    if all_types:
        type_values = sort_contribution_types(set(CREDIT_ROLES) | used_types)
    else:
        type_values = sort_contribution_types(used_types)
    type_choices = [(v, labels.get(v, v)) for v in type_values]

    projects_by_id: dict[int, Project] = {}
    per_project: dict[int, dict[str, int]] = {}
    column_totals: dict[str, int] = {v: 0 for v in type_values}
    grand_total = 0

    for rec in records:
        projects_by_id.setdefault(rec.project_id, rec.project)
        counts = per_project.setdefault(
            rec.project_id, {v: 0 for v in type_values}
        )
        counts[rec.contribution_type] += 1
        column_totals[rec.contribution_type] += 1
        grand_total += 1

    rows = [
        {
            "project": projects_by_id[pid],
            "counts": counts,
            "total": sum(counts.values()),
        }
        for pid, counts in per_project.items()
    ]
    rows.sort(key=lambda r: (-r["total"], r["project"].name.lower()))

    column_totals["grand_total"] = grand_total
    return {"types": type_choices, "rows": rows, "totals": column_totals}


def project_member_users(project: Project) -> list[User]:
    """Every user holding a live membership on ``project``, name-sorted."""
    return list(
        User.objects.filter(
            project_memberships__project=project,
            project_memberships__deleted=False,
        )
        .distinct()
        .order_by("given_name", "family_name", "name", "email")
    )


#: How the matrix is ordered when nothing is asked for: the busiest members
#: first. ``MATRIX_SORT_MEMBER`` and ``MATRIX_SORT_TOTAL`` are the two
#: columns that are not a CRediT role; any role value sorts by that column.
MATRIX_SORT_MEMBER = "member"
MATRIX_SORT_TOTAL = "total"
MATRIX_SORT_DEFAULT = MATRIX_SORT_TOTAL
MATRIX_DIRECTION_DEFAULT = "desc"


def resolve_matrix_sort(sort: str | None, direction: str | None) -> tuple[str, str]:
    """Normalise a requested ordering, falling back to the default.

    Anything unrecognised — a role that is not in the taxonomy, a direction
    that is not asc or desc — lands on the default rather than erroring: a
    sort is a way of looking at the grid, and a stale link asking for a
    column that is no longer there should still show the grid.
    """
    if sort not in {MATRIX_SORT_MEMBER, MATRIX_SORT_TOTAL} and sort not in dict(
        ContributionType.choices
    ):
        sort = MATRIX_SORT_DEFAULT
    if direction not in {"asc", "desc"}:
        # Names read forwards, counts read biggest-first.
        direction = "asc" if sort == MATRIX_SORT_MEMBER else MATRIX_DIRECTION_DEFAULT
    return sort, direction


def get_project_contributions_matrix(
    project: Project,
    *,
    only_used: bool = False,
    sort: str | None = None,
    direction: str | None = None,
) -> dict:
    """Build a members × CRediT-role matrix for a project.

    Returns a dict with:
        - ``types``:  list of ``(value, label)`` for the columns.
        - ``rows``:   list of ``{"user": User, "counts": {type: int},
                      "total": int}`` in the requested order.
        - ``totals``: ``{type: int}`` column totals, plus ``"grand_total"``.
                      Underscore-free keys so templates can reach them.
        - ``sort`` / ``direction``: the ordering actually applied, so the
          table can mark the column it sorted by.

    By default the grid is the whole picture: every CRediT role across the
    top whether or not this project has used it, and every current member
    down the side whether or not they have earned anything. A zero is worth
    reading — it says which roles the project has yet to cover and who has
    yet to be credited — and a grid whose shape depends on the data is hard
    to compare against another project's.

    ``only_used`` trims it to what has been earned: roles with a non-zero
    column, members with a non-zero row. The page offers it as a filter for
    when the full grid is mostly empty.

    ``sort`` names a column — ``"member"``, ``"total"``, or a contribution
    type — and ``direction`` is ``"asc"`` or ``"desc"``. Members' names
    break every tie, so the order is stable however the counts fall.

    Either way, a role off the taxonomy that the project *has* used (a task
    left as "Other", say) keeps its column, and anyone who earned credit
    before leaving the project keeps their row.
    """
    labels = dict(ContributionType.choices)
    sort, direction = resolve_matrix_sort(sort, direction)

    records = list(
        ContributionRecord.objects.filter(project=project)
        .select_related("user")
        .only(
            "user_id",
            "contribution_type",
            "user__name",
            "user__email",
            "user__given_name",
            "user__family_name",
        )
    )

    used_types = {rec.contribution_type for rec in records}
    # CRediT's own roles, plus anything off the taxonomy this project has
    # used, all in the one order the app lists contribution types in.
    type_values = sort_contribution_types(set(CREDIT_ROLES) | used_types)

    users_by_id: dict[int, User] = {u.pk: u for u in project_member_users(project)}
    per_user: dict[int, dict[str, int]] = {
        uid: {v: 0 for v in type_values} for uid in users_by_id
    }
    column_totals: dict[str, int] = {v: 0 for v in type_values}
    grand_total = 0

    for rec in records:
        users_by_id.setdefault(rec.user_id, rec.user)
        counts = per_user.setdefault(rec.user_id, {v: 0 for v in type_values})
        counts[rec.contribution_type] += 1
        column_totals[rec.contribution_type] += 1
        grand_total += 1

    if only_used:
        type_values = [v for v in type_values if column_totals[v]]

    rows = [
        {
            "user": users_by_id[uid],
            "counts": {v: counts.get(v, 0) for v in type_values},
            "total": sum(counts.values()),
        }
        for uid, counts in per_user.items()
    ]
    if only_used:
        rows = [row for row in rows if row["total"]]
    _sort_matrix_rows(rows, sort=sort, direction=direction)

    totals = {v: column_totals[v] for v in type_values}
    totals["grand_total"] = grand_total
    return {
        "types": [(v, labels.get(v, v)) for v in type_values],
        "rows": rows,
        "totals": totals,
        "sort": sort,
        "direction": direction,
    }


def _sort_matrix_rows(rows: list[dict], *, sort: str, direction: str) -> None:
    """Order the matrix rows in place, names breaking every tie.

    Sorted by name first and then by the requested column, which is stable
    in Python — so members who sit level on that column stay in name order
    rather than in whatever order the records happened to arrive.
    """
    rows.sort(key=lambda row: user_sort_key(row["user"]))
    if sort == MATRIX_SORT_MEMBER:
        if direction == "desc":
            rows.reverse()
        return
    if sort == MATRIX_SORT_TOTAL:
        key = lambda row: row["total"]  # noqa: E731
    else:
        key = lambda row: row["counts"].get(sort, 0)  # noqa: E731
    rows.sort(key=key, reverse=direction == "desc")


def user_sort_key(user: User) -> str:
    """Name a user sorts under — the same string they are displayed under."""
    return (user.display_name or "").lower()


def user_display_name(user: User) -> str:
    """A user's full name, falling back to their account name, then email."""
    return user.display_name


# ---------------------------------------------------------------------------
# Corrections — a lead's hand on the ledger
#
# Credit is a claim about who did what, so a wrong claim has to be fixable:
# a role recorded from the wrong task type, someone credited who should not
# have been, someone missed who should have been. Each correction writes an
# audit row naming who made it.
# ---------------------------------------------------------------------------


@transaction.atomic
def update_contribution(
    record: ContributionRecord,
    *,
    contribution_type: str,
    actor=None,
) -> ContributionRecord:
    """Correct the role a contribution is recorded under."""
    previous = record.contribution_type
    if previous == contribution_type:
        return record
    record.contribution_type = contribution_type
    record.save(update_fields=["contribution_type", "updated_at"])
    _log_contribution_action(
        AuditAction.CONTRIBUTION_UPDATED,
        record,
        actor=actor,
        extra={"from": previous, "to": contribution_type},
    )
    return record


@transaction.atomic
def remove_contribution(record: ContributionRecord, *, actor=None) -> None:
    """Withdraw a contribution a lead judges shouldn't stand."""
    _log_contribution_action(AuditAction.CONTRIBUTION_REMOVED, record, actor=actor)
    record.delete()


@transaction.atomic
def add_contribution(
    *,
    task: Task,
    user: User,
    contribution_type: str,
    actor=None,
) -> ContributionRecord:
    """Credit ``user`` on ``task`` by hand.

    For work the automatic writer missed — someone who did the work without
    being an assignee, or whose mark never went in. The record carries no
    submission, because there is none behind it: it stands on the lead's
    say-so, which is what ``added_by_hand`` records.

    Idempotent against the one-credit-per-person-per-task rule: crediting
    someone who already holds credit on the task corrects the role of the
    row they have rather than raising.
    """
    record = ContributionRecord.objects.filter(task=task, user=user).first()
    if record is not None:
        return update_contribution(
            record, contribution_type=contribution_type, actor=actor
        )

    record = ContributionRecord.objects.create(
        task=task,
        task_log=None,
        user=user,
        project=task.project,
        contribution_type=contribution_type,
        verified_by=actor if getattr(actor, "pk", None) else None,
        closed_at=timezone.now(),
        added_by_hand=True,
    )
    _log_contribution_action(AuditAction.CONTRIBUTION_ADDED, record, actor=actor)
    return record


def _log_contribution_action(action, record, *, actor=None, extra=None) -> None:
    metadata = {
        "task_name": record.task.name,
        "user_id": record.user_id,
        "contribution_type": record.contribution_type,
    }
    if extra:
        metadata.update(extra)
    log_action(
        action,
        actor=actor if getattr(actor, "pk", None) else None,
        entity=record.task,
        project=record.project,
        metadata=metadata,
    )
