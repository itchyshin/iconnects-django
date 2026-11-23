"""Smoke tests for the tasks app."""

from __future__ import annotations

import re

from datetime import date, datetime, timedelta, timezone as dt_timezone

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditAction, AuditLog
from apps.core.enums import (
    CREDIT_ROLES,
    ContributionType,
    MemberPrivileges,
    ProjectStatus,
    TaskStatus,
)
from apps.projects.models import (
    ContributionTracking,
    ProjectMember,
    ProjectPrivilege,
)
from apps.projects.services import create_project_with_owner
from apps.tasks.calendar import (
    bucket_tasks_by_day,
    build_day_tasks,
    build_month_grid,
    build_week_grid,
    build_year_grid,
    clamp_month,
    parse_view_date,
    shift_date,
    task_date_label,
    task_range,
)
from apps.tasks.forms import TaskForm
from apps.tasks.models import KanbanBoard, Milestone, Task, TaskLog
from apps.tasks.services import (
    approve_completion,
    close_task,
    complete_task,
    reopen_task,
)


def _make_project_with_board(owner, *, name="P"):
    project = create_project_with_owner(
        user=owner,
        form_data={"name": name, "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    # Drop the default seeded columns so these tests control the board exactly.
    project.containers.all().delete()
    col1 = KanbanBoard.objects.create(project=project, name="Todo", container_order=0)
    col2 = KanbanBoard.objects.create(project=project, name="Doing", container_order=1)
    return project, col1, col2


def _primary_member(project, user):
    return ProjectMember.objects.filter(project=project, users=user).first()


@pytest.mark.django_db
def test_task_move_across_columns(client, user):
    project, col1, col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="T1",
    )
    client.force_login(user)
    response = client.post(
        reverse("tasks:move", kwargs={"pk": task.pk}),
        {"container_id": col2.pk, "order": 0},
    )
    assert response.status_code == 204
    task.refresh_from_db()
    assert task.container_id == col2.pk


@pytest.mark.django_db
def test_task_move_requires_member_access(client, user, other_user):
    project, col1, col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="T2",
    )
    # other_user is NOT a member of this project.
    client.force_login(other_user)
    response = client.post(
        reverse("tasks:move", kwargs={"pk": task.pk}),
        {"container_id": col2.pk, "order": 0},
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_complete_task_creates_tasklog(user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="T3",
    )
    log = complete_task(task=task, completed_by=member)
    assert log.status == TaskStatus.COMPLETED
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED
    assert TaskLog.objects.filter(task=task, status=TaskStatus.COMPLETED).exists()
    assert AuditLog.objects.filter(action=AuditAction.TASK_COMPLETED).exists()


@pytest.mark.django_db
def test_column_reorder_updates_container_order(client, user):
    project, col1, col2 = _make_project_with_board(user)
    col3 = KanbanBoard.objects.create(project=project, name="Done", container_order=2)
    client.force_login(user)
    response = client.post(
        reverse("tasks:column_reorder", kwargs={"project_pk": project.pk}),
        {"order": [col3.pk, col1.pk, col2.pk]},
    )
    assert response.status_code == 204
    col1.refresh_from_db()
    col2.refresh_from_db()
    col3.refresh_from_db()
    assert (col3.container_order, col1.container_order, col2.container_order) == (0, 1, 2)


@pytest.mark.django_db
def test_column_reorder_rejects_partial_payload(client, user):
    project, col1, col2 = _make_project_with_board(user)
    client.force_login(user)
    response = client.post(
        reverse("tasks:column_reorder", kwargs={"project_pk": project.pk}),
        {"order": [col1.pk]},
    )
    assert response.status_code == 400
    col1.refresh_from_db()
    col2.refresh_from_db()
    # Order is unchanged on rejection.
    assert col1.container_order == 0
    assert col2.container_order == 1


@pytest.mark.django_db
def test_approve_completion_requires_pm(client, user, other_user):
    project, col1, _col2 = _make_project_with_board(user)
    pm_member = _primary_member(project, user)
    # Add a second user as a plain contributor.
    contrib_member = ProjectMember.objects.create(project=project)
    contrib_member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project,
        user=other_user,
        privilege=MemberPrivileges.CONTRIBUTOR,
    )
    task = Task.objects.create(
        project=project,
        created_by=pm_member,
        container=col1,
        container_task_order=0,
        name="T4",
    )
    log = complete_task(task=task, completed_by=pm_member)

    # Contributor cannot approve.
    client.force_login(other_user)
    url = reverse(
        "tasks:approve_log", kwargs={"pk": task.pk, "log_pk": log.pk}
    )
    response = client.post(url, {"approved": "true"})
    assert response.status_code == 403

    # PM can approve via the service layer.
    approve_completion(task_log=log, approved=True, verifier_user=user)
    log.refresh_from_db()
    assert log.approved is True
    assert AuditLog.objects.filter(action=AuditAction.TASK_APPROVED).exists()


@pytest.mark.django_db
def test_assigning_new_member_seeds_tasklog(client, user, other_user):
    project, col1, _col2 = _make_project_with_board(user)
    pm_member = _primary_member(project, user)
    extra_member = ProjectMember.objects.create(project=project)
    extra_member.users.add(other_user)
    task = Task.objects.create(
        project=project,
        created_by=pm_member,
        container=col1,
        container_task_order=0,
        name="T5",
    )
    task.assigned_members.add(pm_member)
    TaskLog.objects.create(task=task, assigned_to=pm_member)
    assert TaskLog.objects.filter(task=task).count() == 1

    client.force_login(user)
    url = reverse("tasks:assign", kwargs={"pk": task.pk})

    # Adding a brand-new assignee seeds a TaskLog (activity entry) for them,
    # while leaving the already-assigned member's log untouched.
    response = client.post(
        url,
        {"assigned_members": [pm_member.pk, extra_member.pk]},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert TaskLog.objects.filter(task=task, assigned_to=extra_member).count() == 1
    assert TaskLog.objects.filter(task=task, assigned_to=pm_member).count() == 1
    # The response asks the page to refresh the Completion block.
    assert response["HX-Trigger"] == "assignees-changed"

    # The Completion block now renders a row for the new member.
    block = client.get(
        reverse("tasks:status_block", kwargs={"pk": task.pk}), {"pane": "1"}
    )
    assert f'data-assignee-row="{extra_member.pk}"' in block.content.decode()

    # Re-submitting with the same members does not duplicate logs.
    client.post(
        url,
        {"assigned_members": [pm_member.pk, extra_member.pk]},
        HTTP_HX_REQUEST="true",
    )
    assert TaskLog.objects.filter(task=task, assigned_to=extra_member).count() == 1


@pytest.mark.django_db
def test_assign_without_htmx_redirects_to_task(client, user, other_user):
    # When htmx isn't running, a native form submit should still seed the
    # log and reload the task page (rather than return a bare partial).
    project, col1, _col2 = _make_project_with_board(user)
    pm_member = _primary_member(project, user)
    extra_member = ProjectMember.objects.create(project=project)
    extra_member.users.add(other_user)
    task = Task.objects.create(
        project=project,
        created_by=pm_member,
        container=col1,
        container_task_order=0,
        name="T7",
    )
    client.force_login(user)
    response = client.post(  # no HX-Request header
        reverse("tasks:assign", kwargs={"pk": task.pk}),
        {"assigned_members": [pm_member.pk, extra_member.pk]},
    )
    assert response.status_code == 302
    assert response.url == reverse(
        "tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk}
    )
    assert TaskLog.objects.filter(task=task, assigned_to=extra_member).exists()


@pytest.mark.django_db
def test_assigning_backfills_member_missing_a_tasklog(client, user, other_user):
    # An assignee that was added without a TaskLog (e.g. legacy/demo data)
    # gets one seeded the next time assignees are saved.
    project, col1, _col2 = _make_project_with_board(user)
    pm_member = _primary_member(project, user)
    no_log_member = ProjectMember.objects.create(project=project)
    no_log_member.users.add(other_user)
    task = Task.objects.create(
        project=project,
        created_by=pm_member,
        container=col1,
        container_task_order=0,
        name="T6",
    )
    # Both are assigned, but only pm_member has an activity entry.
    task.assigned_members.add(pm_member, no_log_member)
    TaskLog.objects.create(task=task, assigned_to=pm_member)
    assert not TaskLog.objects.filter(task=task, assigned_to=no_log_member).exists()

    client.force_login(user)
    response = client.post(
        reverse("tasks:assign", kwargs={"pk": task.pk}),
        {"assigned_members": [pm_member.pk, no_log_member.pk]},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert TaskLog.objects.filter(task=task, assigned_to=no_log_member).count() == 1
    block = client.get(
        reverse("tasks:status_block", kwargs={"pk": task.pk}), {"pane": "1"}
    )
    assert f'data-assignee-row="{no_log_member.pk}"' in block.content.decode()


@pytest.mark.django_db
def test_edit_form_seeds_assignee_logs(client, user, other_user):
    # Adding an assignee via the Task edit form should seed an activity entry,
    # just like the "Change assignees" flow does.
    project, col1, _col2 = _make_project_with_board(user)
    pm_member = _primary_member(project, user)
    extra_member = ProjectMember.objects.create(project=project)
    extra_member.users.add(other_user)
    task = Task.objects.create(
        project=project,
        created_by=pm_member,
        container=col1,
        container_task_order=0,
        name="EditSeed",
    )
    task.assigned_members.add(pm_member)
    TaskLog.objects.create(task=task, assigned_to=pm_member)

    client.force_login(user)
    response = client.post(
        reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk}),
        {
            "name": "EditSeed",
            "container": col1.pk,
            "contribution_type": "OTHER",
            "assigned_members": [pm_member.pk, extra_member.pk],
        },
    )
    assert response.status_code in (200, 302)
    assert TaskLog.objects.filter(task=task, assigned_to=extra_member).count() == 1


@pytest.mark.django_db
def test_create_form_sets_is_priority(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    client.force_login(user)
    response = client.post(
        reverse("tasks:create", kwargs={"project_pk": project.pk}),
        {
            "name": "Urgent task",
            "container": col1.pk,
            "contribution_type": "OTHER",
            "is_priority": "on",
        },
    )
    assert response.status_code in (200, 302)
    task = Task.objects.get(project=project, name="Urgent task")
    assert task.is_priority is True


@pytest.mark.django_db
def test_create_form_defaults_is_priority_false(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    client.force_login(user)
    response = client.post(
        reverse("tasks:create", kwargs={"project_pk": project.pk}),
        {
            "name": "Normal task",
            "container": col1.pk,
            "contribution_type": "OTHER",
        },
    )
    assert response.status_code in (200, 302)
    task = Task.objects.get(project=project, name="Normal task")
    assert task.is_priority is False


@pytest.mark.django_db
def test_create_form_prefills_dates_from_calendar_range_params(client, user):
    # A date range picked on the calendar view opens the create form with
    # ?start_date=&deadline= (YYYY-MM-DD) — the form should pre-fill both
    # fields at the form's default times (start at 9:00 AM, due at 5:00 PM),
    # without creating anything until the user saves.
    project, _col1, _col2 = _make_project_with_board(user)
    client.force_login(user)
    response = client.get(
        reverse("tasks:create", kwargs={"project_pk": project.pk}),
        {"start_date": "2026-03-10", "deadline": "2026-03-12"},
    )
    assert response.status_code == 200
    content = response.content.decode()
    assert 'name="start_date_0" value="2026-03-10"' in content
    assert 'name="start_date_1" value="09:00"' in content
    assert 'name="deadline_0" value="2026-03-12"' in content
    assert 'name="deadline_1" value="17:00"' in content
    assert not Task.objects.filter(project=project).exists()


@pytest.mark.django_db
def test_create_form_ignores_invalid_calendar_range_params(client, user):
    project, _col1, _col2 = _make_project_with_board(user)
    client.force_login(user)
    response = client.get(
        reverse("tasks:create", kwargs={"project_pk": project.pk}),
        {"start_date": "not-a-date", "deadline": ""},
    )
    assert response.status_code == 200
    assert 'value="not-a-date"' not in response.content.decode()


def test_task_form_datetime_fields_render_html5_local_format():
    # Each date is a day picker and a time picker, in the formats the
    # browser's date and time inputs take.
    d = timezone.make_aware(datetime(2026, 3, 10, 14, 30))
    form = TaskForm(project=None, initial={"deadline": d, "start_date": d})
    rendered = str(form["deadline"])
    assert 'type="date" name="deadline_0" value="2026-03-10"' in rendered
    assert 'type="time" name="deadline_1" value="14:30"' in rendered
    assert "2026-03-10 14:30:00" not in rendered


@pytest.mark.django_db
def test_priority_task_shows_flag_on_board_card(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="Flagged",
        is_priority=True,
    )
    client.force_login(user)
    response = client.get(reverse("tasks:board", kwargs={"project_pk": project.pk}))
    assert response.status_code == 200
    # The card renders the flag icon-only, so the wording lives in the
    # accessible name. (This page also embeds the List view, which keeps the
    # visible label — the icon-only/labelled split is pinned per view by
    # test_only_the_kanban_card_drops_the_priority_label.)
    assert 'aria-label="High priority"' in response.content.decode()


# ---------------------------------------------------------------------------
# Authorization: only Project Leads may create/edit/delete/move tasks etc.
# ---------------------------------------------------------------------------


def _new_user(email):
    """A third user, for the cases the two fixtures cannot cover."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        email=email, password="password123", name=email.split("@")[0].title()
    )


def _add_contributor(project, user):
    """Add ``user`` as a plain Member (CONTRIBUTOR, not a Lead)."""
    member = ProjectMember.objects.create(project=project)
    member.users.add(user)
    ProjectPrivilege.objects.create(
        project=project, user=user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    return member


@pytest.mark.django_db
def test_member_cannot_mutate_tasks(client, user, other_user):
    project, col1, col2 = _make_project_with_board(user)
    lead_member = _primary_member(project, user)
    _add_contributor(project, other_user)
    task = Task.objects.create(
        project=project,
        created_by=lead_member,
        container=col1,
        container_task_order=0,
        name="Locked",
    )
    milestone_pks = {"project_pk": project.pk}
    client.force_login(other_user)

    # Each of these mutating endpoints must be forbidden for a plain member.
    forbidden = [
        ("post", reverse("tasks:create", kwargs={"project_pk": project.pk}), {}),
        ("post", reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk}), {}),
        ("post", reverse("tasks:delete", kwargs={"project_pk": project.pk, "pk": task.pk}), {}),
        ("post", reverse("tasks:assign", kwargs={"pk": task.pk}), {}),
        ("post", reverse("tasks:move", kwargs={"pk": task.pk}), {"container_id": col2.pk, "order": 0}),
        ("post", reverse("tasks:change_column", kwargs={"pk": task.pk}), {"container_id": col2.pk}),
        ("post", reverse("tasks:reopen", kwargs={"pk": task.pk}), {}),
        ("post", reverse("tasks:set_milestone", kwargs={"pk": task.pk}), {}),
        ("post", reverse("tasks:column_create", kwargs=milestone_pks), {"name": "X"}),
        ("post", reverse("tasks:milestone_create", kwargs=milestone_pks), {"name": "M"}),
    ]
    for method, url, data in forbidden:
        resp = getattr(client, method)(url, data)
        assert resp.status_code == 403, f"{url} returned {resp.status_code}, expected 403"

    # The task must not have moved.
    task.refresh_from_db()
    assert task.container_id == col1.pk


@pytest.mark.django_db
def test_member_can_view_comment_and_complete(client, user, other_user):
    project, col1, _col2 = _make_project_with_board(user)
    lead_member = _primary_member(project, user)
    contrib_member = _add_contributor(project, other_user)
    task = Task.objects.create(
        project=project,
        created_by=lead_member,
        container=col1,
        container_task_order=0,
        name="Viewable",
    )
    task.assigned_members.add(contrib_member)
    TaskLog.objects.create(task=task, assigned_to=contrib_member)
    client.force_login(other_user)

    # View the task detail.
    detail = client.get(
        reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk})
    )
    assert detail.status_code == 200

    # Post a comment.
    comment = client.post(
        reverse("comments:create", kwargs={"task_pk": task.pk}),
        {"content": "Looks good"},
    )
    assert comment.status_code in (200, 201)
    assert task.comments.filter(content="Looks good", author=contrib_member).exists()

    # Mark the task done (members are allowed to).
    done = client.post(reverse("tasks:complete", kwargs={"pk": task.pk}))
    assert done.status_code in (200, 302)
    assert TaskLog.objects.filter(task=task, status=TaskStatus.COMPLETED).exists()


# ---------------------------------------------------------------------------
# TEAM vs INDIVIDUAL task completion
# ---------------------------------------------------------------------------


def _two_assignee_task(user, other_user):
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    contrib = ProjectMember.objects.create(project=project)
    contrib.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    task = Task.objects.create(
        project=project,
        created_by=lead,
        container=col1,
        container_task_order=0,
        name="Moded",
    )
    task.assigned_members.add(lead, contrib)
    TaskLog.objects.create(task=task, assigned_to=lead)
    TaskLog.objects.create(task=task, assigned_to=contrib)
    return project, task, lead, contrib


@pytest.mark.django_db
def test_individual_mode_lead_closes_with_assignees_outstanding(client, user, other_user):
    """A Lead can close an INDIVIDUAL-mode task whenever they judge it done.

    The outstanding assignee's TaskLog is left alone, so the audit trail does
    not credit them with a mark they never made — the gap the All tasks view
    reports as "Closed without your mark".
    """
    project, task, lead, contrib = _two_assignee_task(user, other_user)

    # One of the two assignees marks their own part done.
    client.force_login(other_user)
    client.post(reverse("tasks:complete", kwargs={"pk": task.pk}))
    task.refresh_from_db()
    assert task.status == TaskStatus.NOT_COMPLETED  # self-marks never close it
    assert task.assignees_done_count == 1

    # The Lead closes it anyway, with the other assignee still outstanding.
    client.force_login(user)
    ok = client.post(reverse("tasks:close", kwargs={"pk": task.pk}))
    assert ok.status_code in (200, 302)
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED
    # Still 1 of 2: closing the task did not mark the outstanding assignee done.
    assert task.assignees_done_count == 1
    assert not task.all_assignees_done


@pytest.mark.django_db
def test_individual_mode_lead_closes_with_nobody_done(client, user, other_user):
    """The Lead's close works from a standing start, with no self-marks at all."""
    project, task, lead, contrib = _two_assignee_task(user, other_user)
    assert task.assignees_done_count == 0

    client.force_login(user)
    ok = client.post(reverse("tasks:close", kwargs={"pk": task.pk}))
    assert ok.status_code in (200, 302)
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED
    assert task.assignees_done_count == 0


@pytest.mark.django_db
def test_any_assignee_can_self_mark_but_only_a_lead_closes(client, user, other_user):
    """Self-marking is every assignee's; closing is the Lead's.

    Self-marking used to be refused on a team-mode task. With completion
    modes gone there is no such task, so an assignee can always mark their
    own part — while closing stays Lead-only.
    """
    project, task, _lead, _contrib = _two_assignee_task(user, other_user)

    client.force_login(other_user)
    marked = client.post(reverse("tasks:complete", kwargs={"pk": task.pk}))
    assert marked.status_code in (200, 302)
    task.refresh_from_db()
    assert task.assignees_done_count == 1

    # ...but a member still cannot close the task.
    assert client.post(reverse("tasks:close", kwargs={"pk": task.pk})).status_code == 403
    task.refresh_from_db()
    assert task.status == TaskStatus.NOT_COMPLETED

    client.force_login(user)
    ok = client.post(reverse("tasks:close", kwargs={"pk": task.pk}))
    assert ok.status_code in (200, 302)
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.django_db
def test_board_calendar_only_shows_tasks_with_calendar_fields(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="Dated",
        deadline=datetime(2026, 3, 15, tzinfo=dt_timezone.utc),
    )
    Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=1,
        name="Undated",
    )
    client.force_login(user)
    response = client.get(
        reverse("tasks:board_calendar", kwargs={"project_pk": project.pk}),
        {"view": "month", "date": "2026-03-01"},
    )
    assert response.status_code == 200
    assert b"Dated" in response.content
    assert b"Undated" not in response.content


@pytest.mark.django_db
def test_board_calendar_requires_member_access(client, user, other_user):
    project, _col1, _col2 = _make_project_with_board(user)
    client.force_login(other_user)
    response = client.get(
        reverse("tasks:board_calendar", kwargs={"project_pk": project.pk})
    )
    assert response.status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("view", ["month", "week", "day"])
def test_board_calendar_renders_task_name_at_each_granularity(client, user, view):
    # Year view only renders a dot indicator per day (no room for task names),
    # so it's covered separately below.
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="Granularity task",
        deadline=datetime(2026, 3, 15, tzinfo=dt_timezone.utc),
    )
    client.force_login(user)
    response = client.get(
        reverse("tasks:board_calendar", kwargs={"project_pk": project.pk}),
        {"view": view, "date": "2026-03-15"},
    )
    assert response.status_code == 200
    assert b"Granularity task" in response.content


@pytest.mark.django_db
def test_board_calendar_year_view_renders(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="Granularity task",
        deadline=datetime(2026, 3, 15, tzinfo=dt_timezone.utc),
    )
    client.force_login(user)
    response = client.get(
        reverse("tasks:board_calendar", kwargs={"project_pk": project.pk}),
        {"view": "year", "date": "2026-03-15"},
    )
    assert response.status_code == 200
    assert b"March" in response.content


@pytest.mark.django_db
def test_board_calendar_unknown_view_falls_back_to_month(client, user):
    project, _col1, _col2 = _make_project_with_board(user)
    client.force_login(user)
    response = client.get(
        reverse("tasks:board_calendar", kwargs={"project_pk": project.pk}),
        {"view": "century"},
    )
    assert response.status_code == 200


class _FakeTask:
    def __init__(self, name, deadline=None, start_date=None, is_priority=False):
        self.name = name
        self.deadline = deadline
        self.start_date = start_date
        self.is_priority = is_priority


def test_build_month_grid_buckets_task_on_its_deadline_date():
    task = _FakeTask("A", deadline=datetime(2026, 3, 15, tzinfo=dt_timezone.utc))
    weeks = build_month_grid(2026, 3, [task])
    matches = [
        day
        for week in weeks
        for day in week["days"]
        if day.date.day == 15 and day.in_current_month
    ]
    assert len(matches) == 1
    assert matches[0].tasks == [task]


def test_build_month_grid_falls_back_to_start_date():
    task = _FakeTask("B", start_date=datetime(2026, 3, 3, tzinfo=dt_timezone.utc))
    weeks = build_month_grid(2026, 3, [task])
    matches = [day for week in weeks for day in week["days"] if day.date.day == 3]
    assert matches[0].tasks == [task]


def test_clamp_month_rolls_into_next_year():
    assert clamp_month(2026, 13) == (2027, 1)
    assert clamp_month(2026, 0) == (2025, 12)


def test_build_year_grid_places_task_in_its_month():
    task = _FakeTask("Yearly", deadline=datetime(2026, 7, 4, tzinfo=dt_timezone.utc))
    months = build_year_grid(2026, [task])
    assert len(months) == 12
    july = next(m for m in months if m["month"] == 7)
    matches = [
        day for week in july["weeks"] for day in week
        if day.date.day == 4 and day.in_current_month
    ]
    assert matches[0].tasks == [task]
    # No other month's *own* days (excluding leading/trailing overflow days
    # from adjacent months, which may legitimately echo the same date) carry
    # the task.
    other_months = [m for m in months if m["month"] != 7]
    assert all(
        not day.tasks
        for m in other_months
        for week in m["weeks"]
        for day in week
        if day.in_current_month
    )


def test_build_week_grid_spans_monday_to_sunday_and_includes_anchor():
    task = _FakeTask("Weekly", deadline=datetime(2026, 3, 18, tzinfo=dt_timezone.utc))
    # 2026-03-18 is a Wednesday.
    week = build_week_grid(date(2026, 3, 18), [task])
    days = week["days"]
    assert len(days) == 7
    assert days[0].date == date(2026, 3, 16)  # Monday
    assert days[-1].date == date(2026, 3, 22)  # Sunday
    matches = [day for day in days if day.date == date(2026, 3, 18)]
    assert matches[0].tasks == [task]
    assert week["lanes"] == []  # not a range: no bars


def test_build_day_tasks_returns_only_that_days_tasks():
    on_day = _FakeTask("OnDay", deadline=datetime(2026, 3, 18, tzinfo=dt_timezone.utc))
    other_day = _FakeTask("OtherDay", deadline=datetime(2026, 3, 19, tzinfo=dt_timezone.utc))
    items = build_day_tasks(date(2026, 3, 18), [on_day, other_day])
    assert [item.task for item in items] == [on_day]
    assert items[0].is_range is False


def test_parse_view_date_falls_back_to_today_on_bad_input():
    assert parse_view_date("not-a-date") == timezone.localdate()
    assert parse_view_date(None) == timezone.localdate()
    assert parse_view_date("2026-03-18") == date(2026, 3, 18)


def test_shift_date_per_granularity():
    anchor = date(2026, 3, 18)
    assert shift_date("day", anchor, 1) == date(2026, 3, 19)
    assert shift_date("week", anchor, 1) == date(2026, 3, 25)
    assert shift_date("month", anchor, 1) == date(2026, 4, 1)
    assert shift_date("year", anchor, 1) == date(2027, 1, 1)
    assert shift_date("month", date(2026, 12, 1), 1) == date(2027, 1, 1)


@pytest.mark.django_db
def test_board_full_page_renders_calendar_section(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="Full page dated task",
        deadline=datetime.now(dt_timezone.utc),
    )
    client.force_login(user)
    response = client.get(reverse("tasks:board", kwargs={"project_pk": project.pk}))
    assert response.status_code == 200
    assert b"$store.board.setView('calendar')" in response.content
    assert b"calendar-view-container" in response.content
    assert b"Full page dated task" in response.content


def test_task_range_returns_none_for_single_day_task():
    same_day = _FakeTask(
        "Same day",
        start_date=datetime(2026, 3, 10, 9, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 10, 17, tzinfo=dt_timezone.utc),
    )
    assert task_range(same_day) is None
    assert task_range(_FakeTask("Deadline only", deadline=datetime(2026, 3, 10, tzinfo=dt_timezone.utc))) is None
    assert task_range(_FakeTask("Neither")) is None


def test_task_range_returns_sorted_span_for_multi_day_task():
    task = _FakeTask(
        "Spans days",
        start_date=datetime(2026, 3, 10, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 14, tzinfo=dt_timezone.utc),
    )
    assert task_range(task) == (date(2026, 3, 10), date(2026, 3, 14))


def test_task_range_swaps_reversed_dates():
    # deadline set before start_date — still treated as a valid range, sorted.
    task = _FakeTask(
        "Reversed",
        start_date=datetime(2026, 3, 14, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 10, tzinfo=dt_timezone.utc),
    )
    assert task_range(task) == (date(2026, 3, 10), date(2026, 3, 14))


def test_bucket_tasks_by_day_covers_every_day_in_a_range():
    task = _FakeTask(
        "Ranged",
        start_date=datetime(2026, 3, 10, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    by_day = bucket_tasks_by_day([task])
    assert by_day[date(2026, 3, 10)] == [task]
    assert by_day[date(2026, 3, 11)] == [task]
    assert by_day[date(2026, 3, 12)] == [task]
    assert date(2026, 3, 13) not in by_day


def test_build_month_grid_renders_ranged_task_as_a_bar_not_a_pill():
    task = _FakeTask(
        "Ranged",
        start_date=datetime(2026, 3, 10, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    weeks = build_month_grid(2026, 3, [task])
    # The task never appears in any day cell's pill list...
    assert all(task not in day.tasks for week in weeks for day in week["days"])
    # ...it appears exactly once, as a lane bar, in the week it falls in.
    weeks_with_bars = [w for w in weeks if w["lanes"]]
    assert len(weeks_with_bars) == 1
    lanes = weeks_with_bars[0]["lanes"]
    assert len(lanes) == 1
    bar = lanes[0][0]
    assert bar["task"] is task
    assert bar["is_range_start"] is True
    assert bar["is_range_end"] is True
    # 2026-03-10 is a Tuesday (col 2), 2026-03-12 is a Thursday (col 4).
    assert bar["start_col"] == 2
    assert bar["end_col"] == 4


def test_build_month_grid_splits_range_bar_across_week_boundary():
    # 2026-03-16 (Mon) through 2026-03-20 (Fri) is a single week, so pick a
    # range that crosses into the next week to exercise clipping.
    task = _FakeTask(
        "Crosses weeks",
        start_date=datetime(2026, 3, 20, tzinfo=dt_timezone.utc),  # Friday
        deadline=datetime(2026, 3, 23, tzinfo=dt_timezone.utc),  # Monday (next week)
    )
    weeks = build_month_grid(2026, 3, [task])
    weeks_with_bars = [w for w in weeks if w["lanes"]]
    assert len(weeks_with_bars) == 2

    first_bar = weeks_with_bars[0]["lanes"][0][0]
    assert first_bar["is_range_start"] is True
    assert first_bar["is_range_end"] is False
    assert first_bar["end_col"] == 7  # clipped to Sunday

    second_bar = weeks_with_bars[1]["lanes"][0][0]
    assert second_bar["is_range_start"] is False
    assert second_bar["is_range_end"] is True
    assert second_bar["start_col"] == 1  # clipped to Monday


def test_build_month_grid_stacks_overlapping_ranges_into_separate_lanes():
    task_a = _FakeTask(
        "A",
        start_date=datetime(2026, 3, 9, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 11, tzinfo=dt_timezone.utc),
    )
    task_b = _FakeTask(
        "B",
        start_date=datetime(2026, 3, 10, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    weeks = build_month_grid(2026, 3, [task_a, task_b])
    lanes = next(w["lanes"] for w in weeks if w["lanes"])
    assert len(lanes) == 2  # overlapping columns forced into separate lanes
    assert {bar["task"] for lane in lanes for bar in lane} == {task_a, task_b}


def test_build_month_grid_shares_a_lane_for_non_overlapping_ranges():
    task_a = _FakeTask(
        "A",
        start_date=datetime(2026, 3, 2, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 3, tzinfo=dt_timezone.utc),
    )
    task_b = _FakeTask(
        "B",
        start_date=datetime(2026, 3, 5, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 6, tzinfo=dt_timezone.utc),
    )
    weeks = build_month_grid(2026, 3, [task_a, task_b])
    lanes = next(w["lanes"] for w in weeks if w["lanes"])
    assert len(lanes) == 1  # no column overlap: share lane 0
    assert len(lanes[0]) == 2


def test_build_week_grid_renders_ranged_task_as_a_bar():
    task = _FakeTask(
        "Ranged",
        start_date=datetime(2026, 3, 17, tzinfo=dt_timezone.utc),  # Tuesday
        deadline=datetime(2026, 3, 19, tzinfo=dt_timezone.utc),  # Thursday
    )
    week = build_week_grid(date(2026, 3, 18), [task])
    assert all(task not in day.tasks for day in week["days"])
    assert len(week["lanes"]) == 1
    bar = week["lanes"][0][0]
    assert bar["start_col"] == 2
    assert bar["end_col"] == 4


def test_build_day_tasks_marks_ranged_tasks_with_their_span():
    task = _FakeTask(
        "Ranged",
        start_date=datetime(2026, 3, 10, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    items = build_day_tasks(date(2026, 3, 11), [task])
    assert len(items) == 1
    assert items[0].is_range is True
    assert items[0].range_start == date(2026, 3, 10)
    assert items[0].range_end == date(2026, 3, 12)


@pytest.mark.django_db
def test_board_calendar_month_view_renders_range_bar_markup(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="Ranged task",
        start_date=datetime(2026, 3, 10, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    client.force_login(user)
    response = client.get(
        reverse("tasks:board_calendar", kwargs={"project_pk": project.pk}),
        {"view": "month", "date": "2026-03-01"},
    )
    assert response.status_code == 200
    content = response.content.decode()
    assert "Ranged task" in content
    assert "col-start-2" in content
    assert "col-end-5" in content


# ---------------------------------------------------------------------------
# List view: search / filter / sort
# ---------------------------------------------------------------------------


def _list_url(project):
    return reverse("tasks:board_list", kwargs={"project_pk": project.pk})


def _row_order(content: str, names) -> list[str]:
    """Names in the order they appear in the rendered rows."""
    return sorted(
        (name for name in names if name in content),
        key=content.index,
    )


@pytest.mark.django_db
def test_board_list_search_matches_name_description_and_assignee(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    by_name = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Widget wiring",
    )
    by_desc = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=1, name="Unrelated", description="mentions widget here",
    )
    by_assignee = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=2, name="Assigned elsewhere",
    )
    by_assignee.assigned_members.add(member)
    miss = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=3, name="Nothing to see",
    )

    client.force_login(user)
    content = client.get(_list_url(project), {"q": "widget"}).content.decode()
    assert by_name.name in content
    assert by_desc.name in content
    assert miss.name not in content

    # The member's display name falls back to the user's name, "Alice".
    content = client.get(_list_url(project), {"q": "alice"}).content.decode()
    assert by_assignee.name in content
    assert miss.name not in content


@pytest.mark.django_db
def test_board_list_filters_by_column_completion_and_priority(client, user):
    project, col1, col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    plain = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Plain task",
    )
    done = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=1, name="Finished task", status=TaskStatus.COMPLETED,
    )
    urgent = Task.objects.create(
        project=project, created_by=member, container=col2,
        container_task_order=0, name="Urgent task", is_priority=True,
    )
    client.force_login(user)

    content = client.get(_list_url(project), {"col": col2.pk}).content.decode()
    assert urgent.name in content
    assert plain.name not in content
    # Empty columns are dropped while filtering rather than left as dead sections.
    assert f'data-list-column-id="{col2.pk}"' in content
    assert f'data-list-column-id="{col1.pk}"' not in content

    content = client.get(_list_url(project), {"done": "complete"}).content.decode()
    assert done.name in content
    assert plain.name not in content

    content = client.get(_list_url(project), {"done": "not_complete"}).content.decode()
    assert plain.name in content
    assert done.name not in content

    content = client.get(_list_url(project), {"priority": "high"}).content.decode()
    assert urgent.name in content
    assert plain.name not in content


@pytest.mark.django_db
def test_board_list_filters_by_assignee_and_unassigned(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    mine = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Mine",
    )
    mine.assigned_members.add(member)
    nobodys = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=1, name="Unclaimed",
    )
    client.force_login(user)

    content = client.get(_list_url(project), {"assignee": member.pk}).content.decode()
    assert mine.name in content
    assert nobodys.name not in content

    content = client.get(_list_url(project), {"assignee": "none"}).content.decode()
    assert nobodys.name in content
    assert mine.name not in content


@pytest.mark.django_db
def test_board_list_sort_orders_rows_and_puts_undated_last(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    names = ["Beta", "Alpha", "Gamma"]
    for order, name in enumerate(names):
        Task.objects.create(
            project=project, created_by=member, container=col1,
            container_task_order=order, name=name,
            deadline=(
                datetime(2026, 3, 5, tzinfo=dt_timezone.utc) if name == "Beta"
                else datetime(2026, 3, 1, tzinfo=dt_timezone.utc) if name == "Alpha"
                else None
            ),
        )
    client.force_login(user)

    content = client.get(_list_url(project)).content.decode()
    assert _row_order(content, names) == ["Beta", "Alpha", "Gamma"]

    content = client.get(_list_url(project), {"sort": "name_asc"}).content.decode()
    assert _row_order(content, names) == ["Alpha", "Beta", "Gamma"]

    content = client.get(_list_url(project), {"sort": "name_desc"}).content.decode()
    assert _row_order(content, names) == ["Gamma", "Beta", "Alpha"]

    # Undated tasks sort last in both directions, never ahead of dated ones.
    content = client.get(_list_url(project), {"sort": "deadline_asc"}).content.decode()
    assert _row_order(content, names) == ["Alpha", "Beta", "Gamma"]

    content = client.get(_list_url(project), {"sort": "deadline_desc"}).content.decode()
    assert _row_order(content, names) == ["Beta", "Alpha", "Gamma"]


@pytest.mark.django_db
def test_board_list_renders_rows_without_a_control_bar(client, user):
    """The search / filter / sort bar is hidden; the rows still render, and
    ?results=1 still returns them on their own."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Only task",
    )
    client.force_login(user)

    full = client.get(_list_url(project)).content.decode()
    assert "Only task" in full
    assert 'id="list-results"' in full
    assert 'id="list-filters"' not in full
    assert 'name="q"' not in full
    assert 'name="sort"' not in full

    rows = client.get(_list_url(project), {"results": "1"}).content.decode()
    assert "Only task" in rows
    assert 'id="list-results"' not in rows


@pytest.mark.django_db
def test_board_list_drag_handle_only_shows_in_unfiltered_board_order(client, pm_user):
    project, col1, _col2 = _make_project_with_board(pm_user)
    member = _primary_member(project, pm_user)
    Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Draggable",
    )
    client.force_login(pm_user)

    assert "task-drag-handle" in client.get(_list_url(project)).content.decode()

    sorted_rows = client.get(_list_url(project), {"sort": "name_asc"}).content.decode()
    assert "task-drag-handle" not in sorted_rows

    filtered = client.get(_list_url(project), {"q": "Drag"}).content.decode()
    assert "task-drag-handle" not in filtered


# ---------------------------------------------------------------------------
# Overdue indicator
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_task_is_overdue_only_when_open_past_its_deadline(user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    past = timezone.now() - timedelta(days=1)
    future = timezone.now() + timedelta(days=1)

    def make(**kwargs):
        return Task.objects.create(
            project=project, created_by=member, container=col1,
            container_task_order=0, name="T", **kwargs,
        )

    assert make(deadline=past).is_overdue
    assert not make(deadline=future).is_overdue
    assert not make().is_overdue
    assert not make(deadline=past, status=TaskStatus.COMPLETED).is_overdue


@pytest.mark.django_db
def test_overdue_indicator_shows_on_every_view(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    yesterday = timezone.now() - timedelta(days=1)
    Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Late task", deadline=yesterday,
    )
    Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=1, name="Done late", deadline=yesterday,
        status=TaskStatus.COMPLETED,
    )
    client.force_login(user)
    kwargs = {"project_pk": project.pk}

    for url in (
        reverse("tasks:board_kanban", kwargs=kwargs),
        reverse("tasks:board_list", kwargs=kwargs),
        reverse("tasks:board_table", kwargs=kwargs),
    ):
        content = client.get(url).content.decode()
        # One pill, for the open task only — the completed one is not late.
        assert content.count("Overdue: was due") == 1

    date = timezone.localdate(yesterday).isoformat()
    for view in ("month", "week", "day"):
        content = client.get(
            reverse("tasks:board_calendar", kwargs=kwargs),
            {"view": view, "date": date},
        ).content.decode()
        assert content.count("Overdue: was due") == 1, view


def _overdue_filter_tasks(project, member, column):
    """One overdue task, and three that are not: finished late, due later
    (and high priority), undated."""
    now = timezone.now()
    for order, (name, deadline, status, priority) in enumerate((
        ("Late task", now - timedelta(days=1), TaskStatus.NOT_COMPLETED, False),
        ("Done late", now - timedelta(days=1), TaskStatus.COMPLETED, False),
        ("Later task", now + timedelta(days=1), TaskStatus.NOT_COMPLETED, True),
        ("Undated task", None, TaskStatus.NOT_COMPLETED, False),
    )):
        Task.objects.create(
            project=project, created_by=member, container=column,
            container_task_order=order, name=name, deadline=deadline, status=status,
            is_priority=priority,
        )


@pytest.mark.django_db
def test_board_filter_narrows_every_view(client, user):
    """All, Overdue, High Priority, Completed and Not Completed, in every view."""
    project, col1, _col2 = _make_project_with_board(user)
    _overdue_filter_tasks(project, _primary_member(project, user), col1)
    client.force_login(user)
    kwargs = {"project_pk": project.pk}
    every = {"Late task", "Done late", "Later task", "Undated task"}
    expected = {
        "": every,
        "overdue": {"Late task"},
        "high_priority": {"Later task"},
        "completed": {"Done late"},
        "not_completed": {"Late task", "Later task", "Undated task"},
        "nonsense": every,  # an unknown filter shows every task
    }

    for url in (
        reverse("tasks:board", kwargs=kwargs),
        reverse("tasks:board_kanban", kwargs=kwargs),
        reverse("tasks:board_list", kwargs=kwargs),
        reverse("tasks:board_table", kwargs=kwargs),
    ):
        for value, names in expected.items():
            content = client.get(url, {"filter": value} if value else {}).content.decode()
            shown = {name for name in every if name in content}
            assert shown == names, (url, value)

    date = timezone.localdate(timezone.now() - timedelta(days=1)).isoformat()
    calendar = reverse("tasks:board_calendar", kwargs=kwargs)
    day = {"view": "day", "date": date}
    assert "Done late" in client.get(calendar, day).content.decode()
    overdue = client.get(calendar, {**day, "filter": "overdue"}).content.decode()
    assert "Late task" in overdue and "Done late" not in overdue
    completed = client.get(calendar, {**day, "filter": "completed"}).content.decode()
    assert "Done late" in completed and "Late task" not in completed


@pytest.mark.django_db
def test_board_filter_menu(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    _overdue_filter_tasks(project, _primary_member(project, user), col1)
    client.force_login(user)
    board = reverse("tasks:board", kwargs={"project_pk": project.pk})

    # All: a "Filter" button whose menu links each choice, All in bold; nothing
    # is carried on the views' HTMX requests.
    content = client.get(board).content.decode()
    menu = content[content.index('aria-haspopup="true"'):]
    links = [menu.index(f'href="{board}{q}"') for q in (
        "", "?filter=overdue", "?filter=high_priority", "?filter=completed",
        "?filter=not_completed",
    )]
    assert links == sorted(links)
    for label in (">All</a>", ">Overdue</a>", ">High Priority</a>", ">Completed</a>",
                  ">Not Completed</a>"):
        assert label in menu
    assert 'aria-current="true">All</a>' in menu
    assert "htmx:configRequest" not in content

    # Filtered: the button still reads just "Filter", as the sidebar's
    # dropdowns do; the menu marks the choice, each view's requests keep
    # it, and Kanban does not reorder.
    content = client.get(board, {"filter": "completed"}).content.decode()
    assert "<span>Filter</span>" in content and "Filter: " not in content
    assert 'aria-current="true">Completed</a>' in content
    assert "evt.detail.parameters.filter = 'completed'" in content
    assert "a filtered board does not reorder" in content
    # The old Overdue chip is gone.
    assert "Overdue only" not in content


@pytest.mark.django_db
def test_overdue_is_an_icon_beside_the_flags_on_every_view(client, user):
    """Not a pill: an icon, named for screen readers, among the task's state
    icons (High priority, Overdue, Completed) — and in the Table, beside the
    due date."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    yesterday = timezone.now() - timedelta(days=1)
    Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Late task", deadline=yesterday, is_priority=True,
    )
    client.force_login(user)
    kwargs = {"project_pk": project.pk}
    pill = "rounded px-1.5 py-0.5 bg-error/10"
    date = timezone.localdate(yesterday).isoformat()

    for url, params in (
        (reverse("tasks:board_kanban", kwargs=kwargs), {}),
        (reverse("tasks:board_list", kwargs=kwargs), {}),
        (reverse("tasks:board_calendar", kwargs=kwargs), {"view": "day", "date": date}),
    ):
        content = client.get(url, params).content.decode()
        assert pill not in content, url
        assert content.count('role="img" aria-label="Overdue"') == 1, url
        # In the same row as the High priority flag, right after it.
        flag = content.index('aria-label="High priority"')
        assert flag < content.index('aria-label="Overdue"') < flag + 1500, url

    table = client.get(reverse("tasks:board_table", kwargs=kwargs)).content.decode()
    assert pill not in table
    assert table.count('role="img" aria-label="Overdue"') == 1


@pytest.mark.django_db
def test_high_priority_is_the_red_flag_alone_on_every_view(client, user):
    """No red margin down a high-priority card or row: the flag says it."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    tomorrow = timezone.now() + timedelta(days=1)
    Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Urgent", deadline=tomorrow, is_priority=True,
    )
    client.force_login(user)
    kwargs = {"project_pk": project.pk}
    calendar = reverse("tasks:board_calendar", kwargs=kwargs)
    date = timezone.localdate(tomorrow).isoformat()

    for url, params in (
        (reverse("tasks:board_kanban", kwargs=kwargs), {}),
        (reverse("tasks:board_list", kwargs=kwargs), {}),
        (calendar, {"view": "day", "date": date}),
        (calendar, {"view": "week", "date": date}),
        (calendar, {"view": "month", "date": date}),
    ):
        content = client.get(url, params).content.decode()
        assert "border-l-error" not in content, (url, params)
        assert 'aria-label="High priority"' in content, (url, params)
    assert "border-l-error" not in client.get(reverse("tasks:my_list")).content.decode()


@pytest.mark.django_db
def test_overdue_pill_on_task_pane_and_page_follows_close_and_reopen(client, pm_user):
    project, col1, _col2 = _make_project_with_board(pm_user)
    member = _primary_member(project, pm_user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Late task",
        deadline=timezone.now() - timedelta(days=1),
    )
    client.force_login(pm_user)
    kwargs = {"project_pk": project.pk, "pk": task.pk}

    for url in (reverse("tasks:detail_pane", kwargs=kwargs), reverse("tasks:detail", kwargs=kwargs)):
        content = client.get(url).content.decode()
        # Inside the completion chip, so the out-of-band chip swap carries it.
        chip = content[content.index('id="task-completion-chip"'):]
        assert chip.index("Overdue: was due") < chip.index(col1.name), url

    # Closing re-renders the chip out of band without the pill; reopening
    # brings it back. (The completion buttons post pane_mode on both surfaces.)
    htmx = {"HTTP_HX_REQUEST": "true"}
    closed = client.post(reverse("tasks:close", kwargs={"pk": task.pk}), {"pane_mode": "1"}, **htmx).content.decode()
    assert 'id="task-completion-chip" hx-swap-oob="true"' in closed
    assert "Overdue: was due" not in closed
    reopened = client.post(reverse("tasks:reopen", kwargs={"pk": task.pk}), {"pane_mode": "1"}, **htmx).content.decode()
    assert 'id="task-completion-chip" hx-swap-oob="true"' in reopened
    assert "Overdue: was due" in reopened


@pytest.mark.django_db
def test_task_cards_show_only_the_blue_completed_check(client, user, other_user):
    """Every part marked done and every submission reviewed add no icon to
    a card or row; the task marked completed shows the blue check."""
    from apps.tasks.services import close_task, mark_own_done, mark_reviewed

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    task.reviewers.add(lead)
    client.force_login(user)
    kwargs = {"project_pk": project.pk}
    views = (
        reverse("tasks:board_kanban", kwargs=kwargs),
        reverse("tasks:board_list", kwargs=kwargs),
    )

    mark_own_done(task=task, member=contrib)
    mark_own_done(task=task, member=lead)
    mark_reviewed(task_log=_current_log(task, contrib), reviewer=lead)
    mark_reviewed(task_log=_current_log(task, lead), reviewer=lead)
    close_task(task=task, closed_by=lead)
    for url in views:
        page = client.get(url).content.decode()
        assert "All assignees marked their parts done" not in page, url
        assert "All submissions reviewed" not in page, url
        check = page[page.rindex("<span", 0, page.index('aria-label="Complete"')):]
        assert "text-blue-600" in check[: check.index(">")], url


@pytest.mark.django_db
def test_completion_card_separates_done_and_reviewed_progress(client, user, other_user, pm_user):
    """Marked done and Reviewed each get a labelled bar and count, with no
    count beside the heading — the bars already say how far along it is. A
    task without review shows the done bar alone."""
    from apps.tasks.services import mark_own_done, mark_reviewed

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    third = _add_contributor(project, pm_user)
    task.assigned_members.add(third)
    task.reviewers.add(lead)
    mark_own_done(task=task, member=contrib)
    mark_own_done(task=task, member=lead)
    mark_reviewed(task_log=_current_log(task, contrib), reviewer=lead)
    client.force_login(user)

    pane = client.get(_pane_url(project, task)).content.decode()
    assert "complete</span>" not in pane
    progress = pane[pane.index("data-progress") : pane.index("data-assignee-rows")]
    assert progress.index("Marked done") < progress.index("2 of 3") < progress.index("Reviewed") < progress.index("1 of 3")
    assert "bg-success" in progress and "bg-violet-600" in progress
    # The boxes carry the stage colours; the date and who beside them are
    # plain text. A part done but not reviewed shows the done line alone.
    row = _row_for(pane, contrib)
    assert "completion-tick-done" in row and "completion-tick-review" in row
    assert "text-success" not in row and "text-violet-600" not in row
    assert "data-done-line" in _row_for(pane, lead) and "data-review-line" not in _row_for(pane, lead)
    detail = client.get(
        reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk})
    ).content.decode()
    assert "complete</span>" not in detail

    # No review on the task: one bar, and no count beside the heading — the
    # bar already says how many are done.
    task.reviewers.clear()
    for log in task.task_logs.all():
        log.reviewed_by = None
        log.reviewed_at = None
        log.save(update_fields=["reviewed_by", "reviewed_at"])
    pane = client.get(_pane_url(project, task)).content.decode()
    assert "2 of 3 done" not in pane and "complete</span>" not in pane
    progress = pane[pane.index("data-progress") : pane.index("data-assignee-rows")]
    assert "Marked done" in progress and "Reviewed" not in progress
    assert "2 of 3" in progress


@pytest.mark.django_db
def test_closed_by_is_a_property_row_in_the_pane_and_inline_on_the_page(client, pm_user):
    project, col1, _col2 = _make_project_with_board(pm_user)
    member = _primary_member(project, pm_user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Closable",
    )
    client.force_login(pm_user)
    htmx = {"HTTP_HX_REQUEST": "true"}
    close_url = reverse("tasks:close", kwargs={"pk": task.pk})

    # Closed from the pane (which sends surface=pane): the out-of-band line
    # comes back as a dt/dd row for the pane's property list.
    closed = client.post(close_url, {"pane_mode": "1", "surface": "pane"}, **htmx).content.decode()
    meta = closed[closed.index('id="task-closed-by-meta"') - 5 :]
    assert meta.startswith("<div ")
    assert "<dt" in meta and "Closed by" in meta and member.display_name in meta

    # The pane's days-left hint goes with the same swap: none once closed.
    assert 'id="task-due-hint" class="text-brand-text/60" hx-swap-oob="true"></span>' in closed

    # The pane's own render puts it inside the property list, after Created by.
    pane = client.get(_pane_url(project, task)).content.decode()
    dl = pane[pane.index("<dl") : pane.index("</dl>")]
    assert dl.index("Created by") < dl.index("Closed by")

    # Reopened, then closed from the full page (no surface): the inline item.
    client.post(reverse("tasks:reopen", kwargs={"pk": task.pk}), {"pane_mode": "1"}, **htmx)
    closed = client.post(close_url, {"pane_mode": "1"}, **htmx).content.decode()
    meta = closed[closed.index('id="task-closed-by-meta"') - 6 :]
    assert meta.startswith("<span ")
    assert "<dt" not in meta[: meta.index("</span></span>")]


# ---------------------------------------------------------------------------
# Table view
# ---------------------------------------------------------------------------


def _table_url(project):
    return reverse("tasks:board_table", kwargs={"project_pk": project.pk})


@pytest.mark.django_db
def test_board_page_offers_the_table_view(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Tabled task",
    )
    client.force_login(user)

    content = client.get(
        reverse("tasks:board", kwargs={"project_pk": project.pk})
    ).content.decode()
    assert "setView('table')" in content
    assert 'id="table-view-container"' in content
    assert "<table" in content


@pytest.mark.django_db
def test_board_table_lists_every_task_with_its_counts(client, user):
    """No Column column; Assignees and Reviewers are counts, not names."""
    project, col1, col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    first = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="First task",
    )
    first.assigned_members.add(member)
    Task.objects.create(
        project=project, created_by=member, container=col2,
        container_task_order=0, name="Second task", is_priority=True,
        status=TaskStatus.COMPLETED,
    )
    client.force_login(user)

    response = client.get(_table_url(project))
    assert response.status_code == 200
    content = response.content.decode()
    assert "First task" in content
    assert "Second task" in content
    assert ">Column<" not in content and "table_sort=column" not in content
    assert "table_sort=reviewers" in content and "table_sort=assignees" in content
    assert col1.name not in content and col2.name not in content
    assert "Alice" not in content
    assert 'aria-label="1 assigned member"' in content
    assert 'aria-label="0 assigned members"' in content
    assert content.count('aria-label="0 reviewers"') == 2
    assert "High priority" in content
    assert "Complete" in content
    assert 'scope="col"' in content


@pytest.mark.django_db
def test_board_table_sorts_by_header_and_falls_back_to_board_order(client, user):
    project, col1, col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    Task.objects.create(
        project=project, created_by=member, container=col2,
        container_task_order=0, name="Alpha",
        deadline=datetime(2026, 3, 5, tzinfo=dt_timezone.utc),
    )
    Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Gamma",
    )
    Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=1, name="Beta",
        deadline=datetime(2026, 3, 1, tzinfo=dt_timezone.utc),
    )
    names = ["Alpha", "Beta", "Gamma"]
    client.force_login(user)

    content = client.get(_table_url(project)).content.decode()
    assert _row_order(content, names) == ["Gamma", "Beta", "Alpha"]

    content = client.get(_table_url(project), {"table_sort": "name"}).content.decode()
    assert _row_order(content, names) == ["Alpha", "Beta", "Gamma"]
    assert 'aria-sort="ascending"' in content
    # The active header flips to descending on the next click.
    assert "table_sort=-name" in content

    content = client.get(_table_url(project), {"table_sort": "-name"}).content.decode()
    assert _row_order(content, names) == ["Gamma", "Beta", "Alpha"]
    assert 'aria-sort="descending"' in content

    # Undated tasks sort last in both directions.
    content = client.get(_table_url(project), {"table_sort": "due"}).content.decode()
    assert _row_order(content, names) == ["Beta", "Alpha", "Gamma"]
    content = client.get(_table_url(project), {"table_sort": "-due"}).content.decode()
    assert _row_order(content, names) == ["Alpha", "Beta", "Gamma"]

    content = client.get(_table_url(project), {"table_sort": "bogus"}).content.decode()
    assert _row_order(content, names) == ["Gamma", "Beta", "Alpha"]


@pytest.mark.django_db
def test_board_table_sorts_by_assignee_and_reviewer_counts(client, user, other_user):
    """Assignees and Reviewers sort by how many; board order breaks ties."""
    project, col1, _col2 = _make_project_with_board(user)
    alice = _primary_member(project, user)
    bob = ProjectMember.objects.create(project=project)
    bob.users.add(other_user)
    # Board order deliberately differs from count order.
    two = Task.objects.create(
        project=project, created_by=alice, container=col1,
        container_task_order=0, name="Two people",
    )
    two.assigned_members.add(alice, bob)
    nobody = Task.objects.create(
        project=project, created_by=alice, container=col1,
        container_task_order=1, name="Nobody",
    )
    nobody.reviewers.add(alice, bob)
    one = Task.objects.create(
        project=project, created_by=alice, container=col1,
        container_task_order=2, name="One person",
    )
    one.assigned_members.add(alice)
    one.reviewers.add(alice)
    names = [two.name, nobody.name, one.name]
    client.force_login(user)

    def order(sort):
        content = client.get(_table_url(project), {"table_sort": sort}).content.decode()
        return _row_order(content, names)

    assert order("assignees") == ["Nobody", "One person", "Two people"]
    assert order("-assignees") == ["Two people", "One person", "Nobody"]
    assert order("reviewers") == ["Two people", "One person", "Nobody"]
    assert order("-reviewers") == ["Nobody", "One person", "Two people"]
    content = client.get(_table_url(project), {"table_sort": "reviewers"}).content.decode()
    assert "table_sort=-reviewers" in content


@pytest.mark.django_db
def test_board_table_empty_and_access(client, user, other_user):
    project, _col1, _col2 = _make_project_with_board(user)
    client.force_login(user)
    assert "No tasks yet" in client.get(_table_url(project)).content.decode()

    client.force_login(other_user)
    assert client.get(_table_url(project)).status_code == 403


@pytest.mark.django_db
def test_board_list_reports_no_matches_and_ignores_unknown_params(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Real task",
    )
    client.force_login(user)

    content = client.get(_list_url(project), {"q": "nothing matches this"}).content.decode()
    assert "No matching tasks" in content
    assert "Real task" not in content

    # Junk filter/sort values fall back to "no filter" rather than 500ing.
    content = client.get(
        _list_url(project),
        {"sort": "bogus", "done": "bogus", "priority": "bogus",
         "col": "9999", "assignee": "9999"},
    ).content.decode()
    assert "Real task" in content
    assert "No matching tasks" not in content


@pytest.mark.django_db
def test_board_page_shows_manage_columns_for_every_view(client, pm_user):
    """The button used to be Kanban-only; columns are edited from any view."""
    project, _col1, _col2 = _make_project_with_board(pm_user)
    client.force_login(pm_user)
    response = client.get(reverse("tasks:board", kwargs={"project_pk": project.pk}))
    content = response.content.decode()
    assert "Manage columns" in content
    label_at = re.search(r"Manage columns\s*</button>", content).start()
    button = content[content.rindex("<button", 0, label_at):]
    markup = button[: button.index("</button>")]
    # No x-show gate on the button itself (the view switcher still uses one).
    assert "x-show" not in markup
    # Plain text, as Join requests and Invitations on Contributors, saying
    # "Manage columns" in full, with no icon.
    assert 'class="btn btn-sm btn-ghost"' in markup
    assert "<svg" not in markup
    assert markup.rstrip().endswith("Manage columns")
    # Right of the views, then Filter, then New task.
    toolbar = content[content.index('aria-label="Task views"'):]
    order = [toolbar.index(x) for x in (
        "Calendar", 'class="btn btn-sm btn-ghost"', 'aria-haspopup="true"', "New task",
    )]
    assert order == sorted(order)


# ---------------------------------------------------------------------------
# Shared task date label
# ---------------------------------------------------------------------------


def _label(start=None, end=None, today=date(2026, 6, 1)):
    return task_date_label(_FakeTask("T", start_date=start, deadline=end), today=today)


def test_task_date_label_shows_a_span_when_both_dates_are_set():
    label = _label(
        start=datetime(2026, 3, 10, tzinfo=dt_timezone.utc),
        end=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    # The year is dropped in the current year, and carried once on the end date.
    assert label["short"] == "Mar 10 – Mar 12"
    assert label["full"] == "Mar 10, 2026 – Mar 12, 2026"


def test_task_date_label_carries_the_year_outside_the_current_one():
    label = _label(
        start=datetime(2027, 3, 10, tzinfo=dt_timezone.utc),
        end=datetime(2027, 3, 12, tzinfo=dt_timezone.utc),
    )
    assert label["short"] == "Mar 10 – Mar 12, 2027"

    # A span across a year boundary spells out both years.
    label = _label(
        start=datetime(2026, 12, 28, tzinfo=dt_timezone.utc),
        end=datetime(2027, 1, 3, tzinfo=dt_timezone.utc),
    )
    assert label["short"] == "Dec 28, 2026 – Jan 3, 2027"


def test_task_date_label_names_a_lone_date_so_it_is_never_ambiguous():
    assert _label(end=datetime(2026, 3, 12, tzinfo=dt_timezone.utc))["short"] == "Due Mar 12"
    assert _label(start=datetime(2026, 3, 10, tzinfo=dt_timezone.utc))["short"] == "Starts Mar 10"
    # Both dates on the same day is a due date, not a zero-length span.
    same = datetime(2026, 3, 12, tzinfo=dt_timezone.utc)
    assert _label(start=same, end=same)["short"] == "Due Mar 12"


def test_task_date_label_is_empty_without_dates():
    assert _label() == {"short": "", "full": ""}


def test_task_date_label_orders_a_reversed_span():
    label = _label(
        start=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
        end=datetime(2026, 3, 10, tzinfo=dt_timezone.utc),
    )
    assert label["short"] == "Mar 10 – Mar 12"


@pytest.mark.django_db
def test_kanban_list_and_calendar_render_the_same_date_span(client, user):
    """One task, three views, one date string."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Ranged task",
        start_date=datetime(2026, 3, 10, 9, 0, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 12, 17, 0, tzinfo=dt_timezone.utc),
    )
    client.force_login(user)
    span = task_date_label(Task.objects.get(name="Ranged task"))["short"]
    assert "–" in span

    kanban = client.get(
        reverse("tasks:board_kanban", kwargs={"project_pk": project.pk})
    ).content.decode()
    listing = client.get(
        reverse("tasks:board_list", kwargs={"project_pk": project.pk})
    ).content.decode()
    calendar_day = client.get(
        reverse("tasks:board_calendar", kwargs={"project_pk": project.pk}),
        {"view": "day", "date": "2026-03-11"},
    ).content.decode()

    for view_name, content in (
        ("kanban", kanban), ("list", listing), ("calendar", calendar_day),
    ):
        assert span in content, f"{view_name} view is missing the shared date span"
        # The old deadline-only rendering is gone from all three.
        assert "Mar 12, 2026<" not in content


@pytest.mark.django_db
def test_kanban_card_has_no_status_pill_but_the_column_heading_names_it(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Carded",
    )
    client.force_login(user)
    content = client.get(
        reverse("tasks:board_kanban", kwargs={"project_pk": project.pk})
    ).content.decode()

    assert "Carded" in content
    # The column header still carries the status; the card no longer repeats it.
    card = content[content.index('id="task-card-'):]
    card = card[: card.index("</li>")]
    assert "Carded" in card
    assert col1.name not in card
    assert "rounded-full text-xs font-medium" not in card


@pytest.mark.django_db
def test_task_descriptions_stay_out_of_the_kanban_list_and_day_views(client, user):
    """Descriptions live in the side pane; the browsing views show names only.
    Search still reads the description server-side."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Documented", description="a description nobody should see here",
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    client.force_login(user)

    views = {
        "kanban": (reverse("tasks:board_kanban", kwargs={"project_pk": project.pk}), {}),
        "list": (reverse("tasks:board_list", kwargs={"project_pk": project.pk}), {}),
        "calendar day": (
            reverse("tasks:board_calendar", kwargs={"project_pk": project.pk}),
            {"view": "day", "date": "2026-03-12"},
        ),
    }
    for name, (url, params) in views.items():
        content = client.get(url, params).content.decode()
        assert "Documented" in content, f"{name} view lost the task name"
        assert "nobody should see here" not in content, f"{name} view still shows the description"

    # The pane is where the description belongs.
    pane = client.get(
        reverse("tasks:detail_pane", kwargs={"project_pk": project.pk, "pk": Task.objects.get().pk})
    ).content.decode()
    assert "nobody should see here" in pane

    # ...and searching on it still works even though it is not displayed.
    hit = client.get(views["list"][0], {"q": "nobody should see"}).content.decode()
    assert "Documented" in hit


@pytest.mark.django_db
def test_assignee_avatars_stay_out_of_the_kanban_list_and_day_views(client, user):
    """Assignees are shown in the side pane, not stacked onto browsing rows."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Staffed", deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    task.assigned_members.add(member)
    # The avatar carried str(member) as its title and display_name is what the
    # pane lists, so pin both and assert neither reaches a browsing view.
    member.name = "Assignee Avatarson"
    member.save(update_fields=["name"])
    titled, listed = str(member), member.display_name
    client.force_login(user)

    views = {
        "kanban": (reverse("tasks:board_kanban", kwargs={"project_pk": project.pk}), {}),
        "list": (reverse("tasks:board_list", kwargs={"project_pk": project.pk}), {}),
        "calendar day": (
            reverse("tasks:board_calendar", kwargs={"project_pk": project.pk}),
            {"view": "day", "date": "2026-03-12"},
        ),
    }
    for name, (url, params) in views.items():
        content = client.get(url, params).content.decode()
        assert "Staffed" in content, f"{name} view lost the task name"
        assert titled not in content, f"{name} view still shows an assignee avatar"
        assert listed not in content, f"{name} view still names the assignee"

    # The pane still lists them.
    pane = client.get(
        reverse("tasks:detail_pane", kwargs={"project_pk": project.pk, "pk": task.pk})
    ).content.decode()
    assert listed in pane

    # ...and the List view still searches by assignee even without showing them.
    hit = client.get(views["list"][0], {"q": "Avatarson"}).content.decode()
    assert "Staffed" in hit


# ---------------------------------------------------------------------------
# Side-pane completion button (Lead only)
# ---------------------------------------------------------------------------


def _pane_url(project, task):
    return reverse(
        "tasks:detail_pane", kwargs={"project_pk": project.pk, "pk": task.pk}
    )


@pytest.mark.django_db
def test_pane_completion_button_is_lead_only(client, user, other_user):
    """The pane's completion toggle shows for Leads and not for plain Members."""
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=lead,
        container=col1,
        container_task_order=0,
        name="Closable",
    )
    _add_contributor(project, other_user)

    client.force_login(user)
    lead_pane = client.get(_pane_url(project, task)).content.decode()
    assert "Mark Completed" in lead_pane
    assert reverse("tasks:close", kwargs={"pk": task.pk}) in lead_pane

    client.force_login(other_user)
    member_pane = client.get(_pane_url(project, task)).content.decode()
    assert "Mark Completed" not in member_pane
    assert reverse("tasks:close", kwargs={"pk": task.pk}) not in member_pane


@pytest.mark.django_db
def test_pane_pills_share_one_row_above_the_dates(client, user):
    """Priority and completion read left to right, over the dates; the
    Kanban column is a property row after them, not a pill."""
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=lead,
        container=col1,
        container_task_order=0,
        name="Pilled",
        is_priority=True,
        start_date=datetime(2026, 3, 10, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()

    assert (
        pane.index("High priority")
        < pane.index("Not Complete")
        < pane.index("Mar 10, 2026 12:00 AM – Mar 12, 2026 12:00 AM UTC")
        < pane.index("Kanban column")
    ), "expected High priority, completion, the dates, then the Kanban column row"
    if project.tracks_credit:
        assert pane.index("Task type") < pane.index("Kanban column")


@pytest.mark.django_db
def test_pane_pill_row_drops_the_flag_when_not_priority(client, user):
    """The flag is conditional; the completion chip still leads the row."""
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=lead,
        container=col1,
        container_task_order=0,
        name="Unflagged",
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()

    assert "High priority" not in pane
    assert pane.index("Not Complete") < pane.index("Due Mar 12, 2026") < pane.index("Kanban column")


@pytest.mark.django_db
def test_pane_completion_button_sits_in_the_top_bar(client, user):
    """The button leads the top bar, ahead of the task name and everything else."""
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=lead,
        container=col1,
        container_task_order=0,
        name="Titled",
    )
    client.force_login(user)

    pane = client.get(_pane_url(project, task)).content.decode()
    # In the bar, so ahead of the title and the Delete / Edit / Open group.
    assert (
        pane.index("Mark Completed")
        < pane.index('aria-label="Open full task page"')
        < pane.index("Titled")
        < pane.index(">Completion<")
    )
    # It lives outside #task-status-block, so the block's swap has to reach it
    # out of band.
    assert 'id="task-completion-button"' in pane
    assert pane.index('id="task-completion-button"') < pane.index('id="task-status-block"')


@pytest.mark.django_db
def test_pane_completion_button_toggles_not_complete_to_completed(client, user):
    """A Lead clicking the pane button flips the task, and the button inverts."""
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=lead,
        container=col1,
        container_task_order=0,
        name="Toggling",
    )
    task.assigned_members.add(lead)
    client.force_login(user)

    # Not Complete -> Completed.
    done = client.post(
        reverse("tasks:close", kwargs={"pk": task.pk}),
        {"pane_mode": "1"},
        headers={"hx-request": "true"},
    )
    assert done.status_code == 200
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED
    # The swapped block offers the reverse action and syncs the pane chip.
    body = done.content.decode()
    assert "Mark Not Complete" in body
    assert reverse("tasks:reopen", kwargs={"pk": task.pk}) in body
    assert 'id="task-completion-chip"' in body
    assert "Not Complete</span>" not in body

    # Completed -> Not Complete.
    undone = client.post(
        reverse("tasks:reopen", kwargs={"pk": task.pk}),
        {"pane_mode": "1"},
        headers={"hx-request": "true"},
    )
    assert undone.status_code == 200
    task.refresh_from_db()
    assert task.status == TaskStatus.NOT_COMPLETED
    assert "Mark Completed" in undone.content.decode()


@pytest.mark.django_db
def test_pane_completion_button_is_live_with_assignees_outstanding(client, user, other_user):
    """The Lead's button is actionable however many assignees are outstanding."""
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    client.force_login(user)
    close_url = reverse("tasks:close", kwargs={"pk": task.pk})

    # Nobody done, one of two done, everyone done: same live button throughout.
    for done, marker in enumerate((None, lead, contrib)):
        if marker is not None:
            mark_own_done(task=task, member=marker)
        pane = client.get(_pane_url(project, task)).content.decode()
        assert "Mark Completed" in pane, f"{done} done: button missing"
        assert close_url in pane, f"{done} done: button not wired to close"
        button = pane[pane.index('id="task-completion-button"'):]
        button = button[: button.index("</span>")]
        assert "disabled" not in button, f"{done} done: button disabled"


# ---------------------------------------------------------------------------
# Completed check circle in the browsing views
# ---------------------------------------------------------------------------


def _browsing_views(project):
    """The Kanban, List and Calendar surfaces, each keyed by a readable name."""
    calendar_url = reverse("tasks:board_calendar", kwargs={"project_pk": project.pk})
    return {
        "kanban": (reverse("tasks:board_kanban", kwargs={"project_pk": project.pk}), {}),
        "list": (reverse("tasks:board_list", kwargs={"project_pk": project.pk}), {}),
        "calendar day": (calendar_url, {"view": "day", "date": "2026-03-12"}),
        "calendar week": (calendar_url, {"view": "week", "date": "2026-03-12"}),
        "calendar month": (calendar_url, {"view": "month", "date": "2026-03-12"}),
    }


@pytest.mark.django_db
def test_completed_tasks_show_a_check_in_the_browsing_views(client, user):
    """Kanban, List and Calendar mark completed tasks with a check circle.

    Open tasks get no marker, so exactly one check shows per view even though
    both tasks are listed.
    """
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    due = datetime(2026, 3, 12, tzinfo=dt_timezone.utc)
    Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Finished", status=TaskStatus.COMPLETED, deadline=due,
    )
    Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=1,
        name="Ongoing", deadline=due,
    )
    client.force_login(user)

    for name, (url, params) in _browsing_views(project).items():
        content = client.get(url, params).content.decode()
        assert "Finished" in content, f"{name} view lost the completed task"
        assert "Ongoing" in content, f"{name} view lost the open task"
        assert content.count('aria-label="Complete"') == 1, (
            f"{name} view should mark exactly the completed task"
        )


@pytest.mark.django_db
def test_open_tasks_show_no_check_in_the_browsing_views(client, user):
    """Nothing is marked while every task is still open."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Ongoing", deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    client.force_login(user)

    for name, (url, params) in _browsing_views(project).items():
        content = client.get(url, params).content.decode()
        assert "Ongoing" in content, f"{name} view lost the task"
        assert 'aria-label="Complete"' not in content, (
            f"{name} view marked an open task complete"
        )


@pytest.mark.django_db
def test_completed_calendar_range_bar_shows_a_check(client, user):
    """A completed multi-day task is marked on its calendar range bar too.

    Range tasks render as lane bars rather than day-cell pills, which is a
    separate template from the single-day pill.
    """
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    # Mar 10-12 2026 sits inside one Mon-Sun week, so this spans a single bar.
    Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Spanning", status=TaskStatus.COMPLETED,
        start_date=datetime(2026, 3, 10, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    client.force_login(user)

    url = reverse("tasks:board_calendar", kwargs={"project_pk": project.pk})
    for view in ("week", "month"):
        content = client.get(url, {"view": view, "date": "2026-03-12"}).content.decode()
        assert "Spanning" in content, f"calendar {view} lost the range task"
        assert content.count('aria-label="Complete"') == 1, (
            f"calendar {view} should mark the completed range bar"
        )


@pytest.mark.django_db
def test_delete_confirmation_markup_is_lead_only(client, user, other_user):
    """Members get no delete dialog on any task surface.

    The dialog carries a POST form to tasks:delete. The endpoint is Lead-only
    either way, so this is tidiness rather than a hole — but a member had no
    button to open it, so the markup was only ever dead weight.
    """
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=lead,
        container=col1,
        container_task_order=0,
        name="Deletable",
    )
    _add_contributor(project, other_user)

    surfaces = {
        "pane": reverse("tasks:detail_pane", kwargs={"project_pk": project.pk, "pk": task.pk}),
        "detail page": reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk}),
        # The Project Overview no longer lists tasks with Edit/Delete; its
        # pane loads the delete form with the task, covered above.
    }
    delete_url = reverse("tasks:delete", kwargs={"project_pk": project.pk, "pk": task.pk})

    client.force_login(user)
    for name, url in surfaces.items():
        html = client.get(url).content.decode()
        assert delete_url in html, f"{name} lost the Lead's delete form"

    client.force_login(other_user)
    for name, url in surfaces.items():
        html = client.get(url).content.decode()
        assert "Delete task confirmation" not in html, f"{name} still ships the dialog"
        assert delete_url not in html, f"{name} still ships the delete form"


# ---------------------------------------------------------------------------
# Assignee count in the browsing views
# ---------------------------------------------------------------------------


def _assignee_count_views(project):
    """Kanban, List and Calendar Day — the surfaces showing the people count."""
    calendar_url = reverse("tasks:board_calendar", kwargs={"project_pk": project.pk})
    return {
        "kanban": (reverse("tasks:board_kanban", kwargs={"project_pk": project.pk}), {}),
        "list": (reverse("tasks:board_list", kwargs={"project_pk": project.pk}), {}),
        "calendar day": (calendar_url, {"view": "day", "date": "2026-03-12"}),
    }


@pytest.mark.django_db
def test_browsing_views_show_the_assignee_count(client, user, other_user):
    """Each view reports how many members a task has, zero included."""
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    contrib = _add_contributor(project, other_user)
    due = datetime(2026, 3, 12, tzinfo=dt_timezone.utc)

    solo = Task.objects.create(
        project=project, created_by=lead, container=col1, container_task_order=0,
        name="Solo", deadline=due,
    )
    solo.assigned_members.add(lead)
    pair = Task.objects.create(
        project=project, created_by=lead, container=col1, container_task_order=1,
        name="Pair", deadline=due,
    )
    pair.assigned_members.add(lead, contrib)
    Task.objects.create(
        project=project, created_by=lead, container=col1, container_task_order=2,
        name="Nobody", deadline=due,
    )
    client.force_login(user)

    for name, (url, params) in _assignee_count_views(project).items():
        content = client.get(url, params).content.decode()
        assert "Nobody" in content, f"{name} view lost the unassigned task"
        # Singular and plural for the two staffed tasks, once each; the third
        # task is unassigned and says so: 0.
        assert content.count('aria-label="1 assigned member"') == 1, (
            f"{name} view missed the single assignee"
        )
        assert content.count('aria-label="2 assigned members"') == 1, (
            f"{name} view missed the pair"
        )
        assert content.count('aria-label="0 assigned members"') == 1, (
            f"{name} view hid the unassigned task's count"
        )
        # The count is a number, not a roll-call: names stay in the side pane.
        assert contrib.display_name not in content, f"{name} view named an assignee"


@pytest.mark.django_db
def test_assignee_count_does_not_add_a_query_per_task(client, user):
    """The count reads a prefetch, so query volume is flat in task count."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    client.force_login(user)

    def queries_for(task_total):
        Task.objects.filter(project=project).delete()
        for i in range(task_total):
            task = Task.objects.create(
                project=project, created_by=lead, container=col1,
                container_task_order=i, name=f"T{i}",
                deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
            )
            task.assigned_members.add(lead)
        counts = {}
        for name, (url, params) in _assignee_count_views(project).items():
            with CaptureQueriesContext(connection) as ctx:
                client.get(url, params)
            counts[name] = len(ctx)
        return counts

    few = queries_for(2)
    many = queries_for(20)
    for name, count in many.items():
        assert count <= few[name], (
            f"{name} view went from {few[name]} to {count} queries over 18 more "
            f"tasks — the assignee count is querying per task"
        )


# ---------------------------------------------------------------------------
# Long descriptions are clamped behind a See more toggle
# ---------------------------------------------------------------------------


def _task_with_description(project, member, column, description):
    return Task.objects.create(
        project=project,
        created_by=member,
        container=column,
        container_task_order=0,
        name="Described",
        description=description,
    )


@pytest.mark.django_db
def test_pane_description_ships_a_measured_toggle(client, user):
    """The clamp and toggle are always rendered; the browser decides.

    Where the text wraps depends on the pane's width, so a character count
    cannot tell whether three lines will overflow. The server ships the
    machinery for every description and Alpine measures on init, hiding the
    button when there is nothing behind it.
    """
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    client.force_login(user)

    for label, description in (("short", "One short line."), ("long", "word " * 200)):
        task = _task_with_description(project, member, col1, description)
        pane = client.get(_pane_url(project, task)).content.decode()

        # The clamp is both a static class and a binding, and needs to be
        # both: bound alone stops re-applying after the first HTMX swap, so a
        # reopened task came back expanded; static alone could not be removed.
        assert 'class="text-sm text-brand-text line-clamp-3"' in pane, label
        assert "{ 'line-clamp-3': !expanded }" in pane, label
        # text-sm, matching the pane's other body text — not the inherited
        # 16px the inert prose classes used to leave it at.
        assert "prose" not in pane, label
        # Measured on init, and the button hidden until that says otherwise.
        assert "$refs.body.scrollHeight" in pane, label
        assert 'x-show="overflowing"' in pane, label
        assert "x-cloak" in pane, label
        # Wired to the block it collapses, both labels available to x-text.
        assert f'aria-controls="task-description-{task.pk}"' in pane, label
        assert "See more" in pane and "See less" in pane, label
        # The toggle reads as a link in the palette's blue, not the coral accent.
        assert "text-brand-navy hover:underline" in pane, label
        assert "text-brand-coral hover:underline" not in pane, label

    # Nothing is truncated server-side — the full text is always delivered.
    task = _task_with_description(project, member, col1, "word " * 200)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert pane.count("word") >= 200


@pytest.mark.django_db
def test_pane_skips_the_toggle_when_there_is_no_description(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="Bare",
    )
    client.force_login(user)

    pane = client.get(_pane_url(project, task)).content.decode()
    assert "No description." in pane
    assert "See more" not in pane
    assert 'x-show="overflowing"' not in pane


# ---------------------------------------------------------------------------
# Task dates: start must not fall after the due date
# ---------------------------------------------------------------------------


def _task_form_data(column, **overrides):
    """A minimally valid TaskForm payload, so date errors are the only ones."""
    data = {
        "name": "Dated",
        "container": str(column.pk),
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_task_form_rejects_a_start_after_the_due_date(user):
    project, col1, _col2 = _make_project_with_board(user)
    form = TaskForm(
        data=_task_form_data(
            col1, start_date="2026-03-12T09:00", deadline="2026-03-10T09:00"
        ),
        project=project,
    )
    assert not form.is_valid()
    assert "start_date" in form.errors
    assert "before or equal to the due date" in str(form.errors["start_date"])


@pytest.mark.django_db
def test_task_form_accepts_dates_in_order_or_equal(user):
    """In order and same-instant both pass; the rule is <=, not <."""
    project, col1, _col2 = _make_project_with_board(user)
    for label, start, deadline in (
        ("in order", "2026-03-10T09:00", "2026-03-12T09:00"),
        ("same instant", "2026-03-10T09:00", "2026-03-10T09:00"),
    ):
        form = TaskForm(
            data=_task_form_data(col1, start_date=start, deadline=deadline),
            project=project,
        )
        assert form.is_valid(), f"{label} should validate, got {form.errors!r}"


@pytest.mark.django_db
def test_task_form_leaves_a_lone_date_unconstrained(user):
    """Both dates are optional, and one on its own has nothing to compare to."""
    project, col1, _col2 = _make_project_with_board(user)
    for label, data in (
        ("neither", {}),
        ("start only", {"start_date": "2026-03-12T09:00"}),
        ("due only", {"deadline": "2026-03-10T09:00"}),
    ):
        form = TaskForm(data=_task_form_data(col1, **data), project=project)
        assert form.is_valid(), f"{label} should validate, got {form.errors!r}"


@pytest.mark.django_db
def test_creating_a_task_with_inverted_dates_is_refused(client, user):
    """The rule holds through the create view, and the error reaches the page."""
    project, col1, _col2 = _make_project_with_board(user)
    client.force_login(user)

    response = client.post(
        reverse("tasks:create", kwargs={"project_pk": project.pk}),
        _task_form_data(
            col1, start_date="2026-03-12T09:00", deadline="2026-03-10T09:00"
        ),
    )
    assert not Task.objects.filter(name="Dated").exists()
    assert "before or equal to the due date" in response.content.decode()


@pytest.mark.django_db
def test_editing_a_task_into_inverted_dates_is_refused(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="Dated",
        start_date=datetime(2026, 3, 10, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    client.force_login(user)

    client.post(
        reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk}),
        _task_form_data(
            col1, start_date="2026-03-12T09:00", deadline="2026-03-10T09:00"
        ),
    )
    task.refresh_from_db()
    assert task.start_date == datetime(2026, 3, 10, tzinfo=dt_timezone.utc)
    assert task.deadline == datetime(2026, 3, 12, tzinfo=dt_timezone.utc)


@pytest.mark.django_db
def test_pane_section_labels_are_neutral(client, user):
    """Description, Completion and friends read as quiet labels, not red ones.

    Coral is reserved for accents and actions (the page eyebrow, the See more
    toggle), so no section label should carry it.
    """
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="Labelled",
        description="Some text.",
    )
    client.force_login(user)

    label = "text-xs uppercase tracking-wider text-brand-text/70 font-semibold"
    coral_label = "text-xs uppercase tracking-wider text-brand-coral font-semibold"

    pane = client.get(_pane_url(project, task)).content.decode()
    assert coral_label not in pane
    # Assignees / Reviewers are no longer sections here — the Completion
    # matrix covers those people — and Created by is a meta line beside the
    # dates rather than a section of its own.
    for heading in ("Completion", "Comments"):
        assert heading in pane
    assert '<h3 class="sr-only">Description</h3>' in pane
    assert pane.count(label) >= 2, "expected the pane's section labels to be neutral"
    assert "Created by" in pane


# ---------------------------------------------------------------------------
# Side-pane completion matrix
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_pane_completion_matrix_reports_marks_and_reviews(client, user, other_user):
    """Each assignee gets a row: their own mark, then a reviewer's verdict."""
    from apps.tasks.services import mark_own_done

    from apps.tasks.services import mark_reviewed

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    task.reviewers.add(contrib)
    # The lead marked their part done and a reviewer signed that submission
    # off; the contributor has done neither.
    mark_own_done(task=task, member=lead)
    log = task.task_logs.filter(assigned_to=lead).order_by("-created_at").first()
    mark_reviewed(task_log=log, reviewer=contrib)

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()

    assert "data-done-cell" in pane
    assert "data-review-cell" in pane
    assert lead.display_name in pane
    assert contrib.display_name in pane
    # The lead is yes on both counts. The contributor has not marked their
    # part done, so their row shows that and has no review state yet.
    assert pane.count('data-mark="yes"') == 2
    assert pane.count('data-mark="no"') == 1
    assert "data-done-line" in _row_for(pane, lead) and "data-review-line" in _row_for(pane, lead)
    assert "data-done-line" not in _row_for(pane, contrib)
    assert f"Mark {contrib.display_name}'s part done" in _row_for(pane, contrib)


@pytest.mark.django_db
def test_pane_matrix_treats_an_unreviewed_mark_as_not_reviewed(client, user, other_user):
    """A done mark alone is one check, not two: review is a separate verdict."""
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    task.reviewers.add(contrib)
    mark_own_done(task=task, member=lead)

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()

    assert pane.count('data-mark="yes"') == 1  # the lead's mark, nothing more
    assert pane.count('data-mark="no"') == 1  # the contributor has not marked done
    # The lead's part awaits review, and (separately) verification.
    assert pane.count('data-review-cell data-mark="pending"') == 1
    assert pane.count('data-verify-cell data-mark="pending"') == 1
    assert "data-review-line" not in _row_for(pane, lead)


@pytest.mark.django_db
def test_pane_drops_the_assignees_and_reviewers_sections(client, user, other_user):
    """Neither list is a section of its own; the Completion card carries both.

    The assignees are the matrix rows, and the reviewers are named under it as
    an h4 inside that card rather than a second h3 section beside it.
    """
    project, task, lead, _contrib = _two_assignee_task(user, other_user)
    task.reviewers.add(lead)
    client.force_login(user)

    pane = client.get(_pane_url(project, task)).content.decode()
    assert "Assignees</h3>" not in pane
    assert "Reviewers</h3>" not in pane
    # The Completion block is still there, carrying the matrix.
    assert ">Completion</h3>" in pane
    # ... and the reviewers, inside it.
    completion = pane[pane.index(">Completion</h3>") :]
    assert "Reviewers</h4>" in completion
    assert lead.display_name in completion

    # The full task page carries the same card, and drops the same two lists
    # for the same reason.
    detail = client.get(
        reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk})
    ).content.decode()
    for heading in ("Assignees</h2>", "Assignees</h3>", "Reviewers</h2>", "Reviewers</h3>"):
        assert heading not in detail, f"the page still has a {heading} section"
    assert ">Completion</h3>" in detail
    detail_completion = detail[detail.index(">Completion</h3>") :]
    assert "Reviewers</h4>" in detail_completion
    assert lead.display_name in detail_completion


@pytest.mark.django_db
def test_pane_matrix_says_so_when_nobody_is_assigned(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="Unstaffed",
    )
    client.force_login(user)

    pane = client.get(_pane_url(project, task)).content.decode()
    assert "Nobody is assigned to this task yet." in pane
    assert "Marked Done" not in pane


@pytest.mark.django_db
def test_kanban_check_trails_the_task_name_on_the_title_row(client, user):
    """The check shares the title's line, after the name and before the meta."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Titled", status=TaskStatus.COMPLETED,
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    task.assigned_members.add(member)
    client.force_login(user)

    card = client.get(
        reverse("tasks:board_kanban", kwargs={"project_pk": project.pk})
    ).content.decode()
    # After the name: it trails the title rather than leading it.
    assert card.index("Titled") < card.index('aria-label="Complete"')
    # ...but before the dates and assignee count, so it is on the title row
    # and not tacked onto the meta row below.
    assert card.index('aria-label="Complete"') < card.index("Due Mar 12")
    assert card.index('aria-label="Complete"') < card.index("1 assigned member")


@pytest.mark.django_db
def test_kanban_pairs_the_priority_flag_with_the_check(client, user):
    """Flag and check sit together after the name, on the title's line."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Both", status=TaskStatus.COMPLETED, is_priority=True,
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    task.assigned_members.add(member)
    client.force_login(user)

    card = client.get(
        reverse("tasks:board_kanban", kwargs={"project_pk": project.pk})
    ).content.decode()
    assert (
        card.index("Both")
        < card.index('aria-label="High priority"')
        < card.index('aria-label="Complete"')
        < card.index("Due Mar 12")  # still the title row, not the meta row
    )


@pytest.mark.django_db
def test_only_the_roomy_surfaces_keep_the_priority_label(client, user):
    """The browsing views go icon-only; the pane and detail page keep the words."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Flagged", is_priority=True,
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    client.force_login(user)

    calendar_url = reverse("tasks:board_calendar", kwargs={"project_pk": project.pk})
    wordy = {
        "pane": (reverse("tasks:detail_pane", kwargs={"project_pk": project.pk, "pk": task.pk}), {}),
        "detail page": (reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk}), {}),
    }
    for name, (url, params) in wordy.items():
        content = client.get(url, params).content.decode()
        assert "<span>High priority</span>" in content, f"{name} lost the label"

    icon_only = {
        "kanban": (reverse("tasks:board_kanban", kwargs={"project_pk": project.pk}), {}),
        "list": (reverse("tasks:board_list", kwargs={"project_pk": project.pk}), {}),
        "calendar day": (calendar_url, {"view": "day", "date": "2026-03-12"}),
    }
    for name, (url, params) in icon_only.items():
        content = client.get(url, params).content.decode()
        assert "<span>High priority</span>" not in content, f"{name} still spells it out"
        assert 'aria-label="High priority"' in content, f"{name} lost the accessible name"


@pytest.mark.django_db
def test_list_and_day_rows_end_with_the_check(client, user):
    """On a row the check is last, so it pins to the right-hand edge."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Trailing", status=TaskStatus.COMPLETED, is_priority=True,
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    task.assigned_members.add(member)
    client.force_login(user)

    calendar_url = reverse("tasks:board_calendar", kwargs={"project_pk": project.pk})
    rows = {
        "list": (reverse("tasks:board_list", kwargs={"project_pk": project.pk}), {}),
        "calendar day": (calendar_url, {"view": "day", "date": "2026-03-12"}),
    }
    for name, (url, params) in rows.items():
        content = client.get(url, params).content.decode()
        # Name, then the metadata, then the flag paired with the check at the end.
        assert (
            content.index("Trailing")
            < content.index("Due Mar 12")
            < content.index("1 assigned member")
            < content.index('aria-label="High priority"')
            < content.index('aria-label="Complete"')
        ), f"{name}: unexpected row order"


@pytest.mark.django_db
def test_closing_and_reopening_leaves_assignee_marks_alone(client, user, other_user):
    """The Lead's toggle moves Task.status and nothing else.

    "Marked done" belongs to the assignee who set it, so neither direction of
    the Lead's Mark Completed / Mark Not Complete toggle may write or erase
    one — including the round trip, which used to wipe every mark on reopen.
    """
    from apps.tasks.services import mark_own_done

    project, task, _lead, contrib = _two_assignee_task(user, other_user)
    mark_own_done(task=task, member=contrib)
    assert task.assignees_done_count == 1
    log_count = task.task_logs.count()

    client.force_login(user)

    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED
    assert task.assignees_done_count == 1, "closing changed an assignee's mark"

    client.post(reverse("tasks:reopen", kwargs={"pk": task.pk}))
    task.refresh_from_db()
    assert task.status == TaskStatus.NOT_COMPLETED
    assert task.assignees_done_count == 1, "reopening erased an assignee's mark"

    # And neither direction appended log rows on the assignees' behalf.
    assert task.task_logs.count() == log_count


@pytest.mark.django_db
def test_pane_matrix_survives_the_lead_closing_the_task(client, user, other_user):
    """The matrix still shows who actually marked done after a close."""
    from apps.tasks.services import mark_own_done

    project, task, _lead, contrib = _two_assignee_task(user, other_user)
    mark_own_done(task=task, member=contrib)
    client.force_login(user)
    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))

    pane = client.get(_pane_url(project, task)).content.decode()
    # One assignee marked done, one did not — not two, as a bulk close
    # would have produced.
    assert pane.count('data-mark="yes"') == 1
    progress = pane[pane.index("data-progress") : pane.index("data-assignee-rows")]
    assert "1 of 2" in progress


@pytest.mark.django_db
def test_pane_top_bar_carries_mark_completed_and_the_own_row_the_self_mark(client, user, other_user):
    """A Lead who is also an assignee closes the task from the bar and marks
    their own part from the tick box in their own Completion row."""
    project, task, lead, _contrib = _two_assignee_task(user, other_user)
    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()

    assert 'id="task-completion-button"' in pane
    assert 'id="task-self-mark-button"' not in pane
    # Mark Completed sits in the bar, before the task name, with its icon.
    assert pane.index("Mark Completed") < pane.index("Moded")
    bar = pane[: pane.index("Moded")]
    assert bar.count("<svg") >= 5  # completion + trash + pencil + expand + close
    # The Lead ticks their own part from their row, which says it is theirs.
    row = _row_for(pane, lead)
    assert "(you)" in row
    assert "data-done-cell" in row and "completion-tick-done" in row


@pytest.mark.django_db
def test_an_assignee_ticks_and_unticks_their_own_part(client, user, other_user):
    """An assignee who is not a Lead ticks their own Done box to mark their
    part and unticks it to undo; the swap brings the box back in its new state.
    Other people's boxes stay read-only to them, and closing the task leaves
    their own box live."""
    project, task, lead, contrib = _two_assignee_task(user, other_user)
    client.force_login(other_user)
    complete_url = reverse("tasks:complete", kwargs={"pk": task.pk})
    undo_url = reverse("tasks:undo", kwargs={"pk": task.pk})

    pane = client.get(_pane_url(project, task)).content.decode()
    own = _row_for(pane, contrib)
    assert "(you)" in own and "Mark your part done" in own and complete_url in own
    other = _row_for(pane, lead)
    assert "hx-post" not in other and "disabled" in other

    marked = client.post(complete_url, {"pane_mode": "1"}, headers={"hx-request": "true"}).content.decode()
    own = _row_for(marked, contrib)
    assert "Mark your part not done" in own and undo_url in own and " checked" in own
    assert "data-done-line" in own

    unmarked = client.post(undo_url, {"pane_mode": "1"}, headers={"hx-request": "true"}).content.decode()
    own = _row_for(unmarked, contrib)
    assert "Mark your part done" in own and "data-done-line" not in own

    client.post(complete_url, {"pane_mode": "1"}, headers={"hx-request": "true"})
    client.force_login(user)
    client.post(reverse("tasks:close", kwargs={"pk": task.pk}), {"pane_mode": "1"}, headers={"hx-request": "true"})
    client.force_login(other_user)
    pane = client.get(_pane_url(project, task)).content.decode()
    own = _row_for(pane, contrib)
    assert "Mark your part not done" in own and undo_url in own
    assert " checked" in own and "data-locked" not in own


@pytest.mark.django_db
def test_a_full_task_page_is_laid_out_like_a_wiki_page(client, user):
    """The project's header and tabs stay, with Tasks lit; the way back to
    the board sits first in the content; then the task's own heading."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Leaf",
    )
    client.force_login(user)

    detail = client.get(
        reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk})
    ).content.decode()
    # The project's header and its tab strip, Tasks as the current tab.
    assert f">{project.name}</h1>" in detail
    assert 'aria-label="Project sections"' in detail
    strip = detail[detail.index('aria-label="Project sections"'):]
    strip = strip[: strip.index("</nav>")]
    board_url = reverse("tasks:board", kwargs={"project_pk": project.pk})
    assert f'href="{board_url}"\n       class="project-tab active"' in strip
    # Back to the board first, then the task's heading.
    back = f'href="{board_url}"\n       class="btn btn-sm btn-ghost text-brand-navy">&larr; All tasks</a>'
    assert back in detail
    assert detail.index(back) < detail.index('id="task-summary"') < detail.index(">Leaf</h2>")


@pytest.mark.django_db
def test_full_task_page_orders_its_sections_like_the_pane(client, user, other_user):
    """The pane's top bar as an action row, then the pane's three cards.

    The task's card carries the same things as the pane's (name, tags, the
    property list, the description), then Completion, then Comments.
    Milestone and GitHub are not shown, as in the pane.
    """
    from datetime import datetime, timezone as dt_tz

    from apps.tasks.models import Milestone

    project, task, _lead, _contrib = _two_assignee_task(user, other_user)
    task.description = "Some description."
    task.github_url = "https://github.com/x/y/pull/1"
    task.milestone = Milestone.objects.create(project=project, name="Milestone one")
    task.start_date = datetime(2026, 3, 10, tzinfo=dt_tz.utc)
    task.deadline = datetime(2026, 3, 12, tzinfo=dt_tz.utc)
    task.is_priority = True
    task.save()
    client.force_login(user)

    detail = client.get(
        reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk})
    ).content.decode()

    start = detail.index('id="task-summary"')
    card = detail[start : detail.index("</section>", start)]
    for piece in ("Moded", "High priority", "Not Complete", "Mar 10, 2026 12:00 AM – Mar 12, 2026 12:00 AM UTC",
                  "Created by", "Some description."):
        assert piece in card, f"the task's card is missing {piece}"

    assert (
        detail.index('id="task-completion-button"')   # the action row
        < start                                       # the task's card
        < detail.index(">Completion<")
        < detail.index("Comments")
    )
    assert "Milestone one" not in detail
    assert "github.com/x/y" not in detail
    # Same card as the pane, shared rather than copied.
    pane = client.get(_pane_url(project, task)).content.decode()
    assert 'id="task-summary"' in pane


def _task_surfaces(project, task):
    """The two places a task is read in full, by name."""
    return {
        "detail page": reverse(
            "tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk}
        ),
        "pane": _pane_url(project, task),
    }


@pytest.mark.django_db
def test_the_comment_box_sits_above_the_thread_and_posts_to_the_top(client, user):
    """Write at the top, read down from the newest."""
    from apps.comments.models import Comment

    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Threaded",
    )
    Comment.objects.create(task=task, author=member, content="only one")
    client.force_login(user)

    for name, url in _task_surfaces(project, task).items():
        html = client.get(url).content.decode()
        # Whichever container this surface names its thread.
        thread_id = next(
            marker
            for marker in ('id="task-comments"', f'id="task-pane-comments-{task.pk}"')
            if marker in html
        )
        posts_to = f'hx-post="{reverse("comments:create", kwargs={"task_pk": task.pk})}"'
        assert html.index(posts_to) < html.index(
            thread_id
        ), f"{name} still has the box under the thread"
        assert 'hx-swap="afterbegin"' in html, f"{name} still appends to the end"


@pytest.mark.django_db
def test_a_long_thread_hides_its_older_comments_behind_see_more(client, user):
    """Three newest in the open, the rest behind the toggle."""
    from apps.comments.models import Comment

    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Chatty",
    )
    for i in range(5):
        Comment.objects.create(task=task, author=member, content=f"comment {i}")
    client.force_login(user)

    for name, url in _task_surfaces(project, task).items():
        html = client.get(url).content.decode()
        rest = html[html.index("comments-rest") :]
        rest = rest[: rest.index("</div>")]
        assert 'x-show="expanded"' in rest, f"{name} shows the older comments outright"
        assert "See more" in html and "See less" in html, f"{name} lost the toggle"
        # The two newest are in the open; the oldest is not.
        head = html[: html.index("comments-rest")]
        assert "comment 4" in head and "comment 3" in head and "comment 2" in head
        assert "comment 0" not in head, f"{name} shows a fourth comment up front"


@pytest.mark.django_db
def test_a_short_thread_gets_no_see_more(client, user):
    """Nothing hidden, so nothing to offer."""
    from apps.comments.models import Comment

    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Terse",
    )
    for i in range(3):
        Comment.objects.create(task=task, author=member, content=f"comment {i}")
    client.force_login(user)

    for name, url in _task_surfaces(project, task).items():
        html = client.get(url).content.decode()
        assert "comments-rest" not in html, f"{name} built an empty hidden list"


@pytest.mark.django_db
def test_the_matrix_hides_its_later_rows_and_keeps_them_open_across_a_swap(
    client, user, other_user
):
    """Rows past the third wait behind See more — and stay open when acted on.

    The state has to live in the surrounding template, not in the block: the
    block is the swap target, so state kept inside it would snap back to
    collapsed on every mark and review and take the row just acted on with it.
    """
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=lead, container=col1,
        container_task_order=0, name="Crowded",
    )
    members = [lead] + [
        ProjectMember.objects.create(project=project, name=f"M{i}") for i in range(4)
    ]
    task.assigned_members.set(members)
    for m in members:
        TaskLog.objects.create(task=task, assigned_to=m)
    client.force_login(user)

    # Rows past the third are hidden by position (CSS nth-child), so the
    # hidden ones follow a header sort; the table opens them all when
    # rowsExpanded is on.
    hide_rule = ".completion-table:not(.rows-expanded) tbody tr:nth-child(n+4) { display: none; }"
    for name, url in _task_surfaces(project, task).items():
        html = client.get(url).content.decode()
        assert html.count("data-assignee-row=") == 5, f"{name} dropped a row"
        assert hide_rule in html, f"{name} hid no rows"
        assert ":class=\"{ 'rows-expanded': rowsExpanded }\"" in html
        assert (
            "x-data=\"{ rowsExpanded: false, sortKey: '', sortDesc: false }\"" in html
        ), f"{name} has nowhere to keep it"

    # The swap response carries the rows but not the state — that stayed put.
    swapped = client.post(
        reverse("tasks:complete", kwargs={"pk": task.pk}),
        {"pane_mode": "1"},
        headers={"hx-request": "true"},
    ).content.decode()
    assert "'rows-expanded': rowsExpanded" in swapped
    assert "rowsExpanded: false" not in swapped, "the swap would reset the toggle"
    assert "sortKey: ''" not in swapped, "the swap would reset the sort"


@pytest.mark.django_db
def test_the_matrix_headers_sort_its_rows(client, user, other_user):
    """Each header is a sort button, and each row carries what it sorts by.

    The sort itself runs in the browser; what the server owes it is the keys:
    the name, and for Done and Reviewed a time for the parts that have one,
    with the rest after every time — awaiting review before not done.
    """
    project, task, lead, contrib = _two_assignee_task(user, other_user)
    task.reviewers.set([lead])
    when = timezone.now()
    TaskLog.objects.filter(task=task, assigned_to=lead).update(
        status=TaskStatus.COMPLETED, completed_at=when
    )
    client.force_login(user)

    for name, url in _task_surfaces(project, task).items():
        html = client.get(url).content.decode()
        for key in ("name", "done", "reviewed"):
            assert f"@click=\"sortBy('{key}')\"" in html, f"{name} cannot sort by {key}"
        lead_row = html[html.index(f'data-assignee-row="{lead.pk}"') :]
        lead_row = lead_row[: lead_row.index(">")]
        assert f'data-sort-name="{lead.display_name.lower()}"' in lead_row
        assert f'data-sort-done="{int(when.timestamp())}"' in lead_row
        assert 'data-sort-reviewed="100000000000"' in lead_row, f"{name}: awaiting review"
        contrib_row = html[html.index(f'data-assignee-row="{contrib.pk}"') :]
        contrib_row = contrib_row[: contrib_row.index(">")]
        assert 'data-sort-done="100000000000"' in contrib_row
        assert 'data-sort-reviewed="200000000000"' in contrib_row


@pytest.mark.django_db
def test_a_short_matrix_gets_no_see_more(client, user, other_user):
    """Two assignees, nothing hidden, no toggle."""
    project, task, _lead, _contrib = _two_assignee_task(user, other_user)
    client.force_login(user)

    for name, url in _task_surfaces(project, task).items():
        html = client.get(url).content.decode()
        block = html[html.index('id="task-status-block"') :]
        block = block[: block.index("</div>")]
        assert "See more" not in block, f"{name} offers a toggle with nothing behind it"


@pytest.mark.django_db
def test_task_surfaces_carry_one_copy_of_each_action_bar_control(client, user, other_user):
    """The page took the pane's action bar: the same two, once each.

    Once each is the point. The completion block re-renders both out of
    band alongside its own swap, because they sit outside it — so a *first*
    render that emitted them too would leave a second copy of each sitting in
    the markup, discarded unseen in the pane but plainly visible on a page the
    browser loads itself.
    """
    project, task, _lead, _contrib = _two_assignee_task(user, other_user)
    client.force_login(user)

    surfaces = {
        "detail page": reverse(
            "tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk}
        ),
        "pane": _pane_url(project, task),
    }
    controls = (
        'id="task-completion-chip"',
        'id="task-completion-button"',
    )
    for name, url in surfaces.items():
        html = client.get(url).content.decode()
        assert "Mark your part done" in html, f"{name} lost the assignee's own mark"
        assert "Mark Completed" in html, f"{name} lost the Lead's toggle"
        for control in controls:
            assert html.count(control) == 1, f"{name} has {html.count(control)}x {control}"
        assert "hx-swap-oob" not in html, f"{name} ships an out-of-band copy on first render"


@pytest.mark.django_db
def test_pane_groups_its_content_into_cards(client, user, other_user):
    """Three light cards, in order: the task, its Completion, its Comments.

    Milestone and GitHub are left out of the pane (the full page keeps them).
    """
    from apps.tasks.models import Milestone

    project, task, _lead, _contrib = _two_assignee_task(user, other_user)
    task.description = "Some description."
    task.github_url = "https://github.com/x/y/pull/1"
    task.milestone = Milestone.objects.create(project=project, name="Milestone one")
    task.save()
    client.force_login(user)

    pane = client.get(_pane_url(project, task)).content.decode()
    assert pane.count("brand-card p-4") == 3
    assert (
        pane.index("Moded")               # the task name, in the first card
        < pane.index("Some description.")  # its description, in the same card
        < pane.index(">Completion<")
        < pane.index("Comments")
    )
    assert "Milestone one" not in pane
    assert "github.com/x/y" not in pane


@pytest.mark.django_db
def test_completion_card_survives_its_own_swap(client, user, other_user):
    """The block is the swap target, so it has to carry the card itself."""
    project, task, _lead, _contrib = _two_assignee_task(user, other_user)
    client.force_login(user)

    swapped = client.post(
        reverse("tasks:complete", kwargs={"pk": task.pk}),
        {"pane_mode": "1"},
        headers={"hx-request": "true"},
    ).content.decode()
    opening_tag = swapped[: swapped.index(">", swapped.index('id="task-status-block"'))]
    assert "brand-card" in opening_tag, "the swap would drop the card"


@pytest.mark.django_db
def test_pane_column_is_a_plain_kanban_column_row(client, user):
    """The column is no tag at all in the pane or on the task page: it is a
    "Kanban column" row of the property list, its name in plain text with no
    colour or dot. The two tags (flag, completion) stay chrome-free; a
    project's own badges elsewhere keep the pill."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="Styled",
        is_priority=True,
    )
    client.force_login(user)

    pane = client.get(_pane_url(project, task)).content.decode()
    row = f'Kanban column\n    </dt>\n    <dd class="text-brand-text">{col1.name}</dd>'
    assert "px-2.5 py-0.5 rounded-full" not in pane, "the pane's status is still a pill"
    assert row in pane
    assert 'viewBox="0 0 8 8"' not in pane, "the column still has its status dot"
    assert "High priority" in pane
    assert "Not Complete" in pane

    # The full task page reads the same as the pane, tag for tag.
    detail = client.get(
        reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk})
    ).content.decode()
    # Only the task's own heading: the project header above it badges the
    # project, with pills, as every project page does.
    start = detail.index('id="task-summary"')
    summary = detail[start : detail.index("</section>", start)]
    assert "px-2.5 py-0.5 rounded-full" not in summary, "the page's status is still a pill"
    assert row in summary
    assert 'viewBox="0 0 8 8"' not in summary, "the column still has its status dot"
    assert "High priority" in summary
    assert "Not Complete" in summary

    # The pill lives on where a *project* is badged, which is what it was for.
    project_page = client.get(
        reverse("projects:detail", kwargs={"pk": project.pk})
    ).content.decode()
    assert "px-2.5 py-0.5 rounded-full" in project_page


@pytest.mark.django_db
def test_pane_body_text_is_one_size(client, user, other_user):
    """Tags, dates, author and description all run at text-sm in the pane.

    Section labels and buttons stay at text-xs — they are labels and actions,
    not body copy.
    """
    from datetime import datetime, timezone as dt_tz

    project, task, _lead, _contrib = _two_assignee_task(user, other_user)
    task.description = "Some description."
    task.is_priority = True
    task.start_date = datetime(2026, 3, 10, tzinfo=dt_tz.utc)
    task.deadline = datetime(2026, 3, 12, tzinfo=dt_tz.utc)
    task.save()
    client.force_login(user)

    pane = client.get(_pane_url(project, task)).content.decode()
    # The property list (dates, task type, created by).
    assert "gap-x-3 gap-y-2 text-sm" in pane
    # The two tags: flag, completion chip.
    assert 'gap-1 text-sm font-medium shrink-0 text-error' in pane
    assert "inline-flex items-center gap-1.5 text-sm font-medium" in pane
    # Nothing in the pill row is left at text-xs.
    pill_row = pane[pane.index("flex flex-wrap items-center gap-2") : pane.index("<dl")]
    assert "text-xs" not in pill_row


@pytest.mark.django_db
def test_priority_flag_keeps_its_smaller_size_off_the_task_surfaces(client, user):
    """text_size is opt-in: My tasks sits at text-xs, the task surfaces at text-sm."""
    from datetime import datetime, timezone as dt_tz

    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Flagged", is_priority=True,
        deadline=datetime(2026, 3, 12, tzinfo=dt_tz.utc),
    )
    task.assigned_members.add(member)
    client.force_login(user)

    rows = client.get(reverse("tasks:my_list")).content.decode()
    i = rows.index('title="High priority"')
    flag = rows[rows.rfind("<span", 0, i) : i]
    assert "text-xs" in flag and "text-sm" not in flag


@pytest.mark.django_db
def test_matrix_shows_when_an_assignee_marked_done(client, user, other_user):
    """The row's Done cell carries the date the assignee set it."""
    from apps.tasks.services import mark_own_done

    project, task, _lead, contrib = _two_assignee_task(user, other_user)
    mark_own_done(task=task, member=contrib)
    log = task.task_logs.filter(assigned_to=contrib).order_by("-created_at").first()
    assert log.completed_at is not None

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    stamped = log.completed_at.strftime("%b %-d")
    # In the Done cell, in plain text beside the ticked box: the date, then who.
    row = _row_for(pane, contrib)
    cell = row[row.index("data-done-cell") :]
    line = cell[cell.index("data-done-line") :]
    line = line[: line.index("</span>")]
    assert line.index(stamped) < line.index("by ")
    assert "text-success" not in cell
    assert 'data-mark="yes"' in row


@pytest.mark.django_db
def test_undoing_a_mark_clears_its_date(client, user, other_user):
    """An undone mark must not keep the date it briefly had."""
    from apps.tasks.services import mark_own_done, reopen_own

    project, task, _lead, contrib = _two_assignee_task(user, other_user)
    mark_own_done(task=task, member=contrib)
    reopen_own(task=task, member=contrib)

    log = task.task_logs.filter(assigned_to=contrib).order_by("-created_at").first()
    assert log.status == TaskStatus.NOT_COMPLETED
    assert log.completed_at is None


@pytest.mark.django_db
def test_a_mark_made_before_the_date_existed_still_shows_its_check(client, user, other_user):
    """completed_at is null on old entries; the cell shows the check alone."""
    project, task, _lead, contrib = _two_assignee_task(user, other_user)
    log = task.task_logs.filter(assigned_to=contrib).order_by("-created_at").first()
    log.status = TaskStatus.COMPLETED
    log.completed_at = None
    log.save(update_fields=["status", "completed_at"])

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert pane.count('data-mark="yes"') == 1
    # No stray date, and no placeholder where one would have gone.
    assert "None" not in pane


# ---------------------------------------------------------------------------
# Peer review: a task's reviewers sign off on each assignee's submission
# ---------------------------------------------------------------------------


def _reviewed_task(user, other_user, *, reviewer_is_lead=False):
    """A two-assignee task where one assignee has marked their part done.

    Returns ``(project, task, lead, contrib)``. ``contrib`` is the assignee who
    marked done; the reviewer is the lead (already a Lead by privilege) unless
    ``reviewer_is_lead`` is False, in which case nobody is a named reviewer yet
    so the caller can add one.
    """
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    mark_own_done(task=task, member=contrib)
    if reviewer_is_lead:
        task.reviewers.add(lead)
    return project, task, lead, contrib


def _current_log(task, member):
    return task.task_logs.filter(assigned_to=member).order_by("-created_at").first()


def _review_url(task, log):
    return reverse("tasks:review_log", kwargs={"pk": task.pk, "log_pk": log.pk})


# The Completion table's live tick boxes, by their accessible names. Done and
# Reviewed boxes for someone else's part read "Mark <name>'s part …"; on the
# viewer's own row they read "Mark your part …". The review pair matches both.
MARK_DONE = "'s part done\""
MARK_NOT_DONE = "'s part not done\""
MARK_REVIEWED = ' part reviewed"'
MARK_NOT_REVIEWED = ' part not reviewed"'
# The Verified box, live for Leads only: "Verify <name>'s contribution" and
# "Withdraw the verification of <name>'s contribution".
MARK_VERIFIED = 'aria-label="Verify '
MARK_NOT_VERIFIED = 'aria-label="Withdraw the verification of '


def _verify_url(task, log):
    return reverse("tasks:verify_log", kwargs={"pk": task.pk, "log_pk": log.pk})


def _verify_batch_url(task):
    return reverse("tasks:verify_batch", kwargs={"pk": task.pk})


def _rows(pane):
    """The Completion card's table of assignee rows in the rendered pane."""
    start = pane.index("data-assignee-rows")
    return pane[start : pane.index("</table>", start)]


def _row_for(pane, member):
    """The Completion row of ``member`` in the rendered pane.

    Scoped to the rows list so a name that also appears elsewhere in the pane
    (Created by, for one) cannot be mistaken for the row.
    """
    body = _rows(pane)
    # The row's own name label, not a "by <name>" note in someone else's row.
    start = body.index(f"data-assignee-name>{member.display_name}<")
    return body[body.rindex("<tr", 0, start) : body.index("</tr>", start)]


@pytest.mark.django_db
def test_reviewer_marks_a_submission_reviewed(client, user, other_user):
    """A reviewer signs off a done mark; the pane then names them and the date."""
    project, task, lead, contrib = _reviewed_task(user, other_user, reviewer_is_lead=True)
    log = _current_log(task, contrib)

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_REVIEWED in pane

    response = client.post(_review_url(task, log), {"reviewed": "1", "pane_mode": "1"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200

    log.refresh_from_db()
    assert log.reviewed_by == lead
    assert log.reviewed_at is not None
    assert log.is_reviewed

    pane = client.get(_pane_url(project, task)).content.decode()
    row = _row_for(pane, contrib)
    assert f"by {lead.display_name}" in row
    assert log.reviewed_at.strftime("%b %-d") in row
    # Two checks now: the assignee's own mark and the review of it.
    assert row.count('data-mark="yes"') == 2
    # The Reviewed bar counts it.
    progress = pane[pane.index("data-progress") : pane.index("data-assignee-rows")]
    reviewed = progress[progress.index("Reviewed") :]
    assert f"1 of {task.assigned_members.count()}" in reviewed


@pytest.mark.django_db
def test_review_waits_on_the_assignees_own_mark(client, user, other_user):
    """Nothing to review until the assignee says they are done."""
    project, task, lead, contrib = _two_assignee_task(user, other_user)
    task.reviewers.add(lead)
    log = _current_log(task, contrib)

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_REVIEWED not in pane

    response = client.post(_review_url(task, log), {"reviewed": "1"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 400
    log.refresh_from_db()
    assert not log.is_reviewed


@pytest.mark.django_db
def test_only_a_reviewer_or_lead_can_review(client, user, other_user):
    """A plain assignee gets neither the buttons nor the endpoint."""
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _reviewed_task(
        user, other_user, reviewer_is_lead=True
    )
    mark_own_done(task=task, member=lead)
    ProjectPrivilege.objects.filter(project=project, user=other_user).update(
        privilege=MemberPrivileges.CONTRIBUTOR
    )
    lead_log = _current_log(task, lead)

    # ``contrib`` is an assignee but not a reviewer, and not a Lead.
    client.force_login(other_user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_REVIEWED not in pane
    assert "awaiting review" in pane  # they can see it is waiting on someone

    response = client.post(
        _review_url(task, lead_log), {"reviewed": "1"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 403
    lead_log.refresh_from_db()
    assert not lead_log.is_reviewed

    # Named a reviewer on the task, the same member can act.
    task.reviewers.add(contrib)
    ok = client.post(
        _review_url(task, lead_log),
        {"reviewed": "1", "pane_mode": "1"},
        HTTP_HX_REQUEST="true",
    )
    assert ok.status_code == 200
    lead_log.refresh_from_db()
    assert lead_log.reviewed_by == contrib


@pytest.mark.django_db
def test_a_reviewer_may_review_their_own_row(client, user, other_user):
    """A reviewer's two actions carry no exception for their own submission.

    Only the task being completed stops them, so a reviewer who is also an
    assignee gets the button on their own row like any other.
    """
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _reviewed_task(user, other_user, reviewer_is_lead=True)
    mark_own_done(task=task, member=lead)
    own_log = _current_log(task, lead)

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_REVIEWED in _row_for(pane, lead)
    assert MARK_REVIEWED in _row_for(pane, contrib)

    response = client.post(
        _review_url(task, own_log),
        {"reviewed": "1", "pane_mode": "1"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    own_log.refresh_from_db()
    assert own_log.reviewed_by == lead


@pytest.mark.django_db
def test_reviewer_can_mark_a_submission_not_reviewed(client, user, other_user):
    """Mark not reviewed clears the verdict, leaving the assignee's mark alone."""
    from apps.tasks.services import mark_reviewed

    project, task, lead, contrib = _reviewed_task(user, other_user, reviewer_is_lead=True)
    log = _current_log(task, contrib)
    mark_reviewed(task_log=log, reviewer=lead)

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_NOT_REVIEWED in pane

    response = client.post(_review_url(task, log), {"reviewed": "0", "pane_mode": "1"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200

    log.refresh_from_db()
    assert not log.is_reviewed
    assert log.reviewed_by is None
    assert log.reviewed_at is None
    assert log.status == TaskStatus.COMPLETED  # the done mark survives


@pytest.mark.django_db
def test_reverting_a_done_mark_drops_the_review(client, user, other_user):
    """Withdrawing the submission withdraws the verdict on it."""
    from apps.tasks.services import mark_reviewed

    project, task, lead, contrib = _reviewed_task(user, other_user, reviewer_is_lead=True)
    log = _current_log(task, contrib)
    mark_reviewed(task_log=log, reviewer=lead)

    # The assignee undoes their own mark.
    client.force_login(other_user)
    response = client.post(
        reverse("tasks:undo", kwargs={"pk": task.pk}), {"pane_mode": "1"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200

    log.refresh_from_db()
    assert log.status == TaskStatus.NOT_COMPLETED
    assert log.reviewed_by is None and log.reviewed_at is None

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    row = _row_for(pane, contrib)
    assert lead.display_name not in row
    assert row.count('aria-label="Yes"') == 0
    assert "reviewed" not in pane.split(">Assignee<")[0].split("done")[-1]

    # Marking done again comes back unreviewed, waiting on a fresh look.
    client.force_login(other_user)
    client.post(reverse("tasks:complete", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")
    log.refresh_from_db()
    assert log.status == TaskStatus.COMPLETED
    assert not log.is_reviewed


@pytest.mark.django_db
def test_reviewer_changes_move_the_buttons(client, user, other_user):
    """Review rights are read from the task's live reviewer set every render."""
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _reviewed_task(user, other_user)
    mark_own_done(task=task, member=lead)
    ProjectPrivilege.objects.filter(project=project, user=other_user).update(
        privilege=MemberPrivileges.CONTRIBUTOR
    )

    client.force_login(other_user)
    assert MARK_REVIEWED not in client.get(_pane_url(project, task)).content.decode()

    task.reviewers.add(contrib)
    assert MARK_REVIEWED in client.get(_pane_url(project, task)).content.decode()

    task.reviewers.remove(contrib)
    assert MARK_REVIEWED not in client.get(_pane_url(project, task)).content.decode()


@pytest.mark.django_db
def test_a_removed_reviewers_signoff_still_names_them(client, user, other_user):
    """Taking someone off the reviewer list does not rewrite their verdict."""
    from apps.tasks.services import mark_reviewed

    project, task, lead, contrib = _reviewed_task(user, other_user, reviewer_is_lead=True)
    log = _current_log(task, contrib)
    mark_reviewed(task_log=log, reviewer=lead)
    task.reviewers.remove(lead)

    client.force_login(user)
    row = _row_for(client.get(_pane_url(project, task)).content.decode(), contrib)
    assert f"by {lead.display_name}" in row


@pytest.mark.django_db
def test_dropping_an_assignee_drops_their_row_and_blocks_review(client, user, other_user):
    """An unassigned member has no row to review, and cannot be reviewed."""
    project, task, lead, contrib = _reviewed_task(user, other_user, reviewer_is_lead=True)
    log = _current_log(task, contrib)
    task.assigned_members.remove(contrib)

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    # Scoped to the Completion rows: they are still in the project, so the
    # comment box's @ list names them, as it does anyone who can be tagged.
    rows = _rows(pane)
    assert contrib.display_name not in rows
    assert lead.display_name in rows  # the remaining assignee is untouched

    response = client.post(_review_url(task, log), {"reviewed": "1"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 400
    log.refresh_from_db()
    assert not log.is_reviewed


@pytest.mark.django_db
def test_a_new_assignee_starts_unreviewed(client, user, other_user, pm_user):
    """Adding someone to the task adds an unreviewed row, nothing more."""
    project, task, lead, contrib = _reviewed_task(user, other_user, reviewer_is_lead=True)
    from apps.tasks.services import mark_reviewed

    mark_reviewed(task_log=_current_log(task, contrib), reviewer=lead)

    late = ProjectMember.objects.create(project=project)
    late.users.add(pm_user)
    task.assigned_members.add(late)

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    row = _row_for(pane, late)
    assert row.count('aria-label="Yes"') == 0
    assert MARK_REVIEWED not in row  # they have not marked their part done
    assert task.assignees_reviewed_count == 1


@pytest.mark.django_db
def test_review_notifies_the_assignee(client, user, other_user):
    """The assignee hears that their submission was reviewed."""
    from apps.notifications.models import Notification
    from apps.tasks.services import mark_reviewed

    project, task, lead, contrib = _reviewed_task(user, other_user, reviewer_is_lead=True)
    mark_reviewed(task_log=_current_log(task, contrib), reviewer=lead)

    note = Notification.objects.filter(recipients=other_user).order_by("-id").first()
    assert note is not None
    assert task.name in note.message
    assert lead.display_name in note.message
    # The reviewer does not notify themselves.
    assert not Notification.objects.filter(
        recipients=user, message__contains="Submission reviewed"
    ).exists()


@pytest.mark.django_db
def test_review_events_are_audited(client, user, other_user):
    """Both the sign-off and its withdrawal land in the audit log."""
    from apps.tasks.services import clear_review, mark_reviewed

    project, task, lead, contrib = _reviewed_task(user, other_user, reviewer_is_lead=True)
    log = _current_log(task, contrib)
    mark_reviewed(task_log=log, reviewer=lead)
    assert AuditLog.objects.filter(action=AuditAction.TASK_REVIEWED).exists()

    clear_review(task_log=log, cleared_by=lead)
    assert AuditLog.objects.filter(action=AuditAction.TASK_REVIEW_CLEARED).exists()


@pytest.mark.django_db
def test_review_writes_no_contribution_record(client, user, other_user):
    """Peer review is reversible, so it stays out of the immutable ledger."""
    from apps.contributions.models import ContributionRecord
    from apps.tasks.services import mark_reviewed

    project, task, lead, contrib = _reviewed_task(user, other_user, reviewer_is_lead=True)
    mark_reviewed(task_log=_current_log(task, contrib), reviewer=lead)
    assert not ContributionRecord.objects.filter(task=task).exists()


# ---------------------------------------------------------------------------
# Reviewers: named in the pane, counted on the browsing views
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_pane_names_the_reviewers(client, user, other_user, pm_user):
    """The Completion card lists who the Reviewed column is waiting on."""
    project, task, lead, contrib = _two_assignee_task(user, other_user)
    outsider = _add_contributor(project, pm_user)
    task.reviewers.add(contrib, outsider)

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    reviewers = pane[pane.index("Reviewers</h4>") : pane.index("</div>", pane.index("Reviewers</h4>"))]
    assert contrib.display_name in reviewers
    # A reviewer who is not assigned still gets named — the matrix has no row
    # for them, so this line is the only place they appear.
    assert outsider.display_name in reviewers
    assert outsider.display_name not in pane[: pane.index("Reviewers</h4>")]


@pytest.mark.django_db
def test_a_task_with_no_reviewers_has_no_review_column_at_all(client, user, other_user):
    """Nobody to review means nothing to review: the column and the list go."""
    project, task, _lead, _contrib = _two_assignee_task(user, other_user)
    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert "Reviewers</h4>" not in pane
    assert "data-review-cell" not in pane
    assert "awaiting review" not in pane
    # The assignees and their own marks are all still there.
    assert "data-done-cell" in pane


@pytest.mark.django_db
def test_pane_reviewer_list_follows_the_task(client, user, other_user):
    """Adding and removing a reviewer is reflected on the next render."""
    project, task, _lead, contrib = _two_assignee_task(user, other_user)
    client.force_login(user)

    task.reviewers.add(contrib)
    assert contrib.display_name in client.get(_pane_url(project, task)).content.decode()

    task.reviewers.remove(contrib)
    pane = client.get(_pane_url(project, task)).content.decode()
    # The last reviewer off takes the Reviewed column and its list with them.
    assert "Reviewers</h4>" not in pane
    assert ">Reviewed<" not in pane


@pytest.mark.django_db
def test_task_cards_say_no_date_when_a_task_has_none(client, user):
    """A card or row always carries its dates: "No date" when it has none."""
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    Task.objects.create(
        project=project, created_by=lead, container=col1, container_task_order=0,
        name="Undated",
    )
    client.force_login(user)
    kwargs = {"project_pk": project.pk}
    for url in (
        reverse("tasks:board_kanban", kwargs=kwargs),
        reverse("tasks:board_list", kwargs=kwargs),
    ):
        content = client.get(url).content.decode()
        assert 'data-tooltip="No start or due date"' in content, url
        assert "No date" in content, url
        assert 'aria-label="0 assigned members"' in content, url
        assert 'aria-label="0 reviewers"' in content, url


@pytest.mark.django_db
def test_browsing_views_show_the_reviewer_count(client, user, other_user, pm_user):
    """Kanban, List and Calendar Day each count reviewers, zero included."""
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    contrib = _add_contributor(project, other_user)
    third = _add_contributor(project, pm_user)
    due = datetime(2026, 3, 12, tzinfo=dt_timezone.utc)

    one = Task.objects.create(
        project=project, created_by=lead, container=col1, container_task_order=0,
        name="OneReviewer", deadline=due,
    )
    one.reviewers.add(lead)
    two = Task.objects.create(
        project=project, created_by=lead, container=col1, container_task_order=1,
        name="TwoReviewers", deadline=due,
    )
    two.reviewers.add(contrib, third)
    # Assigned but unreviewed: the reviewer count says 0.
    Task.objects.create(
        project=project, created_by=lead, container=col1, container_task_order=2,
        name="NoReviewer", deadline=due,
    ).assigned_members.add(lead)
    client.force_login(user)

    for name, (url, params) in _assignee_count_views(project).items():
        content = client.get(url, params).content.decode()
        assert "NoReviewer" in content, f"{name} view lost the unreviewed task"
        assert content.count('aria-label="1 reviewer"') == 1, (
            f"{name} view missed the single reviewer"
        )
        assert content.count('aria-label="2 reviewers"') == 1, (
            f"{name} view missed the pair"
        )
        assert content.count('aria-label="0 reviewers"') == 1, (
            f"{name} view hid the unreviewed task's count"
        )
        # A count, not a roll-call: names stay in the side pane.
        assert contrib.display_name not in content, f"{name} view named a reviewer"


@pytest.mark.django_db
def test_kanban_card_meta_row_appears_for_reviewers_alone(client, user):
    """A task with only reviewers still gets the meta row that carries them."""
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=lead, container=col1, container_task_order=0,
        name="ReviewersOnly",
    )
    task.reviewers.add(lead)
    client.force_login(user)

    content = client.get(
        reverse("tasks:board_kanban", kwargs={"project_pk": project.pk})
    ).content.decode()
    assert 'aria-label="1 reviewer"' in content


@pytest.mark.django_db
def test_reviewer_count_does_not_add_a_query_per_task(client, user):
    """The count reads a prefetch, so query volume stays flat in task count."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    client.force_login(user)

    def queries_for(task_total):
        Task.objects.filter(project=project).delete()
        for i in range(task_total):
            task = Task.objects.create(
                project=project, created_by=lead, container=col1,
                container_task_order=i, name=f"R{i}",
                deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
            )
            task.reviewers.add(lead)
        counts = {}
        for name, (url, params) in _assignee_count_views(project).items():
            with CaptureQueriesContext(connection) as ctx:
                client.get(url, params)
            counts[name] = len(ctx)
        return counts

    few = queries_for(2)
    many = queries_for(20)
    for name, count in many.items():
        assert count <= few[name], (
            f"{name} view went from {few[name]} to {count} queries over 18 more "
            f"tasks — the reviewer count is querying per task"
        )


# ---------------------------------------------------------------------------
# The three roles' actions, and the freeze a completed task puts on them
# ---------------------------------------------------------------------------


def _done_url(task, member):
    return reverse(
        "tasks:member_done", kwargs={"pk": task.pk, "member_pk": member.pk}
    )


@pytest.mark.django_db
def test_completed_task_keeps_the_assignees_own_actions_open(client, user, other_user):
    """A Lead closing a task early does not stop the assignees still to tick
    from marking their own part, nor one who marked from undoing it. What a
    Lead sets for others stays frozen."""
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    mark_own_done(task=task, member=contrib)

    client.force_login(user)
    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED

    client.force_login(other_user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert "Mark your part not done" in pane

    undo = client.post(reverse("tasks:undo", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")
    assert undo.status_code == 200
    assert task.assignees_done_count == 0
    redo = client.post(reverse("tasks:complete", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")
    assert redo.status_code == 200
    assert task.assignees_done_count == 1

    # The Lead, an assignee too, marks their own part after closing it.
    client.force_login(user)
    marked = client.post(reverse("tasks:complete", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")
    assert marked.status_code == 200
    assert task.assignees_done_count == 2
    # But clearing someone else's mark still waits for the task to reopen.
    cleared = client.post(
        reverse("tasks:member_done", kwargs={"pk": task.pk, "member_pk": contrib.pk}),
        {"done": "0"},
        HTTP_HX_REQUEST="true",
    )
    assert cleared.status_code == 403
    assert task.assignees_done_count == 2
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED  # none of this reopened it


@pytest.mark.django_db
def test_every_change_of_a_done_mark_is_audited(client, user, other_user):
    """Each mark and undo writes an audit row naming who clicked, whose part
    it was, and whether the task was already closed; a repeated click on a
    part already done writes nothing new."""
    project, task, lead, contrib = _two_assignee_task(user, other_user)
    complete_url = reverse("tasks:complete", kwargs={"pk": task.pk})
    undo_url = reverse("tasks:undo", kwargs={"pk": task.pk})

    client.force_login(other_user)
    client.post(complete_url, HTTP_HX_REQUEST="true")
    client.post(complete_url, HTTP_HX_REQUEST="true")  # double click
    client.force_login(user)
    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))
    client.post(complete_url, HTTP_HX_REQUEST="true")
    client.force_login(other_user)
    client.post(undo_url, HTTP_HX_REQUEST="true")

    rows = list(
        AuditLog.objects.filter(
            action__in=[AuditAction.TASK_PART_DONE, AuditAction.TASK_PART_UNDONE]
        ).order_by("timestamp", "id")
    )
    assert [(r.action, r.actor, r.metadata["assignee_member_id"], r.metadata["after_completion"]) for r in rows] == [
        (AuditAction.TASK_PART_DONE, other_user, contrib.pk, False),
        (AuditAction.TASK_PART_DONE, user, lead.pk, True),
        (AuditAction.TASK_PART_UNDONE, other_user, contrib.pk, True),
    ]
    assert all(r.entity_id == task.pk and r.project == project for r in rows)


@pytest.mark.django_db
def test_a_lead_marking_for_someone_is_audited_under_the_lead(client, user, other_user):
    project, task, lead, contrib = _two_assignee_task(user, other_user)
    client.force_login(user)
    client.post(
        reverse("tasks:member_done", kwargs={"pk": task.pk, "member_pk": contrib.pk}),
        {"done": "1"},
        HTTP_HX_REQUEST="true",
    )
    row = AuditLog.objects.get(action=AuditAction.TASK_PART_DONE)
    assert row.actor == user and row.metadata["assignee_member_id"] == contrib.pk


@pytest.mark.django_db
def test_a_mark_made_after_closing_is_credited_once_verified(client, user, other_user):
    """On a project tracking CRediT, closing writes no credit; a part marked
    done after the Lead closed the task is credited when a Lead verifies it,
    and undoing the mark takes back only that credit."""
    from apps.contributions.models import ContributionRecord
    from apps.tasks.services import mark_own_done, verify_part

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    project.contribution_tracking = ContributionTracking.CREDIT
    project.save(update_fields=["contribution_tracking"])
    verify_part(task_log=mark_own_done(task=task, member=lead), lead=lead)
    client.force_login(user)
    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))
    assert list(ContributionRecord.objects.filter(task=task).values_list("user", flat=True)) == [user.pk]

    client.force_login(other_user)
    client.post(reverse("tasks:complete", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")
    assert not ContributionRecord.objects.filter(task=task, user=other_user).exists()

    client.force_login(user)
    response = client.post(
        _verify_url(task, _current_log(task, contrib)), {"verified": "1"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    late = ContributionRecord.objects.get(task=task, user=other_user)
    assert late.verified_by == user  # the Lead who verified it

    client.force_login(other_user)
    client.post(reverse("tasks:undo", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")
    assert list(ContributionRecord.objects.filter(task=task).values_list("user", flat=True)) == [user.pk]
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.django_db
def test_completed_task_locks_earlier_reviews_and_keeps_later_ones_open(client, user, other_user):
    """On a completed task, a review given before it was completed is locked:
    it cannot be withdrawn. A part marked after completion can be reviewed,
    and that review withdrawn. None of it reopens the task."""
    from apps.tasks.services import mark_own_done, mark_reviewed

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    task.reviewers.add(lead)
    mark_own_done(task=task, member=contrib)
    reviewed_log = _current_log(task, contrib)
    mark_reviewed(task_log=reviewed_log, reviewer=lead)

    client.force_login(user)
    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))
    mark_own_done(task=task, member=lead)  # marked after completion
    unreviewed_log = _current_log(task, lead)

    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_REVIEWED in _row_for(pane, lead)
    early_row = _row_for(pane, contrib)
    assert MARK_NOT_REVIEWED not in early_row
    assert "reviewed (locked)" in early_row
    assert f"by {lead.display_name}" in early_row

    # The earlier review cannot be withdrawn, even by a Lead.
    refused = client.post(
        _review_url(task, reviewed_log), {"reviewed": "0"}, HTTP_HX_REQUEST="true"
    )
    assert refused.status_code == 403
    reviewed_log.refresh_from_db()
    assert reviewed_log.is_reviewed

    # The part marked after completion is reviewed, and that review can
    # still be withdrawn.
    assert client.post(
        _review_url(task, unreviewed_log), {"reviewed": "1"}, HTTP_HX_REQUEST="true"
    ).status_code == 200
    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_NOT_REVIEWED in _row_for(pane, lead)
    assert client.post(
        _review_url(task, unreviewed_log), {"reviewed": "0"}, HTTP_HX_REQUEST="true"
    ).status_code == 200
    unreviewed_log.refresh_from_db()
    assert not unreviewed_log.is_reviewed
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED

    # Reopening the task lifts the lock.
    client.post(reverse("tasks:reopen", kwargs={"pk": task.pk}))
    assert client.post(
        _review_url(task, reviewed_log), {"reviewed": "0"}, HTTP_HX_REQUEST="true"
    ).status_code == 200


@pytest.mark.django_db
def test_lead_can_mark_another_members_part_done(client, user, other_user):
    """Rule 3: a Lead borrows the assignee's action, per row."""
    project, task, lead, contrib = _two_assignee_task(user, other_user)
    log = _current_log(task, contrib)

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_DONE in _row_for(pane, contrib)

    response = client.post(
        _done_url(task, contrib), {"done": "1", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    log.refresh_from_db()
    assert log.status == TaskStatus.COMPLETED
    assert log.completed_at is not None
    # Recorded as the Lead's doing, not as the assignee's own click.
    assert log.completed_by == lead
    task.refresh_from_db()
    assert task.status == TaskStatus.NOT_COMPLETED  # a mark is not a closure

    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_NOT_DONE in _row_for(pane, contrib)


@pytest.mark.django_db
def test_lead_can_clear_another_members_mark_and_its_review(client, user, other_user):
    """Clearing a mark takes the review standing on it, as reverting always does."""
    from apps.tasks.services import mark_own_done, mark_reviewed

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    task.reviewers.add(lead)
    mark_own_done(task=task, member=contrib)
    log = _current_log(task, contrib)
    mark_reviewed(task_log=log, reviewer=lead)

    client.force_login(user)
    response = client.post(
        _done_url(task, contrib), {"done": "0", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    log.refresh_from_db()
    assert log.status == TaskStatus.NOT_COMPLETED
    assert log.completed_at is None
    assert not log.is_reviewed
    assert log.reviewed_by is None


@pytest.mark.django_db
def test_only_a_lead_gets_the_per_row_done_action(client, user, other_user):
    """An assignee marks their own part from the top bar, not other people's."""
    project, task, lead, contrib = _two_assignee_task(user, other_user)
    ProjectPrivilege.objects.filter(project=project, user=other_user).update(
        privilege=MemberPrivileges.CONTRIBUTOR
    )
    # Being a reviewer does not extend to setting marks.
    task.reviewers.add(contrib)
    lead_log = _current_log(task, lead)

    client.force_login(other_user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_DONE not in pane
    assert MARK_NOT_DONE not in pane
    # Their own action is still there, in the bar.
    assert "Mark your part done" in pane

    response = client.post(
        _done_url(task, lead), {"done": "1"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 403
    lead_log.refresh_from_db()
    assert lead_log.status == TaskStatus.NOT_COMPLETED


@pytest.mark.django_db
def test_completed_task_freezes_the_leads_per_row_actions(client, user, other_user):
    """The freeze is uniform: a Lead marks the task not complete to edit marks."""
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    task.reviewers.add(lead)
    mark_own_done(task=task, member=contrib)
    log = _current_log(task, contrib)

    client.force_login(user)
    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))

    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_DONE not in pane and MARK_NOT_DONE not in pane
    # The one action a completed task still offers.
    assert "Mark Not Complete" in pane

    assert client.post(
        _done_url(task, contrib), {"done": "0"}, HTTP_HX_REQUEST="true"
    ).status_code == 403
    log.refresh_from_db()
    assert log.status == TaskStatus.COMPLETED

    # Reopening the task hands the actions back.
    client.post(reverse("tasks:reopen", kwargs={"pk": task.pk}))
    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_NOT_DONE in _row_for(pane, contrib)
    assert client.post(
        _done_url(task, contrib), {"done": "0", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    ).status_code == 200


@pytest.mark.django_db
def test_lead_marking_someone_done_is_not_what_closing_does(client, user, other_user):
    """Mark Completed still writes nobody's mark — only the per-row action does."""
    project, task, lead, contrib = _two_assignee_task(user, other_user)
    client.force_login(user)

    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))
    task.refresh_from_db()
    assert task.assignees_done_count == 0, "closing wrote a mark on someone's behalf"

    client.post(reverse("tasks:reopen", kwargs={"pk": task.pk}))
    client.post(
        _done_url(task, contrib),
        {"done": "1", "pane_mode": "1"},
        HTTP_HX_REQUEST="true",
    )
    task.refresh_from_db()
    assert task.assignees_done_count == 1  # just the one the Lead set, deliberately


@pytest.mark.django_db
def test_lead_can_mark_an_assignee_who_has_no_log_entry_done(client, user, other_user):
    """The action is keyed by member, so an unseeded row still works."""
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    contrib = _add_contributor(project, other_user)
    task = Task.objects.create(
        project=project, created_by=lead, container=col1,
        container_task_order=0, name="Unseeded",
    )
    # Assigned straight on the M2M, the way an import does — no TaskLog seeded.
    task.assigned_members.add(contrib)
    assert not task.task_logs.exists()

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_DONE in _row_for(pane, contrib)

    response = client.post(
        _done_url(task, contrib), {"done": "1", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    log = _current_log(task, contrib)
    assert log is not None and log.status == TaskStatus.COMPLETED
    assert log.completed_by == lead


@pytest.mark.django_db
def test_lead_cannot_mark_a_non_assignee_done(client, user, other_user):
    """Only people actually on the task have a mark to set."""
    project, task, _lead, contrib = _two_assignee_task(user, other_user)
    task.assigned_members.remove(contrib)

    client.force_login(user)
    response = client.post(
        _done_url(task, contrib), {"done": "1"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 404
    assert task.assignees_done_count == 0


@pytest.mark.django_db
def test_completed_task_has_no_locked_note(client, user, other_user):
    """A completed task's row actions go without a note saying so, on both
    render paths (the pane and the HTMX swap); reopening brings them back."""
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    task.reviewers.add(lead)
    mark_own_done(task=task, member=contrib)
    client.force_login(user)

    note = "Marks and reviews are locked"
    assert note not in client.get(_pane_url(project, task)).content.decode()

    # Neither the swap the Lead gets back from Mark Completed...
    closed = client.post(
        reverse("tasks:close", kwargs={"pk": task.pk}),
        {"pane_mode": "1"},
        headers={"hx-request": "true"},
    ).content.decode()
    assert note not in closed and MARK_NOT_DONE not in closed
    # ... nor a fresh open of the pane.
    pane = client.get(_pane_url(project, task)).content.decode()
    assert note not in pane and MARK_NOT_DONE not in pane

    reopened = client.post(
        reverse("tasks:reopen", kwargs={"pk": task.pk}),
        {"pane_mode": "1"},
        headers={"hx-request": "true"},
    ).content.decode()
    assert note not in reopened
    assert MARK_NOT_DONE in reopened


@pytest.mark.django_db
def test_each_tick_cell_says_when_and_by_whom_beside_its_box(client, user, other_user, pm_user):
    """The Done and Reviewed cells carry the date and who beside the box, and
    say what is still to come otherwise; nothing sits under the name."""
    from apps.tasks.services import mark_own_done, mark_reviewed

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    third = _add_contributor(project, pm_user)
    task.assigned_members.add(third)
    task.reviewers.add(lead)
    mark_own_done(task=task, member=contrib)
    mark_reviewed(task_log=_current_log(task, contrib), reviewer=lead)
    mark_own_done(task=task, member=third)
    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()

    def cells(member):
        row = _row_for(pane, member)
        name = row[: row.index("data-done-cell")]
        done = row[row.index("data-done-cell") : row.index("data-review-cell")]
        review = row[row.index("data-review-cell") :]
        return name, done, review

    name, done, review = cells(contrib)
    assert "by " not in name
    assert f"by {contrib.display_name}" in done and "data-done-line" in done
    assert f"by {lead.display_name}" in review and "data-review-line" in review

    _name, done, review = cells(third)
    assert f"by {third.display_name}" in done
    assert "Awaiting review" in review

    _name, done, review = cells(lead)
    assert "Not done yet" in done and "&mdash;" in review


@pytest.mark.django_db
def test_completion_heading_carries_an_info_button_explaining_the_procedure(client, user, other_user):
    """An (i) beside the Completion heading explains done, reviewed, verified
    and completed (and the one lock completing brings), on the pane and the
    full page; the card itself carries no tick hint and no done count beside
    the heading."""
    project, task, _lead, _contrib = _two_assignee_task(user, other_user)
    client.force_login(user)

    for url in (
        _pane_url(project, task),
        reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk}),
    ):
        html = client.get(url).content.decode()
        info = html[html.index("data-completion-info") :]
        info = info[: info.index("</ol>")]
        assert 'aria-label="About Completion"' in info
        assert 'x-show="open"' in info
        for step in ("1. Done.", "2. Reviewed.", "3. Verified.", "4. Completed."):
            assert step in info, step
        verified = info[info.index("3. Verified.") : info.index("4. Completed.")]
        # This project doesn't track CRediT, so verifying credits nothing.
        assert "records the sign-off only" in verified
        completed = info[info.index("4. Completed.") :]
        assert "Assignees can still tick their own Done" in completed
        assert "reviewers can review parts marked done after completion" in completed
        assert "a review given then is locked" in completed
        assert "a part left unreviewed can no longer be reviewed" in completed
        assert "A review given after completion can still be unticked" in completed
        assert "5." not in info
        assert "Tick to mark, untick to undo." not in html
        assert "0 of 2 done" not in html
        # The element x-show hides carries no id: HTMX restores an id'd
        # element's style after a swap, which would open the note on its own.
        panel = html[html.rindex("<div", 0, html.index("data-completion-info-panel")) :]
        panel = panel[: panel.index(">")]
        assert 'x-show="open"' in panel and " id=" not in panel


@pytest.mark.django_db
def test_a_completed_tasks_boxes_explain_the_lock_when_clicked(client, user, other_user):
    """On a completed task the Done boxes a Lead would set for others, and the
    Reviewed box of a part reviewed before completion, are locked: they keep
    their state, post nothing, and a click shows a notice that the task has
    to be marked not complete first. The viewer's own Done box and the
    Verified boxes stay live. Reopening drops the lock and the notice."""
    from apps.tasks.services import mark_own_done, mark_reviewed

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    task.reviewers.add(lead)
    mark_own_done(task=task, member=contrib)
    mark_reviewed(task_log=_current_log(task, contrib), reviewer=lead)
    client.force_login(user)

    pane = client.get(_pane_url(project, task)).content.decode()
    assert "data-locked" not in pane and "task-locked-notice" not in pane

    closed = client.post(
        reverse("tasks:close", kwargs={"pk": task.pk}),
        {"pane_mode": "1"},
        headers={"hx-request": "true"},
    ).content.decode()
    for html in (closed, client.get(_pane_url(project, task)).content.decode()):
        rows = _rows(html)
        # The contributor's Done box and their earlier review are locked
        # for the Lead.
        assert rows.count("data-locked") == 2
        assert rows.count('@click.prevent="showLocked()"') == 2
        own = _row_for(html, lead)
        assert reverse("tasks:complete", kwargs={"pk": task.pk}) in own
        row = _row_for(html, contrib)
        # Done and Reviewed still read as ticked and are locked; the part
        # can still be verified.
        assert row.count(" checked") == 2 and "reviewed (locked)" in row
        assert MARK_NOT_REVIEWED not in row and MARK_VERIFIED in row
        # The notice is there, hidden until a click, and tells a Lead what to do.
        assert 'id="task-locked-notice"' in html and 'x-show="lockedNotice"' in html
        assert "already marked completed" in html
        assert "Mark it not complete first" in html

    # An assignee is told a Lead has to reopen it.
    client.force_login(other_user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert "A Lead has to mark it not complete" in pane
    assert "parts marked done after completion can still be reviewed" in pane
    assert "Mark it not complete first" not in pane

    client.force_login(user)
    reopened = client.post(
        reverse("tasks:reopen", kwargs={"pk": task.pk}),
        {"pane_mode": "1"},
        headers={"hx-request": "true"},
    ).content.decode()
    assert "data-locked" not in reopened and "task-locked-notice" not in reopened
    assert MARK_NOT_DONE in reopened


@pytest.mark.django_db
def test_a_part_marked_after_closing_is_reviewed_and_verified_as_usual(client, user, other_user):
    """On a closed task, a part marked done after closing is reviewed as
    usual, while an unreviewed part marked before closing takes no review.
    Both can be verified: the review earns no credit by itself, the Lead's
    verification does, and withdrawing it takes back only that credit. The
    task stays closed throughout."""
    from apps.contributions.models import ContributionRecord
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    project.contribution_tracking = ContributionTracking.CREDIT
    project.save(update_fields=["contribution_tracking"])
    task.reviewers.add(lead)
    mark_own_done(task=task, member=lead)  # marked before closing, unreviewed
    client.force_login(user)
    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))

    client.force_login(other_user)
    client.post(reverse("tasks:complete", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")
    late_log = _current_log(task, contrib)
    early_log = _current_log(task, lead)

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    late_row = _row_for(pane, contrib)
    assert _review_url(task, late_log) in late_row and "Awaiting review" in late_row
    early_row = _row_for(pane, lead)
    assert _review_url(task, early_log) not in early_row
    assert "not reviewed (locked)" in early_row and "Not reviewed" in early_row
    for row, log in ((late_row, late_log), (early_row, early_log)):
        assert _verify_url(task, log) in row and "Awaiting verification" in row

    refused = client.post(_review_url(task, early_log), {"reviewed": "1"}, HTTP_HX_REQUEST="true")
    assert refused.status_code == 403
    early_log.refresh_from_db()
    assert not early_log.is_reviewed

    reviewed = client.post(_review_url(task, late_log), {"reviewed": "1"}, HTTP_HX_REQUEST="true")
    assert reviewed.status_code == 200
    late_log.refresh_from_db()
    assert late_log.is_reviewed and late_log.reviewed_by == lead
    assert not ContributionRecord.objects.filter(task=task).exists()

    verified = client.post(_verify_url(task, late_log), {"verified": "1"}, HTTP_HX_REQUEST="true")
    assert verified.status_code == 200
    record = ContributionRecord.objects.get(task=task)
    assert record.user == other_user and record.verified_by == user

    withdrawn = client.post(_verify_url(task, late_log), {"verified": "0"}, HTTP_HX_REQUEST="true")
    assert withdrawn.status_code == 200
    assert not ContributionRecord.objects.filter(task=task).exists()
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.django_db
def test_an_assignee_can_undo_a_part_reviewed_before_completion(client, user, other_user):
    """The exception to the review lock: an assignee can always take back
    their own done mark on a closed task. Doing so clears the review given
    before completion, which no one could withdraw directly, and the Lead's
    verification and its credit. The task stays completed."""
    from apps.contributions.models import ContributionRecord
    from apps.tasks.services import mark_own_done, mark_reviewed, verify_part

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    project.contribution_tracking = ContributionTracking.CREDIT
    project.save(update_fields=["contribution_tracking"])
    task.reviewers.add(lead)
    log = mark_own_done(task=task, member=contrib)
    mark_reviewed(task_log=log, reviewer=lead)
    verify_part(task_log=log, lead=lead)
    client.force_login(user)
    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))
    assert ContributionRecord.objects.filter(task=task, user=other_user).exists()

    client.force_login(other_user)
    pane = client.get(_pane_url(project, task)).content.decode()
    own = _row_for(pane, contrib)
    # Their Done box is live; the Reviewed box beside it stays locked.
    assert reverse("tasks:undo", kwargs={"pk": task.pk}) in own
    assert own.count("data-locked") == 1 and "reviewed (locked)" in own

    response = client.post(reverse("tasks:undo", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    log.refresh_from_db()
    assert log.status == TaskStatus.NOT_COMPLETED
    assert not log.is_reviewed and log.reviewed_by is None
    assert not log.is_verified and log.verified_by is None
    assert not ContributionRecord.objects.filter(task=task, user=other_user).exists()
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED
    for action in (AuditAction.TASK_REVIEW_CLEARED, AuditAction.TASK_PART_UNVERIFIED):
        row = AuditLog.objects.get(action=action)
        assert row.actor == other_user
        assert row.metadata["reason"] == "the done mark it stood on was reverted"

    # Marked done again, the part comes back unreviewed and unverified, and
    # open to review — this time after completion, so not locked.
    client.post(reverse("tasks:complete", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")
    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    row = _row_for(pane, contrib)
    assert "Awaiting review" in row and "Awaiting verification" in row
    assert "data-locked" in row  # only the Done box a Lead cannot set for them


@pytest.mark.django_db
def test_undoing_a_part_reviewed_after_completion_leaves_the_task_closed(client, user, other_user):
    """A part marked and reviewed after the Lead completed the task stays open
    to its assignee: undoing it clears that later review, and the task stays
    closed — only a Lead completes or reopens one."""
    from apps.tasks.services import mark_own_done, mark_reviewed

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    task.reviewers.add(lead)
    client.force_login(user)
    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))
    mark_own_done(task=task, member=contrib)
    mark_reviewed(task_log=_current_log(task, contrib), reviewer=lead)

    client.force_login(other_user)
    response = client.post(reverse("tasks:undo", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED
    log = _current_log(task, contrib)
    assert log.status == TaskStatus.NOT_COMPLETED and not log.is_reviewed


def _sole_assignee_task(user, other_user, *, name="Reviewed to done"):
    """A task with one assignee and the Lead as its only reviewer."""
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    contrib = _add_contributor(project, other_user)
    task = Task.objects.create(
        project=project, created_by=lead, container=col1, container_task_order=0,
        name=name,
    )
    task.assigned_members.add(contrib)
    task.reviewers.add(lead)
    return project, task, lead, contrib


@pytest.mark.django_db
def test_reviewing_the_last_submission_leaves_completing_to_a_lead(client, user, other_user):
    """Every submission signed off does not complete the task by itself: a
    Lead marks it completed."""
    from apps.tasks.services import mark_own_done

    project, task, _lead, contrib = _sole_assignee_task(user, other_user)
    mark_own_done(task=task, member=contrib)
    client.force_login(user)

    swapped = client.post(
        _review_url(task, _current_log(task, contrib)),
        {"reviewed": "1", "pane_mode": "1"},
        HTTP_HX_REQUEST="true",
    ).content.decode()
    task.refresh_from_db()
    assert task.status == TaskStatus.NOT_COMPLETED
    assert task.closed_by is None
    assert "Mark Completed" in swapped
    assert not AuditLog.objects.filter(action=AuditAction.TASK_COMPLETED).exists()

    client.post(reverse("tasks:close", kwargs={"pk": task.pk}), {"pane_mode": "1"}, HTTP_HX_REQUEST="true")
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.django_db
def test_a_completed_task_freezes_only_the_leads_marks_for_others(client, user, other_user):
    """On a completed task a Lead cannot set or clear someone else's done
    mark until they reopen it; clearing it then takes the review and the
    verification standing on it."""
    from apps.tasks.services import close_task, mark_own_done, mark_reviewed, verify_part

    project, task, lead, contrib = _sole_assignee_task(user, other_user)
    log = mark_own_done(task=task, member=contrib)
    mark_reviewed(task_log=log, reviewer=lead)
    verify_part(task_log=log, lead=lead)
    close_task(task=task, closed_by=lead)
    client.force_login(user)

    refused = client.post(
        _done_url(task, contrib), {"done": "0", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    )
    assert refused.status_code == 403
    assert _current_log(task, contrib).status == TaskStatus.COMPLETED

    client.post(reverse("tasks:reopen", kwargs={"pk": task.pk}), {"pane_mode": "1"}, HTTP_HX_REQUEST="true")
    client.post(
        _done_url(task, contrib), {"done": "0", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    )
    log = _current_log(task, contrib)
    assert log.status == TaskStatus.NOT_COMPLETED
    assert not log.is_reviewed and not log.is_verified


@pytest.mark.django_db
def test_a_reviewer_who_is_not_assigned_gets_every_row(client, user, other_user):
    """The plain reviewer case: on the task's reviewer list, assigned to none of it."""
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    reviewer = _add_contributor(project, _new_user("carol@test.com"))
    task.reviewers.add(reviewer)
    mark_own_done(task=task, member=contrib)
    log = _current_log(task, contrib)

    client.force_login(reviewer.users.first())
    pane = client.get(_pane_url(project, task)).content.decode()
    # No row of their own, and no assignee actions.
    assert reviewer.display_name not in _rows(pane)
    assert "Mark your part done" not in pane
    assert MARK_DONE not in pane  # setting marks is the Lead's
    # But the review action on the submission that is ready for it.
    assert MARK_REVIEWED in _row_for(pane, contrib)

    response = client.post(
        _review_url(task, log), {"reviewed": "1", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    log.refresh_from_db()
    assert log.reviewed_by == reviewer


# ---------------------------------------------------------------------------
# Who set a done mark
# ---------------------------------------------------------------------------


def _done_cell(row):
    """The green Done line under a Completion row's name ("Done <date> · by
    <name>"), or an empty string on a row whose part is not marked done."""
    first = row.find("data-done-line")
    if first == -1:
        return ""
    return row[first : row.index("</span>", first)]


@pytest.mark.django_db
def test_matrix_names_the_lead_who_marked_an_assignee_done(client, user, other_user):
    """A mark set for someone carries the name of whoever set it."""
    project, task, lead, contrib = _two_assignee_task(user, other_user)

    client.force_login(user)
    client.post(
        _done_url(task, contrib), {"done": "1", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    )

    pane = client.get(_pane_url(project, task)).content.decode()
    row = _row_for(pane, contrib)
    done_cell = _done_cell(row)
    assert f"by {lead.display_name}" in done_cell
    # The date comes first, then who set it.
    log = _current_log(task, contrib)
    assert done_cell.index(log.completed_at.strftime("%b %-d")) < done_cell.index("by ")


@pytest.mark.django_db
def test_matrix_names_an_assignee_on_their_own_mark(client, user, other_user):
    """A self-mark names the assignee, as every Done line names its setter."""
    project, task, _lead, contrib = _two_assignee_task(user, other_user)

    client.force_login(other_user)
    client.post(reverse("tasks:complete", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")

    client.force_login(user)
    row = _row_for(client.get(_pane_url(project, task)).content.decode(), contrib)
    done_cell = _done_cell(row)
    assert f"by {contrib.display_name}" in done_cell


@pytest.mark.django_db
def test_matrix_survives_a_mark_that_recorded_nobody(client, user, other_user):
    """Old entries name no setter; the cell shows the mark without a name."""
    project, task, _lead, contrib = _two_assignee_task(user, other_user)
    log = _current_log(task, contrib)
    log.status = TaskStatus.COMPLETED
    log.completed_by = None
    log.save(update_fields=["status", "completed_by"])

    client.force_login(user)
    row = _row_for(client.get(_pane_url(project, task)).content.decode(), contrib)
    done_cell = _done_cell(row)
    assert "by a former member" in done_cell
    assert "None" not in done_cell


@pytest.mark.django_db
def test_a_cleared_mark_takes_its_setters_name_with_it(client, user, other_user):
    """No mark, no name — and re-marking it themselves drops the Lead's."""
    project, task, lead, contrib = _two_assignee_task(user, other_user)
    client.force_login(user)
    client.post(
        _done_url(task, contrib), {"done": "1", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    )
    client.post(
        _done_url(task, contrib), {"done": "0", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    )

    row = _row_for(client.get(_pane_url(project, task)).content.decode(), contrib)
    assert lead.display_name not in row

    client.force_login(other_user)
    client.post(reverse("tasks:complete", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")
    client.force_login(user)
    row = _row_for(client.get(_pane_url(project, task)).content.decode(), contrib)
    done_cell = _done_cell(row)
    assert f"by {contrib.display_name}" in done_cell
    assert lead.display_name not in done_cell


# ---------------------------------------------------------------------------
# Withdrawing a review: the author's, or any of them if you are a Lead
# ---------------------------------------------------------------------------


def _two_reviewer_task(user, other_user):
    """A done submission and two plain reviewers, neither of them a Lead.

    Returns ``(project, task, log, first, second)`` where ``first`` and
    ``second`` are the reviewers and ``log`` is the submission they review.
    ``user`` stays the project's Lead and its lone assignee.
    """
    from apps.tasks.services import mark_own_done

    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    first = _add_contributor(project, other_user)
    second = _add_contributor(project, _new_user("dave@test.com"))
    task = Task.objects.create(
        project=project, created_by=lead, container=col1,
        container_task_order=0, name="Reviewed twice over",
    )
    task.assigned_members.add(lead)
    task.reviewers.add(first, second)
    TaskLog.objects.create(task=task, assigned_to=lead)
    mark_own_done(task=task, member=lead)
    return project, task, _current_log(task, lead), first, second


@pytest.mark.django_db
def test_a_reviewer_can_withdraw_their_own_review(client, user, other_user):
    """The reviewer who signed it off gets the button, and it works."""
    project, task, log, first, _second = _two_reviewer_task(user, other_user)

    client.force_login(other_user)
    client.post(
        _review_url(task, log), {"reviewed": "1", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    )
    log.refresh_from_db()
    assert log.reviewed_by == first

    # That review closed the task, which locks it: a Lead reopens it first.
    from apps.tasks.services import reopen_task

    task.refresh_from_db()
    reopen_task(task=task)

    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_NOT_REVIEWED in pane

    response = client.post(
        _review_url(task, log), {"reviewed": "0", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    log.refresh_from_db()
    assert not log.is_reviewed


@pytest.mark.django_db
def test_a_reviewer_cannot_withdraw_another_reviewers_review(client, user, other_user):
    """Someone else's verdict is not theirs to take back."""
    from apps.tasks.services import mark_reviewed

    project, task, log, first, second = _two_reviewer_task(user, other_user)
    mark_reviewed(task_log=log, reviewer=second)

    # ``other_user`` is ``first``: a reviewer on this task, but not this one's.
    client.force_login(other_user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_NOT_REVIEWED not in pane
    # They can still see the verdict and who gave it.
    assert f"by {second.display_name}" in pane

    response = client.post(
        _review_url(task, log), {"reviewed": "0"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 403
    log.refresh_from_db()
    assert log.reviewed_by == second


@pytest.mark.django_db
def test_a_lead_can_withdraw_any_review(client, user, other_user):
    """A Lead is not held to the author rule."""
    from apps.tasks.services import mark_reviewed

    from apps.tasks.services import reopen_task

    project, task, log, _first, second = _two_reviewer_task(user, other_user)
    mark_reviewed(task_log=log, reviewer=second)
    # That review closed the task, which locks it; reopened, the review
    # still stands and can be withdrawn.
    task.refresh_from_db()
    reopen_task(task=task)

    client.force_login(user)  # the Lead, and not the reviewer who signed it
    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_NOT_REVIEWED in pane

    response = client.post(
        _review_url(task, log), {"reviewed": "0", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    log.refresh_from_db()
    assert not log.is_reviewed
    assert log.reviewed_by is None


@pytest.mark.django_db
def test_a_review_left_by_a_deleted_member_is_the_leads_to_clear(client, user, other_user):
    """With no author left to withdraw it, only a Lead can."""
    from apps.tasks.services import mark_reviewed

    from apps.tasks.services import reopen_task

    project, task, log, first, second = _two_reviewer_task(user, other_user)
    mark_reviewed(task_log=log, reviewer=second)
    task.refresh_from_db()
    reopen_task(task=task)  # the review closed it; a completed task is locked
    second.delete()  # hard delete nulls reviewed_by; the review still stands
    log.refresh_from_db()
    assert log.is_reviewed and log.reviewed_by is None

    client.force_login(other_user)  # ``first``, a reviewer on the task
    assert MARK_NOT_REVIEWED not in client.get(_pane_url(project, task)).content.decode()
    assert client.post(
        _review_url(task, log), {"reviewed": "0"}, HTTP_HX_REQUEST="true"
    ).status_code == 403

    client.force_login(user)
    assert MARK_NOT_REVIEWED in client.get(_pane_url(project, task)).content.decode()
    assert client.post(
        _review_url(task, log), {"reviewed": "0", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    ).status_code == 200


@pytest.mark.django_db
def test_making_a_review_is_still_open_to_every_reviewer(client, user, other_user):
    """The new rule narrows withdrawing only — signing off is unchanged."""
    project, task, log, _first, _second = _two_reviewer_task(user, other_user)

    client.force_login(other_user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_REVIEWED in pane

    assert client.post(
        _review_url(task, log), {"reviewed": "1", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    ).status_code == 200


# ---------------------------------------------------------------------------
# Tooltips on the browsing views' task icons
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_browsing_views_put_a_tooltip_on_every_task_icon(client, user, other_user):
    """Kanban, List and Day: all five icons name themselves on hover."""
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    contrib = _add_contributor(project, other_user)
    task = Task.objects.create(
        project=project, created_by=lead, container=col1, container_task_order=0,
        name="Everything", is_priority=True, status=TaskStatus.COMPLETED,
        start_date=datetime(2026, 3, 10, tzinfo=dt_timezone.utc),
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    task.assigned_members.add(lead, contrib)
    task.reviewers.add(lead)
    client.force_login(user)

    expected = [
        'data-tooltip="High priority"',
        'data-tooltip="Task marked complete"',
        'data-tooltip="Mar 10, 2026 – Mar 12, 2026"',
        'data-tooltip="2 assigned members"',
        'data-tooltip="1 reviewer"',
    ]
    for name, (url, params) in _assignee_count_views(project).items():
        content = client.get(url, params).content.decode()
        for tip in expected:
            assert tip in content, f"{name} view is missing {tip}"
        # The styled tooltip replaces the plain title rather than joining it,
        # so a hover shows one bubble and not two.
        assert 'title="High priority"' not in content, f"{name} view shows both"
        assert 'title="Complete"' not in content, f"{name} view shows both"
        assert content.count("hover-tooltip") >= 5, f"{name} view lost the class"


@pytest.mark.django_db
def test_the_calendar_month_grid_keeps_plain_titles(client, user):
    """That grid is overflow-hidden, so a hanging tooltip would be clipped."""
    project, col1, _col2 = _make_project_with_board(user)
    lead = _primary_member(project, user)
    Task.objects.create(
        project=project, created_by=lead, container=col1, container_task_order=0,
        name="Dated", is_priority=True, status=TaskStatus.COMPLETED,
        deadline=datetime(2026, 3, 12, tzinfo=dt_timezone.utc),
    )
    client.force_login(user)

    month = client.get(
        reverse("tasks:board_calendar", kwargs={"project_pk": project.pk}),
        {"view": "month", "date": "2026-03-12"},
    ).content.decode()
    # The month pill's only icon is the completion check — priority shows there
    # as a left border, not the flag — and it keeps its plain title.
    assert 'title="Complete"' in month
    assert "data-tooltip" not in month
    assert "hover-tooltip" not in month


@pytest.mark.django_db
def test_the_tooltip_class_is_styled_by_the_page_shell(client, user):
    """The card icons ask for hover-tooltip, so base.html has to define it."""
    client.force_login(user)
    page = client.get(reverse("projects:list")).content.decode()
    assert ".hover-tooltip { position: relative; }" in page
    assert "content: attr(data-tooltip);" in page


# ---------------------------------------------------------------------------
# Task type: the CRediT role, on CRediT projects only
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_task_form_omits_task_type_off_a_credit_project(user):
    project, col1, _col2 = _make_project_with_board(user)
    assert not project.tracks_credit
    form = TaskForm(project=project)
    assert "contribution_type" not in form.fields


@pytest.mark.django_db
def test_task_form_offers_the_credit_roles(user):
    project, col1, _col2 = _make_project_with_board(user)
    project.contribution_tracking = ContributionTracking.CREDIT
    project.save(update_fields=["contribution_tracking"])

    form = TaskForm(project=project)
    field = form.fields["contribution_type"]
    assert field.label == "Task type"
    values = [value for value, _label in field.choices]
    assert values[0] == ""  # "No task type"
    # The 14 CRediT roles, then "Other" as the last option.
    assert set(values[1:-1]) == set(CREDIT_ROLES)
    assert values[-1] == ContributionType.OTHER
    # The legacy type stays off a fresh task.
    assert ContributionType.WRITING not in values
    # A new task opens on "No task type", not on "Other".
    assert 'value="" selected' in str(form["contribution_type"])


@pytest.mark.django_db
def test_task_form_keeps_a_task_off_the_taxonomy_in_its_choices(user):
    """Editing a task saved as "Other" must not quietly rewrite its type."""
    project, col1, _col2 = _make_project_with_board(user)
    project.contribution_tracking = ContributionTracking.CREDIT
    project.save(update_fields=["contribution_tracking"])
    task = Task.objects.create(
        project=project,
        created_by=_primary_member(project, user),
        container=col1,
        container_task_order=0,
        name="Legacy",
        contribution_type=ContributionType.OTHER,
    )

    form = TaskForm(project=project, instance=task)
    values = [value for value, _label in form.fields["contribution_type"].choices]
    # Offered once, still last.
    assert values.count(ContributionType.OTHER) == 1
    assert values[-1] == ContributionType.OTHER

    # A legacy type is kept too, and "Other" still closes the list.
    task.contribution_type = ContributionType.WRITING
    task.save(update_fields=["contribution_type"])
    form = TaskForm(project=project, instance=task)
    values = [value for value, _label in form.fields["contribution_type"].choices]
    assert ContributionType.WRITING in values
    assert values[-1] == ContributionType.OTHER


@pytest.mark.django_db
def test_creating_a_task_saves_its_task_type(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    project.contribution_tracking = ContributionTracking.CREDIT
    project.save(update_fields=["contribution_tracking"])
    client.force_login(user)

    client.post(
        reverse("tasks:create", kwargs={"project_pk": project.pk}),
        _task_form_data(col1, contribution_type=ContributionType.SOFTWARE),
    )
    task = Task.objects.get(name="Dated")
    assert task.contribution_type == ContributionType.SOFTWARE


@pytest.mark.django_db
def test_an_unpicked_task_type_falls_back_to_other(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    project.contribution_tracking = ContributionTracking.CREDIT
    project.save(update_fields=["contribution_tracking"])
    client.force_login(user)

    client.post(
        reverse("tasks:create", kwargs={"project_pk": project.pk}),
        _task_form_data(col1, contribution_type=""),
    )
    task = Task.objects.get(name="Dated")
    assert task.contribution_type == ContributionType.OTHER


@pytest.mark.django_db
def test_task_form_page_shows_the_task_type_field_only_for_credit(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    client.force_login(user)
    url = reverse("tasks:create", kwargs={"project_pk": project.pk})

    assert "Task type" not in client.get(url).content.decode()

    project.contribution_tracking = ContributionTracking.CREDIT
    project.save(update_fields=["contribution_tracking"])
    body = client.get(url).content.decode()
    assert "Task type" in body
    assert "https://credit.niso.org/" in body


# ---------------------------------------------------------------------------
# A completed task is frozen until its completion is reversed
# ---------------------------------------------------------------------------


def _completed_task(user):
    project, col1, col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project,
        created_by=member,
        container=col1,
        container_task_order=0,
        name="Frozen",
        status=TaskStatus.COMPLETED,
    )
    return project, col1, col2, member, task


@pytest.mark.django_db
def test_a_completed_task_refuses_edits(client, user):
    project, col1, _col2, _member, task = _completed_task(user)
    client.force_login(user)
    url = reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk})

    # The form itself is refused, not just the save.
    assert client.get(url).status_code == 302
    client.post(url, _task_form_data(col1, name="Renamed"))
    task.refresh_from_db()
    assert task.name == "Frozen"


@pytest.mark.django_db
def test_a_completed_task_refuses_deletion(client, user):
    project, _col1, _col2, _member, task = _completed_task(user)
    client.force_login(user)
    client.post(
        reverse("tasks:delete", kwargs={"project_pk": project.pk, "pk": task.pk})
    )
    assert Task.objects.filter(pk=task.pk).exists()


@pytest.mark.django_db
def test_a_completed_task_refuses_assignee_changes(client, user, other_user):
    project, _col1, _col2, _member, task = _completed_task(user)
    newcomer = ProjectMember.objects.create(project=project)
    newcomer.users.add(other_user)
    client.force_login(user)

    client.post(
        reverse("tasks:assign", kwargs={"pk": task.pk}),
        {"assigned_members": [newcomer.pk]},
    )
    assert list(task.assigned_members.all()) == []


@pytest.mark.django_db
def test_a_completed_task_refuses_a_milestone_change(client, user):
    project, _col1, _col2, _member, task = _completed_task(user)
    milestone = Milestone.objects.create(project=project, name="M1")
    client.force_login(user)

    client.post(
        reverse("tasks:set_milestone", kwargs={"pk": task.pk}),
        {"milestone": milestone.pk},
    )
    task.refresh_from_db()
    assert task.milestone_id is None


@pytest.mark.django_db
def test_an_htmx_edit_of_a_completed_task_gets_a_conflict(client, user):
    project, _col1, _col2, _member, task = _completed_task(user)
    client.force_login(user)
    resp = client.get(
        reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk}),
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 409
    assert b"mark it not complete" in resp.content


@pytest.mark.django_db
def test_reversing_the_completion_unlocks_the_task(client, user):
    project, col1, _col2, _member, task = _completed_task(user)
    client.force_login(user)

    client.post(reverse("tasks:reopen", kwargs={"pk": task.pk}))
    task.refresh_from_db()
    assert task.status == TaskStatus.NOT_COMPLETED

    url = reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk})
    assert client.get(url).status_code == 200
    client.post(url, _task_form_data(col1, name="Renamed"))
    task.refresh_from_db()
    assert task.name == "Renamed"


@pytest.mark.django_db
def test_a_completed_task_still_moves_between_columns(client, user):
    """Which column it sits in is where it is, not what it is."""
    project, _col1, col2, _member, task = _completed_task(user)
    client.force_login(user)

    client.post(
        reverse("tasks:move", kwargs={"pk": task.pk}),
        {"container_id": col2.pk, "order": 0},
    )
    task.refresh_from_db()
    assert task.container_id == col2.pk


@pytest.mark.django_db
def test_the_detail_page_drops_edit_and_delete_once_completed(client, user):
    project, _col1, _col2, _member, task = _completed_task(user)
    client.force_login(user)
    url = reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk})

    edit_url = reverse(
        "tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk}
    )
    # What opens the delete dialog, rather than the dialog itself: the
    # dialog is inert markup that renders either way, so the button is what
    # says whether deleting is on offer.
    delete_opener = f"task-delete-modal-{task.pk}').showModal()"

    body = client.get(url).content.decode()
    assert edit_url not in body
    assert delete_opener not in body
    assert "Mark Not Complete" in body

    task.status = TaskStatus.NOT_COMPLETED
    task.save(update_fields=["status"])
    body = client.get(url).content.decode()
    assert edit_url in body
    assert delete_opener in body


# ---------------------------------------------------------------------------
# Require review: the tickbox that puts a task under review
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_require_review_asks_for_a_reviewer(user, other_user):
    project, col1, _col2 = _make_project_with_board(user)
    form = TaskForm(
        data=_task_form_data(col1, requires_review="on"), project=project
    )
    assert not form.is_valid()
    assert "reviewers" in form.errors
    assert "at least one reviewer" in str(form.errors["reviewers"])


@pytest.mark.django_db
def test_require_review_with_a_reviewer_validates(user, other_user):
    project, col1, _col2 = _make_project_with_board(user)
    reviewer = _add_contributor(project, other_user)
    form = TaskForm(
        data=_task_form_data(
            col1, requires_review="on", reviewers=[str(reviewer.pk)]
        ),
        project=project,
    )
    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_unticking_require_review_clears_the_reviewers(client, user, other_user):
    """What the box says and who is named never drift apart."""
    project, col1, _col2 = _make_project_with_board(user)
    reviewer = _add_contributor(project, other_user)
    client.force_login(user)

    client.post(
        reverse("tasks:create", kwargs={"project_pk": project.pk}),
        _task_form_data(col1, requires_review="on", reviewers=[str(reviewer.pk)]),
    )
    task = Task.objects.get(name="Dated")
    assert task.requires_review is True
    assert list(task.reviewers.all()) == [reviewer]
    assert task.needs_review is True

    # Re-save with the box unticked: the reviewer goes with it.
    client.post(
        reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk}),
        _task_form_data(col1, reviewers=[str(reviewer.pk)]),
    )
    task.refresh_from_db()
    assert task.requires_review is False
    assert list(task.reviewers.all()) == []
    assert task.needs_review is False


@pytest.mark.django_db
def test_the_task_form_carries_the_require_review_tickbox(client, user):
    project, _col1, _col2 = _make_project_with_board(user)
    client.force_login(user)
    body = client.get(
        reverse("tasks:create", kwargs={"project_pk": project.pk})
    ).content.decode()
    assert "Require review" in body
    assert 'name="requires_review"' in body
    # The reviewers field is in the markup but folded away until it is ticked.
    assert 'x-show="requiresReview"' in body


@pytest.mark.django_db
def test_task_form_layout_flag_button_review_panel_and_one_picker_style(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    project.contribution_tracking = ContributionTracking.CREDIT
    project.save(update_fields=["contribution_tracking"])
    client.force_login(user)
    body = client.get(
        reverse("tasks:create", kwargs={"project_pk": project.pk})
    ).content.decode()

    # High priority: the real checkbox, hidden inside a flag toggle button
    # that sits on the task name's row.
    flag = body[body.rindex("<label", 0, body.index('name="is_priority"')):]
    flag = flag[: flag.index("</label>")]
    assert 'class="sr-only"' in flag and "High priority" in flag
    assert body.index('name="name"') < body.index('name="is_priority"') < body.index('name="description"')

    # Require review: a switch in a panel under Assignees, Reviewers inside it.
    assert 'class="toggle toggle-sm toggle-primary' in body
    assert body.index('name="assigned_members"') < body.index('name="requires_review"') < body.index('name="reviewers"')

    # One picker style: Kanban column is a plain select, and the member
    # pickers no longer draw a second chevron over the select's own arrow.
    assert '<select name="container"' in body
    assert "m19.5 8.25-7.5 7.5-7.5-7.5" not in body

    # The CRediT link sits on the Task type label line; the help text stays
    # on the page as the select's description.
    assert "CRediT roles" in body
    assert 'id="id_contribution_type_helptext"' in body


@pytest.mark.django_db
def test_reviewing_is_refused_on_a_task_that_asks_for_none(client, user, other_user):
    """No reviewers means no Reviewed column — and no endpoint behind it."""
    from apps.tasks.services import mark_own_done

    project, task, _lead, contrib = _two_assignee_task(user, other_user)
    mark_own_done(task=task, member=contrib)
    log = _current_log(task, contrib)

    client.force_login(user)  # a Lead, who could review if the task asked
    resp = client.post(
        _review_url(task, log), {"reviewed": "1"}, HTTP_HX_REQUEST="true"
    )
    assert resp.status_code == 403
    log.refresh_from_db()
    assert not log.is_reviewed


@pytest.mark.django_db
def test_a_task_with_no_reviewers_closes_without_one(client, user, other_user):
    """The done marks alone finish it, and the credit follows them."""
    from apps.tasks.services import close_task, mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    mark_own_done(task=task, member=lead)
    mark_own_done(task=task, member=contrib)

    close_task(task=task, closed_by=lead)
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.django_db
def test_a_standing_verdict_survives_the_last_reviewer_leaving(client, user, other_user):
    """Taking someone off the list does not unsay what they signed."""
    from apps.tasks.services import mark_reviewed

    project, task, lead, contrib = _reviewed_task(
        user, other_user, reviewer_is_lead=True
    )
    mark_reviewed(task_log=_current_log(task, contrib), reviewer=lead)
    task.reviewers.remove(lead)

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    # The verdict stays; the reviewer list and the buttons go.
    assert "data-review-cell" in pane
    assert f"by {lead.display_name}" in pane
    assert "Reviewers</h4>" not in pane
    assert MARK_NOT_REVIEWED not in pane


@pytest.mark.django_db
def test_closing_from_the_pane_takes_the_lead_actions_with_it(client, user):
    """The toggle is an HTMX swap, so the buttons have to move with it.

    Otherwise a Lead who just closed a task keeps looking at two buttons
    whose endpoints refuse, until something reloads the page.
    """
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Toggled",
    )
    client.force_login(user)
    edit_url = reverse(
        "tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk}
    )

    closed = client.post(
        reverse("tasks:close", kwargs={"pk": task.pk}),
        {"pane_mode": "1"},
        headers={"hx-request": "true"},
    ).content.decode()
    assert 'id="task-lead-actions"' in closed  # the swap reached them...
    assert edit_url not in closed  # ...and emptied them

    reopened = client.post(
        reverse("tasks:reopen", kwargs={"pk": task.pk}),
        {"pane_mode": "1"},
        headers={"hx-request": "true"},
    ).content.decode()
    assert edit_url in reopened


@pytest.mark.django_db
def test_the_task_page_names_who_closed_it(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Named",
    )
    client.force_login(user)
    url = reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk})

    # Nothing to name while it is open.
    assert "Closed by" not in client.get(url).content.decode()

    close_task(task=task, closed_by=member)
    task.refresh_from_db()
    assert task.closed_by_id == member.pk
    assert task.closed_at is not None

    body = client.get(url).content.decode()
    assert "Closed by" in body
    assert member.display_name in body


@pytest.mark.django_db
def test_closing_from_the_pane_names_the_closer_without_a_reload(client, user):
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Named",
    )
    client.force_login(user)

    closed = client.post(
        reverse("tasks:close", kwargs={"pk": task.pk}),
        {"pane_mode": "1"},
        headers={"hx-request": "true"},
    ).content.decode()
    assert "Closed by" in closed
    assert member.display_name in closed


@pytest.mark.django_db
def test_reopening_clears_who_closed_it(client, user):
    """The completion it named no longer stands."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Named",
    )
    close_task(task=task, closed_by=member)
    reopen_task(task=task)
    task.refresh_from_db()

    assert task.closed_by_id is None
    assert task.closed_at is None

    client.force_login(user)
    body = client.get(
        reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk})
    ).content.decode()
    assert "Closed by" not in body


@pytest.mark.django_db
def test_a_closer_whose_membership_is_gone_is_still_reported(client, user, other_user):
    """SET_NULL outlives the row; the page says so rather than going short."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    closer = _add_contributor(project, other_user)
    task = Task.objects.create(
        project=project, created_by=member, container=col1,
        container_task_order=0, name="Named",
    )
    close_task(task=task, closed_by=closer)
    closer.delete()
    task.refresh_from_db()
    assert task.closed_by_id is None
    assert task.status == TaskStatus.COMPLETED

    client.force_login(user)
    body = client.get(
        reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk})
    ).content.decode()
    assert "Closed by" in body
    assert "a former member" in body


@pytest.mark.django_db
def test_board_toolbar_groups_views_with_columns_and_ends_with_new_task(client, pm_user, user):
    """Left: the views. Right: Manage columns, Filter, then New task, the
    page's one primary action. A member sees no Manage columns or New task."""
    project, _col1, _col2 = _make_project_with_board(pm_user)
    url = reverse("tasks:board", kwargs={"project_pk": project.pk})

    client.force_login(pm_user)
    html = client.get(url).content.decode()
    toolbar = html[html.index("setView('board')") :]
    order = [
        toolbar.index(x)
        for x in ("Kanban", "List", "Table", "Calendar", "Manage columns", "New task")
    ]
    assert order == sorted(order)
    # The views share one segmented-control track; Manage columns sits after it.
    assert 'aria-label="Task views"' in html
    assert html.index('aria-label="Task views"') < html.index("setView('board')")

    _add_contributor(project, user)
    client.force_login(user)
    member = client.get(url).content.decode()
    # The buttons, not the words: the columns dialog's own aria-label still
    # says "Manage columns".
    assert "document.getElementById('columns-modal').showModal()" not in member
    create_url = reverse("tasks:create", kwargs={"project_pk": project.pk})
    assert f'hx-get="{create_url}"' not in member


@pytest.mark.django_db
def test_a_new_tasks_editor_saves_itself_when_closed_by_accident(client, user):
    """The create form carries the accidental-close save; an edit form does
    not. A save sent that way (autosave=1) creates the task like Save does
    and leaves a note for the board's reload."""
    project, col1, _col2 = _make_project_with_board(user)
    member = _primary_member(project, user)
    client.force_login(user)
    create_url = reverse("tasks:create", kwargs={"project_pk": project.pk})

    form = client.get(create_url, headers={"hx-request": "true"}).content.decode()
    assert "dialog.addEventListener('close'" in form
    assert "autosave: '1'" in form
    assert "dataset.discard = '1'" in form, "Cancel discards on purpose"

    task = Task.objects.create(
        project=project, created_by=member, container=col1, container_task_order=0,
        name="Existing",
    )
    edit = client.get(
        reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk}),
        headers={"hx-request": "true"},
    ).content.decode()
    assert "dialog.addEventListener('close'" not in edit

    response = client.post(
        create_url,
        {"name": "Saved on close", "container": col1.pk, "autosave": "1"},
        headers={"hx-request": "true"},
    )
    assert "task-created" in response["HX-Trigger"]
    assert Task.objects.filter(project=project, name="Saved on close").exists()
    board = client.get(reverse("tasks:board", kwargs={"project_pk": project.pk})).content.decode()
    assert "Task &#x27;Saved on close&#x27; was saved when its editor closed." in board


# ---------------------------------------------------------------------------
# A Lead verifies each member's contribution — one by one, or in a batch
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_matrix_has_a_verified_column_live_for_leads_only(client, user, other_user):
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    mark_own_done(task=task, member=contrib)

    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert "sortBy('verified')" in _rows(pane)
    row = _row_for(pane, contrib)
    assert 'data-verify-cell data-mark="pending"' in row
    assert MARK_VERIFIED in row and _verify_url(task, _current_log(task, contrib)) in row
    # The Lead's own part is not done, so there is nothing to verify yet.
    own = _row_for(pane, lead)
    assert 'data-verify-cell data-mark="none"' in own and MARK_VERIFIED not in own
    assert "data-verify-batch" in pane

    # An assignee sees the column, read-only, and no batch actions.
    client.force_login(other_user)
    pane = client.get(_pane_url(project, task)).content.decode()
    row = _row_for(pane, contrib)
    assert 'data-verify-cell data-mark="pending"' in row
    assert MARK_VERIFIED not in row and "Awaiting verification" in row
    assert "data-verify-batch" not in pane


@pytest.mark.django_db
def test_a_lead_verifies_and_unverifies_a_part(client, user, other_user):
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    log = mark_own_done(task=task, member=contrib)
    client.force_login(user)

    swapped = client.post(
        _verify_url(task, log), {"verified": "1", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    log.refresh_from_db()
    assert log.is_verified and log.verified_by == lead
    row = _row_for(swapped, contrib)
    assert 'data-verify-cell data-mark="yes"' in row
    assert f"by {lead.display_name}" in row[row.index("data-verify-cell") :]
    assert MARK_NOT_VERIFIED in row
    assert AuditLog.objects.filter(action=AuditAction.TASK_PART_VERIFIED).count() == 1

    client.post(_verify_url(task, log), {"verified": "0", "pane_mode": "1"}, HTTP_HX_REQUEST="true")
    log.refresh_from_db()
    assert not log.is_verified and log.verified_by is None
    assert AuditLog.objects.filter(action=AuditAction.TASK_PART_UNVERIFIED).count() == 1


@pytest.mark.django_db
def test_only_a_lead_can_verify(client, user, other_user):
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    log = mark_own_done(task=task, member=contrib)
    client.force_login(other_user)

    assert client.post(_verify_url(task, log), {"verified": "1"}, HTTP_HX_REQUEST="true").status_code == 403
    assert client.post(_verify_batch_url(task), {"scope": "done"}, HTTP_HX_REQUEST="true").status_code == 403
    log.refresh_from_db()
    assert not log.is_verified


@pytest.mark.django_db
def test_a_part_not_done_cannot_be_verified(client, user, other_user):
    project, task, lead, contrib = _two_assignee_task(user, other_user)
    log = _current_log(task, contrib)
    client.force_login(user)

    response = client.post(_verify_url(task, log), {"verified": "1"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 400
    log.refresh_from_db()
    assert not log.is_verified


@pytest.mark.django_db
def test_a_lead_verifies_on_a_completed_task(client, user, other_user):
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    log = mark_own_done(task=task, member=contrib)
    client.force_login(user)
    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))

    pane = client.get(_pane_url(project, task)).content.decode()
    assert MARK_VERIFIED in _row_for(pane, contrib)
    response = client.post(_verify_url(task, log), {"verified": "1"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    log.refresh_from_db()
    assert log.is_verified
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.django_db
def test_the_batch_buttons_count_and_verify_their_parts(client, user, other_user, pm_user):
    from apps.tasks.services import mark_own_done, mark_reviewed

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    third = _add_contributor(project, pm_user)
    task.assigned_members.add(third)
    task.reviewers.add(lead)
    mark_own_done(task=task, member=contrib)
    mark_own_done(task=task, member=third)
    mark_reviewed(task_log=_current_log(task, contrib), reviewer=lead)
    client.force_login(user)

    detail = reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk})
    for url in (_pane_url(project, task), detail):
        page = client.get(url).content.decode()
        batch = page[page.index("data-verify-batch") :]
        batch = batch[: batch.index("</div>")]
        assert "Verify all marked done (2)" in batch, url
        assert "Verify all marked reviewed (1)" in batch, url
        # Under the progress bars, which come first.
        assert page.index("data-progress") < page.index("data-verify-batch"), url
        assert ">0 of 3</span>" in page, url  # the Verified bar's count

    swapped = client.post(
        _verify_batch_url(task), {"scope": "reviewed", "pane_mode": "1"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    assert _current_log(task, contrib).is_verified
    assert not _current_log(task, third).is_verified
    assert "Verify all marked done (1)" in swapped
    assert "Verify all marked reviewed (0)" in swapped

    client.post(_verify_batch_url(task), {"scope": "done", "pane_mode": "1"}, HTTP_HX_REQUEST="true")
    assert _current_log(task, third).is_verified
    assert not _current_log(task, lead).is_verified  # never marked done

    refused = client.post(_verify_batch_url(task), {"scope": "all"}, HTTP_HX_REQUEST="true")
    assert refused.status_code == 400


@pytest.mark.django_db
def test_the_review_batch_button_needs_a_reviewed_task(client, user, other_user):
    project, task, _lead, _contrib = _two_assignee_task(user, other_user)
    client.force_login(user)
    pane = client.get(_pane_url(project, task)).content.decode()
    assert 'data-verify-scope="done"' in pane
    assert 'data-verify-scope="reviewed"' not in pane



@pytest.mark.django_db
def test_editing_the_type_carries_onto_verified_credit(client, user, other_user):
    """Credit can be written while a task is open, so changing its type moves
    the credit with it."""
    from apps.contributions.models import ContributionRecord
    from apps.core.enums import ContributionType
    from apps.tasks.services import mark_own_done, verify_part

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    project.contribution_tracking = ContributionTracking.CREDIT
    project.save(update_fields=["contribution_tracking"])
    verify_part(task_log=mark_own_done(task=task, member=contrib), lead=lead)
    assert ContributionRecord.objects.get().contribution_type == ContributionType.OTHER

    client.force_login(user)
    client.post(
        reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk}),
        _task_form_data(
            task.container,
            name=task.name,
            contribution_type=ContributionType.SOFTWARE,
            assigned_members=[lead.pk, contrib.pk],
        ),
    )
    task.refresh_from_db()
    assert task.contribution_type == ContributionType.SOFTWARE
    assert ContributionRecord.objects.get().contribution_type == ContributionType.SOFTWARE


@pytest.mark.django_db
def test_a_part_marked_before_completion_takes_no_review_until_remarked_or_reopened(client, user, other_user):
    """An unreviewed part already marked when the Lead completed the task
    takes no review. It opens again if its assignee undoes and redoes the
    mark (it is then marked after completion), or if a Lead reopens the task."""
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    task.reviewers.add(lead)
    mark_own_done(task=task, member=contrib)
    client.force_login(user)
    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))

    review = lambda: client.post(  # noqa: E731
        _review_url(task, _current_log(task, contrib)), {"reviewed": "1"}, HTTP_HX_REQUEST="true"
    )
    assert review().status_code == 403
    assert MARK_REVIEWED not in _row_for(client.get(_pane_url(project, task)).content.decode(), contrib)

    # The assignee redoes the mark: now it was marked after completion.
    client.force_login(other_user)
    client.post(reverse("tasks:undo", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")
    client.post(reverse("tasks:complete", kwargs={"pk": task.pk}), HTTP_HX_REQUEST="true")
    client.force_login(user)
    assert MARK_REVIEWED in _row_for(client.get(_pane_url(project, task)).content.decode(), contrib)
    assert review().status_code == 200
    assert _current_log(task, contrib).is_reviewed

    # And on another part marked before completion, reopening lifts it.
    other = _sole_assignee_task(user, other_user, name="Reopened")[1]
    mark_own_done(task=other, member=other.assigned_members.get())
    client.post(reverse("tasks:close", kwargs={"pk": other.pk}))
    part = other.task_logs.get()
    assert client.post(_review_url(other, part), {"reviewed": "1"}, HTTP_HX_REQUEST="true").status_code == 403
    client.post(reverse("tasks:reopen", kwargs={"pk": other.pk}))
    assert client.post(_review_url(other, part), {"reviewed": "1"}, HTTP_HX_REQUEST="true").status_code == 200


@pytest.mark.django_db
def test_verify_all_marked_done_works_on_a_completed_task(client, user, other_user):
    """The batch buttons stay live once the task is completed. A part marked
    before completion and left unreviewed — which can no longer be reviewed —
    is still verified by Verify all marked done, though not by Verify all
    marked reviewed."""
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    task.reviewers.add(lead)
    mark_own_done(task=task, member=contrib)
    client.force_login(user)
    client.post(reverse("tasks:close", kwargs={"pk": task.pk}))

    pane = client.get(_pane_url(project, task)).content.decode()
    assert "Verify all marked done (1)" in pane
    assert "Verify all marked reviewed (0)" in pane

    client.post(_verify_batch_url(task), {"scope": "reviewed", "pane_mode": "1"}, HTTP_HX_REQUEST="true")
    assert not _current_log(task, contrib).is_verified
    client.post(_verify_batch_url(task), {"scope": "done", "pane_mode": "1"}, HTTP_HX_REQUEST="true")
    assert _current_log(task, contrib).is_verified
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.django_db
def test_the_batch_buttons_confirm_in_the_standard_modal(client, user, other_user, pm_user):
    """No browser confirm(): each button opens a <dialog class="modal"> styled
    like the app's other modals, naming the members it would verify, and its
    Verify button posts the batch."""
    from apps.tasks.services import mark_own_done, mark_reviewed

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    third = _add_contributor(project, pm_user)
    task.assigned_members.add(third)
    task.reviewers.add(lead)
    mark_own_done(task=task, member=contrib)
    mark_own_done(task=task, member=third)
    mark_reviewed(task_log=_current_log(task, contrib), reviewer=lead)
    client.force_login(user)

    pane = client.get(_pane_url(project, task)).content.decode()
    assert "hx-confirm" not in pane[pane.index("data-verify-batch") :]
    for scope, members, absent in (
        ("done", (contrib, third), (lead,)),
        ("reviewed", (contrib,), (lead, third)),
    ):
        button = pane[pane.index(f'data-verify-scope="{scope}"') - 400 : pane.index(f'data-verify-scope="{scope}"')]
        assert f"verify-{scope}-modal-{task.pk}').showModal()" in button
        start = pane.index(f'<dialog id="verify-{scope}-modal-{task.pk}" class="modal"')
        modal = pane[start : pane.index("</dialog>", start)]
        assert 'class="modal-box"' in modal and 'class="modal-backdrop"' in modal
        assert "text-lg font-semibold text-brand-navy" in modal
        assert "btn bg-brand-coral" in modal and "btn btn-ghost" in modal
        listed = modal[modal.index("data-verify-modal-members") : modal.index("</ul>")]
        for member in members:
            assert member.display_name in listed
        for member in absent:
            assert member.display_name not in listed
        assert f"Verify {len(members)}" in modal
        assert reverse("tasks:verify_batch", kwargs={"pk": task.pk}) in modal
        assert f'name="scope" value="{scope}"' in modal

    # Nothing left to verify: the buttons switch off and no modal is drawn.
    client.post(_verify_batch_url(task), {"scope": "done", "pane_mode": "1"}, HTTP_HX_REQUEST="true")
    pane = client.get(_pane_url(project, task)).content.decode()
    assert "data-verify-modal" not in pane
    assert "Verify all marked done (0)" in pane


@pytest.mark.django_db
def test_verify_notes_say_what_verifying_means_for_credit(client, user, other_user):
    """The verify modal, and the Completion help, explain that on a CRediT
    project verifying is what records a member's contribution under the
    task's CRediT role; elsewhere it credits nothing."""
    from apps.core.enums import ContributionType
    from apps.tasks.services import mark_own_done

    project, task, lead, contrib = _two_assignee_task(user, other_user)
    mark_own_done(task=task, member=contrib)
    client.force_login(user)

    def note(html):
        start = html.index("data-verify-credit-note")
        return html[start : html.index("</div>", start)]

    pane = client.get(_pane_url(project, task)).content.decode()
    assert "No CRediT on this project" in note(pane)
    assert "no contribution is credited" in note(pane)

    project.contribution_tracking = ContributionTracking.CREDIT
    project.save(update_fields=["contribution_tracking"])
    task.contribution_type = ContributionType.SOFTWARE
    task.save(update_fields=["contribution_type"])
    pane = client.get(_pane_url(project, task)).content.decode()
    credit = note(pane)
    assert "What verifying means for CRediT" in credit
    assert "Software" in credit  # the task's CRediT role, by name
    assert "Nothing is credited until a part is verified" in credit
    assert "https://credit.niso.org/" in credit
    help_ = pane[pane.index("3. Verified.") : pane.index("4. Completed.")]
    assert "On this CRediT project, that is what credits them" in help_
    assert "(Software)" in help_



# ---------------------------------------------------------------------------
# Links in the description
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_web_addresses_in_the_description_are_links(client, user):
    """Linked the way comments link them — new tab, through the leaving
    dialog — on the page and in the pane, with paragraphs kept and
    everything else escaped."""
    project, col1, _col2 = _make_project_with_board(user)
    task = Task.objects.create(
        project=project,
        created_by=_primary_member(project, user),
        container=col1,
        container_task_order=0,
        name="Linked",
        description=(
            "Protocol: https://example.org/protocol?v=2&lang=en.\n"
            "Notes at www.example.org/notes\n\n"
            "<script>alert(1)</script> javascript:alert(1)"
        ),
    )
    client.force_login(user)
    for url in (
        reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk}),
        reverse("tasks:detail_pane", kwargs={"project_pk": project.pk, "pk": task.pk}),
    ):
        html = client.get(url).content.decode()
        body = html[html.index(f'id="task-description-{task.pk}"') :]
        body = body[body.index(">") + 1 : body.index("</div>")]
        assert (
            '<p>Protocol: <a href="https://example.org/protocol?v=2&amp;lang=en" '
            'class="comment-link text-brand-navy underline underline-offset-2 '
            'hover:text-brand-coral" target="_blank" rel="noopener noreferrer nofollow" '
            'data-external-link>https://example.org/protocol?v=2&amp;lang=en</a>.<br>'
        ) in body
        assert 'href="https://www.example.org/notes"' in body
        assert "<p>&lt;script&gt;alert(1)&lt;/script&gt; javascript:alert(1)</p>" in body
        assert body.count("<a ") == 2
