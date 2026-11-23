"""Tests for a member's own contribution type on a task.

A Lead can give one assignee a contribution type of their own
(``AssigneeContributionType``); the task's type still holds for everyone
else. Covers which type credit is recorded under, how credit already
written follows a change of either type, the assignment owning the entry,
and the Lead's endpoint and status block.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditAction, AuditLog
from apps.contributions.models import ContributionRecord
from apps.contributions.services import (
    add_contribution,
    sync_contribution_type,
    update_contribution,
)
from apps.core.enums import ContributionType, MemberPrivileges, ProjectStatus
from apps.notifications.models import Notification
from apps.projects.models import ContributionTracking, ProjectMember, ProjectPrivilege
from apps.projects.services import create_project_with_owner
from apps.tasks.models import AssigneeContributionType, KanbanBoard, Task
from apps.tasks.services import (
    close_task,
    mark_own_done,
    set_member_contribution_type,
    unverify_part,
    verify_part,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_user(email):
    return get_user_model().objects.create_user(
        email=email, password="password123", name=email.split("@")[0],
        email_confirmed_at=timezone.now(),
    )


def _setup(lead_user, *others, tracking=ContributionTracking.CREDIT,
           ctype=ContributionType.SOFTWARE):
    """A project led by ``lead_user`` with one task of type ``ctype``,
    assigned to a member for each of ``others``. Returns
    (project, task, lead_member, [members])."""
    project = create_project_with_owner(
        user=lead_user,
        form_data={
            "name": "Types",
            "status": ProjectStatus.ACTIVE,
            "is_public": True,
            "contribution_tracking": tracking,
        },
    )
    lead = ProjectMember.objects.filter(project=project, users=lead_user).first()
    column = KanbanBoard.objects.filter(project=project).first() or KanbanBoard.objects.create(
        project=project, name="Todo", container_order=0
    )
    task = Task.objects.create(
        project=project, created_by=lead, container=column,
        container_task_order=0, name="Write chapter", contribution_type=ctype,
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


def _verify(task, member, lead):
    log = mark_own_done(task=task, member=member)
    return verify_part(task_log=log, lead=lead)


def _type_of(user):
    return ContributionRecord.objects.get(user=user).contribution_type


def _post(client, task, **data):
    return client.post(
        reverse("tasks:member_type", kwargs={"pk": task.pk}),
        {"pane_mode": "1", **data},
        HTTP_HX_REQUEST="true",
    )


# ---------------------------------------------------------------------------
# Which type credit is recorded under
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_effective_type_is_the_members_own_else_the_tasks(pm_user, user, other_user):
    _p, task, lead, [alice, bob] = _setup(pm_user, user, other_user)
    set_member_contribution_type(
        task=task, member=alice, lead=lead,
        contribution_type=ContributionType.VISUALIZATION,
    )

    assert task.contribution_type_for(alice) == ContributionType.VISUALIZATION
    assert task.contribution_type_for(bob) == ContributionType.SOFTWARE
    assert task.contribution_type_for(None) == ContributionType.SOFTWARE


@pytest.mark.django_db
def test_verifying_credits_a_member_under_their_own_type(pm_user, user, other_user):
    _p, task, lead, [alice, bob] = _setup(pm_user, user, other_user)
    set_member_contribution_type(
        task=task, member=alice, lead=lead,
        contribution_type=ContributionType.VISUALIZATION,
    )

    _verify(task, alice, lead)
    _verify(task, bob, lead)

    assert _type_of(user) == ContributionType.VISUALIZATION
    # Bob has no type of his own: the task's still holds for him.
    assert _type_of(other_user) == ContributionType.SOFTWARE


@pytest.mark.django_db
def test_own_type_credits_every_user_behind_a_team(pm_user, user, other_user):
    project, task, lead, _ = _setup(pm_user)
    team = ProjectMember.objects.create(project=project, name="Team A")
    team.users.add(user, other_user)
    task.assigned_members.add(team)
    set_member_contribution_type(
        task=task, member=team, lead=lead,
        contribution_type=ContributionType.METHODOLOGY,
    )

    _verify(task, team, lead)

    assert set(
        ContributionRecord.objects.values_list("user_id", "contribution_type")
    ) == {
        (user.id, ContributionType.METHODOLOGY),
        (other_user.id, ContributionType.METHODOLOGY),
    }


@pytest.mark.django_db
def test_reverifying_after_unverify_uses_the_current_own_type(pm_user, user):
    _p, task, lead, [alice] = _setup(pm_user, user)
    log = _verify(task, alice, lead)
    unverify_part(task_log=log, actor=lead)
    set_member_contribution_type(
        task=task, member=alice, lead=lead,
        contribution_type=ContributionType.RESOURCES,
    )

    log.refresh_from_db()
    verify_part(task_log=log, lead=lead)

    assert _type_of(user) == ContributionType.RESOURCES


# ---------------------------------------------------------------------------
# Credit already written follows a change of type
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_changing_the_task_type_leaves_members_with_their_own(pm_user, user, other_user):
    _p, task, lead, [alice, bob] = _setup(pm_user, user, other_user)
    set_member_contribution_type(
        task=task, member=alice, lead=lead,
        contribution_type=ContributionType.SOFTWARE,  # pinned, same as the task's
    )
    _verify(task, alice, lead)
    _verify(task, bob, lead)

    task.contribution_type = ContributionType.FORMAL_ANALYSIS
    task.save()
    sync_contribution_type(task, old_type=ContributionType.SOFTWARE)

    # Alice's own type is kept even though it matched the task's before.
    assert _type_of(user) == ContributionType.SOFTWARE
    assert task.contribution_type_for(alice) == ContributionType.SOFTWARE
    assert _type_of(other_user) == ContributionType.FORMAL_ANALYSIS


@pytest.mark.django_db
def test_task_edit_form_keeps_own_types_on_credit(client, pm_user, user, other_user):
    _p, task, lead, [alice, bob] = _setup(pm_user, user, other_user)
    set_member_contribution_type(
        task=task, member=alice, lead=lead,
        contribution_type=ContributionType.VISUALIZATION,
    )
    _verify(task, alice, lead)
    _verify(task, bob, lead)
    client.force_login(pm_user)

    response = client.post(
        reverse("tasks:update", kwargs={"project_pk": task.project_id, "pk": task.pk}),
        {
            "name": task.name,
            "description": "",
            "container": task.container_id,
            "assigned_members": [alice.pk, bob.pk],
            "contribution_type": ContributionType.INVESTIGATION,
        },
    )

    assert response.status_code in (200, 302)
    task.refresh_from_db()
    assert task.contribution_type == ContributionType.INVESTIGATION
    assert _type_of(user) == ContributionType.VISUALIZATION
    assert _type_of(other_user) == ContributionType.INVESTIGATION


@pytest.mark.django_db
def test_setting_and_removing_an_own_type_moves_verified_credit(pm_user, user):
    _p, task, lead, [alice] = _setup(pm_user, user)
    _verify(task, alice, lead)
    assert _type_of(user) == ContributionType.SOFTWARE

    set_member_contribution_type(
        task=task, member=alice, lead=lead,
        contribution_type=ContributionType.VALIDATION,
    )
    assert _type_of(user) == ContributionType.VALIDATION

    set_member_contribution_type(
        task=task, member=alice, lead=lead,
        contribution_type=ContributionType.SUPERVISION,
    )
    assert _type_of(user) == ContributionType.SUPERVISION

    set_member_contribution_type(
        task=task, member=alice, lead=lead, contribution_type=None
    )
    assert _type_of(user) == ContributionType.SOFTWARE
    assert not AssigneeContributionType.objects.exists()


@pytest.mark.django_db
def test_own_type_leaves_hand_corrected_and_hand_entered_credit(pm_user, user, other_user):
    project, task, lead, [alice] = _setup(pm_user, user)
    _verify(task, alice, lead)
    corrected = ContributionRecord.objects.get(user=user)
    update_contribution(corrected, contribution_type=ContributionType.RESOURCES)
    extra = _new_user("carol@test.com")
    member = ProjectMember.objects.create(project=project)
    member.users.add(extra)
    task.assigned_members.add(member)
    by_hand = add_contribution(
        task=task, user=extra, contribution_type=ContributionType.SOFTWARE
    )

    set_member_contribution_type(
        task=task, member=alice, lead=lead,
        contribution_type=ContributionType.VALIDATION,
    )
    set_member_contribution_type(
        task=task, member=member, lead=lead,
        contribution_type=ContributionType.VALIDATION,
    )

    corrected.refresh_from_db()
    by_hand.refresh_from_db()
    assert corrected.contribution_type == ContributionType.RESOURCES
    assert by_hand.contribution_type == ContributionType.SOFTWARE


@pytest.mark.django_db
def test_setting_the_same_type_twice_writes_nothing_twice(pm_user, user):
    _p, task, lead, [alice] = _setup(pm_user, user)
    for _ in range(2):
        set_member_contribution_type(
            task=task, member=alice, lead=lead,
            contribution_type=ContributionType.VALIDATION,
        )
    set_member_contribution_type(task=task, member=alice, lead=lead, contribution_type=None)
    set_member_contribution_type(task=task, member=alice, lead=lead, contribution_type=None)

    actions = list(
        AuditLog.objects.filter(
            action__in=[
                AuditAction.TASK_MEMBER_TYPE_SET,
                AuditAction.TASK_MEMBER_TYPE_CLEARED,
            ]
        ).order_by("id").values_list("action", flat=True)
    )
    assert actions == [
        AuditAction.TASK_MEMBER_TYPE_SET,
        AuditAction.TASK_MEMBER_TYPE_CLEARED,
    ]


@pytest.mark.django_db
def test_member_is_told_their_type_changed(pm_user, user):
    _p, task, lead, [alice] = _setup(pm_user, user)
    set_member_contribution_type(
        task=task, member=alice, lead=lead,
        contribution_type=ContributionType.VALIDATION,
    )

    note = Notification.objects.filter(recipients=user).latest("id")
    assert "Validation" in note.message
    # The Lead who made the change is not told about it.
    assert not Notification.objects.filter(recipients=pm_user).exists()


# ---------------------------------------------------------------------------
# The entry belongs to the assignment
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_taking_a_member_off_the_task_drops_their_type(pm_user, user, other_user):
    _p, task, lead, [alice, bob] = _setup(pm_user, user, other_user)
    for member in (alice, bob):
        set_member_contribution_type(
            task=task, member=member, lead=lead,
            contribution_type=ContributionType.VALIDATION,
        )

    task.assigned_members.remove(alice)
    assert list(
        AssigneeContributionType.objects.values_list("member_id", flat=True)
    ) == [bob.pk]

    bob.assigned_tasks.clear()
    assert not AssigneeContributionType.objects.exists()


# ---------------------------------------------------------------------------
# The Lead's endpoint and the status block
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_lead_sets_a_members_type_from_the_status_block(client, pm_user, user):
    _p, task, _lead, [alice] = _setup(pm_user, user)
    client.force_login(pm_user)

    response = _post(
        client, task, member=alice.pk,
        contribution_type=ContributionType.VISUALIZATION,
    )

    assert response.status_code == 200
    entry = AssigneeContributionType.objects.get(task=task, member=alice)
    assert entry.contribution_type == ContributionType.VISUALIZATION
    content = response.content.decode()
    assert 'data-member-type="own"' in content
    # Another type than the task's, so it is shown under their name.
    assert "data-member-type-line" in content
    assert "Task type: Visualization</div>" in content
    assert "data-member-type-modal" in content


@pytest.mark.django_db
def test_empty_type_or_clear_puts_the_member_back_on_the_tasks(client, pm_user, user):
    _p, task, lead, [alice] = _setup(pm_user, user)
    client.force_login(pm_user)

    set_member_contribution_type(
        task=task, member=alice, lead=lead,
        contribution_type=ContributionType.VISUALIZATION,
    )
    assert _post(client, task, member=alice.pk, contribution_type="").status_code == 200
    assert not AssigneeContributionType.objects.exists()

    set_member_contribution_type(
        task=task, member=alice, lead=lead,
        contribution_type=ContributionType.VISUALIZATION,
    )
    response = _post(
        client, task, member=alice.pk, action="clear",
        contribution_type=ContributionType.VISUALIZATION,
    )
    assert response.status_code == 200
    assert not AssigneeContributionType.objects.exists()
    assert 'data-member-type="task"' in response.content.decode()


@pytest.mark.django_db
def test_lead_can_set_a_type_on_a_completed_task(client, pm_user, user):
    _p, task, lead, [alice] = _setup(pm_user, user)
    _verify(task, alice, lead)
    close_task(task=task, closed_by=lead)
    client.force_login(pm_user)

    response = _post(
        client, task, member=alice.pk,
        contribution_type=ContributionType.VALIDATION,
    )

    assert response.status_code == 200
    assert _type_of(user) == ContributionType.VALIDATION


@pytest.mark.django_db
def test_only_a_lead_sets_a_members_type(client, pm_user, user):
    _p, task, _lead, [alice] = _setup(pm_user, user)
    client.force_login(user)

    response = _post(
        client, task, member=alice.pk,
        contribution_type=ContributionType.VISUALIZATION,
    )

    assert response.status_code == 403
    assert not AssigneeContributionType.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "tracking, member_ok, value",
    [
        (ContributionTracking.NONE, True, ContributionType.VISUALIZATION),
        (ContributionTracking.CREDIT, False, ContributionType.VISUALIZATION),
        (ContributionTracking.CREDIT, True, "NOT_A_TYPE"),
    ],
)
def test_member_type_refusals(client, pm_user, user, other_user, tracking, member_ok, value):
    project, task, _lead, [alice] = _setup(pm_user, user, tracking=tracking)
    stranger = ProjectMember.objects.create(project=project)
    stranger.users.add(other_user)
    client.force_login(pm_user)

    response = _post(
        client, task, member=(alice if member_ok else stranger).pk,
        contribution_type=value,
    )

    assert response.status_code == 400
    assert not AssigneeContributionType.objects.exists()


@pytest.mark.django_db
def test_status_block_offers_the_type_control_to_leads_only(client, pm_user, user):
    _p, task, _lead, [alice] = _setup(pm_user, user)
    url = reverse("tasks:status_block", kwargs={"pk": task.pk}) + "?pane=1"

    client.force_login(pm_user)
    content = client.get(url, HTTP_HX_REQUEST="true").content.decode()
    assert f'data-set-type="{alice.pk}"' in content
    assert 'data-member-type="task"' in content

    client.force_login(user)
    content = client.get(url, HTTP_HX_REQUEST="true").content.decode()
    assert "data-set-type" not in content
    assert "data-member-type-modal" not in content


@pytest.mark.django_db
def test_status_block_hides_types_off_credit_projects(client, pm_user, user):
    _p, task, _lead, [alice] = _setup(pm_user, user, tracking=ContributionTracking.NONE)
    client.force_login(pm_user)

    content = client.get(
        reverse("tasks:status_block", kwargs={"pk": task.pk}) + "?pane=1",
        HTTP_HX_REQUEST="true",
    ).content.decode()

    assert "data-member-type" not in content
    assert "data-set-type" not in content


@pytest.mark.django_db
@pytest.mark.parametrize("route", ["tasks:detail", "tasks:detail_pane"])
def test_first_render_carries_the_type_dialog(client, pm_user, user, route):
    """The task page and the pane include the block with its context spelled
    out, so the dialog has to be handed through there too, not only on a
    swap."""
    _p, task, _lead, [alice] = _setup(pm_user, user)
    client.force_login(pm_user)

    content = client.get(
        reverse(route, kwargs={"project_pk": task.project_id, "pk": task.pk})
    ).content.decode()

    assert f'data-set-type="{alice.pk}"' in content
    assert "data-member-type-modal" in content
    assert "Software" in content
