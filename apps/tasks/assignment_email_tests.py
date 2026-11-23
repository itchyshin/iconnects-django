"""Tests for telling members, in-app and by email, that they were assigned a task."""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from kombu.exceptions import OperationalError

from apps.core.enums import ProjectStatus
from apps.notifications.models import Notification
from apps.projects.models import ProjectMember
from apps.projects.services import create_project_with_owner
from apps.tasks.import_csv import import_rows
from apps.tasks.models import KanbanBoard, Task
from apps.tasks.services import notify_new_assignees
from apps.tasks.tasks import send_task_assigned_email_task


@pytest.fixture(autouse=True)
def _site_url(settings):
    settings.SITE_URL = "https://iconnects.test"


def _make_project(owner, *, name="Coral reefs"):
    project = create_project_with_owner(
        user=owner,
        form_data={"name": name, "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    project.containers.all().delete()
    column = KanbanBoard.objects.create(project=project, name="Todo", container_order=0)
    return project, column


def _primary_member(project, user):
    return ProjectMember.objects.filter(project=project, users=user).first()


def _member(project, *users, name=None):
    member = ProjectMember.objects.create(project=project, name=name)
    member.users.add(*users)
    return member


def _task(project, column, *, name="Screen abstracts", **kwargs):
    return Task.objects.create(
        project=project,
        created_by=project.project_members.order_by("id").first(),
        container=column,
        container_task_order=0,
        name=name,
        **kwargs,
    )


def _assignment_notifications(user):
    return Notification.objects.for_user(user).filter(
        message__startswith="Task assigned to you"
    )


def _make_user(email, name):
    return get_user_model().objects.create_user(
        email=email,
        password="password123",
        name=name,
        email_confirmed_at=timezone.now(),
    )


# ---------------------------------------------------------------------------
# Change assignees
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_assign_view_emails_only_newly_added_members(
    client, user, other_user, pm_user, django_capture_on_commit_callbacks
):
    project, column = _make_project(user)
    lead = _primary_member(project, user)
    bob = _member(project, other_user)
    pm = _member(project, pm_user)
    task = _task(
        project,
        column,
        deadline=timezone.make_aware(datetime(2026, 11, 20, 17, 0)),
    )
    task.assigned_members.add(bob)

    client.force_login(user)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            reverse("tasks:assign", kwargs={"pk": task.pk}),
            {"assigned_members": [lead.pk, bob.pk, pm.pk]},
            HTTP_HX_REQUEST="true",
        )
    assert response.status_code == 200

    # Bob was already assigned and the Lead assigned themselves: only PM hears.
    assert [m.to for m in mail.outbox] == [["pm@test.com"]]
    message = mail.outbox[0]
    assert message.subject == 'i-connects: you were assigned "Screen abstracts"'
    url = "https://iconnects.test" + reverse(
        "tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk}
    )
    assert url in message.body
    assert "Coral reefs" in message.body
    assert "Nov 20, 2026" in message.body
    assert "Alice assigned you a task" in message.body
    html = message.alternatives[0][0]
    assert f'href="{url}"' in html

    assert _assignment_notifications(pm_user).count() == 1
    assert not _assignment_notifications(other_user).exists()
    assert not _assignment_notifications(user).exists()


@pytest.mark.django_db
def test_reassigning_the_same_members_sends_nothing(
    client, user, other_user, django_capture_on_commit_callbacks
):
    project, column = _make_project(user)
    bob = _member(project, other_user)
    task = _task(project, column)
    task.assigned_members.add(bob)

    client.force_login(user)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            reverse("tasks:assign", kwargs={"pk": task.pk}),
            {"assigned_members": [bob.pk]},
            HTTP_HX_REQUEST="true",
        )
    assert mail.outbox == []
    assert not _assignment_notifications(other_user).exists()


@pytest.mark.django_db
def test_a_team_member_emails_each_of_its_users(
    client, user, other_user, pm_user, django_capture_on_commit_callbacks
):
    project, column = _make_project(user)
    team = _member(project, other_user, pm_user, name="Extraction team")
    task = _task(project, column)

    client.force_login(user)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            reverse("tasks:assign", kwargs={"pk": task.pk}),
            {"assigned_members": [team.pk]},
            HTTP_HX_REQUEST="true",
        )
    assert sorted(m.to[0] for m in mail.outbox) == ["bob@test.com", "pm@test.com"]
    notification = Notification.objects.get(message__startswith="Task assigned")
    assert set(notification.recipients.values_list("pk", flat=True)) == {
        other_user.pk,
        pm_user.pk,
    }


@pytest.mark.django_db
def test_opting_out_stops_the_email_but_not_the_bell(
    client, user, other_user, django_capture_on_commit_callbacks
):
    other_user.email_task_assigned = False
    other_user.save(update_fields=["email_task_assigned"])
    project, column = _make_project(user)
    bob = _member(project, other_user)
    task = _task(project, column)

    client.force_login(user)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            reverse("tasks:assign", kwargs={"pk": task.pk}),
            {"assigned_members": [bob.pk]},
            HTTP_HX_REQUEST="true",
        )
    assert mail.outbox == []
    assert _assignment_notifications(other_user).count() == 1


# ---------------------------------------------------------------------------
# Task form: create and edit
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_creating_a_task_emails_its_assignees_but_not_the_lead(
    client, user, other_user, django_capture_on_commit_callbacks
):
    project, column = _make_project(user)
    lead = _primary_member(project, user)
    bob = _member(project, other_user)

    client.force_login(user)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            reverse("tasks:create", kwargs={"project_pk": project.pk}),
            {
                "name": "Extract effect sizes",
                "container": column.pk,
                "contribution_type": "OTHER",
                "assigned_members": [lead.pk, bob.pk],
            },
        )
    assert response.status_code in (200, 302)
    assert [m.to for m in mail.outbox] == [["bob@test.com"]]
    assert "Extract effect sizes" in mail.outbox[0].subject
    assert _assignment_notifications(other_user).count() == 1
    assert not _assignment_notifications(user).exists()


@pytest.mark.django_db
def test_editing_a_task_emails_only_assignees_the_edit_adds(
    client, user, other_user, pm_user, django_capture_on_commit_callbacks
):
    project, column = _make_project(user)
    bob = _member(project, other_user)
    pm = _member(project, pm_user)
    task = _task(project, column)
    task.assigned_members.add(bob)
    url = reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk})
    data = {"name": "Renamed", "container": column.pk, "contribution_type": "OTHER"}

    client.force_login(user)
    # A rename alone tells nobody.
    with django_capture_on_commit_callbacks(execute=True):
        client.post(url, {**data, "assigned_members": [bob.pk]})
    assert mail.outbox == []

    with django_capture_on_commit_callbacks(execute=True):
        client.post(url, {**data, "assigned_members": [bob.pk, pm.pk]})
    assert [m.to for m in mail.outbox] == [["pm@test.com"]]
    assert not _assignment_notifications(other_user).exists()


@pytest.mark.django_db
def test_adding_a_reviewer_sends_no_assignment_email(
    client, user, other_user, django_capture_on_commit_callbacks
):
    project, column = _make_project(user)
    bob = _member(project, other_user)
    task = _task(project, column)

    client.force_login(user)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk}),
            {
                "name": task.name,
                "container": column.pk,
                "contribution_type": "OTHER",
                "requires_review": "on",
                "reviewers": [bob.pk],
            },
        )
    assert task.reviewers.filter(pk=bob.pk).exists()
    assert mail.outbox == []


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_csv_import_sends_one_email_per_person_listing_their_tasks(
    user, other_user, pm_user, django_capture_on_commit_callbacks
):
    project, _column = _make_project(user)
    _member(project, other_user)
    _member(project, pm_user)
    rows = [
        {"name": "Task A", "assignee_emails": "bob@test.com"},
        {"name": "Task B", "assignee_emails": "bob@test.com; pm@test.com"},
        {"name": "Task C", "assignee_emails": "bob@test.com, alice@test.com"},
        {"name": "Task D"},
    ]
    with django_capture_on_commit_callbacks(execute=True):
        created, errors = import_rows(project, rows, user=user)
    assert (created, errors) == (4, [])

    by_recipient = {m.to[0]: m for m in mail.outbox}
    assert sorted(by_recipient) == ["bob@test.com", "pm@test.com"]
    bob_email = by_recipient["bob@test.com"]
    assert bob_email.subject == "i-connects: you were assigned 3 tasks"
    for name in ("Task A", "Task B", "Task C"):
        assert name in bob_email.body
    assert by_recipient["pm@test.com"].subject == (
        'i-connects: you were assigned "Task B"'
    )
    # The bell still gets one entry per task.
    assert _assignment_notifications(other_user).count() == 3
    assert not _assignment_notifications(user).exists()


@pytest.mark.django_db
def test_a_failed_csv_import_tells_nobody(
    user, other_user, django_capture_on_commit_callbacks, monkeypatch
):
    project, _column = _make_project(user)
    _member(project, other_user)

    def boom(*args, **kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr("apps.tasks.import_csv.log_action", boom)
    with django_capture_on_commit_callbacks(execute=True):
        created, errors = import_rows(
            project, [{"name": "A", "assignee_emails": "bob@test.com"}], user=user
        )
    assert created == 0
    assert mail.outbox == []
    assert not Notification.objects.exists()


# ---------------------------------------------------------------------------
# Broker down
# ---------------------------------------------------------------------------


def _broker_down(monkeypatch):
    """Make queueing the assignment email fail the way an unreachable Redis does."""

    def boom(*args, **kwargs):
        raise OperationalError("broker unreachable")

    monkeypatch.setattr(send_task_assigned_email_task, "delay", boom)


@pytest.fixture
def django_log(caplog):
    """``caplog`` for the "django" logger, which does not propagate to root."""
    logger = logging.getLogger("django")
    logger.addHandler(caplog.handler)
    yield caplog
    logger.removeHandler(caplog.handler)


def _create_task_for(client, project, column, member):
    return client.post(
        reverse("tasks:create", kwargs={"project_pk": project.pk}),
        {
            "name": "Extract effect sizes",
            "container": column.pk,
            "contribution_type": "OTHER",
            "assigned_members": [member.pk],
        },
    )


@pytest.mark.django_db
def test_a_broker_outage_after_commit_does_not_fail_the_request(
    client, user, other_user, django_capture_on_commit_callbacks, monkeypatch, django_log
):
    project, column = _make_project(user)
    bob = _member(project, other_user)
    _broker_down(monkeypatch)

    client.force_login(user)
    with django_capture_on_commit_callbacks(execute=True):
        response = _create_task_for(client, project, column, bob)

    assert response.status_code in (200, 302)
    assert Task.objects.filter(name="Extract effect sizes").exists()
    assert _assignment_notifications(other_user).count() == 1
    assert mail.outbox == []
    assert "in on_commit()" in django_log.text
    assert "broker unreachable" in django_log.text


@pytest.mark.django_db(transaction=True)
def test_a_broker_outage_outside_a_transaction_does_not_fail_the_request(
    client, user, other_user, monkeypatch, django_log
):
    # No ATOMIC_REQUESTS: on_commit runs the callback immediately, which is
    # the path production requests take.
    project, column = _make_project(user)
    bob = _member(project, other_user)
    _broker_down(monkeypatch)

    client.force_login(user)
    response = _create_task_for(client, project, column, bob)

    assert response.status_code in (200, 302)
    assert Task.objects.filter(name="Extract effect sizes").exists()
    assert mail.outbox == []
    assert "in on_commit()" in django_log.text
    assert "broker unreachable" in django_log.text


# ---------------------------------------------------------------------------
# The email task itself
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_email_for_a_task_unassigned_before_it_was_sent(
    user, other_user, django_capture_on_commit_callbacks
):
    project, column = _make_project(user)
    bob = _member(project, other_user)
    task = _task(project, column)
    task.assigned_members.add(bob)

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        notify_new_assignees([(task, {bob.pk})], assigned_by=user)
    task.assigned_members.remove(bob)
    for callback in callbacks:
        callback()
    assert mail.outbox == []


@pytest.mark.django_db
def test_task_names_are_not_html_escaped_in_the_plain_text(
    user, other_user, django_capture_on_commit_callbacks
):
    project, column = _make_project(user, name="Fish & frogs")
    bob = _member(project, other_user)
    task = _task(project, column, name="Q&A <draft>")
    task.assigned_members.add(bob)

    with django_capture_on_commit_callbacks(execute=True):
        notify_new_assignees([(task, {bob.pk})], assigned_by=user)
    message = mail.outbox[0]
    assert message.subject == 'i-connects: you were assigned "Q&A <draft>"'
    assert "Q&A <draft> (Fish & frogs)" in message.body
    assert "Q&amp;A &lt;draft&gt;" in message.alternatives[0][0]


# ---------------------------------------------------------------------------
# The preference
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_assignment_email_is_on_by_default():
    assert _make_user("new@test.com", "New").email_task_assigned is True


@pytest.mark.django_db
def test_profile_form_turns_the_assignment_email_off(client_logged_in, user):
    url = reverse("accounts:profile")
    page = client_logged_in.get(url).content.decode()
    assert "Email me when I&#x27;m assigned a task" in page

    response = client_logged_in.post(
        url,
        {
            "family_name": "Smith",
            "email_project_activity_frequency": "NEVER",
            "email_overdue_task_frequency": "NEVER",
            # Unticked checkboxes are simply absent from the POST.
        },
    )
    assert response.status_code in (200, 302)
    user.refresh_from_db()
    assert user.email_task_assigned is False
