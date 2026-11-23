"""Tests for the bulk CSV task import feature."""

from __future__ import annotations

import io

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.core.enums import ContributionType, MemberPrivileges, ProjectStatus
from apps.projects.models import ProjectMember, ProjectPrivilege
from apps.projects.services import create_project_with_owner
from apps.tasks.import_csv import import_rows, parse_csv
from apps.tasks.models import KanbanBoard, Task, TaskLog


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_project(owner, *, name="P"):
    project = create_project_with_owner(
        user=owner,
        form_data={"name": name, "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    KanbanBoard.objects.create(project=project, name="Todo", container_order=0)
    return project


def _csv(text: str) -> io.StringIO:
    return io.StringIO(text)


# ---------------------------------------------------------------------------
# parse_csv
# ---------------------------------------------------------------------------


def test_parse_csv_valid_rows_no_errors():
    text = "name,description\nA,first\nB,second\n"
    rows, errors = parse_csv(_csv(text))
    assert errors == []
    assert rows == [
        {"name": "A", "description": "first"},
        {"name": "B", "description": "second"},
    ]


def test_parse_csv_missing_header():
    text = "title,description\nA,first\n"
    rows, errors = parse_csv(_csv(text))
    assert rows == []
    assert len(errors) == 1
    assert "name" in errors[0]


# ---------------------------------------------------------------------------
# import_rows
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_import_rows_creates_tasks(user):
    project = _make_project(user)
    rows = [
        {"name": "A"},
        {"name": "B", "description": "second"},
        {"name": "C", "deadline": "2026-06-01"},
    ]
    created, errors = import_rows(project, rows, user=user)
    assert created == 3
    assert errors == []
    assert Task.objects.filter(project=project).count() == 3


@pytest.mark.django_db
def test_import_rows_skips_blank_name(user):
    project = _make_project(user)
    rows = [
        {"name": "A"},
        {"name": ""},
        {"name": "C"},
    ]
    created, errors = import_rows(project, rows, user=user)
    assert created == 2
    assert any("row 3" in e and "name is required" in e for e in errors)
    assert Task.objects.filter(project=project).count() == 2


@pytest.mark.django_db
def test_import_rows_handles_unknown_column_creates_it(user):
    project = _make_project(user)
    columns = KanbanBoard.objects.filter(project=project)
    assert not columns.filter(name="Backlog").exists()
    columns_before = columns.count()
    rows = [{"name": "A", "column": "Backlog"}]
    created, errors = import_rows(project, rows, user=user)
    assert created == 1
    assert errors == []
    assert columns.filter(name="Backlog").exists()
    assert columns.count() == columns_before + 1
    task = Task.objects.get(project=project)
    assert task.container.name == "Backlog"


@pytest.mark.django_db
def test_import_rows_assigns_known_member_emails_only(user, other_user):
    project = _make_project(user)
    # Add other_user as a member of the project.
    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)

    rows = [
        {
            "name": "A",
            "assignee_emails": f"{other_user.email}, ghost@nowhere.test",
        }
    ]
    created, errors = import_rows(project, rows, user=user)
    assert created == 1
    # The unknown email must surface as a warning, not an error that stops
    # the import.
    assert any("ghost@nowhere.test" in e for e in errors)

    task = Task.objects.get(project=project)
    assigned_ids = list(task.assigned_members.values_list("id", flat=True))
    assert member.pk in assigned_ids
    assert len(assigned_ids) == 1
    # The known assignee should get a seeded TaskLog (mirrors TaskCreateView).
    assert TaskLog.objects.filter(task=task, assigned_to=member).exists()


@pytest.mark.django_db
def test_import_rows_contribution_type_fallback_to_other(user):
    """Unknown contribution type logs a warning but still creates the task."""
    project = _make_project(user)
    rows = [{"name": "A", "contribution_type": "NOT_A_REAL_TYPE"}]
    created, errors = import_rows(project, rows, user=user)
    assert created == 1
    task = Task.objects.get(project=project)
    assert task.contribution_type == ContributionType.OTHER
    assert any("NOT_A_REAL_TYPE" in e for e in errors)


# ---------------------------------------------------------------------------
# View-level permission
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_import_view_requires_pm(client, user, other_user):
    project = _make_project(user)
    # Add other_user as a plain CONTRIBUTOR.
    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project,
        user=other_user,
        privilege=MemberPrivileges.CONTRIBUTOR,
    )
    client.force_login(other_user)
    url = reverse("tasks:import", kwargs={"project_pk": project.pk})
    response = client.get(url)
    assert response.status_code == 403
