"""Individual deadlines: a Lead gives one assignee, or every assignee not yet
done, a deadline of their own on a task (``AssigneeDeadline``).

The task deadline still holds for the task and everyone else. A task past
its deadline whose every assignee still working holds a later individual
deadline is *Extended* rather than overdue.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditAction, AuditLog
from apps.core.enums import EmailFrequency, MemberPrivileges, ProjectStatus, TaskStatus
from apps.notifications.models import Notification
from apps.projects.models import ProjectMember, ProjectPrivilege
from apps.projects.services import create_project_with_owner
from apps.tasks.models import AssigneeDeadline, KanbanBoard, Task, TaskLog
from apps.tasks.services import (
    clear_member_dates,
    mark_own_done,
    set_member_dates,
    set_pending_dates,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _local_input(dt):
    """A datetime as the deadline picker (datetime-local) submits it."""
    return timezone.localtime(dt).strftime("%Y-%m-%dT%H:%M")


def _setup(lead_user, *others, deadline_days=-2):
    """A project led by ``lead_user`` with one task, due ``deadline_days``
    from now (past by default), assigned to a member for each of ``others``.
    Returns (project, task, lead_member, [members])."""
    project = create_project_with_owner(
        user=lead_user,
        form_data={"name": "Deadlines", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    lead = ProjectMember.objects.filter(project=project, users=lead_user).first()
    column = KanbanBoard.objects.filter(project=project).first() or KanbanBoard.objects.create(
        project=project, name="Todo", container_order=0
    )
    task = Task.objects.create(
        project=project,
        created_by=lead,
        container=column,
        container_task_order=0,
        name="Write chapter",
        deadline=timezone.now() + timedelta(days=deadline_days),
    )
    members = []
    for user in others:
        member = ProjectMember.objects.create(project=project)
        member.users.add(user)
        ProjectPrivilege.objects.create(
            project=project, user=user, privilege=MemberPrivileges.CONTRIBUTOR
        )
        task.assigned_members.add(member)
        members.append(member)
    return project, task, lead, members


def _post(client, task, **data):
    return client.post(
        reverse("tasks:member_dates", kwargs={"pk": task.pk}),
        {"pane_mode": "1", **data},
        HTTP_HX_REQUEST="true",
    )


# ---------------------------------------------------------------------------
# Setting, changing and removing
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_lead_sets_an_individual_deadline_later_than_the_task(client, pm_user, other_user):
    _project, task, _lead, [bob] = _setup(pm_user, other_user)
    later = timezone.now() + timedelta(days=5)
    client.force_login(pm_user)

    response = _post(client, task, member=bob.pk, deadline=_local_input(later), reason="Conference")

    assert response.status_code == 200
    entry = AssigneeDeadline.objects.get(task=task, member=bob)
    assert entry.reason == "Conference"
    assert abs(entry.deadline - later) < timedelta(minutes=1)
    # The task deadline is untouched: it still holds for the task.
    task.refresh_from_db()
    assert task.deadline < timezone.now()
    assert task.deadline_for(bob) == entry.deadline
    # The swapped-in block shows the member's own deadline and the Change control.
    content = response.content.decode()
    assert 'data-member-deadline="own"' in content
    # Their deadline differs from the task's, so it is shown under their name.
    assert f"Due {timezone.localtime(later):%b %-d}</span>" in content


@pytest.mark.django_db
def test_individual_deadline_may_be_earlier_than_the_task(client, pm_user, other_user):
    _project, task, _lead, [bob] = _setup(pm_user, other_user, deadline_days=10)
    earlier = timezone.now() + timedelta(days=2)
    client.force_login(pm_user)

    response = _post(client, task, member=bob.pk, deadline=_local_input(earlier))

    assert response.status_code == 200
    assert task.deadline_for(bob) < task.deadline


@pytest.mark.django_db
def test_individual_deadline_may_be_in_the_past(client, pm_user, other_user):
    """The only rule for a member's dates is that an own start comes no
    later than their deadline; a deadline already passed is the Lead's call."""
    _project, task, _lead, [bob] = _setup(pm_user, other_user)
    client.force_login(pm_user)

    response = _post(
        client, task, member=bob.pk,
        deadline=_local_input(timezone.now() - timedelta(days=1)),
    )

    assert response.status_code == 200
    assert AssigneeDeadline.objects.get(member=bob).deadline < timezone.now()


@pytest.mark.django_db
def test_only_a_lead_can_set_a_deadline(client, pm_user, user, other_user):
    project, task, _lead, [alice, _bob] = _setup(pm_user, user, other_user)
    client.force_login(user)

    response = _post(
        client, task, member=alice.pk,
        deadline=_local_input(timezone.now() + timedelta(days=3)),
    )

    assert response.status_code == 403
    assert not AssigneeDeadline.objects.exists()


@pytest.mark.django_db
def test_deadline_only_for_an_assignee(client, pm_user, user, other_user):
    project, task, _lead, [bob] = _setup(pm_user, other_user)
    outsider = ProjectMember.objects.create(project=project)
    outsider.users.add(user)
    client.force_login(pm_user)

    response = _post(
        client, task, member=outsider.pk,
        deadline=_local_input(timezone.now() + timedelta(days=3)),
    )

    assert response.status_code == 400
    assert not AssigneeDeadline.objects.exists()


@pytest.mark.django_db
def test_deadline_refused_on_a_completed_task(client, pm_user, other_user):
    _project, task, _lead, [bob] = _setup(pm_user, other_user)
    task.status = TaskStatus.COMPLETED
    task.save()
    client.force_login(pm_user)

    response = _post(
        client, task, member=bob.pk,
        deadline=_local_input(timezone.now() + timedelta(days=3)),
    )

    assert response.status_code == 403
    assert not AssigneeDeadline.objects.exists()


@pytest.mark.django_db
def test_lead_removes_an_individual_deadline(client, pm_user, other_user):
    _project, task, lead, [bob] = _setup(pm_user, other_user)
    set_member_dates(
        task=task, member=bob, deadline=timezone.now() + timedelta(days=3), lead=lead
    )
    client.force_login(pm_user)

    response = _post(client, task, member=bob.pk, action="clear")

    assert response.status_code == 200
    assert not AssigneeDeadline.objects.exists()
    assert task.deadline_for(bob) == task.deadline


@pytest.mark.django_db
def test_setting_and_removing_write_audit_rows_and_tell_the_member(pm_user, other_user):
    _project, task, lead, [bob] = _setup(pm_user, other_user)
    first = timezone.now() + timedelta(days=3)
    second = timezone.now() + timedelta(days=6)

    set_member_dates(task=task, member=bob, deadline=first, lead=lead, reason="Ill")
    set_member_dates(task=task, member=bob, deadline=second, lead=lead, reason="Ill")
    clear_member_dates(task=task, member=bob, actor=lead)

    rows = list(
        AuditLog.objects.filter(
            action__in=[AuditAction.TASK_DEADLINE_SET, AuditAction.TASK_DEADLINE_CLEARED]
        ).order_by("id")
    )
    assert [r.action for r in rows] == [
        AuditAction.TASK_DEADLINE_SET,
        AuditAction.TASK_DEADLINE_SET,
        AuditAction.TASK_DEADLINE_CLEARED,
    ]
    assert rows[0].metadata["previous_deadline"] is None
    assert rows[0].metadata["reason"] == "Ill"
    assert rows[1].metadata["previous_deadline"] == first.isoformat()
    assert rows[2].metadata["previous_deadline"] == second.isoformat()
    assert rows[0].actor == pm_user

    notes = Notification.objects.for_user(other_user)
    assert notes.count() == 3
    assert not Notification.objects.for_user(pm_user).exists()


@pytest.mark.django_db
def test_setting_the_same_deadline_twice_writes_nothing_new(pm_user, other_user):
    _project, task, lead, [bob] = _setup(pm_user, other_user)
    when = timezone.now() + timedelta(days=3)
    set_member_dates(task=task, member=bob, deadline=when, lead=lead)
    set_member_dates(task=task, member=bob, deadline=when, lead=lead)
    assert AuditLog.objects.filter(action=AuditAction.TASK_DEADLINE_SET).count() == 1


@pytest.mark.django_db
def test_unassigning_drops_the_individual_deadline(pm_user, user, other_user):
    _project, task, lead, [alice, bob] = _setup(pm_user, user, other_user)
    for m in (alice, bob):
        set_member_dates(
            task=task, member=m, deadline=timezone.now() + timedelta(days=3), lead=lead
        )

    task.assigned_members.set([alice])
    assert list(AssigneeDeadline.objects.values_list("member_id", flat=True)) == [alice.pk]

    bob.assigned_tasks.add(task)
    alice.assigned_tasks.remove(task)
    assert not AssigneeDeadline.objects.exists()


# ---------------------------------------------------------------------------
# Everyone not yet done
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_set_deadline_for_all_not_done_skips_members_done(client, pm_user, user, other_user):
    _project, task, _lead, [alice, bob] = _setup(pm_user, user, other_user)
    mark_own_done(task=task, member=alice)
    later = timezone.now() + timedelta(days=4)
    client.force_login(pm_user)

    response = _post(client, task, scope="pending", deadline=_local_input(later))

    assert response.status_code == 200
    assert list(AssigneeDeadline.objects.values_list("member_id", flat=True)) == [bob.pk]
    row = AuditLog.objects.get(action=AuditAction.TASK_DEADLINE_SET)
    assert row.metadata["batch"] == "pending"


@pytest.mark.django_db
def test_set_deadline_for_all_refused_when_everyone_is_done(client, pm_user, other_user):
    _project, task, _lead, [bob] = _setup(pm_user, other_user)
    mark_own_done(task=task, member=bob)
    client.force_login(pm_user)

    response = _post(
        client, task, scope="pending",
        deadline=_local_input(timezone.now() + timedelta(days=4)),
    )

    assert response.status_code == 400
    assert not AssigneeDeadline.objects.exists()


# ---------------------------------------------------------------------------
# Overdue vs Extended
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_task_is_extended_when_everyone_still_working_has_a_later_deadline(
    pm_user, user, other_user
):
    _project, task, lead, [alice, bob] = _setup(pm_user, user, other_user)
    later = timezone.now() + timedelta(days=3)

    assert task.is_overdue and not task.is_extended

    # One of two still working has an extension: still overdue.
    set_member_dates(task=task, member=alice, deadline=later, lead=lead)
    assert task.is_overdue and not task.is_extended
    assert list(Task.objects.overdue()) == [task]
    assert not Task.objects.extended().exists()

    # The other one finishes: everyone still working is extended.
    mark_own_done(task=task, member=bob)
    assert task.is_extended and not task.is_overdue
    assert list(Task.objects.extended()) == [task]
    assert not Task.objects.overdue().exists()


@pytest.mark.django_db
def test_extension_that_has_itself_passed_makes_the_task_overdue_again(pm_user, other_user):
    _project, task, lead, [bob] = _setup(pm_user, other_user, deadline_days=-5)
    entry = set_member_dates(
        task=task, member=bob, deadline=timezone.now() + timedelta(days=1), lead=lead
    )
    assert task.is_extended
    AssigneeDeadline.objects.filter(pk=entry.pk).update(
        deadline=timezone.now() - timedelta(days=1)
    )
    assert task.is_overdue
    assert list(Task.objects.overdue()) == [task]


@pytest.mark.django_db
def test_task_with_nobody_assigned_or_everyone_done_stays_overdue(pm_user, other_user):
    _project, task, _lead, [bob] = _setup(pm_user, other_user)
    mark_own_done(task=task, member=bob)
    assert task.is_overdue
    task.assigned_members.clear()
    assert task.is_overdue
    assert list(Task.objects.overdue()) == [task]


@pytest.mark.django_db
def test_earlier_individual_deadline_does_not_make_the_task_overdue(pm_user, other_user):
    _project, task, lead, [bob] = _setup(pm_user, other_user, deadline_days=10)
    entry = set_member_dates(
        task=task, member=bob, deadline=timezone.now() + timedelta(days=1), lead=lead
    )
    AssigneeDeadline.objects.filter(pk=entry.pk).update(
        deadline=timezone.now() - timedelta(hours=1)
    )
    assert not task.is_overdue and not task.is_extended
    # ...but the member is late on theirs.
    assert list(Task.objects.overdue_for(other_user)) == [task]


@pytest.mark.django_db
def test_board_shows_extended_and_filters_it(client, pm_user, other_user):
    project, task, lead, [bob] = _setup(pm_user, other_user)
    set_member_dates(
        task=task, member=bob, deadline=timezone.now() + timedelta(days=3), lead=lead
    )
    client.force_login(pm_user)
    kanban = reverse("tasks:board_kanban", kwargs={"project_pk": project.pk})

    content = client.get(kanban).content.decode()
    assert "data-extended-badge" in content
    assert 'aria-label="Overdue"' not in content

    assert "Write chapter" in client.get(kanban, {"filter": "extended"}).content.decode()
    assert "Write chapter" not in client.get(kanban, {"filter": "overdue"}).content.decode()


@pytest.mark.django_db
def test_project_overview_does_not_count_an_extended_task_overdue(client, pm_user, other_user):
    project, task, lead, [bob] = _setup(pm_user, other_user)
    client.force_login(pm_user)
    url = reverse("projects:detail", kwargs={"pk": project.pk})
    assert client.get(url).context["overdue_count"] == 1

    set_member_dates(
        task=task, member=bob, deadline=timezone.now() + timedelta(days=3), lead=lead
    )
    assert client.get(url).context["overdue_count"] == 0


# ---------------------------------------------------------------------------
# The member's own views use their own deadline
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_dashboard_overdue_follows_the_users_own_deadline(client, pm_user, user, other_user):
    _project, task, lead, [alice, bob] = _setup(pm_user, user, other_user)
    set_member_dates(
        task=task, member=bob, deadline=timezone.now() + timedelta(days=3), lead=lead
    )
    dashboard = reverse("projects:dashboard")

    client.force_login(other_user)
    response = client.get(dashboard, {"type": "overdue"})
    assert response.context["active_tasks_match_count"] == 0
    content = client.get(dashboard).content.decode()
    assert "(yours)" in content

    client.force_login(user)
    response = client.get(dashboard, {"type": "overdue"})
    assert response.context["active_tasks_match_count"] == 1


@pytest.mark.django_db
def test_my_tasks_shows_and_sorts_by_the_users_own_deadline(client, pm_user, other_user):
    project, task, lead, [bob] = _setup(pm_user, other_user, deadline_days=20)
    later_task = Task.objects.create(
        project=project, created_by=lead, container=task.container,
        container_task_order=1, name="Review draft",
        deadline=timezone.now() + timedelta(days=5),
    )
    later_task.assigned_members.add(bob)
    # Bob's own deadline on the first task comes before the second task's.
    set_member_dates(
        task=task, member=bob, deadline=timezone.now() + timedelta(days=2), lead=lead
    )
    client.force_login(other_user)

    response = client.get(reverse("tasks:my_list"), {"sort": "deadline_asc"})

    names = [t.name for t in response.context["tasks"]]
    assert names == ["Write chapter", "Review draft"]
    assert "(your deadline)" in response.content.decode()


@pytest.mark.django_db
def test_digest_lists_only_tasks_past_the_users_own_deadline(pm_user, user, other_user):
    from apps.notifications.tasks import _overdue_tasks_for

    _project, task, lead, [alice, bob] = _setup(pm_user, user, other_user)
    set_member_dates(
        task=task, member=bob, deadline=timezone.now() + timedelta(days=3), lead=lead
    )
    assert _overdue_tasks_for(other_user) == []
    assert _overdue_tasks_for(user) == [task]


@pytest.mark.django_db
def test_overdue_reminder_skips_members_on_a_later_deadline(
    monkeypatch, pm_user, user, other_user
):
    from cron.tasks import overdue_task_reminder_cron

    for u in (user, other_user):
        u.email_overdue_task_frequency = EmailFrequency.DAILY
        u.save()
    _project, task, lead, [alice, bob] = _setup(pm_user, user, other_user)
    set_member_dates(
        task=task, member=bob, deadline=timezone.now() + timedelta(days=3), lead=lead
    )
    queued = []
    monkeypatch.setattr(
        "apps.notifications.tasks.send_digest_email_task.delay",
        lambda uid, freq: queued.append(uid),
    )

    overdue_task_reminder_cron()

    assert queued == [user.pk]


@pytest.mark.django_db
def test_due_hint_speaks_to_the_viewers_own_deadline(client, pm_user, other_user):
    project, task, lead, [bob] = _setup(pm_user, other_user, deadline_days=10)
    set_member_dates(
        task=task, member=bob, deadline=timezone.now() + timedelta(days=3), lead=lead
    )
    url = reverse("tasks:detail_pane", kwargs={"project_pk": project.pk, "pk": task.pk})

    client.force_login(other_user)
    assert "Your deadline" in client.get(url).content.decode()

    client.force_login(pm_user)
    content = client.get(url).content.decode()
    assert "Your deadline" not in content
    assert "days left" in content


# ---------------------------------------------------------------------------
# Lead controls on the completion table
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_lead_sees_deadline_controls_and_member_does_not(client, pm_user, other_user):
    project, task, _lead, [bob] = _setup(pm_user, other_user)
    url = reverse("tasks:detail_pane", kwargs={"project_pk": project.pk, "pk": task.pk})

    client.force_login(pm_user)
    content = client.get(url).content.decode()
    assert f'data-set-dates="{bob.pk}"' in content
    assert "data-dates-all" in content
    assert "data-dates-modal" in content
    # Bob's part is late on the task deadline.
    assert "data-member-overdue" in content

    client.force_login(other_user)
    content = client.get(url).content.decode()
    assert "data-set-dates" not in content
    assert "data-dates-modal" not in content


# ---------------------------------------------------------------------------
# Moving the task's due date asks what happens to individual deadlines
# ---------------------------------------------------------------------------


def _edit(client, project, task, members, **extra):
    data = {
        "name": task.name,
        "container": task.container_id,
        "contribution_type": "OTHER",
        "assigned_members": [m.pk for m in members],
        **extra,
    }
    return client.post(
        reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk}), data
    )


def _minute(dt):
    return dt.replace(second=0, microsecond=0)


@pytest.fixture
def moved(pm_user, user, other_user):
    """A task due in 2 days; Bo has his own deadline 7 days after it, Ari
    has none. The tests move the task's due date 13 days later."""
    project, task, lead, [ari, bo] = _setup(pm_user, user, other_user, deadline_days=2)
    task.deadline = _minute(task.deadline)
    task.save()
    own = set_member_dates(
        task=task, member=bo, deadline=task.deadline + timedelta(days=7), lead=lead,
        reason="Conference",
    )
    new_deadline = task.deadline + timedelta(days=13)
    return project, task, ari, bo, own, new_deadline


@pytest.mark.django_db
def test_edit_form_offers_the_choice_only_when_members_have_their_own(
    client, pm_user, other_user
):
    project, task, lead, [bo] = _setup(pm_user, other_user, deadline_days=2)
    url = reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk})
    client.force_login(pm_user)
    assert 'data-individual-dates="deadline"' not in client.get(url).content.decode()

    set_member_dates(
        task=task, member=bo, deadline=task.deadline + timedelta(days=7), lead=lead
    )
    content = client.get(url).content.decode()
    assert 'data-individual-dates="deadline"' in content
    assert "Shift them by the same amount" in content
    assert 'value="shift"' in content


@pytest.mark.django_db
def test_shifting_moves_individual_deadlines_by_the_same_amount(client, pm_user, moved):
    project, task, ari, bo, own, new_deadline = moved
    old_own = own.deadline
    client.force_login(pm_user)

    response = _edit(
        client, project, task, [ari, bo],
        deadline=_local_input(new_deadline), individual_deadlines="shift",
    )

    assert response.status_code == 302
    task.refresh_from_db()
    own.refresh_from_db()
    assert task.deadline == new_deadline
    assert own.deadline == old_own + timedelta(days=13)
    assert own.reason == "Conference"
    row = AuditLog.objects.filter(action=AuditAction.TASK_DEADLINE_SET).latest("id")
    assert row.metadata["batch"] == "task_deadline_shift"
    assert row.metadata["previous_deadline"] == old_own.isoformat()


@pytest.mark.django_db
def test_keeping_leaves_individual_deadlines_alone(client, pm_user, moved):
    project, task, ari, bo, own, new_deadline = moved
    old_own = own.deadline
    client.force_login(pm_user)

    _edit(
        client, project, task, [ari, bo],
        deadline=_local_input(new_deadline), individual_deadlines="keep",
    )

    own.refresh_from_db()
    assert own.deadline == old_own
    assert AuditLog.objects.filter(action=AuditAction.TASK_DEADLINE_SET).count() == 1


@pytest.mark.django_db
def test_removing_puts_members_on_the_new_due_date(client, pm_user, moved):
    project, task, ari, bo, _own, new_deadline = moved
    client.force_login(pm_user)

    _edit(
        client, project, task, [ari, bo],
        deadline=_local_input(new_deadline), individual_deadlines="clear",
    )

    task.refresh_from_db()
    assert not AssigneeDeadline.objects.exists()
    assert task.deadline_for(bo) == new_deadline
    row = AuditLog.objects.get(action=AuditAction.TASK_DEADLINE_CLEARED)
    assert row.metadata["batch"] == "task_deadline_clear"


@pytest.mark.django_db
def test_moving_the_due_date_without_a_choice_is_refused(client, pm_user, moved):
    project, task, ari, bo, own, new_deadline = moved
    old_deadline = task.deadline
    client.force_login(pm_user)

    response = _edit(client, project, task, [ari, bo], deadline=_local_input(new_deadline))

    assert response.status_code == 200
    assert "Choose whether to shift, keep or remove them" in response.content.decode()
    task.refresh_from_db()
    assert task.deadline == old_deadline


@pytest.mark.django_db
def test_an_edit_that_keeps_the_due_date_asks_nothing(client, pm_user, moved):
    project, task, ari, bo, own, _new = moved
    old_own = own.deadline
    client.force_login(pm_user)

    response = _edit(
        client, project, task, [ari, bo], deadline=_local_input(task.deadline),
        individual_deadlines="clear", description="Edited",
    )

    assert response.status_code == 302
    own.refresh_from_db()
    assert own.deadline == old_own


@pytest.mark.django_db
def test_shift_is_refused_when_the_due_date_is_removed(client, pm_user, moved):
    project, task, ari, bo, own, _new = moved
    client.force_login(pm_user)

    response = _edit(client, project, task, [ari, bo], deadline="", individual_deadlines="shift")
    assert response.status_code == 200
    assert "no longer has a due date" in response.content.decode()

    response = _edit(client, project, task, [ari, bo], deadline="", individual_deadlines="keep")
    assert response.status_code == 302
    task.refresh_from_db()
    assert task.deadline is None
    assert AssigneeDeadline.objects.filter(member=bo).exists()


@pytest.mark.django_db
def test_member_taken_off_in_the_same_edit_is_not_shifted_back(client, pm_user, moved):
    project, task, ari, bo, _own, new_deadline = moved
    client.force_login(pm_user)

    _edit(
        client, project, task, [ari],
        deadline=_local_input(new_deadline), individual_deadlines="shift",
    )

    assert not AssigneeDeadline.objects.exists()


# ---------------------------------------------------------------------------
# Individual start dates
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_lead_sets_only_an_individual_start_date(client, pm_user, other_user):
    _project, task, _lead, [bob] = _setup(pm_user, other_user, deadline_days=10)
    start = timezone.now() + timedelta(days=3)
    client.force_login(pm_user)

    response = _post(client, task, member=bob.pk, start_date=_local_input(start), deadline="")

    assert response.status_code == 200
    entry = AssigneeDeadline.objects.get(task=task, member=bob)
    assert entry.deadline is None
    assert abs(entry.start_date - start) < timedelta(minutes=1)
    assert task.start_date_for(bob) == entry.start_date
    # Only a start of their own: they still follow the task's deadline.
    assert task.deadline_for(bob) == task.deadline
    content = response.content.decode()
    assert 'data-member-start="own"' in content
    assert 'data-member-deadline="task"' in content
    # Only their start differs from the task's, so only it is shown.
    assert f">Starts {timezone.localtime(entry.start_date):%b %-d}</span>" in content


@pytest.mark.django_db
def test_start_date_may_be_in_the_past(client, pm_user, other_user):
    _project, task, _lead, [bob] = _setup(pm_user, other_user, deadline_days=10)
    client.force_login(pm_user)

    response = _post(
        client, task, member=bob.pk,
        start_date=_local_input(timezone.now() - timedelta(days=3)),
    )

    assert response.status_code == 200
    assert AssigneeDeadline.objects.get(member=bob).start_date < timezone.now()


@pytest.mark.django_db
def test_start_after_the_members_deadline_is_refused(client, pm_user, other_user):
    _project, task, _lead, [bob] = _setup(pm_user, other_user, deadline_days=5)
    client.force_login(pm_user)

    # After the task's deadline, which is the one Bo follows.
    response = _post(
        client, task, member=bob.pk,
        start_date=_local_input(timezone.now() + timedelta(days=7)),
    )
    assert response.status_code == 400
    assert "on or before the deadline" in response.content.decode()

    # With a later deadline of his own it is fine.
    response = _post(
        client, task, member=bob.pk,
        start_date=_local_input(timezone.now() + timedelta(days=7)),
        deadline=_local_input(timezone.now() + timedelta(days=9)),
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_a_passed_own_deadline_can_be_sent_back_with_a_new_start(client, pm_user, other_user):
    _project, task, lead, [bob] = _setup(pm_user, other_user, deadline_days=-5)
    entry = set_member_dates(
        task=task, member=bob, lead=lead, deadline=timezone.now() + timedelta(days=1)
    )
    passed = timezone.now() - timedelta(days=1)
    AssigneeDeadline.objects.filter(pk=entry.pk).update(deadline=passed)
    client.force_login(pm_user)

    response = _post(
        client, task, member=bob.pk,
        start_date=_local_input(timezone.now() - timedelta(days=4)),
        deadline=_local_input(passed),
    )

    assert response.status_code == 200
    assert AssigneeDeadline.objects.get(member=bob).start_date is not None


@pytest.mark.django_db
def test_emptying_one_date_keeps_the_other_and_both_removes_them(client, pm_user, other_user):
    _project, task, lead, [bob] = _setup(pm_user, other_user, deadline_days=10)
    start = timezone.now() + timedelta(days=2)
    set_member_dates(
        task=task, member=bob, lead=lead, start_date=start,
        deadline=timezone.now() + timedelta(days=12),
    )
    client.force_login(pm_user)

    _post(client, task, member=bob.pk, start_date=_local_input(start), deadline="")
    entry = AssigneeDeadline.objects.get(member=bob)
    assert entry.deadline is None and entry.start_date is not None

    _post(client, task, member=bob.pk, start_date="", deadline="")
    assert not AssigneeDeadline.objects.exists()
    assert AuditLog.objects.filter(action=AuditAction.TASK_DEADLINE_CLEARED).count() == 1


@pytest.mark.django_db
def test_set_dates_for_all_not_done_leaves_empty_dates_as_they_are(
    client, pm_user, user, other_user
):
    _project, task, lead, [ari, bo] = _setup(pm_user, user, other_user, deadline_days=10)
    bo_deadline = timezone.now() + timedelta(days=14)
    set_member_dates(task=task, member=bo, lead=lead, deadline=bo_deadline, reason="Ill")
    start = timezone.now() + timedelta(days=1)
    client.force_login(pm_user)

    response = _post(client, task, scope="pending", start_date=_local_input(start))

    assert response.status_code == 200
    ari_entry = AssigneeDeadline.objects.get(member=ari)
    bo_entry = AssigneeDeadline.objects.get(member=bo)
    assert ari_entry.deadline is None and ari_entry.start_date is not None
    assert bo_entry.deadline == bo_deadline and bo_entry.start_date is not None
    assert bo_entry.reason == "Ill"


@pytest.mark.django_db
def test_set_dates_for_all_needs_at_least_one_date(client, pm_user, other_user):
    _project, task, _lead, [bob] = _setup(pm_user, other_user)
    client.force_login(pm_user)

    response = _post(client, task, scope="pending", start_date="", deadline="")

    assert response.status_code == 400
    assert "Change the start date or the deadline" in response.content.decode()


@pytest.mark.django_db
def test_own_start_date_alone_does_not_extend_the_task(pm_user, other_user):
    _project, task, lead, [bob] = _setup(pm_user, other_user, deadline_days=-2)
    set_member_dates(
        task=task, member=bob, lead=lead, start_date=timezone.now() - timedelta(days=5)
    )
    assert task.is_overdue and not task.is_extended
    assert list(Task.objects.overdue()) == [task]
    assert list(Task.objects.overdue_for(other_user)) == [task]


@pytest.mark.django_db
def test_due_hint_names_the_viewers_own_start(client, pm_user, other_user):
    project, task, lead, [bob] = _setup(pm_user, other_user, deadline_days=10)
    set_member_dates(
        task=task, member=bob, lead=lead, start_date=timezone.now() + timedelta(days=3)
    )
    url = reverse("tasks:detail_pane", kwargs={"project_pk": project.pk, "pk": task.pk})
    client.force_login(other_user)

    content = client.get(url).content.decode()

    assert "Your start" in content
    assert "days left" in content


@pytest.fixture
def moved_start(pm_user, user, other_user):
    """A task starting in 2 days, due in 20; Bo starts 3 days after the task
    and has his own deadline too. The tests move the task's start 5 days."""
    project, task, lead, [ari, bo] = _setup(pm_user, user, other_user, deadline_days=20)
    task.start_date = _minute(timezone.now() + timedelta(days=2))
    task.deadline = _minute(task.deadline)
    task.save()
    own = set_member_dates(
        task=task, member=bo, lead=lead,
        start_date=task.start_date + timedelta(days=3),
        deadline=task.deadline + timedelta(days=4),
    )
    return project, task, ari, bo, own, task.start_date + timedelta(days=5)


@pytest.mark.django_db
def test_edit_form_offers_the_start_choice_only_for_own_start_dates(
    client, pm_user, other_user
):
    project, task, lead, [bo] = _setup(pm_user, other_user, deadline_days=10)
    url = reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk})
    set_member_dates(
        task=task, member=bo, lead=lead, deadline=task.deadline + timedelta(days=1)
    )
    client.force_login(pm_user)
    content = client.get(url).content.decode()
    assert 'data-individual-dates="deadline"' in content
    assert 'data-individual-dates="start_date"' not in content

    set_member_dates(task=task, member=bo, lead=lead, start_date=timezone.now())
    content = client.get(url).content.decode()
    assert 'data-individual-dates="start_date"' in content
    assert "follow the task&#x27;s start date" in content


@pytest.mark.django_db
def test_shifting_the_start_moves_own_starts_only(client, pm_user, moved_start):
    project, task, ari, bo, own, new_start = moved_start
    old_own_start, old_own_deadline = own.start_date, own.deadline
    client.force_login(pm_user)

    response = _edit(
        client, project, task, [ari, bo],
        start_date=_local_input(new_start), deadline=_local_input(task.deadline),
        individual_start_dates="shift",
    )

    assert response.status_code == 302
    own.refresh_from_db()
    assert own.start_date == old_own_start + timedelta(days=5)
    assert own.deadline == old_own_deadline
    row = AuditLog.objects.filter(action=AuditAction.TASK_DEADLINE_SET).latest("id")
    assert row.metadata["batch"] == "task_start_date_shift"
    assert row.metadata["previous_start_date"] == old_own_start.isoformat()


@pytest.mark.django_db
def test_removing_own_starts_keeps_own_deadlines(client, pm_user, moved_start):
    project, task, ari, bo, own, new_start = moved_start
    old_own_deadline = own.deadline
    client.force_login(pm_user)

    _edit(
        client, project, task, [ari, bo],
        start_date=_local_input(new_start), deadline=_local_input(task.deadline),
        individual_start_dates="clear",
    )

    own.refresh_from_db()
    assert own.start_date is None
    assert own.deadline == old_own_deadline


@pytest.mark.django_db
def test_moving_the_start_without_a_choice_is_refused(client, pm_user, moved_start):
    project, task, ari, bo, own, new_start = moved_start
    old_start = task.start_date
    client.force_login(pm_user)

    response = _edit(
        client, project, task, [ari, bo],
        start_date=_local_input(new_start), deadline=_local_input(task.deadline),
    )

    assert response.status_code == 200
    assert "Some assignees have their own start date" in response.content.decode()
    task.refresh_from_db()
    assert task.start_date == old_start


@pytest.mark.django_db
def test_moving_both_dates_applies_each_choice(client, pm_user, moved_start):
    project, task, ari, bo, own, new_start = moved_start
    old_own_start = own.start_date
    client.force_login(pm_user)

    _edit(
        client, project, task, [ari, bo],
        start_date=_local_input(new_start),
        deadline=_local_input(task.deadline + timedelta(days=2)),
        individual_start_dates="keep", individual_deadlines="clear",
    )

    own.refresh_from_db()
    assert own.start_date == old_own_start
    assert own.deadline is None


# ---------------------------------------------------------------------------
# A start date never falls after the deadline — for any member
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_shifting_own_starts_past_kept_own_deadlines_is_refused(client, pm_user, moved_start):
    """The task starts on day 2 and is due on day 20; Bo starts on day 5 and
    is due on day 15 (both his own). Moving the task's start 12 days later
    and shifting his start puts it on day 17, after his own deadline, which
    is kept."""
    project, task, ari, bo, own, _new = moved_start
    lead = ProjectMember.objects.filter(project=project, users=pm_user).first()
    own = set_member_dates(
        task=task, member=bo, lead=lead, deadline=task.deadline - timedelta(days=5)
    )
    old_start, old_own_start = task.start_date, own.start_date
    late_start = task.start_date + timedelta(days=12)
    client.force_login(pm_user)

    response = _edit(
        client, project, task, [ari, bo],
        start_date=_local_input(late_start), deadline=_local_input(task.deadline),
        individual_start_dates="shift",
    )

    assert response.status_code == 200
    content = response.content.decode()
    assert "would start" in content and "after their deadline" in content
    assert bo.display_name in content
    task.refresh_from_db()
    own.refresh_from_db()
    assert task.start_date == old_start
    assert own.start_date == old_own_start

    # Keeping his start instead is fine.
    response = _edit(
        client, project, task, [ari, bo],
        start_date=_local_input(late_start), deadline=_local_input(task.deadline),
        individual_start_dates="keep",
    )
    assert response.status_code == 302


@pytest.mark.django_db
def test_shifting_own_deadlines_before_kept_own_starts_is_refused(client, pm_user, moved_start):
    project, task, ari, bo, own, _new = moved_start
    # Pull the task deadline in so Bo's shifted deadline lands before his start.
    early = task.deadline - timedelta(days=22)  # Bo due day 2, but starts day 5
    client.force_login(pm_user)

    response = _edit(
        client, project, task, [ari, bo],
        start_date="", deadline=_local_input(early),
        individual_start_dates="keep", individual_deadlines="shift",
    )

    assert response.status_code == 200
    assert "after their deadline" in response.content.decode()

    # Removing his own deadline puts him on the task's, still before his start.
    response = _edit(
        client, project, task, [ari, bo],
        start_date="", deadline=_local_input(early),
        individual_start_dates="keep", individual_deadlines="clear",
    )
    assert response.status_code == 200

    # Removing his own start as well is fine.
    response = _edit(
        client, project, task, [ari, bo],
        start_date="", deadline=_local_input(early),
        individual_start_dates="clear", individual_deadlines="clear",
    )
    assert response.status_code == 302


@pytest.mark.django_db
def test_task_start_moved_past_an_own_deadline_is_allowed(client, pm_user, user, other_user):
    """Bo has only his own (earlier) deadline and follows the task's start:
    with no start of his own there is nothing to check, so the task's start
    may move past his deadline (collaborators in other timezones)."""
    project, task, lead, [ari, bo] = _setup(pm_user, user, other_user, deadline_days=20)
    task.deadline = _minute(task.deadline)
    task.save()
    set_member_dates(task=task, member=bo, lead=lead, deadline=task.deadline - timedelta(days=10))
    client.force_login(pm_user)

    response = _edit(
        client, project, task, [ari, bo],
        start_date=_local_input(task.deadline - timedelta(days=5)),
        deadline=_local_input(task.deadline),
    )

    assert response.status_code == 302


@pytest.mark.django_db
def test_member_taken_off_is_not_checked(client, pm_user, user, other_user):
    project, task, lead, [ari, bo] = _setup(pm_user, user, other_user, deadline_days=20)
    task.deadline = _minute(task.deadline)
    task.save()
    set_member_dates(task=task, member=bo, lead=lead, deadline=task.deadline - timedelta(days=10))
    client.force_login(pm_user)

    response = _edit(
        client, project, task, [ari],
        start_date=_local_input(task.deadline - timedelta(days=5)),
        deadline=_local_input(task.deadline),
    )

    assert response.status_code == 302


@pytest.mark.django_db
def test_set_dates_for_all_checks_each_members_own_dates(client, pm_user, user, other_user):
    _project, task, lead, [ari, bo] = _setup(pm_user, user, other_user, deadline_days=20)
    # Bo's own deadline is in 5 days; the task's in 20.
    set_member_dates(task=task, member=bo, lead=lead, deadline=timezone.now() + timedelta(days=5))
    client.force_login(pm_user)

    # A common start in 10 days fits Ari (task deadline) but not Bo.
    response = _post(
        client, task, scope="pending",
        start_date=_local_input(timezone.now() + timedelta(days=10)),
    )
    assert response.status_code == 400
    content = response.content.decode()
    assert bo.display_name in content and ari.display_name not in content
    assert not AssigneeDeadline.objects.filter(member=ari).exists()

    # A common deadline before Bo's... and Ari's own start.
    set_member_dates(task=task, member=ari, lead=lead, start_date=timezone.now() + timedelta(days=8))
    response = _post(
        client, task, scope="pending",
        deadline=_local_input(timezone.now() + timedelta(days=6)),
    )
    assert response.status_code == 400
    assert ari.display_name in response.content.decode()


@pytest.mark.django_db
def test_start_equal_to_deadline_is_allowed(client, pm_user, other_user):
    _project, task, _lead, [bob] = _setup(pm_user, other_user, deadline_days=10)
    same = timezone.now() + timedelta(days=4)
    client.force_login(pm_user)

    response = _post(
        client, task, member=bob.pk,
        start_date=_local_input(same), deadline=_local_input(same),
    )

    assert response.status_code == 200
