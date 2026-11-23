"""Tests for the contributions ledger.

Covers the verification wiring in ``tasks.services.verify_part`` — who
earns credit and who does not — its withdrawal, and the matrix and CSV
helpers behind the contributions pages.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.audit.models import AuditAction, AuditLog
from apps.contributions.forms import ContributionAddForm
from apps.contributions.models import ContributionRecord
from apps.contributions.services import (
    add_contribution,
    sync_contribution_type,
    user_display_name,
    get_project_contributions_matrix,
    get_user_contributions,
)
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
from apps.tasks.models import KanbanBoard, Task
from apps.tasks.services import (
    approve_completion,
    clear_review,
    close_task,
    complete_task,
    mark_own_done,
    mark_reviewed,
    reopen_own,
    reopen_task,
    unverify_part,
    verify_part,
    verify_parts,
    VerifyScope,
)


def _make_project(owner, *, name="P", tracking=ContributionTracking.CREDIT):
    project = create_project_with_owner(
        user=owner,
        form_data={
            "name": name,
            "status": ProjectStatus.ACTIVE,
            "is_public": True,
            "contribution_tracking": tracking,
        },
    )
    col = KanbanBoard.objects.create(project=project, name="Todo", container_order=0)
    member = ProjectMember.objects.filter(project=project, users=owner).first()
    return project, col, member


def _make_task(project, col, pm_member, *, assignees=None, reviewers=None,
               ctype=ContributionType.OTHER):
    task = Task.objects.create(
        project=project,
        created_by=pm_member,
        container=col,
        container_task_order=0,
        name="T",
        contribution_type=ctype,
    )
    for member in assignees or []:
        task.assigned_members.add(member)
    for member in reviewers or []:
        task.reviewers.add(member)
    return task


def _member_for(project, user, privilege=MemberPrivileges.CONTRIBUTOR):
    """Add ``user`` to ``project`` the way a real join does: a membership
    row plus the privilege the section pages read access off."""
    member = ProjectMember.objects.create(project=project)
    member.users.add(user)
    ProjectPrivilege.objects.create(
        project=project, user=user, privilege=privilege
    )
    return member


# ---------------------------------------------------------------------------
# A Lead verifying a part is the ledger event
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_verifying_credits_the_member_whose_part_it_is(user, other_user):
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    task = _make_task(
        project, col, lead, assignees=[alice], ctype=ContributionType.DATA_CURATION
    )
    log = mark_own_done(task=task, member=alice)
    assert ContributionRecord.objects.count() == 0

    verify_part(task_log=log, lead=lead)

    record = ContributionRecord.objects.get(user=other_user)
    assert record.task_id == task.id
    assert record.task_log_id == log.id
    assert record.project_id == project.id
    assert record.contribution_type == ContributionType.DATA_CURATION
    assert record.verified_by_id == user.id
    log.refresh_from_db()
    assert record.closed_at == log.verified_at


@pytest.mark.django_db
def test_closing_a_task_writes_no_credit(user):
    """Closing is the Lead's call about the task; credit waits on verifying."""
    project, col, lead = _make_project(user)
    task = _make_task(project, col, lead, assignees=[lead])
    mark_own_done(task=task, member=lead)

    close_task(task=task, closed_by=lead)
    assert ContributionRecord.objects.count() == 0


@pytest.mark.django_db
def test_verifying_needs_no_review(user, other_user):
    """A Lead may verify a part the reviewers have not reached."""
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    task = _make_task(project, col, lead, assignees=[alice], reviewers=[lead])
    log = mark_own_done(task=task, member=alice)
    assert not log.is_reviewed

    verify_part(task_log=log, lead=lead)
    assert ContributionRecord.objects.filter(user=other_user).exists()


@pytest.mark.django_db
def test_a_review_alone_writes_no_credit(user, other_user):
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    task = _make_task(project, col, lead, assignees=[alice], reviewers=[lead])
    log = mark_own_done(task=task, member=alice)

    mark_reviewed(task_log=log, reviewer=lead)
    assert ContributionRecord.objects.count() == 0


@pytest.mark.django_db
def test_verifying_credits_every_user_behind_a_team_assignee(user, other_user):
    project, col, pm_member = _make_project(user)
    team = ProjectMember.objects.create(project=project, name="Team A")
    team.users.add(user, other_user)
    task = _make_task(project, col, pm_member, assignees=[team])
    log = mark_own_done(task=task, member=team)

    verify_part(task_log=log, lead=pm_member)

    assert set(ContributionRecord.objects.values_list("user_id", flat=True)) == {
        user.id,
        other_user.id,
    }


@pytest.mark.django_db
def test_verifying_twice_writes_one_record(user):
    project, col, pm_member = _make_project(user)
    task = _make_task(project, col, pm_member, assignees=[pm_member])
    log = mark_own_done(task=task, member=pm_member)

    verify_part(task_log=log, lead=pm_member)
    verify_part(task_log=log, lead=pm_member)
    verify_parts(task=task, lead=pm_member, scope=VerifyScope.DONE)

    assert ContributionRecord.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_a_project_not_tracking_credit_keeps_no_ledger(user):
    project, col, pm_member = _make_project(user, tracking=ContributionTracking.NONE)
    task = _make_task(project, col, pm_member, assignees=[pm_member])
    log = mark_own_done(task=task, member=pm_member)

    verify_part(task_log=log, lead=pm_member)
    log.refresh_from_db()
    assert log.is_verified  # the Lead's check still stands
    assert ContributionRecord.objects.count() == 0


@pytest.mark.django_db
def test_verifying_on_a_closed_task_credits_the_member(user, other_user):
    """A part marked after the Lead closed the task is still verified as usual."""
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    task = _make_task(project, col, lead, assignees=[alice])
    close_task(task=task, closed_by=lead)
    log = mark_own_done(task=task, member=alice)

    verify_part(task_log=log, lead=lead)
    assert ContributionRecord.objects.filter(user=other_user).exists()
    task.refresh_from_db()
    assert task.status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# Batch verification
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_verifying_all_done_credits_every_part_marked_done(user, other_user, pm_user):
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    bob = _member_for(project, pm_user)
    task = _make_task(project, col, lead, assignees=[alice, bob, lead])
    mark_own_done(task=task, member=alice)
    mark_own_done(task=task, member=bob)  # the Lead's own part is not done

    verified = verify_parts(task=task, lead=lead, scope=VerifyScope.DONE)

    assert {log.assigned_to_id for log in verified} == {alice.pk, bob.pk}
    assert set(ContributionRecord.objects.values_list("user_id", flat=True)) == {
        other_user.id,
        pm_user.id,
    }


@pytest.mark.django_db
def test_verifying_all_reviewed_skips_parts_not_reviewed(user, other_user, pm_user):
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    bob = _member_for(project, pm_user)
    task = _make_task(project, col, lead, assignees=[alice, bob], reviewers=[lead])
    alice_log = mark_own_done(task=task, member=alice)
    mark_own_done(task=task, member=bob)
    mark_reviewed(task_log=alice_log, reviewer=lead)

    verified = verify_parts(task=task, lead=lead, scope=VerifyScope.REVIEWED)

    assert [log.pk for log in verified] == [alice_log.pk]
    assert set(ContributionRecord.objects.values_list("user_id", flat=True)) == {
        other_user.id
    }


@pytest.mark.django_db
def test_a_batch_writes_an_audit_row_per_part_naming_the_batch(user, other_user, pm_user):
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    bob = _member_for(project, pm_user)
    task = _make_task(project, col, lead, assignees=[alice, bob])
    mark_own_done(task=task, member=alice)
    mark_own_done(task=task, member=bob)

    verify_parts(task=task, lead=lead, scope=VerifyScope.DONE)

    rows = AuditLog.objects.filter(action=AuditAction.TASK_PART_VERIFIED)
    assert rows.count() == 2
    assert {row.metadata["assignee_member_id"] for row in rows} == {alice.pk, bob.pk}
    assert all(row.metadata["batch"] == "done" for row in rows)
    assert all(row.actor_id == user.pk for row in rows)


@pytest.mark.django_db
def test_an_unknown_batch_scope_is_refused(user):
    project, col, lead = _make_project(user)
    task = _make_task(project, col, lead, assignees=[lead])
    with pytest.raises(ValueError):
        verify_parts(task=task, lead=lead, scope="everything")


# ---------------------------------------------------------------------------
# Taking the verification back takes the credit with it
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_withdrawing_a_verification_withdraws_the_credit(user, other_user):
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    task = _make_task(project, col, lead, assignees=[alice])
    log = mark_own_done(task=task, member=alice)
    verify_part(task_log=log, lead=lead)
    assert ContributionRecord.objects.count() == 1

    unverify_part(task_log=log, actor=lead)

    assert ContributionRecord.objects.count() == 0
    log.refresh_from_db()
    assert not log.is_verified and log.verified_by_id is None
    assert AuditLog.objects.filter(action=AuditAction.TASK_PART_UNVERIFIED).count() == 1


@pytest.mark.django_db
def test_undoing_a_done_mark_clears_the_verification_and_its_credit(user, other_user):
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    task = _make_task(project, col, lead, assignees=[alice])
    log = mark_own_done(task=task, member=alice)
    verify_part(task_log=log, lead=lead)

    reopen_own(task=task, member=alice)

    assert ContributionRecord.objects.count() == 0
    log.refresh_from_db()
    assert not log.is_verified
    row = AuditLog.objects.get(action=AuditAction.TASK_PART_UNVERIFIED)
    assert row.metadata["reason"] == "the done mark it stood on was reverted"

    # Marking done again brings the part back unverified.
    log = mark_own_done(task=task, member=alice)
    assert not log.is_verified
    assert ContributionRecord.objects.count() == 0


@pytest.mark.django_db
def test_withdrawing_a_review_leaves_the_verification(user, other_user):
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    task = _make_task(project, col, lead, assignees=[alice], reviewers=[lead])
    log = mark_own_done(task=task, member=alice)
    mark_reviewed(task_log=log, reviewer=lead)
    verify_part(task_log=log, lead=lead)

    clear_review(task_log=log, cleared_by=lead)

    log.refresh_from_db()
    assert log.is_verified
    assert ContributionRecord.objects.count() == 1


@pytest.mark.django_db
def test_reopening_a_task_keeps_the_credit(user):
    """Credit stands on the verification, not on the task's status."""
    project, col, pm_member = _make_project(user)
    task = _make_task(project, col, pm_member, assignees=[pm_member])
    log = mark_own_done(task=task, member=pm_member)
    verify_part(task_log=log, lead=pm_member)
    close_task(task=task, closed_by=pm_member)

    reopen_task(task=task)
    assert ContributionRecord.objects.count() == 1


@pytest.mark.django_db
def test_changing_the_tasks_type_moves_its_credit_but_not_a_correction(user, other_user, pm_user):
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    bob = _member_for(project, pm_user)
    task = _make_task(project, col, lead, assignees=[alice, bob])
    for member in (alice, bob):
        verify_part(task_log=mark_own_done(task=task, member=member), lead=lead)
    corrected = ContributionRecord.objects.get(user=pm_user)
    corrected.contribution_type = ContributionType.SOFTWARE
    corrected.save(update_fields=["contribution_type"])

    task.contribution_type = ContributionType.DATA_CURATION
    task.save(update_fields=["contribution_type"])
    sync_contribution_type(task, old_type=ContributionType.OTHER)

    assert ContributionRecord.objects.get(user=other_user).contribution_type == (
        ContributionType.DATA_CURATION
    )
    corrected.refresh_from_db()
    assert corrected.contribution_type == ContributionType.SOFTWARE


# ---------------------------------------------------------------------------
# Approving a submission is not a ledger event
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_approving_a_submission_writes_no_contribution(user):
    project, col, pm_member = _make_project(user)
    task = _make_task(project, col, pm_member, assignees=[pm_member])
    log = complete_task(task=task, completed_by=pm_member)

    approve_completion(task_log=log, approved=True, verifier_user=user)
    assert ContributionRecord.objects.count() == 0


# ---------------------------------------------------------------------------
# Deleting the work deletes the credit recorded against it
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_deleting_a_task_takes_its_contributions_with_it(user):
    project, col, pm_member = _make_project(user)
    task = _make_task(project, col, pm_member, assignees=[pm_member])
    verify_part(task_log=mark_own_done(task=task, member=pm_member), lead=pm_member)
    assert ContributionRecord.objects.count() == 1

    task.delete()
    assert ContributionRecord.objects.count() == 0


@pytest.mark.django_db
def test_deleting_a_project_takes_its_contributions_with_it(user):
    project, col, pm_member = _make_project(user)
    task = _make_task(project, col, pm_member, assignees=[pm_member])
    verify_part(task_log=mark_own_done(task=task, member=pm_member), lead=pm_member)

    project.delete()
    assert ContributionRecord.objects.count() == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _credit(project, col, pm_member, member, ctype):
    """Verify a one-assignee task's part so ``member`` earns one ``ctype``
    record."""
    task = _make_task(project, col, pm_member, assignees=[member], ctype=ctype)
    verify_part(task_log=mark_own_done(task=task, member=member), lead=pm_member)
    return task


@pytest.mark.django_db
def test_project_contributions_matrix(user, other_user):
    project, col, pm_member = _make_project(user)
    other_member = _member_for(project, other_user)

    for _ in range(2):
        _credit(project, col, pm_member, pm_member, ContributionType.WRITING_ORIGINAL_DRAFT)
    _credit(project, col, pm_member, other_member, ContributionType.DATA_CURATION)

    matrix = get_project_contributions_matrix(project)
    assert matrix["totals"]["grand_total"] == 3
    assert matrix["totals"][ContributionType.WRITING_ORIGINAL_DRAFT] == 2
    assert matrix["totals"][ContributionType.DATA_CURATION] == 1

    rows_by_user = {row["user"].id: row for row in matrix["rows"]}
    assert rows_by_user[user.id]["counts"][ContributionType.WRITING_ORIGINAL_DRAFT] == 2
    assert rows_by_user[user.id]["total"] == 2
    assert rows_by_user[other_user.id]["counts"][ContributionType.DATA_CURATION] == 1
    # Highest-total user is sorted first.
    assert matrix["rows"][0]["user"].id == user.id


@pytest.mark.django_db
def test_get_user_contributions_scoped_to_project(user):
    project_a, col_a, pm_a = _make_project(user, name="A")
    project_b, col_b, pm_b = _make_project(user, name="B")
    for project, col, pm in ((project_a, col_a, pm_a), (project_b, col_b, pm_b)):
        _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    assert get_user_contributions(user).count() == 2
    assert get_user_contributions(user, project=project_a).count() == 1


# ---------------------------------------------------------------------------
# CSV downloads — list and matrix
# ---------------------------------------------------------------------------


def _csv_lines(response) -> list[str]:
    body = b"".join(response.streaming_content).decode("utf-8")
    return [line for line in body.splitlines() if line]


@pytest.mark.django_db
def test_my_contributions_list_csv_returns_rows(client, user):
    project, col, pm_member = _make_project(user)
    _credit(project, col, pm_member, pm_member, ContributionType.WRITING_ORIGINAL_DRAFT)

    client.force_login(user)
    resp = client.get("/my/contributions.csv")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/csv")
    assert "attachment;" in resp["Content-Disposition"]
    assert "my-contributions-list-" in resp["Content-Disposition"]
    lines = _csv_lines(resp)
    # header + one data row
    assert lines[0].split(",")[0] == "project"
    assert len(lines) == 2
    assert user.email in lines[1]
    assert "Writing" in lines[1]


@pytest.mark.django_db
def test_my_contributions_matrix_csv_is_a_grid(client, user):
    project, col, pm_member = _make_project(user, name="Alpha")
    _credit(project, col, pm_member, pm_member, ContributionType.SOFTWARE)
    _credit(project, col, pm_member, pm_member, ContributionType.SOFTWARE)

    client.force_login(user)
    resp = client.get("/my/contributions.csv", {"format": "matrix"})
    assert resp.status_code == 200
    assert "my-contributions-matrix-" in resp["Content-Disposition"]
    lines = _csv_lines(resp)
    header = lines[0].split(",")
    assert header[0] == "project"
    assert header[-1] == "total"
    # The whole taxonomy, so a downloaded grid has a stable shape.
    assert len(header) == len(CREDIT_ROLES) + 2
    assert "Software" in header
    body = lines[1].split(",")
    assert body[0] == "Alpha"
    assert body[header.index("Software")] == "2"
    assert body[-1] == "2"
    assert lines[-1].split(",")[0] == "total"


@pytest.mark.django_db
def test_my_contributions_csv_honours_the_project_filter(client, user):
    project_a, col_a, pm_a = _make_project(user, name="Alpha")
    project_b, col_b, pm_b = _make_project(user, name="Beta")
    _credit(project_a, col_a, pm_a, pm_a, ContributionType.SOFTWARE)
    _credit(project_b, col_b, pm_b, pm_b, ContributionType.METHODOLOGY)

    client.force_login(user)
    resp = client.get("/my/contributions.csv", {"project": project_a.pk})
    lines = _csv_lines(resp)
    assert len(lines) == 2
    assert "Alpha" in lines[1]
    assert "alpha" in resp["Content-Disposition"]


@pytest.mark.django_db
def test_project_matrix_csv_is_open_to_members_but_the_list_is_not(
    client, user, other_user
):
    project, col, pm_member = _make_project(user)
    _member_for(project, other_user)
    _credit(project, col, pm_member, pm_member, ContributionType.SOFTWARE)
    url = f"/projects/{project.pk}/contributions.csv"

    # A lead takes either shape.
    client.force_login(user)
    assert client.get(url, {"format": "matrix"}).status_code == 200
    assert client.get(url).status_code == 200

    # An ordinary member gets the grid they can already see, not the raw
    # sheet of everyone's emails and ORCIDs.
    client.force_login(other_user)
    assert client.get(url, {"format": "matrix"}).status_code == 200
    assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_project_contributions_csv_refuses_a_non_member(client, user, other_user):
    project, col, pm_member = _make_project(user)
    _credit(project, col, pm_member, pm_member, ContributionType.SOFTWARE)

    client.force_login(other_user)
    url = f"/projects/{project.pk}/contributions.csv"
    assert client.get(url, {"format": "matrix"}).status_code == 403
    assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_project_matrix_csv_names_every_member(client, user, other_user):
    project, col, pm_member = _make_project(user)
    _member_for(project, other_user)
    _credit(project, col, pm_member, pm_member, ContributionType.SOFTWARE)

    client.force_login(user)
    lines = _csv_lines(
        client.get(f"/projects/{project.pk}/contributions.csv", {"format": "matrix"})
    )
    assert lines[0].split(",")[0] == "member"
    names = [line.split(",")[0] for line in lines[1:-1]]
    assert "Alice" in names
    assert "Bob" in names


@pytest.mark.django_db
def test_my_contributions_csv_empty_only_header(client, user):
    client.force_login(user)
    resp = client.get("/my/contributions.csv")
    assert resp.status_code == 200
    assert len(_csv_lines(resp)) == 1  # header only


# ---------------------------------------------------------------------------
# The project's Contribution Tracking tab
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_tracking_tab_shows_the_matrix(client, user, other_user):
    project, col, pm = _make_project(user)
    other = _member_for(project, other_user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)
    _credit(project, col, pm, other, ContributionType.DATA_CURATION)

    client.force_login(user)
    body = client.get(
        reverse("projects:contribution_tracking", kwargs={"pk": project.pk})
    ).content.decode()
    assert "Contributions by member" in body
    assert "Software" in body
    assert "Data curation" in body
    assert "Alice" in body
    assert "Bob" in body


@pytest.mark.django_db
def test_tracking_tab_lists_a_member_with_no_credit_yet(client, user, other_user):
    """A gap in the credit should be as visible as the credit itself."""
    project, col, pm = _make_project(user)
    _member_for(project, other_user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    client.force_login(user)
    body = client.get(
        reverse("projects:contribution_tracking", kwargs={"pk": project.pk})
    ).content.decode()
    assert "Bob" in body


@pytest.mark.django_db
def test_tracking_tab_explains_an_untracked_project(client, user):
    project, _col, _pm = _make_project(user, tracking=ContributionTracking.NONE)
    client.force_login(user)
    body = client.get(
        reverse("projects:contribution_tracking", kwargs={"pk": project.pk})
    ).content.decode()
    assert "tracking contributions" in body
    assert "Contributions by member" not in body


@pytest.mark.django_db
def test_the_old_project_contributions_url_redirects_to_the_tab(client, user):
    project, _col, _pm = _make_project(user)
    client.force_login(user)
    resp = client.get(f"/projects/{project.pk}/contributions/")
    assert resp.status_code == 302
    assert resp["Location"] == reverse(
        "projects:contribution_tracking", kwargs={"pk": project.pk}
    )


# ---------------------------------------------------------------------------
# The Contributions module
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_contributions_module_lists_every_project(client, user):
    project_a, col_a, pm_a = _make_project(user, name="Alpha")
    project_b, col_b, pm_b = _make_project(user, name="Beta")
    _credit(project_a, col_a, pm_a, pm_a, ContributionType.SOFTWARE)
    _credit(project_b, col_b, pm_b, pm_b, ContributionType.METHODOLOGY)

    client.force_login(user)
    body = client.get(reverse("contributions:contributions")).content.decode()
    assert "Alpha" in body
    assert "Beta" in body
    assert "Software" in body
    assert "Methodology" in body


@pytest.mark.django_db
def test_contributions_module_filters_by_project(client, user):
    project_a, col_a, pm_a = _make_project(user, name="Alpha")
    project_b, col_b, pm_b = _make_project(user, name="Beta")
    _credit(project_a, col_a, pm_a, pm_a, ContributionType.SOFTWARE)
    _credit(project_b, col_b, pm_b, pm_b, ContributionType.METHODOLOGY)

    client.force_login(user)
    resp = client.get(
        reverse("contributions:contributions"), {"project": project_a.pk}
    )
    records = list(resp.context["records"])
    assert [r.project_id for r in records] == [project_a.pk]
    assert resp.context["selected_project"] == project_a
    # Both projects stay in the filter's option list.
    assert {p.pk for p in resp.context["filter_projects"]} == {
        project_a.pk,
        project_b.pk,
    }


@pytest.mark.django_db
def test_the_filter_offers_a_project_with_no_credit_yet(client, user):
    """So "have I been credited here at all?" has an answer."""
    project, _col, _pm = _make_project(user, name="Quiet")
    client.force_login(user)
    resp = client.get(reverse("contributions:contributions"))
    assert [p.pk for p in resp.context["filter_projects"]] == [project.pk]


@pytest.mark.django_db
def test_the_filter_leaves_out_a_project_not_tracking_credit(client, user):
    _make_project(user, name="Untracked", tracking=ContributionTracking.NONE)
    client.force_login(user)
    resp = client.get(reverse("contributions:contributions"))
    assert resp.context["filter_projects"] == []


@pytest.mark.django_db
def test_the_filter_leaves_out_someone_elses_project(client, user, other_user):
    project, col, pm = _make_project(other_user, name="Theirs")
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    client.force_login(user)
    resp = client.get(
        reverse("contributions:contributions"), {"project": project.pk}
    )
    assert resp.context["filter_projects"] == []
    # An unreachable pk falls back to all projects rather than 404ing.
    assert resp.context["selected_project"] is None
    assert list(resp.context["records"]) == []


@pytest.mark.django_db
def test_a_junk_project_filter_falls_back_to_all(client, user):
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)
    client.force_login(user)
    resp = client.get(reverse("contributions:contributions"), {"project": "nope"})
    assert resp.context["selected_project"] is None
    assert resp.context["contribution_total"] == 1


@pytest.mark.django_db
def test_my_contributions_redirects_to_the_module(client, user):
    client.force_login(user)
    resp = client.get("/my/contributions/")
    assert resp.status_code == 302
    assert resp["Location"] == reverse("contributions:contributions")


@pytest.mark.django_db
def test_the_pages_offer_both_downloads(client, user, other_user):
    project, col, pm = _make_project(user)
    _member_for(project, other_user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    client.force_login(user)
    body = client.get(
        reverse("projects:contribution_tracking", kwargs={"pk": project.pk})
    ).content.decode()
    # Both say "(CSV)" on the tab, as on My Contributions.
    assert 'aria-label="Download the contribution matrix as CSV"' in body and 'aria-label="Download the contribution list as CSV"' in body
    assert "Matrix (CSV)" in body and "List (CSV)" in body

    body = client.get(reverse("contributions:contributions")).content.decode()
    assert "Matrix (CSV)" in body and "List (CSV)" in body
    assert "format=matrix" in body

    # A member who is not a lead is offered only the grid they can see.
    client.force_login(other_user)
    body = client.get(
        reverse("projects:contribution_tracking", kwargs={"pk": project.pk})
    ).content.decode()
    assert 'aria-label="Download the contribution matrix as CSV"' in body and 'aria-label="Download the contribution list as CSV"' not in body


@pytest.mark.django_db
def test_the_module_downloads_carry_the_project_filter(client, user):
    project, col, pm = _make_project(user, name="Alpha")
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    client.force_login(user)
    body = client.get(
        reverse("contributions:contributions"), {"project": project.pk}
    ).content.decode()
    assert f"project={project.pk}" in body


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def _edit_url(project, record):
    return reverse(
        "contributions:contribution_edit",
        kwargs={"project_pk": project.pk, "pk": record.pk},
    )


def _remove_url(project, record):
    return reverse(
        "contributions:contribution_remove",
        kwargs={"project_pk": project.pk, "pk": record.pk},
    )


def _add_url(project):
    return reverse(
        "contributions:contribution_add", kwargs={"project_pk": project.pk}
    )


@pytest.mark.django_db
def test_a_lead_corrects_a_contributions_role(client, user):
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.OTHER)
    record = ContributionRecord.objects.get()

    client.force_login(user)
    resp = client.post(
        _edit_url(project, record),
        {"contribution_type": ContributionType.SOFTWARE},
    )
    assert resp.status_code == 302
    record.refresh_from_db()
    assert record.contribution_type == ContributionType.SOFTWARE
    assert AuditLog.objects.filter(
        action=AuditAction.CONTRIBUTION_UPDATED, actor=user, project=project
    ).exists()


@pytest.mark.django_db
def test_a_lead_withdraws_a_contribution(client, user):
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)
    record = ContributionRecord.objects.get()

    client.force_login(user)
    resp = client.post(_remove_url(project, record))
    assert resp.status_code == 302
    assert ContributionRecord.objects.count() == 0
    assert AuditLog.objects.filter(
        action=AuditAction.CONTRIBUTION_REMOVED, actor=user, project=project
    ).exists()


@pytest.mark.django_db
def test_a_lead_adds_a_contribution_by_hand(client, user, other_user):
    """Someone who did the work without ever being an assignee."""
    project, col, pm = _make_project(user)
    _member_for(project, other_user)
    task = _make_task(project, col, pm, assignees=[pm])

    client.force_login(user)
    resp = client.post(
        _add_url(project),
        {
            "task": task.pk,
            "user": other_user.pk,
            "contribution_type": ContributionType.VISUALIZATION,
        },
    )
    assert resp.status_code == 302
    record = ContributionRecord.objects.get(user=other_user)
    assert record.task_id == task.pk
    assert record.project_id == project.pk
    assert record.contribution_type == ContributionType.VISUALIZATION
    assert record.added_by_hand is True
    assert record.task_log is None
    assert record.verified_by_id == user.pk
    assert AuditLog.objects.filter(
        action=AuditAction.CONTRIBUTION_ADDED, actor=user, project=project
    ).exists()


@pytest.mark.django_db
def test_adding_credit_someone_already_holds_corrects_it(client, user):
    """One credit per person per task — a second add is a correction."""
    project, col, pm = _make_project(user)
    task = _credit(project, col, pm, pm, ContributionType.OTHER)

    client.force_login(user)
    client.post(
        _add_url(project),
        {
            "task": task.pk,
            "user": user.pk,
            "contribution_type": ContributionType.SUPERVISION,
        },
    )
    record = ContributionRecord.objects.get()
    assert record.contribution_type == ContributionType.SUPERVISION


@pytest.mark.django_db
def test_a_hand_entered_record_survives_the_verification_being_withdrawn(client, user, other_user):
    project, col, pm = _make_project(user)
    _member_for(project, other_user)
    task = _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    client.force_login(user)
    client.post(
        _add_url(project),
        {
            "task": task.pk,
            "user": other_user.pk,
            "contribution_type": ContributionType.VISUALIZATION,
        },
    )
    assert ContributionRecord.objects.count() == 2

    unverify_part(task_log=task.task_logs.get(assigned_to=pm), actor=pm)

    # The earned one goes with the verification; the entered one stands.
    assert [r.user_id for r in ContributionRecord.objects.all()] == [other_user.id]


@pytest.mark.django_db
def test_a_correction_is_not_overwritten_when_the_part_is_verified_again(client, user):
    project, col, pm = _make_project(user)
    task = _credit(project, col, pm, pm, ContributionType.OTHER)
    record = ContributionRecord.objects.get()

    client.force_login(user)
    client.post(
        _edit_url(project, record),
        {"contribution_type": ContributionType.SOFTWARE},
    )
    close_task(task=task, closed_by=pm)
    verify_parts(task=task, lead=pm, scope=VerifyScope.DONE)

    record.refresh_from_db()
    assert record.contribution_type == ContributionType.SOFTWARE


@pytest.mark.django_db
def test_corrections_are_refused_to_anyone_but_a_lead(client, user, other_user):
    project, col, pm = _make_project(user)
    _member_for(project, other_user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)
    record = ContributionRecord.objects.get()

    client.force_login(other_user)
    assert client.get(_add_url(project)).status_code == 403
    assert client.get(_edit_url(project, record)).status_code == 403
    assert client.post(
        _edit_url(project, record),
        {"contribution_type": ContributionType.OTHER},
    ).status_code == 403
    assert client.post(_remove_url(project, record)).status_code == 403
    assert ContributionRecord.objects.count() == 1


@pytest.mark.django_db
def test_a_correction_cannot_reach_another_project(client, user):
    mine, col, pm = _make_project(user, name="Mine")
    theirs, col_b, pm_b = _make_project(user, name="Theirs")
    _credit(theirs, col_b, pm_b, pm_b, ContributionType.SOFTWARE)
    record = ContributionRecord.objects.get()

    client.force_login(user)
    # A lead on both, but the record does not belong to the project in the URL.
    assert client.get(_edit_url(mine, record)).status_code == 404
    assert client.post(_remove_url(mine, record)).status_code == 404


@pytest.mark.django_db
def test_the_add_form_offers_only_this_projects_tasks_and_members(
    client, user, other_user
):
    project, col, pm = _make_project(user, name="Mine")
    mine_task = _make_task(project, col, pm)
    elsewhere, col_b, pm_b = _make_project(other_user, name="Elsewhere")
    other_task = _make_task(elsewhere, col_b, pm_b)

    form = ContributionAddForm(project=project)
    assert list(form.fields["task"].queryset) == [mine_task]
    assert other_task not in form.fields["task"].queryset
    assert [u.pk for u in form.fields["user"].queryset] == [user.pk]


@pytest.mark.django_db
def test_the_add_form_offers_other_last(client, user, other_user):
    """Other follows the 14 CRediT roles, and a contribution can be added
    under it."""
    project, col, pm = _make_project(user)
    _member_for(project, other_user)
    task = _make_task(project, col, pm, assignees=[pm])

    choices = ContributionAddForm(project=project).fields["contribution_type"].choices
    assert choices[-1] == (ContributionType.OTHER, "Other")
    assert len(choices) == 15

    client.force_login(user)
    client.post(
        _add_url(project),
        {"task": task.pk, "user": other_user.pk, "contribution_type": ContributionType.OTHER},
    )
    assert ContributionRecord.objects.get(user=other_user).contribution_type == ContributionType.OTHER


@pytest.mark.django_db
def test_the_tracking_tab_offers_the_controls_to_a_lead_only(
    client, user, other_user
):
    project, col, pm = _make_project(user)
    _member_for(project, other_user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)
    url = reverse("projects:contribution_tracking", kwargs={"pk": project.pk})

    client.force_login(user)
    body = client.get(url).content.decode()
    assert "Add contribution" in body
    assert "contribution-modal" in body

    client.force_login(other_user)
    body = client.get(url).content.decode()
    assert "Add contribution" not in body
    assert "contribution-modal" not in body


@pytest.mark.django_db
def test_add_contribution_shares_the_toolbar_with_the_downloads(client, user):
    """One row: Matrix, List, Add contribution, then the views."""
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    client.force_login(user)
    body = client.get(
        reverse("projects:contribution_tracking", kwargs={"pk": project.pk})
    ).content.decode()
    assert body.count("Add contribution") == 1
    assert (
        body.index('aria-label="Download the contribution matrix as CSV"')
        < body.index('aria-label="Download the contribution list as CSV"')
        < body.index("Add contribution")
        < body.index('id="contributions-by-member-container"')
    )


@pytest.mark.django_db
def test_a_lead_can_add_the_first_contribution(client, user):
    """No credit yet means no downloads, but adding is still offered."""
    project, _col, _pm = _make_project(user)

    client.force_login(user)
    body = client.get(
        reverse("projects:contribution_tracking", kwargs={"pk": project.pk})
    ).content.decode()
    assert 'aria-label="Download the contribution matrix as CSV"' not in body
    assert "Add contribution" in body


@pytest.mark.django_db
def test_a_hand_entered_record_is_marked_as_such_on_the_page(
    client, user, other_user
):
    project, col, pm = _make_project(user)
    _member_for(project, other_user)
    task = _make_task(project, col, pm, assignees=[pm])
    add_contribution(
        task=task,
        user=other_user,
        contribution_type=ContributionType.RESOURCES,
        actor=user,
    )

    client.force_login(user)
    body = client.get(
        reverse("projects:contribution_tracking", kwargs={"pk": project.pk})
    ).content.decode()
    assert "Added by hand" in body


@pytest.mark.django_db
def test_modal_partials_render(client, user):
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.OTHER)
    record = ContributionRecord.objects.get()
    client.force_login(user)

    body = client.get(
        reverse("contributions:contribution_add", kwargs={"project_pk": project.pk}),
        HTTP_HX_REQUEST="true",
    ).content.decode()
    assert "Add a contribution" in body
    assert "credit.niso.org" in body

    body = client.get(
        reverse(
            "contributions:contribution_edit",
            kwargs={"project_pk": project.pk, "pk": record.pk},
        ),
        HTTP_HX_REQUEST="true",
    ).content.decode()
    assert "Edit contribution" in body
    assert "Withdraw" in body
    # The role currently recorded is preselected.
    assert 'value="OTHER" selected' in body


@pytest.mark.django_db
def test_an_htmx_post_redirects_the_page(client, user):
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.OTHER)
    record = ContributionRecord.objects.get()
    client.force_login(user)

    resp = client.post(
        reverse(
            "contributions:contribution_edit",
            kwargs={"project_pk": project.pk, "pk": record.pk},
        ),
        {"contribution_type": ContributionType.SOFTWARE},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 204
    assert resp["HX-Redirect"] == reverse(
        "projects:contribution_tracking", kwargs={"pk": project.pk}
    )


@pytest.mark.django_db
def test_an_invalid_add_comes_back_into_the_modal(client, user):
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.OTHER)
    client.force_login(user)

    resp = client.post(
        reverse("contributions:contribution_add", kwargs={"project_pk": project.pk}),
        {"task": "", "user": "", "contribution_type": ""},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 400
    assert "Please correct the following" in resp.content.decode()


# ---------------------------------------------------------------------------
# The section's two views
# ---------------------------------------------------------------------------


def _by_member_url(project):
    return reverse("projects:contributions_by_member", kwargs={"pk": project.pk})


def _every_url(project):
    return reverse("projects:every_contribution", kwargs={"pk": project.pk})


@pytest.mark.django_db
def test_the_section_ships_both_views_and_a_switcher(client, user):
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    client.force_login(user)
    body = client.get(
        reverse("projects:contribution_tracking", kwargs={"pk": project.pk})
    ).content.decode()
    # The switcher, in the same shape the Tasks board uses.
    assert "$store.credit.setView('by-member')" in body
    assert "$store.credit.setView('every')" in body
    # Both views are in the first render, so switching is instant.
    assert 'id="contributions-by-member-container"' in body
    assert 'id="every-contribution-container"' in body


@pytest.mark.django_db
def test_the_by_member_view_renders_on_its_own(client, user, other_user):
    project, col, pm = _make_project(user)
    _member_for(project, other_user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    client.force_login(user)
    body = client.get(_by_member_url(project)).content.decode()
    # No heading of its own: the switcher on the page names the view.
    assert "<h2" not in body
    assert "Software" in body
    assert "Alice" in body
    # It is the partial, not the whole page.
    assert "<!DOCTYPE html>" not in body


@pytest.mark.django_db
def test_the_every_contribution_view_renders_on_its_own(client, user):
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    client.force_login(user)
    body = client.get(_every_url(project)).content.decode()
    # No heading of its own: the switcher on the page names the view.
    assert "<h2" not in body
    assert "Software" in body
    assert "<!DOCTYPE html>" not in body


@pytest.mark.django_db
def test_both_views_are_closed_to_non_members(client, user, other_user):
    project, _col, _pm = _make_project(user)
    client.force_login(other_user)
    assert client.get(_by_member_url(project)).status_code == 403
    assert client.get(_every_url(project)).status_code == 403


# ---------------------------------------------------------------------------
# The matrix: the whole grid by default, narrowable on request
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_matrix_carries_every_role_and_every_member(client, user, other_user):
    """A zero says which roles are uncovered and who is uncredited."""
    project, col, pm = _make_project(user)
    _member_for(project, other_user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    client.force_login(user)
    resp = client.get(_by_member_url(project))
    matrix = resp.context["matrix"]

    assert [value for value, _label in matrix["types"]] == list(CREDIT_ROLES)
    # Bob has earned nothing and is still listed.
    by_name = {user_display_name(row["user"]): row for row in matrix["rows"]}
    assert set(by_name) == {"Alice", "Bob"}
    assert by_name["Bob"]["total"] == 0
    assert by_name["Alice"]["counts"][ContributionType.SOFTWARE] == 1
    assert by_name["Alice"]["counts"][ContributionType.METHODOLOGY] == 0


@pytest.mark.django_db
def test_the_filter_narrows_the_matrix_to_what_has_been_earned(
    client, user, other_user
):
    project, col, pm = _make_project(user)
    _member_for(project, other_user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    client.force_login(user)
    matrix = client.get(_by_member_url(project), {"used": "1"}).context["matrix"]

    assert [value for value, _label in matrix["types"]] == [ContributionType.SOFTWARE]
    assert [user_display_name(row["user"]) for row in matrix["rows"]] == ["Alice"]
    assert matrix["totals"]["grand_total"] == 1


@pytest.mark.django_db
def test_the_filter_is_off_unless_asked_for(client, user):
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    client.force_login(user)
    for params in ({}, {"used": "0"}):
        resp = client.get(_by_member_url(project), params)
        assert resp.context["only_used"] is False
        assert len(resp.context["matrix"]["types"]) == len(CREDIT_ROLES)


@pytest.mark.django_db
def test_a_role_off_the_taxonomy_keeps_its_column_when_used(client, user):
    """A task left as "Other" is not a CRediT role, but it was still earned."""
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.OTHER)

    client.force_login(user)
    matrix = client.get(_by_member_url(project)).context["matrix"]
    values = [value for value, _label in matrix["types"]]
    assert values == list(CREDIT_ROLES) + [ContributionType.OTHER]


@pytest.mark.django_db
def test_the_matrix_download_carries_the_whole_taxonomy(client, user):
    """The filter is the page's; a downloaded grid keeps a stable shape."""
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)

    client.force_login(user)
    lines = _csv_lines(
        client.get(
            f"/projects/{project.pk}/contributions.csv",
            {"format": "matrix", "used": "1"},
        )
    )
    header = lines[0].split(",")
    assert len(header) == len(CREDIT_ROLES) + 2


# ---------------------------------------------------------------------------
# Sorting the matrix
# ---------------------------------------------------------------------------


def _matrix_names(response):
    return [user_display_name(row["user"]) for row in response.context["matrix"]["rows"]]


@pytest.fixture
def sortable_project(user, other_user, pm_user):
    """Alice 2 software, Bob 1 software + 1 methodology, PM nothing."""
    project, col, pm = _make_project(user)
    bob = _member_for(project, other_user)
    _member_for(project, pm_user)  # named PM by the fixture; uncredited
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)
    _credit(project, col, pm, bob, ContributionType.SOFTWARE)
    _credit(project, col, pm, bob, ContributionType.METHODOLOGY)
    return project


@pytest.mark.django_db
def test_the_matrix_sorts_by_total_desc_by_default(client, user, sortable_project):
    client.force_login(user)
    resp = client.get(_by_member_url(sortable_project))
    assert resp.context["matrix"]["sort"] == "total"
    assert resp.context["matrix"]["direction"] == "desc"
    # Alice 2, Bob 2, PM 0 — the tie holding its name order.
    assert _matrix_names(resp) == ["Alice", "Bob", "PM"]


@pytest.mark.django_db
def test_the_matrix_sorts_by_member_name(client, user, sortable_project):
    client.force_login(user)
    url = _by_member_url(sortable_project)

    assert _matrix_names(client.get(url, {"sort": "member"})) == [
        "Alice",
        "Bob",
        "PM",
    ]
    assert _matrix_names(client.get(url, {"sort": "member", "dir": "desc"})) == [
        "PM",
        "Bob",
        "Alice",
    ]


@pytest.mark.django_db
def test_the_matrix_sorts_by_one_role(client, user, sortable_project):
    """Who has the most of a role, not just the most overall."""
    client.force_login(user)
    url = _by_member_url(sortable_project)

    # Alice has two Software, Bob one, PM none.
    assert _matrix_names(client.get(url, {"sort": ContributionType.SOFTWARE})) == [
        "Alice",
        "Bob",
        "PM",
    ]
    # Only Bob has Methodology, so he leads it however the totals fall.
    assert _matrix_names(client.get(url, {"sort": ContributionType.METHODOLOGY})) == [
        "Bob",
        "Alice",
        "PM",
    ]


@pytest.mark.django_db
def test_ties_hold_their_name_order_whatever_the_sort(client, user, sortable_project):
    """Alice and Bob both total 2; their name order breaks the tie."""
    client.force_login(user)
    rows = client.get(
        _by_member_url(sortable_project), {"sort": "total", "dir": "asc"}
    ).context["matrix"]["rows"]
    names = [user_display_name(row["user"]) for row in rows]
    assert names == ["PM", "Alice", "Bob"]


@pytest.mark.django_db
def test_an_unknown_sort_falls_back_to_the_default(client, user, sortable_project):
    """A stale link should still show the grid."""
    client.force_login(user)
    for params in (
        {"sort": "NOT_A_ROLE"},
        {"sort": "total", "dir": "sideways"},
        {"sort": ""},
    ):
        matrix = client.get(_by_member_url(sortable_project), params).context["matrix"]
        assert matrix["sort"] in {"total", params.get("sort")}
        assert matrix["direction"] in {"asc", "desc"}


@pytest.mark.django_db
def test_the_sort_and_the_filter_keep_each_other(client, user, sortable_project):
    """Neither control throws the other away."""
    client.force_login(user)
    resp = client.get(
        _by_member_url(sortable_project),
        {"sort": "member", "dir": "desc", "used": "1"},
    )
    matrix = resp.context["matrix"]
    assert resp.context["only_used"] is True
    assert matrix["sort"] == "member"
    assert matrix["direction"] == "desc"
    # PM has nothing, so the filter drops them; the sort still reverses.
    assert [user_display_name(row["user"]) for row in matrix["rows"]] == [
        "Bob",
        "Alice",
    ]


@pytest.mark.django_db
def test_the_headers_carry_the_sort_controls(client, user, sortable_project):
    client.force_login(user)
    body = client.get(_by_member_url(sortable_project)).content.decode()
    # The sorted column is marked for a screen reader and flips on the next
    # click; an unsorted one starts at its own natural end.
    assert 'aria-sort="descending"' in body
    assert 'aria-sort="none"' in body
    assert '"sort": "total", "dir": "asc"' in body
    assert '"sort": "member", "dir": "asc"' in body
    assert f'"sort": "{ContributionType.SOFTWARE}", "dir": "desc"' in body


# ---------------------------------------------------------------------------
# Who verified the part the credit was earned on
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_project_list_names_who_verified_the_part(client, user, other_user):
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    task = _make_task(project, col, lead, assignees=[alice])
    verify_part(task_log=mark_own_done(task=task, member=alice), lead=lead)

    client.force_login(user)
    body = client.get(_every_url(project)).content.decode()
    assert "Verified by" in body
    # The lead who verified it, not the member who earned the credit.
    assert ContributionRecord.objects.get().verified_by_id == user.pk
    assert "Alice" in body


@pytest.mark.django_db
def test_the_contributions_module_names_who_verified_the_part(client, user, other_user):
    """Your own credit says who credited you."""
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    task = _make_task(project, col, lead, assignees=[alice])
    verify_part(task_log=mark_own_done(task=task, member=alice), lead=lead)

    client.force_login(other_user)
    body = client.get(reverse("contributions:contributions")).content.decode()
    assert "Verified by" in body
    assert "Alice" in body  # the lead, on Bob's own page


@pytest.mark.django_db
def test_a_hand_entered_record_names_who_entered_it(client, user, other_user):
    project, col, lead = _make_project(user)
    _member_for(project, other_user)
    task = _make_task(project, col, lead, assignees=[lead])
    add_contribution(
        task=task,
        user=other_user,
        contribution_type=ContributionType.RESOURCES,
        actor=user,
    )

    client.force_login(user)
    body = client.get(_every_url(project)).content.decode()
    # Named, and badged so it does not read as credit the task earned.
    assert "Alice" in body
    assert "Added by hand" in body


@pytest.mark.django_db
def test_a_verifier_whose_account_is_gone_is_still_reported(client, user, other_user):
    """SET_NULL outlives the account; say so rather than leaving it blank."""
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    task = _make_task(project, col, lead, assignees=[alice])
    verify_part(task_log=mark_own_done(task=task, member=alice), lead=lead)

    record = ContributionRecord.objects.get()
    record.verified_by = None
    record.save(update_fields=["verified_by"])

    client.force_login(other_user)
    body = client.get(reverse("contributions:contributions")).content.decode()
    assert "A former member" in body


@pytest.mark.django_db
def test_the_public_profile_names_the_verifier(client, user, other_user):
    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    task = _make_task(project, col, lead, assignees=[alice])
    verify_part(task_log=mark_own_done(task=task, member=alice), lead=lead)

    client.force_login(other_user)
    body = client.get(
        reverse("public_profile", kwargs={"pk": other_user.pk})
    ).content.decode()
    assert "verified by Alice" in body


# ---------------------------------------------------------------------------
# The order contribution types are listed in — alphabetical, "Other" last
# ---------------------------------------------------------------------------


def test_sort_contribution_types_is_alphabetical_by_label_with_other_last():
    from apps.core.enums import ContributionType, sort_contribution_types

    labels = dict(ContributionType.choices)
    ordered = [labels[v] for v in sort_contribution_types(labels)]

    assert ordered[-1] == "Other"
    # Sorting on the label, not the value: "Data curation" belongs under D,
    # not where ``DATA_CURATION`` would fall.
    assert ordered[:3] == ["Conceptualization", "Data curation", "Formal analysis"]
    assert ordered[:-1] == sorted(ordered[:-1], key=str.lower)


def test_credit_role_choices_keeps_other_last_when_it_is_included():
    from apps.core.enums import credit_role_choices

    labels = [label for _value, label in credit_role_choices("OTHER")]
    assert labels[0] == "Conceptualization"
    assert labels[-1] == "Other"


@pytest.mark.django_db
def test_contributions_module_shows_the_profile_card(client, user):
    """The same card, over the same numbers: whole taxonomy, "Other" last."""
    project, col, pm = _make_project(user, name="Alpha")
    for ctype in (
        ContributionType.OTHER,
        ContributionType.SOFTWARE,
        ContributionType.DATA_CURATION,
    ):
        _credit(project, col, pm, pm, ctype)

    client.force_login(user)
    context = client.get(reverse("contributions:contributions")).context
    tiles = dict(context["contribution_tiles"])
    labels = [label for label, _count in context["contribution_tiles"]]

    assert labels[0] == "Conceptualization"
    assert labels[-1] == "Other"
    # Roles never earned are shown at zero rather than left out.
    assert tiles["Data curation"] == 1
    assert tiles["Investigation"] == 0
    assert context["contribution_total"] == 3
    # The legacy Writing type is not a role anyone can earn any more.
    assert "Writing (legacy)" not in tiles


@pytest.mark.django_db
def test_contributions_module_card_keeps_legacy_writing_when_earned(client, user):
    project, col, pm = _make_project(user, name="Alpha")
    _credit(project, col, pm, pm, ContributionType.WRITING)

    client.force_login(user)
    context = client.get(reverse("contributions:contributions")).context
    tiles = dict(context["contribution_tiles"])
    assert tiles["Writing (legacy)"] == 1


@pytest.mark.django_db
def test_contributions_module_card_follows_the_project_filter(client, user):
    project_a, col_a, pm_a = _make_project(user, name="Alpha")
    project_b, col_b, pm_b = _make_project(user, name="Beta")
    _credit(project_a, col_a, pm_a, pm_a, ContributionType.SOFTWARE)
    _credit(project_b, col_b, pm_b, pm_b, ContributionType.METHODOLOGY)

    client.force_login(user)
    context = client.get(
        reverse("contributions:contributions"), {"project": project_a.pk}
    ).context
    tiles = dict(context["contribution_tiles"])
    assert tiles["Software"] == 1
    assert tiles["Methodology"] == 0
    assert context["contribution_total"] == 1


@pytest.mark.django_db
def test_matrix_columns_use_the_same_order(client, user):
    from apps.contributions.services import get_project_contributions_matrix

    project, col, pm = _make_project(user, name="Alpha")
    _credit(project, col, pm, pm, ContributionType.OTHER)

    labels = [label for _value, label in get_project_contributions_matrix(project)["types"]]
    assert labels[0] == "Conceptualization"
    assert labels[-1] == "Other"


@pytest.mark.django_db
def test_the_page_offers_no_with_contributions_only_filter(client, user):
    """The "With contributions only" toggle is gone: not drawn by the page,
    nor re-sent when the matrix is re-sorted."""
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)
    client.force_login(user)

    page = client.get(
        reverse("projects:contribution_tracking", kwargs={"pk": project.pk})
    ).content.decode()
    swap = client.get(
        _by_member_url(project), {"sort": "member", "dir": "desc"}
    ).content.decode()
    for body in (page, swap):
        assert "With contributions only" not in body
        assert 'id="matrix-filter"' not in body


@pytest.mark.django_db
def test_the_add_modal_keeps_its_explanation_behind_an_info_button(client, user):
    """The modal's explanation opens from an (i) beside its title rather than
    sitting under it as a paragraph."""
    project, _col, _pm = _make_project(user)
    client.force_login(user)

    html = client.get(_add_url(project), headers={"hx-request": "true"}).content.decode()
    assert 'aria-label="About Add a contribution"' in html
    title_row = html[: html.index('aria-label="About Add a contribution"')]
    assert "Add a contribution</h2>" in title_row
    assert '<p class="text-sm text-brand-text/70 mb-4">' not in html


@pytest.mark.django_db
def test_the_add_modal_links_the_credit_roles_from_the_label_line(client, user):
    """"CRediT roles ↗" on the CRediT role label line, as on a new task's
    Task type, in place of the "What each role covers" line."""
    project, _col, _pm = _make_project(user)
    client.force_login(user)

    html = client.get(_add_url(project), headers={"hx-request": "true"}).content.decode()
    assert "What each role covers" not in html
    assert ">credit.niso.org</a>" not in html
    link = html.index("CRediT roles &#8599;")
    assert html.index("CRediT role <span") < link < html.index('name="contribution_type"')
    assert 'href="https://credit.niso.org/" target="_blank" rel="noopener noreferrer"' in html


@pytest.mark.django_db
def test_every_contribution_sorts_by_every_header_but_actions(client, user):
    """Member, Task, Role, Verified and Verified by are sort buttons; Actions
    is not; each row carries what it sorts by."""
    project, col, pm = _make_project(user)
    task = _credit(project, col, pm, pm, ContributionType.SOFTWARE)
    record = ContributionRecord.objects.get()

    client.force_login(user)
    html = client.get(_every_url(project)).content.decode()
    for key in ("member", "task", "role", "completed", "closer"):
        assert f"@click=\"sortBy('{key}')\"" in html, key
    assert '<th scope="col" class="text-right">Actions</th>' in html
    row = html[html.index('data-order="0"') :]
    row = row[: row.index(">")]
    assert f'data-sort-task="{task.name.lower()}"' in row
    assert 'data-sort-role="software"' in row
    assert f'data-sort-completed="{int(record.closed_at.timestamp())}"' in row


@pytest.mark.django_db
def test_the_edit_modal_links_the_credit_roles_from_the_label_line(client, user):
    """"CRediT roles ↗" on the CRediT role label line, as in Add a
    contribution, in place of the "What each role covers" line."""
    project, col, pm = _make_project(user)
    _credit(project, col, pm, pm, ContributionType.SOFTWARE)
    record = ContributionRecord.objects.get()
    client.force_login(user)

    html = client.get(_edit_url(project, record), headers={"hx-request": "true"}).content.decode()
    assert "What each role covers" not in html
    link = html.index("CRediT roles &#8599;")
    assert html.index("CRediT role <span") < link < html.index('name="contribution_type"')


@pytest.mark.django_db
def test_withdrawing_asks_first_in_a_remove_style_modal(client, user):
    """Withdraw opens a confirmation modal in the Remove design rather than
    the browser's confirm box; its form posts the withdrawal."""
    project, col, pm = _make_project(user)
    task = _credit(project, col, pm, pm, ContributionType.SOFTWARE)
    record = ContributionRecord.objects.get()
    client.force_login(user)

    html = client.get(_edit_url(project, record), headers={"hx-request": "true"}).content.decode()
    assert "hx-confirm" not in html
    assert "showModal()\">Withdraw</button>" in html
    modal = html[html.index('id="withdraw-contribution-modal"') :]
    modal = modal[: modal.index("</dialog>")]
    assert 'class="modal-box border border-error/40"' in modal
    assert task.name in modal and "Keep contribution" in modal
    assert f'hx-post="{_remove_url(project, record)}"' in modal

    client.post(_remove_url(project, record), headers={"hx-request": "true"})
    assert not ContributionRecord.objects.exists()


# ---------------------------------------------------------------------------
# Credit written before verification existed reads as verified
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_backfill_marks_credited_parts_verified(user, other_user):
    import importlib

    from django.apps import apps as django_apps
    from django.utils import timezone

    backfill = importlib.import_module(
        "apps.tasks.migrations.0016_tasklog_verified"
    ).backfill_from_the_ledger

    project, col, lead = _make_project(user)
    alice = _member_for(project, other_user)
    task = _make_task(project, col, lead, assignees=[alice, lead])
    credited = mark_own_done(task=task, member=alice)
    uncredited = mark_own_done(task=task, member=lead)
    when = timezone.now() - timezone.timedelta(days=3)
    # The shape the old close-time writer left: a record, no verification.
    ContributionRecord.objects.create(
        task=task, task_log=credited, user=other_user, project=project,
        contribution_type=task.contribution_type, verified_by=user, closed_at=when,
    )
    add_contribution(
        task=task, user=user, contribution_type=ContributionType.SOFTWARE, actor=user
    )

    backfill(django_apps, None)

    credited.refresh_from_db()
    uncredited.refresh_from_db()
    assert credited.verified_at == when and credited.verified_by_id == lead.pk
    assert not uncredited.is_verified  # a hand-entered record verifies nothing
