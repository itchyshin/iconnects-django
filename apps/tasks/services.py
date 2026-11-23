"""Service helpers for the tasks app.

Small, compact helpers used by the HTMX views. They keep view bodies
focused on request/response handling and concentrate the mutation logic
(ordering, TaskLog audit entries, approval state) here.
"""

from __future__ import annotations

from typing import Iterable

from django.db import transaction
from django.utils import timezone

from apps.audit.services import AuditAction, log_action
from apps.core.enums import CompletedAs, TaskStatus
from apps.projects.models import ProjectMember
from apps.tasks.models import (
    ActivityEvent,
    AssigneeContributionType,
    AssigneeDeadline,
    KanbanBoard,
    Task,
    TaskActivity,
    TaskLog,
)


@transaction.atomic
def move_task(task: Task, *, container_id: int, order: int) -> Task:
    """Move ``task`` to ``container_id`` at position ``order``.

    Rewrites ``container_task_order`` on sibling tasks in both the source
    and destination columns so each column ends up densely numbered
    ``0, 1, 2, ...`` in the intended sequence.
    """

    new_container = KanbanBoard.objects.select_for_update().get(
        pk=container_id, project=task.project
    )
    source_container_id = task.container_id
    # Pull sibling ids (excluding the moving task) in current order.
    if new_container.id == source_container_id:
        siblings = list(
            Task.objects.filter(container_id=new_container.id)
            .exclude(pk=task.pk)
            .order_by("container_task_order", "id")
            .values_list("id", flat=True)
        )
        order = max(0, min(order, len(siblings)))
        siblings.insert(order, task.pk)
        for idx, tid in enumerate(siblings):
            Task.objects.filter(pk=tid).update(
                container_task_order=idx, container_id=new_container.id
            )
    else:
        # Renumber source column (excluding moving task).
        source_ids = list(
            Task.objects.filter(container_id=source_container_id)
            .exclude(pk=task.pk)
            .order_by("container_task_order", "id")
            .values_list("id", flat=True)
        )
        for idx, tid in enumerate(source_ids):
            Task.objects.filter(pk=tid).update(container_task_order=idx)
        # Insert into destination column at ``order``.
        dest_ids = list(
            Task.objects.filter(container_id=new_container.id)
            .order_by("container_task_order", "id")
            .values_list("id", flat=True)
        )
        order = max(0, min(order, len(dest_ids)))
        dest_ids.insert(order, task.pk)
        for idx, tid in enumerate(dest_ids):
            Task.objects.filter(pk=tid).update(
                container_task_order=idx, container_id=new_container.id
            )

    task.refresh_from_db()
    return task


@transaction.atomic
def complete_task(
    *,
    task: Task,
    completed_by: ProjectMember,
    completed_as: str = CompletedAs.INDIVIDUAL,
) -> TaskLog:
    """Append a COMPLETED TaskLog entry and flip Task.status.

    Creates one TaskLog per currently-assigned ProjectMember so the audit
    trail keeps its row-per-assignee shape. If no assignees exist, a
    single log is recorded against ``completed_by``.
    """

    assignees = list(task.assigned_members.all())
    if not assignees:
        assignees = [completed_by]

    logs = []
    for assignee in assignees:
        log = TaskLog.objects.create(
            task=task,
            assigned_to=assignee,
            completed_by=completed_by,
            status=TaskStatus.COMPLETED,
            completed_as=completed_as,
            completed_at=timezone.now(),
        )
        logs.append(log)

    task.status = TaskStatus.COMPLETED
    task.closed_by = completed_by
    task.closed_at = timezone.now()
    task.save(
        update_fields=["status", "closed_by", "closed_at", "updated_at"]
    )

    completer_user = completed_by.users.first()
    log_action(
        AuditAction.TASK_COMPLETED,
        actor=completer_user,
        entity=task,
        project=task.project,
        metadata={"name": task.name},
    )

    _notify_task_completed(task=task, completed_by=completed_by)

    # Return the log entry that actually represents the caller, if present.
    for log in logs:
        if log.assigned_to_id == completed_by.id:
            return log
    return logs[0]


@transaction.atomic
def reopen_task(*, task: Task) -> Task:
    """Reopen a closed task (Lead action), flipping ``Task.status`` back.

    The mirror of :func:`close_task`, and status-only for the same reason:
    the assignees' marks, the reviews and the Lead's verifications all
    survive, and so does the credit those verifications wrote. Credit stands
    on a Lead verifying each part, not on the task's status.

    Who closed it goes: the completion it named no longer stands, and the
    next close writes its own.
    """
    task.status = TaskStatus.NOT_COMPLETED
    task.closed_by = None
    task.closed_at = None
    task.save(
        update_fields=["status", "closed_by", "closed_at", "updated_at"]
    )
    return task


def marked_after_closing(task: Task, log: TaskLog | None) -> bool:
    """True when ``log`` is a part marked done after ``task`` was completed.

    On a completed task only such a part is open to a new review: one that
    was already marked when the Lead completed the task was in front of
    them then, and stays as it stood — reviewed or not — until a Lead
    reopens it. A part with no completion time (marked before that field
    existed) counts as marked before.
    """
    return bool(
        task.status == TaskStatus.COMPLETED
        and task.closed_at is not None
        and log is not None
        and log.status == TaskStatus.COMPLETED
        and log.completed_at is not None
        and log.completed_at > task.closed_at
    )


def review_closed(task: Task, log: TaskLog | None) -> bool:
    """True when a completed task no longer takes a review on ``log``.

    That is an unreviewed part marked done before the task was completed —
    see :func:`marked_after_closing`. Its assignee can still undo and redo
    the mark, which brings it back as marked after closing, and reviewable.
    """
    return bool(
        task.status == TaskStatus.COMPLETED
        and log is not None
        and log.status == TaskStatus.COMPLETED
        and not log.is_reviewed
        and not marked_after_closing(task, log)
    )


def review_locked(task: Task, log: TaskLog | None) -> bool:
    """True when ``log``'s review was given before ``task`` was completed.

    Such a review was part of what stood when the Lead marked the task
    completed, so nobody can withdraw it until a Lead reopens the task. A
    review given after the task was completed is not locked, and can be
    withdrawn as usual.

    The one way past it is the assignee's own: undoing their done mark
    (:func:`reopen_own`) is open on a closed task whatever its review, and
    takes the review and the verification with it, since both rest on a
    mark that no longer stands.
    """
    return bool(
        task.status == TaskStatus.COMPLETED
        and task.closed_at is not None
        and log is not None
        and log.reviewed_at is not None
        and log.reviewed_at <= task.closed_at
    )


def _current_log_for(task: Task, member: ProjectMember) -> TaskLog | None:
    """The member's most-recent TaskLog on this task (their current state)."""
    return (
        task.task_logs.filter(assigned_to=member).order_by("-created_at").first()
    )


@transaction.atomic
def mark_own_done(
    *, task: Task, member: ProjectMember, actor: ProjectMember | None = None
) -> TaskLog:
    """Mark a single assignee's part of an INDIVIDUAL-mode task done.

    Updates the member's current TaskLog in place (or seeds one) and leaves
    ``Task.status`` untouched — closing the whole task is a separate, Lead-only
    action handled by :func:`close_task`.

    ``actor`` is who clicked, recorded as ``completed_by``; it defaults to
    ``member``, the assignee marking their own part. A Lead marking someone
    else done passes themselves, so the entry says who really set it rather
    than crediting the assignee with a click they never made.

    The mark is the assignee's statement about their own work, so it stays
    open after a Lead closes the task: on a task with many assignees, the
    Lead often closes it before everyone has ticked. Marking earns no credit
    by itself; that waits on a Lead verifying the part (:func:`verify_part`).

    Every change of the mark writes an audit row, so who marked whose part
    done, and when, survives the TaskLog being updated in place.
    """
    log = _current_log_for(task, member)
    if log is None:
        log = TaskLog(
            task=task, assigned_to=member, completed_as=CompletedAs.INDIVIDUAL
        )
    was_done = log.pk is not None and log.status == TaskStatus.COMPLETED
    log.status = TaskStatus.COMPLETED
    log.completed_by = actor or member
    log.completed_as = CompletedAs.INDIVIDUAL
    log.completed_at = timezone.now()
    log.save()
    if not was_done:
        _log_mark_action(
            AuditAction.TASK_PART_DONE, task_log=log, actor_member=actor or member
        )
    return log


@transaction.atomic
def reopen_own(
    *, task: Task, member: ProjectMember, actor: ProjectMember | None = None
) -> TaskLog | None:
    """Undo a single assignee's INDIVIDUAL-mode completion.

    A review and a verification are verdicts on a submission, so withdrawing
    the submission withdraws both with it: the names and dates are cleared
    rather than left standing over work that is no longer marked done, and
    the credit the verification wrote goes too. If the member marks their
    part done again it comes back unreviewed and unverified, waiting on a
    fresh look.

    ``actor`` is who clicked, used for the audit rows; it defaults to
    ``member``, the assignee undoing their own mark.

    Like marking, undoing stays open on a closed task, and never reopens it:
    the task's completion is the Lead's call, not a sum of the marks.
    """
    log = _current_log_for(task, member)
    if log is None:
        return None
    was_done = log.status == TaskStatus.COMPLETED
    had_review = log.is_reviewed or log.reviewed_by_id is not None
    had_verification = log.is_verified or log.verified_by_id is not None
    log.status = TaskStatus.NOT_COMPLETED
    log.completed_at = None
    log.reviewed_by = None
    log.reviewed_at = None
    log.verified_by = None
    log.verified_at = None
    log.save(
        update_fields=[
            "status",
            "completed_at",
            "reviewed_by",
            "reviewed_at",
            "verified_by",
            "verified_at",
        ]
    )
    if had_review:
        _log_review_action(
            AuditAction.TASK_REVIEW_CLEARED,
            task_log=log,
            actor_member=actor or member,
            reason="the done mark it stood on was reverted",
        )
    if had_verification:
        _log_verify_action(
            AuditAction.TASK_PART_UNVERIFIED,
            task_log=log,
            actor_member=actor or member,
            reason="the done mark it stood on was reverted",
        )
    _withdraw_member_contributions(task, member)
    if was_done:
        _log_mark_action(
            AuditAction.TASK_PART_UNDONE, task_log=log, actor_member=actor or member
        )
    return log


@transaction.atomic
def close_task(
    *, task: Task, closed_by: ProjectMember, reason: str | None = None
) -> Task:
    """Close a task, finalising ``Task.status`` to COMPLETED.

    A Lead's action, always allowed: a Lead can close a task whenever they
    judge it done, whether or not every assignee has marked their own part.
    Nothing closes a task on its own; reviews in particular do not.

    Only the task's own status moves. Per-assignee TaskLogs are left exactly
    as they are in *both* completion modes: "marked done" is an assignee's
    statement about their own work, and nothing a Lead does writes one on
    their behalf. So an outstanding assignee stays outstanding rather than
    being credited with work they never marked — the gap the All tasks view
    surfaces as "Closed without your mark". In TEAM mode, where assignees
    have no self-mark action at all, that means a closed task simply carries
    no individual marks.

    Closing writes no credit. On a project tracking contributions as CRediT,
    credit is written per member as a Lead verifies their part
    (:func:`verify_part`), before or after the task is closed.

    The task records who closed it and when, so the page can name them
    without going to the audit log.
    """
    closer_user = closed_by.users.first()
    task.status = TaskStatus.COMPLETED
    task.closed_by = closed_by
    task.closed_at = timezone.now()
    task.save(
        update_fields=["status", "closed_by", "closed_at", "updated_at"]
    )
    metadata = {"name": task.name}
    if reason:
        metadata["reason"] = reason
    log_action(
        AuditAction.TASK_COMPLETED,
        actor=closer_user,
        entity=task,
        project=task.project,
        metadata=metadata,
    )
    _notify_task_completed(task=task, completed_by=closed_by)
    return task


def _record_member_contribution(task_log: TaskLog, *, verifier_user=None) -> None:
    """Write one verified part's credit, late-imported to dodge a cycle."""
    from apps.contributions.services import record_member_contribution

    record_member_contribution(task_log, verifier_user=verifier_user)


def _withdraw_member_contributions(task: Task, member: ProjectMember) -> None:
    """Take back one member's credit on ``task``, late-imported likewise."""
    from apps.contributions.services import withdraw_member_contributions

    withdraw_member_contributions(task, member)


def all_submissions_reviewed(task: Task) -> bool:
    """True when every assignee has marked their part done and had it reviewed.

    Read from the task's *current* assignees and their current TaskLog, so it
    follows the same live sets the completion matrix does: a member added to
    the task brings an unreviewed row with them, and one taken off stops being
    counted.

    False for a task nobody is assigned to, and for a task that asks for no
    review: there are no verdicts there for "all of them" to describe.
    """
    if not task.needs_review:
        return False
    current = {}
    for log in task.task_logs.all():  # newest-first; first seen is current
        current.setdefault(log.assigned_to_id, log)
    # Read off the prefetched set rather than values_list, which would issue a
    # query of its own on every render of the completion matrix.
    assignee_ids = [member.pk for member in task.assigned_members.all()]
    if not assignee_ids:
        return False
    for member_id in assignee_ids:
        log = current.get(member_id)
        if log is None or log.status != TaskStatus.COMPLETED or not log.is_reviewed:
            return False
    return True


@transaction.atomic
def mark_reviewed(*, task_log: TaskLog, reviewer: ProjectMember) -> TaskLog:
    """Record ``reviewer``'s sign-off on one assignee's submission.

    Peer review is deliberately separate from a Lead's verification
    (:func:`verify_part`), which is what writes credit: a review is a
    verdict a reviewer can withdraw and an assignee can invalidate by
    reverting their mark. Stamping who and when is all this does, so the
    reversal in :func:`clear_review` really is complete.

    Callers are responsible for checking that ``reviewer`` may review this
    task and that the submission exists (see ``views.TaskLogReviewView``).

    Open whether or not the task is completed, and it changes neither the
    task's status nor anyone's credit: closing is a Lead's call, and credit
    waits on a Lead's verification (:func:`verify_part`).
    """
    task_log.reviewed_by = reviewer
    task_log.reviewed_at = timezone.now()
    task_log.save(update_fields=["reviewed_by", "reviewed_at"])
    _log_review_action(
        AuditAction.TASK_REVIEWED, task_log=task_log, actor_member=reviewer
    )
    _notify_submission_reviewed(task_log=task_log, reviewer=reviewer)
    return task_log


@transaction.atomic
def clear_review(
    *, task_log: TaskLog, cleared_by: ProjectMember | None = None
) -> TaskLog:
    """Withdraw the review on a submission, leaving the assignee's mark alone.

    A no-op on an unreviewed entry, so a double-clicked Undo is harmless.

    Open on a completed task for a review given after it was completed; one
    given before stays locked (see :func:`review_locked`, which callers
    check). It leaves the task's status, the Lead's verification and the
    credit alone: verification does not rest on the review (see
    :func:`verify_part`).
    """
    if not task_log.is_reviewed and task_log.reviewed_by_id is None:
        return task_log
    task_log.reviewed_by = None
    task_log.reviewed_at = None
    task_log.save(update_fields=["reviewed_by", "reviewed_at"])
    _log_review_action(
        AuditAction.TASK_REVIEW_CLEARED,
        task_log=task_log,
        actor_member=cleared_by,
    )
    return task_log


class VerifyScope:
    """What a batch verification covers — see :func:`verify_parts`."""

    DONE = "done"
    REVIEWED = "reviewed"
    ALL = (DONE, REVIEWED)


@transaction.atomic
def verify_part(*, task_log: TaskLog, lead: ProjectMember, batch: str | None = None) -> TaskLog:
    """A Lead verifies one member's contribution, crediting them for it.

    This is the ledger event: on a project tracking contributions as CRediT
    it writes the member's ContributionRecord, verified by ``lead``. It is
    open whether or not the task is completed and does not need a review:
    a Lead may verify a part the reviewers have not reached.

    A no-op on a part already verified, so a double click writes nothing
    twice. Callers check that ``lead`` is a Lead, that the part is marked
    done and that its member is still on the task (see
    ``views.TaskLogVerifyView``). ``batch`` names the batch action this came
    from, for the audit row.
    """
    if task_log.is_verified:
        return task_log
    task_log.verified_by = lead
    task_log.verified_at = timezone.now()
    task_log.save(update_fields=["verified_by", "verified_at"])
    _log_verify_action(
        AuditAction.TASK_PART_VERIFIED,
        task_log=task_log,
        actor_member=lead,
        batch=batch,
    )
    _record_member_contribution(task_log, verifier_user=lead.users.first())
    return task_log


@transaction.atomic
def unverify_part(*, task_log: TaskLog, actor: ProjectMember | None) -> TaskLog:
    """Withdraw a Lead's verification, and the credit it wrote.

    Leaves the done mark and any review alone. A no-op on a part that is not
    verified.
    """
    if not task_log.is_verified and task_log.verified_by_id is None:
        return task_log
    task_log.verified_by = None
    task_log.verified_at = None
    task_log.save(update_fields=["verified_by", "verified_at"])
    _log_verify_action(
        AuditAction.TASK_PART_UNVERIFIED, task_log=task_log, actor_member=actor
    )
    _withdraw_member_contributions(task_log.task, task_log.assigned_to)
    return task_log


def current_logs(task: Task) -> dict[int, TaskLog]:
    """Each member's current TaskLog on ``task``, keyed by member id.

    Read off the prefetched set where there is one; newest first, so the
    first seen per member is their current state.
    """
    current: dict[int, TaskLog] = {}
    for log in task.task_logs.all():
        current.setdefault(log.assigned_to_id, log)
    return current


def verifiable_parts(task: Task, scope: str) -> list[TaskLog]:
    """The current, unverified parts a batch verification would cover.

    ``scope`` is :attr:`VerifyScope.DONE` — every part marked done — or
    :attr:`VerifyScope.REVIEWED` — every part marked done and reviewed. Only
    the task's current assignees count, in the order the matrix lists them.
    """
    current = current_logs(task)
    parts = []
    for member in task.assigned_members.all():
        log = current.get(member.pk)
        if log is None or log.status != TaskStatus.COMPLETED or log.is_verified:
            continue
        if scope == VerifyScope.REVIEWED and not log.is_reviewed:
            continue
        parts.append(log)
    return parts


@transaction.atomic
def verify_parts(*, task: Task, lead: ProjectMember, scope: str) -> list[TaskLog]:
    """Verify every part in ``scope`` at once; see :func:`verifiable_parts`.

    Each part is verified as :func:`verify_part` would, with its own audit
    row and credit, the row naming the batch it came from. Returns the parts
    verified, empty when there was nothing left to verify.
    """
    if scope not in VerifyScope.ALL:
        raise ValueError(f"Unknown verification scope: {scope!r}")
    parts = verifiable_parts(task, scope)
    for log in parts:
        verify_part(task_log=log, lead=lead, batch=scope)
    return parts


def _log_verify_action(
    action,
    *,
    task_log: TaskLog,
    actor_member: ProjectMember | None,
    reason: str | None = None,
    batch: str | None = None,
) -> None:
    """Append an audit row for a verification set or withdrawn on ``task_log``."""
    task = task_log.task
    metadata = {
        "task_name": task.name,
        "assignee_member_id": task_log.assigned_to_id,
        "task_log_id": task_log.pk,
        "task_completed": task.status == TaskStatus.COMPLETED,
    }
    if reason:
        metadata["reason"] = reason
    if batch:
        metadata["batch"] = batch
    log_action(
        action,
        actor=actor_member.users.first() if actor_member is not None else None,
        entity=task,
        project=task.project,
        metadata=metadata,
    )


def _log_review_action(
    action,
    *,
    task_log: TaskLog,
    actor_member: ProjectMember | None,
    reason: str | None = None,
) -> None:
    """Append an audit row for a review event on ``task_log``."""
    task = task_log.task
    metadata = {
        "task_name": task.name,
        "assignee_member_id": task_log.assigned_to_id,
        "task_log_id": task_log.pk,
    }
    if reason:
        metadata["reason"] = reason
    log_action(
        action,
        actor=actor_member.users.first() if actor_member is not None else None,
        entity=task,
        project=task.project,
        metadata=metadata,
    )


def _log_mark_action(
    action, *, task_log: TaskLog, actor_member: ProjectMember | None
) -> None:
    """Append an audit row for a done mark set or undone on ``task_log``.

    ``after_completion`` flags a mark changed on a task already closed, so
    marks made after the Lead closed it can be told apart.
    """
    task = task_log.task
    log_action(
        action,
        actor=actor_member.users.first() if actor_member is not None else None,
        entity=task,
        project=task.project,
        metadata={
            "task_name": task.name,
            "assignee_member_id": task_log.assigned_to_id,
            "task_log_id": task_log.pk,
            "after_completion": task.status == TaskStatus.COMPLETED,
        },
    )


@transaction.atomic
def approve_completion(
    *,
    task_log: TaskLog,
    approved: bool | None,
    verifier_user=None,
) -> TaskLog:
    """Set approval state on a TaskLog.

    A lead's verdict on one assignee's submission, recorded and announced.
    It does not touch the contributions ledger: a Lead verifying the part is
    the ledger event (see :func:`verify_part`).
    """
    task_log.approved = approved
    task_log.save(update_fields=["approved"])

    if approved is True:
        log_action(
            AuditAction.TASK_APPROVED,
            actor=verifier_user,
            entity=task_log.task,
            project=task_log.task.project,
            metadata={"task_name": task_log.task.name},
        )
        try:
            from apps.integrations.discord import announce_task_approved

            announce_task_approved(task_log.task, verifier=verifier_user)
        except Exception:  # noqa: BLE001 — never let webhook failure break approval
            pass
    elif approved is False:
        log_action(
            AuditAction.TASK_REJECTED,
            actor=verifier_user,
            entity=task_log.task,
            project=task_log.task.project,
            metadata={"task_name": task_log.task.name},
        )

    _notify_task_approval(task_log=task_log, approved=approved)

    return task_log


# ---------------------------------------------------------------------------
# Individual dates (a member's own start date and/or deadline)
# ---------------------------------------------------------------------------
UNSET = object()  # "leave this date as it is" in set_member_dates


def pending_members(task: Task) -> list[ProjectMember]:
    """The task's assignees who have not marked their part done."""
    current = current_logs(task)
    return [
        member
        for member in task.assigned_members.all()
        if (log := current.get(member.id)) is None
        or log.status != TaskStatus.COMPLETED
    ]


@transaction.atomic
def set_member_dates(
    *,
    task: Task,
    member: ProjectMember,
    lead: ProjectMember | None,
    start_date=UNSET,
    deadline=UNSET,
    timezone=UNSET,
    reason: str | None = None,
    batch: str | None = None,
) -> AssigneeDeadline | None:
    """A Lead sets one assignee's own dates on ``task``.

    ``timezone`` is the IANA zone governing the member's own dates; "" (or
    None) makes them follow the task's zone, and ``UNSET`` keeps the one
    they have.

    Each of ``start_date`` and ``deadline`` replaces the task's date for that
    member alone, and may be earlier or later than it; None removes the
    member's own date, so they follow the task's again; ``UNSET`` (the
    default) leaves it as it is. ``reason`` replaces the note, or with None
    keeps it. When neither date is left the entry is deleted
    (:func:`clear_member_dates`) and None returned. Callers check that
    ``lead`` is a Lead and ``member`` is assigned (``views.TaskMemberDatesView``).

    Writes an audit row naming the old and new dates, and tells the member.
    A no-op, with nothing written, when nothing changes. ``batch`` names the
    bulk action this came from, for the audit row.
    """
    existing = AssigneeDeadline.objects.filter(task=task, member=member).first()
    old_start = existing.start_date if existing else None
    old_deadline = existing.deadline if existing else None
    old_zone = existing.timezone if existing else ""
    new_start = old_start if start_date is UNSET else start_date
    new_deadline = old_deadline if deadline is UNSET else deadline
    new_zone = old_zone if timezone is UNSET else (timezone or "")
    new_reason = (
        (existing.reason if existing else "") if reason is None else reason.strip()
    )
    if new_start is None and new_deadline is None:
        clear_member_dates(task=task, member=member, actor=lead, batch=batch)
        return None
    if existing and (new_start, new_deadline, new_zone, new_reason) == (
        old_start, old_deadline, old_zone, existing.reason
    ):
        return existing
    entry = existing or AssigneeDeadline(task=task, member=member)
    entry.start_date = new_start
    entry.deadline = new_deadline
    entry.timezone = new_zone
    entry.reason = new_reason
    entry.set_by = lead
    entry.save()
    _log_dates_action(
        AuditAction.TASK_DEADLINE_SET,
        task=task,
        member=member,
        actor_member=lead,
        start_date=new_start,
        previous_start_date=old_start,
        deadline=new_deadline,
        previous=old_deadline,
        timezone=new_zone,
        previous_timezone=old_zone if existing else None,
        reason=new_reason,
        batch=batch,
    )
    _notify_member_dates(task=task, member=member, entry=entry, lead=lead)
    return entry


@transaction.atomic
def set_pending_dates(
    *,
    task: Task,
    lead: ProjectMember | None,
    start_date=UNSET,
    deadline=UNSET,
    timezone=UNSET,
    reason: str | None = None,
) -> list[ProjectMember]:
    """Give every assignee not yet done the same own start date and/or
    deadline, and the same zone for their own dates. A date passed as
    ``UNSET`` is left as each member has it (the same moment, shown in the
    new zone), and a None ``reason`` keeps each member's note.

    Members who have marked their part done are left as they are. Returns
    the members it covered.
    """
    members = pending_members(task)
    for member in members:
        set_member_dates(
            task=task, member=member, lead=lead, start_date=start_date,
            deadline=deadline, timezone=timezone, reason=reason, batch="pending",
        )
    return members


@transaction.atomic
def clear_member_dates(
    *,
    task: Task,
    member: ProjectMember,
    actor: ProjectMember | None,
    batch: str | None = None,
) -> bool:
    """Remove ``member``'s own dates, putting them back on the task's.
    Returns False, writing nothing, when they had none."""
    existing = AssigneeDeadline.objects.filter(task=task, member=member).first()
    if existing is None:
        return False
    existing.delete()
    _log_dates_action(
        AuditAction.TASK_DEADLINE_CLEARED,
        task=task,
        member=member,
        actor_member=actor,
        start_date=None,
        previous_start_date=existing.start_date,
        deadline=None,
        previous=existing.deadline,
        timezone=None,
        previous_timezone=existing.timezone,
        batch=batch,
    )
    _notify_member_dates(task=task, member=member, entry=None, lead=actor)
    return True


@transaction.atomic
def set_member_contribution_type(
    *,
    task: Task,
    member: ProjectMember,
    lead: ProjectMember | None,
    contribution_type: str | None,
) -> AssigneeContributionType | None:
    """A Lead gives one assignee their own contribution type on ``task``, or
    with None puts them back on the task's.

    The type replaces the task's for ``member`` alone: their credit is
    recorded under it when their part is verified, and credit already
    written for them moves with it (see
    :func:`apps.contributions.services.sync_member_contribution_type`). A
    type equal to the task's is still kept as their own, so they stay on it
    if the task's type changes later. Callers check that ``lead`` is a Lead,
    ``member`` is assigned and the type is a valid one
    (``views.TaskMemberContributionTypeView``).

    Writes an audit row naming the old and new type, and tells the member.
    A no-op, with nothing written, when nothing changes. Returns the entry,
    or None once the member follows the task's type.
    """
    from apps.contributions.services import sync_member_contribution_type

    existing = AssigneeContributionType.objects.filter(
        task=task, member=member
    ).first()
    old_own = existing.contribution_type if existing else None
    if (contribution_type or None) == old_own:
        return existing
    old_effective = old_own or task.contribution_type
    if contribution_type:
        entry = existing or AssigneeContributionType(task=task, member=member)
        entry.contribution_type = contribution_type
        entry.set_by = lead
        entry.save()
        new_effective = contribution_type
        action = AuditAction.TASK_MEMBER_TYPE_SET
    else:
        existing.delete()
        entry = None
        new_effective = task.contribution_type
        action = AuditAction.TASK_MEMBER_TYPE_CLEARED
    moved = sync_member_contribution_type(
        task, member, old_type=old_effective, new_type=new_effective
    )
    log_action(
        action,
        actor=lead.users.first() if lead is not None else None,
        entity=task,
        project=task.project,
        metadata={
            "task_name": task.name,
            "assignee_member_id": member.pk,
            "contribution_type": contribution_type,
            "previous_contribution_type": old_own,
            "task_contribution_type": task.contribution_type,
            "records_moved": moved,
        },
    )
    _notify_member_type(
        task=task, member=member, contribution_type=new_effective,
        own=entry is not None, lead=lead,
    )
    return entry


def _notify_member_type(
    *,
    task: Task,
    member: ProjectMember,
    contribution_type: str,
    own: bool,
    lead: ProjectMember | None,
) -> None:
    """Tell the member the type their part is credited under changed."""
    try:
        from apps.notifications.services import create_notification
    except Exception:  # pragma: no cover
        return

    user_ids = set(member.users.values_list("id", flat=True))
    if lead is not None:
        user_ids.difference_update(lead.users.values_list("id", flat=True))
    if not user_ids:
        return
    from apps.core.enums import ContributionType

    label = ContributionType(contribution_type).label
    if own:
        title = f"Your contribution type changed: {task.name}"
        body = f"Your part is now credited as {label}."
    else:
        title = f"Your individual contribution type was removed: {task.name}"
        body = f"The task's type applies to you again: {label}."
    create_notification(
        users=list(user_ids),
        title=title,
        body=body,
        project=task.project,
        route="tasks:detail",
        route_kwargs={"project_pk": task.project_id, "pk": task.pk},
    )


class TaskDateChange:
    """What happens to members' own dates when a Lead moves the task's start
    or due date — the choice the task form asks for every time."""

    SHIFT = "shift"
    KEEP = "keep"
    CLEAR = "clear"


@transaction.atomic
def rezone_following_members(
    *,
    task: Task,
    old_zone: str,
    new_zone: str,
    actor: ProjectMember | None,
) -> list[AssigneeDeadline]:
    """The task moved from ``old_zone`` to ``new_zone``: members whose own
    dates follow the task's zone keep their clock times in the new zone,
    as the task's own dates do. Members with a zone of their own are left
    as they are. Each change is audited (``batch="task_timezone"``) and
    told to the member. Returns the entries it changed.
    """
    from apps.tasks.timezones import canonical, rezone

    if canonical(old_zone) == canonical(new_zone):
        return []
    changed = []
    for entry in task.assignee_deadlines.select_related("member").filter(timezone=""):
        start = rezone(entry.start_date, old_zone, new_zone)
        deadline = rezone(entry.deadline, old_zone, new_zone)
        if (start, deadline) == (entry.start_date, entry.deadline):
            continue
        set_member_dates(
            task=task, member=entry.member, lead=actor,
            start_date=start, deadline=deadline, batch="task_timezone",
        )
        changed.append(entry)
    return changed


@transaction.atomic
def apply_task_date_change(
    *,
    task: Task,
    field: str,
    old_value,
    new_value,
    choice: str,
    actor: ProjectMember | None,
    old_zone: str | None = None,
) -> list[AssigneeDeadline]:
    """Carry a change of ``task``'s ``field`` ("start_date" or "deadline")
    onto the members' own dates of the same kind.

    ``shift`` moves each by the same amount on the clock as the task's
    (``old_value`` in ``old_zone``, the task's zone before the edit, to
    ``new_value`` in its zone now), in the zone governing the member's
    date — so a member given a week past the task keeps that week, and
    5:00 PM stays 5:00 PM across a daylight-saving change. ``clear``
    removes them, so those members follow the task's new date (their other
    own date, if any, stays); ``keep`` leaves them as they were. Each
    change is audited (``batch`` names the task edit, e.g.
    ``task_deadline_shift``) and told to the member. Called after the task
    is saved — and after :func:`rezone_following_members`, when the zone
    changed too — so members taken off it in the same edit have already
    lost theirs. Returns the entries it touched. See
    ``timezones.carried_own_date``, which the task form checks against.
    """
    from apps.tasks.timezones import carried_own_date

    assert field in ("start_date", "deadline")
    entries = [
        e
        for e in task.assignee_deadlines.select_related("member")
        if getattr(e, field) is not None
    ]
    if choice == TaskDateChange.SHIFT:
        if old_value is None or new_value is None:
            return []
        for entry in entries:
            shifted = carried_own_date(
                getattr(entry, field),
                own_zone=entry.timezone,
                # Already moved into the new zone, when the zone changed.
                old_task_zone=task.timezone,
                new_task_zone=task.timezone,
                moved=True,
                choice=TaskDateChange.SHIFT,
                old_value=_rezoned(old_value, old_zone or task.timezone, task.timezone),
                new_value=new_value,
            )
            set_member_dates(
                task=task, member=entry.member, lead=actor,
                **{field: shifted},
                batch=f"task_{field}_shift",
            )
        return entries
    if choice == TaskDateChange.CLEAR:
        for entry in entries:
            set_member_dates(
                task=task, member=entry.member, lead=actor, **{field: None},
                batch=f"task_{field}_clear",
            )
        return entries
    return []


def _rezoned(value, old_zone: str, new_zone: str):
    """``value``'s clock time in ``old_zone``, read in ``new_zone``: the
    task's old date as it reads in its new zone, so the shift is the
    distance on the clock alone."""
    from apps.tasks.timezones import canonical, rezone

    if canonical(old_zone) == canonical(new_zone):
        return value
    return rezone(value, old_zone, new_zone)


def _iso(value):
    return value.isoformat() if value else None


def _log_dates_action(
    action,
    *,
    task: Task,
    member: ProjectMember,
    actor_member: ProjectMember | None,
    start_date,
    previous_start_date,
    deadline,
    previous,
    timezone: str | None = None,
    previous_timezone: str | None = None,
    reason: str = "",
    batch: str | None = None,
) -> None:
    """Append an audit row for a member's own dates set or removed.

    ``timezone`` / ``previous_timezone`` are the member's own zone after and
    before ("" when their dates follow the task's zone, None when they had
    no own dates at all)."""
    metadata = {
        "task_name": task.name,
        "assignee_member_id": member.pk,
        "start_date": _iso(start_date),
        "previous_start_date": _iso(previous_start_date),
        "deadline": _iso(deadline),
        "previous_deadline": _iso(previous),
        "timezone": timezone,
        "previous_timezone": previous_timezone,
        "task_start_date": _iso(task.start_date),
        "task_deadline": _iso(task.deadline),
        "task_timezone": task.timezone,
    }
    if reason:
        metadata["reason"] = reason
    if batch:
        metadata["batch"] = batch
    log_action(
        action,
        actor=actor_member.users.first() if actor_member is not None else None,
        entity=task,
        project=task.project,
        metadata=metadata,
    )


def _when(value, zone=None) -> str:
    """``value`` in ``zone`` (the server's when None), with the zone's
    abbreviation: "Nov 30, 2026 5:00 PM MST"."""
    from apps.tasks.timezones import format_when

    return format_when(value, zone)


def _notify_member_dates(
    *,
    task: Task,
    member: ProjectMember,
    entry: AssigneeDeadline | None,
    lead: ProjectMember | None,
) -> None:
    """Tell the member their dates on ``task`` changed (in-app)."""
    try:
        from apps.notifications.services import create_notification
    except Exception:  # pragma: no cover
        return

    user_ids = set(member.users.values_list("id", flat=True))
    if lead is not None:
        user_ids.difference_update(lead.users.values_list("id", flat=True))
    if not user_ids:
        return
    own_start = entry.start_date if entry else None
    own_deadline = entry.deadline if entry else None
    own_zone = (entry.timezone if entry else "") or task.timezone
    start = own_start or task.start_date
    deadline = own_deadline or task.deadline
    parts = []
    if start:
        parts.append(f"starts {_when(start, own_zone if own_start else task.timezone)}")
    if deadline:
        parts.append(
            f"is due {_when(deadline, own_zone if own_deadline else task.timezone)}"
        )
    dates = " and ".join(parts)
    if entry is not None:
        title = f"Your dates changed: {task.name}"
        body = f"Your part now {dates}." if dates else ""
    else:
        title = f"Your individual dates were removed: {task.name}"
        body = (
            f"The task's dates apply to you again: it {dates}."
            if dates
            else "The task has no dates."
        )
    create_notification(
        users=list(user_ids),
        title=title,
        body=body,
        project=task.project,
        route="tasks:detail",
        route_kwargs={"project_pk": task.project_id, "pk": task.pk},
    )


# ---------------------------------------------------------------------------
# Notification helpers (late-imported to avoid circular imports).
# ---------------------------------------------------------------------------
def _queue_assignment_email(user_id: int, task_ids: list[int], assigned_by_id):
    """Return an ``on_commit`` callback that queues one assignment email.

    A named function, not ``functools.partial``: when a robust callback
    raises, Django logs it by ``func.__qualname__``, which a partial lacks,
    so the logging itself raised and the "robust" callback failed the
    request anyway.
    """
    from apps.tasks.tasks import send_task_assigned_email_task

    def queue_task_assigned_email() -> None:
        send_task_assigned_email_task.delay(user_id, task_ids, assigned_by_id)

    return queue_task_assigned_email


def notify_new_assignees(
    assignments: Iterable[tuple[Task, Iterable[int]]], *, assigned_by=None
) -> None:
    """Tell members they were just assigned a task, in-app and by email.

    ``assignments`` pairs each task with the ``ProjectMember`` ids *newly*
    added to it — callers compare assignees before and after the change, so
    re-saving a task does not tell its existing assignees again. A team
    member reaches each of its users. ``assigned_by`` (a user) is left out:
    a Lead assigning themselves needs no word of it.

    Each task gets its own in-app notification. The email goes once per
    person, listing every task they were given here, so a CSV import that
    hands someone ten tasks sends one email rather than ten. It is queued
    for after the surrounding transaction commits, so the worker sees the
    saved assignment; whether to send at all is the recipient's
    ``email_task_assigned`` preference, read by the task itself.
    """
    from apps.notifications.services import create_notification

    task_ids_by_user: dict[int, list[int]] = {}
    for task, member_ids in assignments:
        member_ids = set(member_ids)
        if not member_ids:
            continue
        user_ids = set(
            ProjectMember.objects.filter(pk__in=member_ids).values_list(
                "users", flat=True
            )
        )
        user_ids.discard(None)  # a member with no users left on it
        if assigned_by is not None:
            user_ids.discard(assigned_by.pk)
        if not user_ids:
            continue
        create_notification(
            users=list(user_ids),
            title=f"Task assigned to you: {task.name}",
            body=f"In project {task.project.name}.",
            project=task.project,
            route="tasks:detail",
            route_kwargs={"project_pk": task.project_id, "pk": task.pk},
        )
        for user_id in user_ids:
            task_ids_by_user.setdefault(user_id, []).append(task.pk)

    assigned_by_id = assigned_by.pk if assigned_by is not None else None
    for user_id, task_ids in task_ids_by_user.items():
        transaction.on_commit(
            _queue_assignment_email(user_id, task_ids, assigned_by_id),
            # A broker that is down must not fail an assignment already saved.
            robust=True,
        )


def _notify_task_completed(*, task: Task, completed_by: ProjectMember) -> None:
    try:
        from apps.notifications.services import create_notification
    except Exception:  # pragma: no cover
        return

    user_ids = set()
    for member in task.assigned_members.all():
        user_ids.update(member.users.values_list("id", flat=True))
    # Notify the task creator too so they see progress on their task.
    user_ids.update(task.created_by.users.values_list("id", flat=True))
    # Don't notify the person who just clicked "complete".
    user_ids.difference_update(completed_by.users.values_list("id", flat=True))
    if not user_ids:
        return

    create_notification(
        users=list(user_ids),
        title=f"Task completed: {task.name}",
        body=f"In project {task.project.name}.",
        project=task.project,
        route="tasks:detail",
        route_kwargs={"project_pk": task.project_id, "pk": task.pk},
    )


def _notify_submission_reviewed(*, task_log: TaskLog, reviewer: ProjectMember) -> None:
    """Tell the assignee (and only them) that their submission was reviewed."""
    try:
        from apps.notifications.services import create_notification
    except Exception:  # pragma: no cover
        return

    user_ids = set(task_log.assigned_to.users.values_list("id", flat=True))
    # A reviewer who is also an assignee doesn't need to hear it from us.
    user_ids.difference_update(reviewer.users.values_list("id", flat=True))
    if not user_ids:
        return

    task = task_log.task
    create_notification(
        users=list(user_ids),
        title=f"Submission reviewed: {task.name}",
        body=f"{reviewer.display_name} marked your part reviewed.",
        project=task.project,
        route="tasks:detail",
        route_kwargs={"project_pk": task.project_id, "pk": task.pk},
    )


def _notify_task_approval(*, task_log: TaskLog, approved: bool | None) -> None:
    try:
        from apps.notifications.services import create_notification
    except Exception:  # pragma: no cover
        return

    if approved is None:
        return

    assignee = task_log.assigned_to
    user_ids = list(assignee.users.values_list("id", flat=True))
    if not user_ids:
        return

    task = task_log.task
    verb = "approved" if approved else "rejected"
    create_notification(
        users=user_ids,
        title=f"Task completion {verb}: {task.name}",
        body=f"In project {task.project.name}.",
        project=task.project,
        route="tasks:detail",
        route_kwargs={"project_pk": task.project_id, "pk": task.pk},
    )
