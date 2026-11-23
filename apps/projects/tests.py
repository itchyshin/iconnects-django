"""Smoke tests for the projects app."""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from apps.audit.models import AuditAction, AuditLog
from apps.core.enums import JoinRequestStatus, MemberPrivileges, ProjectStatus, TaskStatus
from apps.projects.models import (
    ContributionTracking,
    Invitation,
    JoinRequest,
    Project,
    ProjectMember,
    ProjectPrivilege,
)
from apps.projects.services import (
    JoinRequestError,
    LastProjectManagerError,
    approve_join_request,
    assert_pm_self_action_allowed,
    cancel_join_request,
    create_invitation,
    create_join_request,
    create_project_with_owner,
    decline_join_request,
    is_last_project_manager,
    resend_invitation,
)


@pytest.mark.django_db
def test_public_list_is_anonymous_accessible(client):
    response = client.get(reverse("projects:list"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_my_list_requires_login(client):
    response = client.get(reverse("projects:my_list"))
    # Anonymous -> redirect to login.
    assert response.status_code in (301, 302)
    assert "/accounts/login" in response["Location"]


@pytest.mark.django_db
def test_my_list_htmx_search_returns_results_partial_only(client, user):
    create_project_with_owner(
        user=user,
        form_data={"name": "Alpha Findable", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    create_project_with_owner(
        user=user,
        form_data={"name": "Beta Other", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    client.force_login(user)

    response = client.get(
        reverse("projects:my_list"), {"q": "Findable"}, HTTP_HX_REQUEST="true"
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert "Alpha Findable" in content
    assert "Beta Other" not in content
    # The partial doesn't re-render the page shell (sidebar, filter form, etc.).
    assert 'id="filter-form-my"' not in content
    assert 'aria-label="Sidebar navigation"' not in content


@pytest.mark.django_db
def test_create_project_makes_creator_a_pm(client_logged_in, user):
    response = client_logged_in.post(
        reverse("projects:create"),
        {
            "name": "Test Project",
            "description": "A project",
            "abstract": "",
            "keywords": "",
            "status": ProjectStatus.PLANNING,
            "is_public": "on",
            "public_summary": "",
        },
    )
    # Redirects to detail on success.
    assert response.status_code in (301, 302)
    project = Project.objects.get(name="Test Project")
    # Membership + PM privilege exist.
    assert ProjectMember.objects.filter(project=project, users=user).exists()
    assert ProjectPrivilege.objects.filter(
        project=project,
        user=user,
        privilege=MemberPrivileges.PROJECT_MANAGER,
    ).exists()
    assert AuditLog.objects.filter(
        action=AuditAction.PROJECT_CREATED, actor=user, project=project
    ).exists()


@pytest.mark.django_db
def test_create_project_can_select_credit_tracking(client_logged_in, user):
    response = client_logged_in.post(
        reverse("projects:create"),
        {
            "name": "CRediT Project",
            "description": "A project",
            "status": ProjectStatus.PLANNING,
            "is_public": "on",
            "contribution_tracking": ContributionTracking.CREDIT,
        },
    )
    assert response.status_code in (301, 302)
    project = Project.objects.get(name="CRediT Project")
    assert project.contribution_tracking == ContributionTracking.CREDIT


@pytest.mark.django_db
def test_create_project_without_tracking_defaults_to_none(client_logged_in, user):
    response = client_logged_in.post(
        reverse("projects:create"),
        {
            "name": "Untracked Project",
            "description": "A project",
            "status": ProjectStatus.PLANNING,
            "is_public": "on",
        },
    )
    assert response.status_code in (301, 302)
    project = Project.objects.get(name="Untracked Project")
    assert project.contribution_tracking == ContributionTracking.NONE


@pytest.mark.django_db
def test_create_project_seeds_default_kanban_columns(user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Seeded", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    columns = list(project.containers.order_by("container_order"))
    assert [c.name for c in columns] == ["To Do", "In Progress", "Done"]
    assert [c.container_order for c in columns] == [0, 1, 2]


@pytest.mark.django_db
def test_only_pm_can_edit_project(client, user, other_user):
    # User A creates a project (PM).
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P1", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    # User B is a contributor, not a PM.
    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )

    client.force_login(other_user)
    response = client.get(
        reverse("projects:update", kwargs={"pk": project.pk})
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_invitation_code_is_unique_urlsafe(user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Inv", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    invitation = create_invitation(
        project=project,
        email="new@test.com",
        privilege=MemberPrivileges.CONTRIBUTOR,
        added_by="alice",
    )
    code = invitation.invitation_code
    assert isinstance(code, str)
    assert len(code) >= 20
    # URL-safe: only these characters.
    allowed = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )
    assert set(code).issubset(allowed)
    assert AuditLog.objects.filter(
        action=AuditAction.INVITATION_SENT,
        entity_type="invitation",
        entity_id=invitation.pk,
    ).exists()


@pytest.mark.django_db
def test_resend_invitation_regenerates_code_and_resends_email(user):
    from django.core import mail

    project = create_project_with_owner(
        user=user,
        form_data={"name": "Inv Resend", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    invitation = create_invitation(
        project=project,
        email="new@test.com",
        privilege=MemberPrivileges.CONTRIBUTOR,
        added_by="alice",
    )
    original_code = invitation.invitation_code
    mail.outbox.clear()

    resend_invitation(invitation=invitation, resender=user)
    invitation.refresh_from_db()

    assert invitation.invitation_code != original_code
    assert len(mail.outbox) == 1
    assert invitation.invitation_code in mail.outbox[0].body
    assert AuditLog.objects.filter(
        action=AuditAction.INVITATION_RESENT,
        entity_type="invitation",
        entity_id=invitation.pk,
    ).exists()


# ---------------------------------------------------------------------------
# Invitations show the privilege label (Lead / Member), not the stored value
# ---------------------------------------------------------------------------

PRIVILEGE_LABELS = [
    (MemberPrivileges.CONTRIBUTOR, "Member"),
    (MemberPrivileges.PROJECT_MANAGER, "Lead"),
]


def _invite(user, privilege, **kwargs):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Labels", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    return create_invitation(project=project, privilege=privilege, added_by="Alice", **kwargs)


@pytest.mark.django_db
@pytest.mark.parametrize("privilege,label", PRIVILEGE_LABELS)
def test_invitation_email_names_the_privilege_label(user, privilege, label):
    from django.core import mail

    mail.outbox.clear()
    _invite(user, privilege, email="new@test.com")

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    html = message.alternatives[0][0]
    assert f"on i-connects as a {label}." in message.body
    assert f"as\n  a <em>{label}</em>." in html
    for body in (message.body, html):
        assert privilege.value not in body


@pytest.mark.django_db
@pytest.mark.parametrize("privilege,label", PRIVILEGE_LABELS)
def test_invitation_accept_page_names_the_privilege_label(client, user, other_user, privilege, label):
    invitation = _invite(user, privilege, invited_user=other_user)
    client.force_login(other_user)

    response = client.get(
        reverse("projects:accept_or_decline", kwargs={"code": invitation.invitation_code})
    )

    assert response.status_code == 200
    content = response.content.decode()
    assert f"\n        {label}\n      </span>" in content
    assert f">\n        {privilege.value}\n" not in content


@pytest.mark.django_db
@pytest.mark.parametrize("privilege,label", PRIVILEGE_LABELS)
def test_my_invitations_list_names_the_privilege_label(client, user, other_user, privilege, label):
    _invite(user, privilege, invited_user=other_user)
    client.force_login(other_user)

    response = client.get(reverse("projects:my_invitations"))

    assert response.status_code == 200
    content = response.content.decode()
    assert f"\n                  {label}\n                </span>" in content
    assert f">\n                  {privilege.value}\n" not in content


@pytest.mark.django_db
def test_resend_invitation_view_requires_project_manager(client, user, other_user):
    project = _make_project(user)
    invitation = create_invitation(
        project=project,
        email="new@test.com",
        privilege=MemberPrivileges.CONTRIBUTOR,
        added_by="alice",
    )
    # A contributor, not a PM, may not resend.
    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    client.force_login(other_user)
    response = client.post(
        reverse(
            "projects:resend_invitation",
            kwargs={"pk": project.pk, "invitation_pk": invitation.pk},
        )
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_resend_invitation_view_redirects_pm_to_invitation_list(client_logged_in, user):
    project = _make_project(user)
    invitation = create_invitation(
        project=project,
        email="new@test.com",
        privilege=MemberPrivileges.CONTRIBUTOR,
        added_by="alice",
    )
    response = client_logged_in.post(
        reverse(
            "projects:resend_invitation",
            kwargs={"pk": project.pk, "invitation_pk": invitation.pk},
        )
    )
    assert response.status_code in (301, 302)
    assert response["Location"] == reverse(
        "projects:project_invitations", kwargs={"pk": project.pk}
    )


# ---------------------------------------------------------------------------
# Only the invitee may open or act on an invitation link
# ---------------------------------------------------------------------------


def _make_user(email, name):
    from django.utils import timezone

    return get_user_model().objects.create_user(
        email=email,
        username=f"{name.lower().replace(' ', '-')}-{email}",
        password="password123",
        name=name,
        email_confirmed_at=timezone.now(),
    )


def _private_invite(owner, **kwargs):
    project = create_project_with_owner(
        user=owner,
        form_data={
            "name": "Secret Seagrass",
            "abstract": "Unpublished eelgrass sites.",
            "status": ProjectStatus.ACTIVE,
            "is_public": False,
        },
    )
    return create_invitation(
        project=project, privilege=MemberPrivileges.PROJECT_MANAGER, added_by="Alice", **kwargs
    )


def _invite_url(invitation):
    return reverse("projects:accept_or_decline", kwargs={"code": invitation.invitation_code})


@pytest.mark.django_db
def test_invitation_link_hidden_from_another_signed_in_user(client, user, other_user):
    invitation = _private_invite(user, invited_user=other_user)
    stranger = _make_user("carol@test.com", "Carol")
    client.force_login(stranger)

    response = client.get(_invite_url(invitation))

    assert response.status_code == 403
    assert "invites/wrong_account.html" in [t.name for t in response.templates]
    content = response.content.decode()
    for secret in ("Secret Seagrass", "Unpublished eelgrass sites.", "Lead", other_user.email):
        assert secret not in content
    assert "Accept invitation" not in content
    assert "carol@test.com" in content  # who is signed in, so they know why


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["accept", "decline"])
def test_another_signed_in_user_cannot_accept_or_decline(client, user, other_user, action):
    invitation = _private_invite(user, invited_user=other_user)
    stranger = _make_user("carol@test.com", "Carol")
    client.force_login(stranger)

    response = client.post(_invite_url(invitation), {"action": action})

    assert response.status_code == 403
    assert Invitation.objects.filter(pk=invitation.pk).exists()
    assert not ProjectMember.objects.filter(project=invitation.project, users=stranger).exists()


@pytest.mark.django_db
def test_invitee_matched_by_email_in_any_case(client, user, other_user):
    invitation = _private_invite(user, email=other_user.email.upper())
    client.force_login(other_user)

    assert client.get(_invite_url(invitation)).status_code == 200
    response = client.post(_invite_url(invitation), {"action": "accept"})

    assert response.status_code == 302
    assert ProjectMember.objects.filter(project=invitation.project, users=other_user).exists()


@pytest.mark.django_db
def test_picked_invitee_keeps_invitation_after_changing_email(client, user, other_user):
    """An invitation to a picked account follows the account, not its address."""
    invitation = _private_invite(user, invited_user=other_user)
    other_user.email = "bob.new@test.com"
    other_user.save(update_fields=["email"])
    client.force_login(other_user)

    response = client.get(_invite_url(invitation))
    assert response.status_code == 200
    assert "Secret Seagrass" in response.content.decode()
    assert Invitation.objects.addressed_to(other_user).filter(pk=invitation.pk).exists()
    client.post(_invite_url(invitation), {"action": "accept"})
    assert ProjectMember.objects.filter(project=invitation.project, users=other_user).exists()


@pytest.mark.django_db
def test_new_holder_of_a_picked_invitees_old_email_is_not_the_invitee(client, user, other_user):
    invitation = _private_invite(user, invited_user=other_user)
    old_email = other_user.email
    other_user.email = "bob.new@test.com"
    other_user.save(update_fields=["email"])
    newcomer = _make_user(old_email, "Not Bob")
    client.force_login(newcomer)

    assert client.get(_invite_url(invitation)).status_code == 403
    assert not Invitation.objects.addressed_to(newcomer).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("known_code", [True, False])
def test_signed_out_visitor_is_sent_to_sign_in_for_any_code(client, user, other_user, known_code):
    if known_code:
        url = _invite_url(_private_invite(user, invited_user=other_user))
    else:
        url = reverse("projects:accept_or_decline", kwargs={"code": "no-such-code"})

    for response in (client.get(url), client.post(url, {"action": "accept"})):
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login") + "?next=")


@pytest.mark.django_db
def test_sign_out_and_continue_returns_to_the_invitation(client, user, other_user):
    from urllib.parse import parse_qs, urlparse

    invitation = _private_invite(user, invited_user=other_user)
    client.force_login(_make_user("carol@test.com", "Carol"))
    page = client.get(_invite_url(invitation)).content.decode()
    next_value = re.search(r'name="next" value="([^"]+)"', page).group(1).replace("&amp;", "&")

    response = client.post(reverse("accounts:logout"), {"next": next_value})

    assert "_auth_user_id" not in client.session
    location = urlparse(response["Location"])
    assert location.path == reverse("accounts:login")
    assert parse_qs(location.query)["next"] == [_invite_url(invitation)]


@pytest.mark.django_db
def test_accept_invitation_adds_member(client, user, other_user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Inv2", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    invitation = create_invitation(
        project=project,
        email=other_user.email,
        privilege=MemberPrivileges.CONTRIBUTOR,
        added_by="alice",
    )
    code = invitation.invitation_code

    client.force_login(other_user)
    # GET should render the accept page.
    get_resp = client.get(
        reverse("projects:accept_or_decline", kwargs={"code": code})
    )
    assert get_resp.status_code == 200

    # POST accept.
    post_resp = client.post(
        reverse("projects:accept_or_decline", kwargs={"code": code}),
        {"action": "accept"},
    )
    assert post_resp.status_code in (301, 302)
    assert ProjectMember.objects.filter(project=project, users=other_user).exists()
    # Invitation should be consumed.
    assert not Invitation.objects.filter(pk=invitation.pk).exists()
    assert AuditLog.objects.filter(
        action=AuditAction.INVITATION_ACCEPTED, actor=other_user, project=project
    ).exists()
    assert AuditLog.objects.filter(
        action=AuditAction.MEMBER_ADDED, actor=other_user, project=project
    ).exists()


# ---------------------------------------------------------------------------
# Join requests
# ---------------------------------------------------------------------------


def _make_project(user, *, is_public=True):
    return create_project_with_owner(
        user=user,
        form_data={
            "name": "JR Project",
            "status": ProjectStatus.ACTIVE,
            "is_public": is_public,
        },
    )


@pytest.mark.django_db
def test_create_join_request_records_audit_and_blocks_duplicates(user, other_user):
    project = _make_project(user)
    jr = create_join_request(
        project=project,
        user=other_user,
        requested_privilege=MemberPrivileges.CONTRIBUTOR,
        message="Hi!",
    )
    assert jr.status == JoinRequestStatus.PENDING
    assert AuditLog.objects.filter(
        action=AuditAction.JOIN_REQUEST_CREATED,
        actor=other_user,
        project=project,
        entity_id=jr.pk,
    ).exists()

    with pytest.raises(JoinRequestError):
        create_join_request(
            project=project,
            user=other_user,
            requested_privilege=MemberPrivileges.CONTRIBUTOR,
        )


@pytest.mark.django_db
def test_create_join_request_blocked_for_existing_members(user):
    project = _make_project(user)
    # ``user`` is the PM owner — already a member.
    with pytest.raises(JoinRequestError):
        create_join_request(
            project=project,
            user=user,
            requested_privilege=MemberPrivileges.CONTRIBUTOR,
        )


@pytest.mark.django_db
def test_approve_join_request_adds_member_at_requested_privilege(user, other_user):
    project = _make_project(user)
    jr = create_join_request(
        project=project,
        user=other_user,
        requested_privilege=MemberPrivileges.PROJECT_MANAGER,
    )
    approve_join_request(join_request=jr, approver=user)
    jr.refresh_from_db()
    assert jr.status == JoinRequestStatus.APPROVED
    assert jr.decided_by == user

    assert ProjectMember.objects.filter(project=project, users=other_user).exists()
    assert ProjectPrivilege.objects.filter(
        project=project,
        user=other_user,
        privilege=MemberPrivileges.PROJECT_MANAGER,
    ).exists()
    assert AuditLog.objects.filter(
        action=AuditAction.JOIN_REQUEST_APPROVED,
        actor=user,
        project=project,
    ).exists()
    assert AuditLog.objects.filter(
        action=AuditAction.MEMBER_ADDED, actor=user, project=project
    ).exists()


@pytest.mark.django_db
def test_decline_join_request_marks_declined_no_membership(user, other_user):
    project = _make_project(user)
    jr = create_join_request(
        project=project,
        user=other_user,
        requested_privilege=MemberPrivileges.CONTRIBUTOR,
    )
    decline_join_request(join_request=jr, approver=user, reason="Not now")
    jr.refresh_from_db()
    assert jr.status == JoinRequestStatus.DECLINED
    assert jr.decision_note == "Not now"
    assert not ProjectPrivilege.objects.filter(
        project=project, user=other_user
    ).exists()
    assert AuditLog.objects.filter(
        action=AuditAction.JOIN_REQUEST_DECLINED, project=project
    ).exists()


@pytest.mark.django_db
def test_cancel_join_request_only_requester(user, other_user, pm_user):
    project = _make_project(user)
    jr = create_join_request(
        project=project,
        user=other_user,
        requested_privilege=MemberPrivileges.CONTRIBUTOR,
    )
    with pytest.raises(JoinRequestError):
        cancel_join_request(join_request=jr, user=pm_user)

    cancel_join_request(join_request=jr, user=other_user)
    jr.refresh_from_db()
    assert jr.status == JoinRequestStatus.CANCELLED


@pytest.mark.django_db
def test_request_to_join_view_requires_login(client, user):
    project = _make_project(user)
    response = client.get(
        reverse("projects:request_to_join", kwargs={"pk": project.pk})
    )
    assert response.status_code in (301, 302)
    assert "/accounts/login" in response["Location"]


@pytest.mark.django_db
def test_request_to_join_post_creates_request(client, user, other_user):
    project = _make_project(user)
    client.force_login(other_user)
    response = client.post(
        reverse("projects:request_to_join", kwargs={"pk": project.pk}),
        {
            "requested_privilege": MemberPrivileges.PROJECT_MANAGER,
            "message": "I'd love to lead this.",
        },
    )
    assert response.status_code in (301, 302)
    jr = JoinRequest.objects.get(project=project, user=other_user)
    assert jr.requested_privilege == MemberPrivileges.PROJECT_MANAGER
    assert jr.status == JoinRequestStatus.PENDING


@pytest.mark.django_db
def test_request_to_join_redirects_when_already_member(client, user):
    project = _make_project(user)
    client.force_login(user)
    response = client.get(
        reverse("projects:request_to_join", kwargs={"pk": project.pk})
    )
    # PM owner is already a member -> redirected to detail.
    assert response.status_code in (301, 302)
    assert reverse("projects:detail", kwargs={"pk": project.pk}) in response["Location"]


@pytest.mark.django_db
def test_join_request_list_pm_only(client, user, other_user):
    project = _make_project(user)
    client.force_login(other_user)
    response = client.get(
        reverse("projects:join_requests", kwargs={"pk": project.pk})
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_approve_decline_endpoints_pm_only(client, user, other_user):
    project = _make_project(user)
    jr = create_join_request(
        project=project,
        user=other_user,
        requested_privilege=MemberPrivileges.CONTRIBUTOR,
    )
    client.force_login(other_user)
    approve_url = reverse(
        "projects:approve_join_request",
        kwargs={"pk": project.pk, "join_request_pk": jr.pk},
    )
    response = client.post(approve_url)
    assert response.status_code == 403


@pytest.mark.django_db
def test_pm_can_approve_via_view(client, user, other_user):
    project = _make_project(user)
    jr = create_join_request(
        project=project,
        user=other_user,
        requested_privilege=MemberPrivileges.CONTRIBUTOR,
    )
    client.force_login(user)
    approve_url = reverse(
        "projects:approve_join_request",
        kwargs={"pk": project.pk, "join_request_pk": jr.pk},
    )
    response = client.post(approve_url)
    assert response.status_code in (301, 302)
    jr.refresh_from_db()
    assert jr.status == JoinRequestStatus.APPROVED
    assert ProjectPrivilege.objects.filter(
        project=project, user=other_user
    ).exists()


@pytest.mark.django_db
def test_invitation_form_accepts_existing_user(client_logged_in, user, other_user):
    project = _make_project(user)
    response = client_logged_in.post(
        reverse("projects:invite", kwargs={"pk": project.pk}),
        {
            "invited_user": other_user.pk,
            "email": "",
            "privilege": MemberPrivileges.CONTRIBUTOR,
        },
    )
    assert response.status_code in (301, 302)
    invitation = Invitation.objects.get(project=project)
    assert invitation.invited_user_id == other_user.pk
    assert invitation.email == other_user.email.lower()


@pytest.mark.django_db
def test_invitation_form_rejects_existing_member(client_logged_in, user, other_user):
    project = _make_project(user)
    # Make other_user already a member.
    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    response = client_logged_in.post(
        reverse("projects:invite", kwargs={"pk": project.pk}),
        {
            "invited_user": other_user.pk,
            "email": "",
            "privilege": MemberPrivileges.CONTRIBUTOR,
        },
    )
    # invited_user queryset excludes existing members -> form refuses the choice.
    assert response.status_code == 200
    assert not Invitation.objects.filter(project=project).exists()


@pytest.mark.django_db
def test_detail_shows_request_to_join_button_for_non_members(client, user, other_user):
    project = _make_project(user)
    client.force_login(other_user)
    response = client.get(reverse("projects:detail", kwargs={"pk": project.pk}))
    assert response.status_code == 200
    content = response.content.decode()
    # The modal the button opens asks which privilege (Lead or Member) to
    # request, so the button itself names none.
    assert "Request to join" in content
    assert "contributor" not in content.lower().split("request to join", 1)[1][:40]


# ---------------------------------------------------------------------------
# Last-PM invariant
# ---------------------------------------------------------------------------


def _add_pm(project, target_user):
    """Helper: make ``target_user`` a PROJECT_MANAGER on ``project``."""
    member = ProjectMember.objects.create(project=project)
    member.users.add(target_user)
    ProjectPrivilege.objects.create(
        project=project,
        user=target_user,
        privilege=MemberPrivileges.PROJECT_MANAGER,
    )
    return member


@pytest.mark.django_db
def test_is_last_project_manager_true_when_solo(user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Solo PM", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    assert is_last_project_manager(project=project, user=user) is True


@pytest.mark.django_db
def test_is_last_project_manager_false_when_another_pm_exists(user, other_user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Two PMs", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    _add_pm(project, other_user)
    assert is_last_project_manager(project=project, user=user) is False


@pytest.mark.django_db
def test_assert_pm_self_action_blocks_solo_self_demote(user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Solo", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    with pytest.raises(LastProjectManagerError):
        assert_pm_self_action_allowed(
            project=project,
            actor=user,
            target_user=user,
            new_privilege=MemberPrivileges.CONTRIBUTOR,
        )


@pytest.mark.django_db
def test_assert_pm_self_action_blocks_solo_self_removal(user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Solo", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    with pytest.raises(LastProjectManagerError):
        assert_pm_self_action_allowed(
            project=project,
            actor=user,
            target_user=user,
            new_privilege=None,
        )


@pytest.mark.django_db
def test_assert_pm_self_action_allows_acting_on_others(user, other_user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Solo", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    _add_pm(project, other_user)
    # Solo PM demoting another PM is fine — actor remains PM.
    assert_pm_self_action_allowed(
        project=project,
        actor=user,
        target_user=other_user,
        new_privilege=MemberPrivileges.CONTRIBUTOR,
    )


@pytest.mark.django_db
def test_assert_pm_self_action_allows_self_when_coexisting_pm(user, other_user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Two", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    _add_pm(project, other_user)
    # Co-PM exists — self-demotion is allowed.
    assert_pm_self_action_allowed(
        project=project,
        actor=user,
        target_user=user,
        new_privilege=MemberPrivileges.CONTRIBUTOR,
    )


@pytest.mark.django_db
def test_last_pm_cannot_self_demote_via_view(client, user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.get(project=project, users=user)
    client.force_login(user)
    response = client.post(
        reverse(
            "projects:update_privilege",
            kwargs={"pk": project.pk, "member_pk": member.pk},
        ),
        {"privilege": MemberPrivileges.CONTRIBUTOR},
    )
    assert response.status_code in (301, 302)
    # Privilege unchanged.
    assert ProjectPrivilege.objects.filter(
        project=project, user=user, privilege=MemberPrivileges.PROJECT_MANAGER
    ).exists()
    assert not ProjectPrivilege.objects.filter(
        project=project, user=user, privilege=MemberPrivileges.CONTRIBUTOR
    ).exists()


@pytest.mark.django_db
def test_last_pm_cannot_self_demote_via_htmx_returns_400(client, user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.get(project=project, users=user)
    client.force_login(user)
    response = client.post(
        reverse(
            "projects:update_privilege",
            kwargs={"pk": project.pk, "member_pk": member.pk},
        ),
        {"privilege": MemberPrivileges.CONTRIBUTOR},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_last_pm_cannot_self_remove_via_view(client, user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.get(project=project, users=user)
    client.force_login(user)
    response = client.post(
        reverse(
            "projects:remove_member",
            kwargs={"pk": project.pk, "member_pk": member.pk},
        )
    )
    assert response.status_code in (301, 302)
    member.refresh_from_db()
    assert member.deleted is False
    assert ProjectPrivilege.objects.filter(
        project=project, user=user, privilege=MemberPrivileges.PROJECT_MANAGER
    ).exists()


@pytest.mark.django_db
def test_pm_can_self_demote_when_another_pm_exists(client, user, other_user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    _add_pm(project, other_user)
    member = ProjectMember.objects.get(project=project, users=user)
    client.force_login(user)
    response = client.post(
        reverse(
            "projects:update_privilege",
            kwargs={"pk": project.pk, "member_pk": member.pk},
        ),
        {"privilege": MemberPrivileges.CONTRIBUTOR},
    )
    assert response.status_code == 200
    assert ProjectPrivilege.objects.filter(
        project=project, user=user, privilege=MemberPrivileges.CONTRIBUTOR
    ).exists()
    assert not ProjectPrivilege.objects.filter(
        project=project, user=user, privilege=MemberPrivileges.PROJECT_MANAGER
    ).exists()


@pytest.mark.django_db
def test_pm_can_self_remove_when_another_pm_exists(client, user, other_user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    _add_pm(project, other_user)
    member = ProjectMember.objects.get(project=project, users=user)
    client.force_login(user)
    response = client.post(
        reverse(
            "projects:remove_member",
            kwargs={"pk": project.pk, "member_pk": member.pk},
        )
    )
    assert response.status_code in (301, 302)
    member.refresh_from_db()
    assert member.deleted is True
    assert not ProjectPrivilege.objects.filter(
        project=project, user=user
    ).exists()


@pytest.mark.django_db
def test_pm_can_demote_another_pm(client, user, other_user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    other_member = _add_pm(project, other_user)
    client.force_login(user)
    response = client.post(
        reverse(
            "projects:update_privilege",
            kwargs={"pk": project.pk, "member_pk": other_member.pk},
        ),
        {"privilege": MemberPrivileges.CONTRIBUTOR},
    )
    assert response.status_code == 200
    assert ProjectPrivilege.objects.filter(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    ).exists()


@pytest.mark.django_db
def test_members_page_disables_self_controls_for_last_pm(client, user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    client.force_login(user)
    response = client.get(reverse("projects:members", kwargs={"pk": project.pk}))
    assert response.status_code == 200
    content = response.content.decode()
    assert "A project must always have at least one Lead." in content


@pytest.mark.django_db
def test_dashboard_projects_card_links_to_projects_module_and_drops_old_sections(client, user):
    create_project_with_owner(
        user=user,
        form_data={"name": "Sole Project", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    client.force_login(user)
    response = client.get(reverse("projects:dashboard"))
    content = response.content.decode()

    assert response.status_code == 200
    assert reverse("projects:my_list") in content
    # The standalone Projects/Open Tasks/Join Requests/Invitations sections
    # are gone -- their content now lives in the unified timeline instead.
    assert 'id="projects-heading"' not in content
    assert 'id="active-tasks-heading"' not in content
    assert 'id="join-requests-heading"' not in content
    assert 'id="invitations-heading"' not in content


# ---------------------------------------------------------------------------
# Dashboard: active-task and request lists
# ---------------------------------------------------------------------------


def _task(project, member, name, **kwargs):
    """Create a task on ``project``, ordered after the ones before it."""
    from apps.tasks.models import KanbanBoard, Task

    column = KanbanBoard.objects.filter(project=project).first()
    return Task.objects.create(
        project=project,
        created_by=member,
        container=column,
        container_task_order=Task.objects.filter(project=project).count(),
        name=name,
        **kwargs,
    )


def _active_task_rows(content):
    """The dashboard's active-task list, sliced out of the rendered page so
    an assertion about a row cannot be satisfied by the surrounding chrome."""
    start = content.index('<ul aria-label="Active tasks"')
    return content[start : content.index("</ul>", start)]


@pytest.mark.django_db
def test_dashboard_active_task_list_orders_by_deadline_and_caps(client, user):
    """The list is ordered by deadline (undated last either way) and shows
    at most ACTIVE_TASK_LIMIT rows, with a link out to the full Tasks
    page."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.projects.views import DashboardView

    project = create_project_with_owner(
        user=user,
        form_data={"name": "Deadlines", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.filter(project=project, users=user).first()
    now = timezone.now()

    overflow = DashboardView.ACTIVE_TASK_LIMIT + 1
    for i in range(overflow):
        task = _task(project, member, f"Dated task {i}", deadline=now + timedelta(days=i + 1))
        task.assigned_members.add(member)
    undated = _task(project, member, "Undated task")
    undated.assigned_members.add(member)

    client.force_login(user)
    response = client.get(reverse("projects:dashboard"), {"sort": "oldest"})
    content = response.content.decode()

    assert response.status_code == 200
    assert 'id="dashboard-open-work-heading"' in content
    # Oldest first, capped: the earliest deadlines are shown...
    for i in range(DashboardView.ACTIVE_TASK_LIMIT):
        assert f"Dated task {i}" in content
    # ...and everything past the cap is not, undated tasks included even
    # though they sort last only because of the nulls_last ordering.
    assert f"Dated task {overflow - 1}" not in content
    assert "Undated task" not in content
    # The count on the section, and the link out, cover the whole list.
    assert response.context["active_tasks_total"] == overflow + 1
    assert f"All {overflow + 1} tasks" in content
    assert reverse("tasks:my_list") in content


@pytest.mark.django_db
def test_dashboard_active_task_list_flags_review_work_and_skips_finished_tasks(client, user):
    """A task the user only reviews is listed and marked "To review"; tasks
    the user has already marked complete, and closed tasks, are not listed."""
    from apps.tasks.models import TaskLog

    project = create_project_with_owner(
        user=user,
        form_data={"name": "Mixed", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.filter(project=project, users=user).first()

    reviewing = _task(project, member, "Task to review")
    reviewing.reviewers.add(member)

    self_marked = _task(project, member, "Task I finished")
    self_marked.assigned_members.add(member)
    TaskLog.objects.create(task=self_marked, assigned_to=member, status=TaskStatus.COMPLETED)

    closed = _task(project, member, "Closed task", status=TaskStatus.COMPLETED)
    closed.assigned_members.add(member)

    client.force_login(user)
    response = client.get(reverse("projects:dashboard"))
    content = response.content.decode()

    assert response.status_code == 200
    assert "Task to review" in content
    assert "To review" in content
    assert "Task I finished" not in content
    assert "Closed task" not in content
    assert response.context["active_tasks_total"] == 1


@pytest.mark.django_db
def test_dashboard_active_task_rows_label_the_deadline_and_drop_assignees(client, user):
    """A deadline still to come reads "Due <date>"; an overdue one keeps
    the red "Overdue" wording it already had. Neither row names who the
    task is assigned to -- on this list that is almost always the reader."""
    from datetime import timedelta

    from django.utils import timezone

    project = create_project_with_owner(
        user=user,
        form_data={"name": "Dated", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.filter(project=project, users=user).first()
    now = timezone.now()

    upcoming = _task(project, member, "Upcoming task", deadline=now + timedelta(days=3))
    upcoming.assigned_members.add(member)
    late = _task(project, member, "Late task", deadline=now - timedelta(days=3))
    late.assigned_members.add(member)

    client.force_login(user)
    content = client.get(reverse("projects:dashboard")).content.decode()
    rows = _active_task_rows(content)

    assert f"Due {(now + timedelta(days=3)).strftime('%b %-d')}" in rows
    assert f"Overdue &middot; {(now - timedelta(days=3)).strftime('%b %-d')}" in rows
    # The overdue row keeps its red treatment.
    assert "text-error font-medium" in rows
    # Neither row names who the task is assigned to. Scoped to the list:
    # the navbar prints the reader's own name too.
    assert user.name not in rows

@pytest.mark.django_db
def test_dashboard_priority_task_row_marks_priority_with_the_flag_alone(client, user):
    """A high-priority task is marked once -- the red flag, carrying its
    wording in a hover tooltip and an aria-label -- rather than by a flag
    and a red border down the row."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Urgent", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.filter(project=project, users=user).first()
    task = _task(project, member, "Urgent task", is_priority=True)
    task.assigned_members.add(member)

    client.force_login(user)
    rows = _active_task_rows(client.get(reverse("projects:dashboard")).content.decode())

    assert 'aria-label="High priority"' in rows
    assert 'data-tooltip="High priority"' in rows
    # Left-hung, so the scroll box the list sits in cannot clip it.
    assert "hover-tooltip hover-tooltip-left" in rows
    assert "border-l-error" not in rows


@pytest.mark.django_db
def test_dashboard_active_task_list_shows_an_empty_state(client, user):
    create_project_with_owner(
        user=user,
        form_data={"name": "Quiet", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    client.force_login(user)
    response = client.get(reverse("projects:dashboard"))
    content = response.content.decode()

    assert response.status_code == 200
    # Both halves of the section are empty, so it collapses to one panel
    # rather than stacking a "no tasks" and a "no requests" one.
    assert "Nothing waiting on you" in content
    assert "No tasks right now" not in content
    assert "No requests waiting" not in content
    # Nothing is hidden, so there is no "view the rest" link to render.
    assert "All 0 tasks" not in content


@pytest.mark.django_db
def test_dashboard_request_list_merges_join_requests_and_invitations(client, user, other_user):
    """Join requests on a project the user manages and invitations addressed
    to the user land in one list, each with a link to act on it."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Managed", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    join_request = create_join_request(
        project=project, user=other_user, requested_privilege=MemberPrivileges.CONTRIBUTOR
    )
    invited_to = create_project_with_owner(
        user=other_user,
        form_data={"name": "Invited", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    invitation = create_invitation(
        project=invited_to,
        email=user.email,
        privilege=MemberPrivileges.CONTRIBUTOR,
        added_by=other_user,
    )

    client.force_login(user)
    response = client.get(reverse("projects:dashboard"))
    content = response.content.decode()

    assert response.status_code == 200
    assert 'id="dashboard-open-work-heading"' in content
    assert response.context["incoming_request_count"] == 2
    assert other_user.name in content
    assert "Invited" in content
    assert (
        reverse(
            "projects:review_join_request",
            kwargs={"pk": project.pk, "join_request_pk": join_request.pk},
        )
        in content
    )
    assert (
        reverse("projects:accept_or_decline", kwargs={"code": invitation.invitation_code})
        in content
    )


@pytest.mark.django_db
def test_dashboard_request_list_ignores_requests_the_user_cannot_act_on(client, user, other_user):
    """A join request on someone else's project, and a resolved one on the
    user's own project, are both absent -- neither is waiting on the user."""
    mine = create_project_with_owner(
        user=user,
        form_data={"name": "Mine", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    theirs = create_project_with_owner(
        user=other_user,
        form_data={"name": "Theirs", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    resolved = create_join_request(
        project=mine, user=other_user, requested_privilege=MemberPrivileges.CONTRIBUTOR
    )
    resolved.status = JoinRequestStatus.DECLINED
    resolved.save(update_fields=["status"])
    create_join_request(
        project=theirs, user=user, requested_privilege=MemberPrivileges.CONTRIBUTOR
    )
    # An active task keeps the section from collapsing to its combined
    # empty state, so the requests column's own empty panel is what shows.
    member = ProjectMember.objects.filter(project=mine, users=user).first()
    _task(mine, member, "Something to do").assigned_members.add(member)

    client.force_login(user)
    response = client.get(reverse("projects:dashboard"))
    content = response.content.decode()

    assert response.status_code == 200
    assert response.context["incoming_request_count"] == 0
    assert "No requests waiting" in content
    assert "All 0 requests" not in content


@pytest.mark.django_db
def test_dashboard_type_filter_shows_one_group_at_a_time(client, user, other_user):
    """"Tasks only" drops the request rows and "Requests only" drops the
    task rows, and the section's count follows what is left showing."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Filtered", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.filter(project=project, users=user).first()
    _task(project, member, "A task of mine").assigned_members.add(member)
    create_join_request(
        project=project, user=other_user, requested_privilege=MemberPrivileges.CONTRIBUTOR
    )

    client.force_login(user)
    url = reverse("projects:dashboard")

    both = client.get(url).content.decode()
    assert "A task of mine" in both
    assert other_user.name in both

    tasks_only = client.get(url, {"type": "tasks"})
    content = tasks_only.content.decode()
    assert "A task of mine" in content
    assert other_user.name not in content
    # Not the "no requests" line either -- they were filtered out, not absent.
    assert "No requests waiting" not in content
    assert tasks_only.context["active_tasks_match_count"] == 1
    assert tasks_only.context["incoming_request_match_count"] == 0

    requests_only = client.get(url, {"type": "requests"})
    content = requests_only.content.decode()
    assert other_user.name in content
    assert "A task of mine" not in content
    assert "No tasks right now" not in content
    assert requests_only.context["incoming_request_match_count"] == 1
    assert requests_only.context["active_tasks_match_count"] == 0


@pytest.mark.django_db
def test_dashboard_sort_runs_tasks_by_deadline_in_both_directions(client, user):
    """Both sorts key off the deadline the row shows; a task with no
    deadline sorts last either way rather than leading the oldest list."""
    from datetime import timedelta

    from django.utils import timezone

    project = create_project_with_owner(
        user=user,
        form_data={"name": "Sorted", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.filter(project=project, users=user).first()
    now = timezone.now()
    _task(project, member, "Soon", deadline=now + timedelta(days=1)).assigned_members.add(member)
    _task(project, member, "Later", deadline=now + timedelta(days=9)).assigned_members.add(member)
    _task(project, member, "Undated").assigned_members.add(member)

    client.force_login(user)
    url = reverse("projects:dashboard")

    # Oldest first is the default: soonest due leads, undated trails.
    oldest = client.get(url)
    assert [t.name for t in oldest.context["active_tasks"]] == ["Soon", "Later", "Undated"]
    assert oldest.context["current_sort"] == "oldest"

    newest = client.get(url, {"sort": "newest"})
    assert [t.name for t in newest.context["active_tasks"]] == ["Later", "Soon", "Undated"]
    assert newest.context["current_sort"] == "newest"


@pytest.mark.django_db
def test_dashboard_sort_runs_requests_by_age_in_both_directions(client, user, other_user):
    """A request has no deadline, so its date is when it arrived."""
    first = create_project_with_owner(
        user=user,
        form_data={"name": "First Asked", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    second = create_project_with_owner(
        user=user,
        form_data={"name": "Second Asked", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    create_join_request(
        project=first, user=other_user, requested_privilege=MemberPrivileges.CONTRIBUTOR
    )
    create_join_request(
        project=second, user=other_user, requested_privilege=MemberPrivileges.CONTRIBUTOR
    )

    client.force_login(user)
    url = reverse("projects:dashboard")

    oldest = client.get(url)
    assert [
        item["obj"].project.name for item in oldest.context["incoming_requests"]
    ] == ["First Asked", "Second Asked"]

    newest = client.get(url, {"sort": "newest"})
    assert [
        item["obj"].project.name for item in newest.context["incoming_requests"]
    ] == ["Second Asked", "First Asked"]


@pytest.mark.django_db
def test_dashboard_ignores_unknown_filter_and_sort_values(client, user):
    """A hand-edited query string falls back to the defaults rather than
    erroring or dropping the list."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Defaults", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.filter(project=project, users=user).first()
    _task(project, member, "Still listed").assigned_members.add(member)

    client.force_login(user)
    response = client.get(
        reverse("projects:dashboard"), {"type": "bogus", "sort": "whatever"}
    )

    assert response.status_code == 200
    assert response.context["current_type"] == ""
    assert response.context["current_sort"] == "oldest"
    assert "Still listed" in response.content.decode()


@pytest.mark.django_db
def test_dashboard_overdue_type_keeps_only_tasks_past_their_deadline(client, user):
    from datetime import timedelta

    from django.utils import timezone

    project = create_project_with_owner(
        user=user,
        form_data={"name": "Due", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.filter(project=project, users=user).first()
    now = timezone.now()
    _task(project, member, "Late task", deadline=now - timedelta(days=2)).assigned_members.add(member)
    _task(project, member, "Future task", deadline=now + timedelta(days=2)).assigned_members.add(member)
    _task(project, member, "Undated task").assigned_members.add(member)

    client.force_login(user)
    response = client.get(reverse("projects:dashboard"), {"type": "overdue"})
    content = response.content.decode()

    assert response.status_code == 200
    assert "Late task" in content
    assert "Future task" not in content
    assert "Undated task" not in content
    # The list is narrowed to one row; the Tasks stat tile still counts all.
    assert response.context["active_tasks_match_count"] == 1
    assert response.context["active_tasks_total"] == 3


@pytest.mark.django_db
def test_dashboard_review_type_keeps_only_tasks_the_user_reviews(client, user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Reviewed", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.filter(project=project, users=user).first()
    _task(project, member, "Mine to do").assigned_members.add(member)
    _task(project, member, "Mine to review").reviewers.add(member)

    client.force_login(user)
    response = client.get(reverse("projects:dashboard"), {"type": "review"})
    content = response.content.decode()

    assert response.status_code == 200
    assert "Mine to review" in content
    assert "Mine to do" not in content
    assert response.context["active_tasks_match_count"] == 1


@pytest.mark.django_db
def test_dashboard_splits_requests_into_join_requests_and_invitations(client, user, other_user):
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Managed", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    create_join_request(
        project=project, user=other_user, requested_privilege=MemberPrivileges.CONTRIBUTOR
    )
    invited_to = create_project_with_owner(
        user=other_user,
        form_data={"name": "Invited Here", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    create_invitation(
        project=invited_to,
        email=user.email,
        privilege=MemberPrivileges.CONTRIBUTOR,
        added_by=other_user,
    )

    client.force_login(user)
    url = reverse("projects:dashboard")

    joins = client.get(url, {"type": "join_requests"})
    content = joins.content.decode()
    assert other_user.name in content
    assert "Invited Here" not in content
    assert joins.context["incoming_request_match_count"] == 1

    invites = client.get(url, {"type": "invitations"})
    content = invites.content.decode()
    assert "Invited Here" in content
    assert f"{other_user.name} requested to join" not in content
    assert invites.context["incoming_request_match_count"] == 1


@pytest.mark.django_db
def test_dashboard_link_and_empty_line_name_the_narrowed_set(client, user):
    """A filtered group says what it was filtered to, so "All N ..." never
    promises a count of something the user is not looking at."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.projects.views import DashboardView

    project = create_project_with_owner(
        user=user,
        form_data={"name": "Named", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.filter(project=project, users=user).first()
    now = timezone.now()
    overflow = DashboardView.ACTIVE_TASK_LIMIT + 1
    for i in range(overflow):
        _task(
            project, member, f"Late task {i}", deadline=now - timedelta(days=i + 1)
        ).assigned_members.add(member)

    client.force_login(user)
    url = reverse("projects:dashboard")

    overdue = client.get(url, {"type": "overdue"}).content.decode()
    assert f"All {overflow} overdue tasks" in overdue

    # Nothing is overdue for a user whose only task is undated.
    _task(project, member, "Someday").assigned_members.add(member)
    review = client.get(url, {"type": "review"}).content.decode()
    assert "No tasks to review right now" in review

    invitations = client.get(url, {"type": "invitations"}).content.decode()
    assert "No invitations waiting on you right now" in invitations


@pytest.mark.django_db
def test_dashboard_request_rows_pill_lead_requests_only(client, user, other_user):
    """"Member" is what nearly every request asks for, so only the
    exception -- a request to lead the project -- carries a pill."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Pilled", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    create_join_request(
        project=project, user=other_user, requested_privilege=MemberPrivileges.CONTRIBUTOR
    )

    client.force_login(user)
    content = client.get(reverse("projects:dashboard")).content.decode()
    assert other_user.name in content
    assert "Member" not in content

    lead_asker = get_user_model().objects.create_user(
        email="lead@test.com", password="password123", name="Lead Asker"
    )
    create_join_request(
        project=project, user=lead_asker, requested_privilege=MemberPrivileges.PROJECT_MANAGER
    )
    content = client.get(reverse("projects:dashboard")).content.decode()
    assert "Lead" in content
    assert "Member" not in content


# ---------------------------------------------------------------------------
# Discover projects: the projects you are already in
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_discover_marks_the_projects_you_belong_to(client, user, other_user):
    mine = create_project_with_owner(
        user=user,
        form_data={"name": "Mine", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    joined = create_project_with_owner(
        user=other_user,
        form_data={"name": "Joined", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    ProjectPrivilege.objects.create(
        project=joined, user=user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    theirs = create_project_with_owner(
        user=other_user,
        form_data={"name": "Theirs", "status": ProjectStatus.ACTIVE, "is_public": True},
    )

    client.force_login(user)
    rows = {
        row["project"].name: row["my_privilege"]
        for row in client.get(reverse("projects:list")).context["project_rows"]
    }
    assert rows["Mine"] == MemberPrivileges.PROJECT_MANAGER
    assert rows["Joined"] == MemberPrivileges.CONTRIBUTOR
    # Nothing on a project you have no standing in.
    assert rows["Theirs"] is None


@pytest.mark.django_db
def test_discover_shows_the_membership_pill(client, user, other_user):
    create_project_with_owner(
        user=user,
        form_data={"name": "Mine", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    create_project_with_owner(
        user=other_user,
        form_data={"name": "Theirs", "status": ProjectStatus.ACTIVE, "is_public": True},
    )

    client.force_login(user)
    body = client.get(reverse("projects:list")).content.decode()
    # "Lead" is the label for the privilege that owning a project grants.
    assert "Lead" in body


@pytest.mark.django_db
def test_discover_marks_nothing_for_an_anonymous_visitor(client, user):
    """The page is open to visitors, who belong to nothing."""
    create_project_with_owner(
        user=user,
        form_data={"name": "Mine", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    response = client.get(reverse("projects:list"))
    assert response.status_code == 200
    assert [row["my_privilege"] for row in response.context["project_rows"]] == [None]


# ---------------------------------------------------------------------------
# Project dashboard: wiki card
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_project_dashboard_hides_wiki_from_non_members(client, user, other_user):
    """The wiki is members-only, and so is its card on a public project."""
    from apps.wiki.services import create_page

    project = _make_project(user)
    create_page(project=project, actor=user, title="Internal notes")
    client.force_login(other_user)
    content = client.get(
        reverse("projects:detail", kwargs={"pk": project.pk})
    ).content.decode()
    assert "Internal notes" not in content
    assert "Wiki pages" not in content


# ---------------------------------------------------------------------------
# Contributors sub-pages: laid out like a Wiki sub-page
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url_name, heading",
    [
        ("projects:join_requests", "Pending join requests"),
        ("projects:project_invitations", "Pending invitation requests"),
    ],
)
def test_contributor_subpages_look_like_wiki_subpages(client, user, url_name, heading):
    project = _make_project(user)
    client.force_login(user)
    content = client.get(reverse(url_name, kwargs={"pk": project.pk})).content.decode()

    # The project's own header and tab strip, with Contributors as the tab.
    assert f">{project.name}</h1>" in content
    strip = content[content.index('aria-label="Project sections"'):]
    strip = strip[: strip.index("</nav>")]
    members_url = reverse("projects:members", kwargs={"pk": project.pk})
    assert f'href="{members_url}"\n       class="project-tab active"' in strip
    # Back to the tab first, then the page's own heading.
    back = f'href="{members_url}"\n     class="btn btn-sm btn-ghost text-brand-navy">&larr; All contributors</a>'
    assert back in content
    assert content.index(back) < content.index(f">{heading}</h2>")


# ---------------------------------------------------------------------------
# Project Overview: what the tab reports
# ---------------------------------------------------------------------------


def _summary_project(user, other_user):
    """A project with one task in each state the summary cards report."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.projects.models import ContributionTracking as CT
    from apps.tasks.models import KanbanBoard, Milestone, Task
    from apps.tasks.services import close_task, mark_own_done

    project = _make_project(user)
    project.contribution_tracking = CT.CREDIT
    project.save()
    lead = ProjectMember.objects.filter(project=project, users=user).first()
    helper = ProjectMember.objects.create(project=project)
    helper.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    col = KanbanBoard.objects.create(project=project, name="Doing", container_order=0)

    def task(name, **kw):
        t = Task.objects.create(
            project=project, created_by=lead, container=col,
            container_task_order=Task.objects.count(), name=name, **kw,
        )
        t.assigned_members.add(helper)
        return t

    now = timezone.now()
    done = task("Done one")
    mark_own_done(task=done, member=helper)
    close_task(task=done, closed_by=lead)                       # completed
    waiting = task("Waiting one")
    mark_own_done(task=waiting, member=helper)                  # ready for a lead
    task("Late one", deadline=now - timedelta(days=2))          # overdue
    task("Soon one", deadline=now + timedelta(days=3))          # coming up
    Milestone.objects.create(project=project, name="Ship it", end_date=now + timedelta(days=1))
    finished_late = task("Finished late", deadline=now - timedelta(days=5))
    mark_own_done(task=finished_late, member=helper)
    close_task(task=finished_late, closed_by=lead)              # not overdue
    return project


@pytest.mark.django_db
def test_summary_on_an_empty_project(client, user):
    project = _make_project(user)
    client.force_login(user)
    response = client.get(reverse("projects:detail", kwargs={"pk": project.pk}))
    body = response.content.decode()
    assert "Nothing has happened here yet." in body


@pytest.mark.django_db
def test_summary_reports_activity(client, user, other_user):
    project = _summary_project(user, other_user)

    client.force_login(user)
    response = client.get(reverse("projects:detail", kwargs={"pk": project.pk}))
    summary = response.context["summary"]

    # A task finished after its deadline is not overdue.
    assert response.context["overdue_count"] == 1

    # Recent activity covers completions and done marks, newest first.
    verbs = [e["verb"] for e in summary["activity"]]
    assert "completed" in verbs and "marked their part done on" in verbs
    whens = [e["when"] for e in summary["activity"]]
    assert whens == sorted(whens, reverse=True)
    assert all(e["ago"] == "just now" for e in summary["activity"])

    body = response.content.decode()
    # The Progress, Needs attention and Coming up cards are gone.
    for heading in ("proj-progress-heading", "proj-attention-heading", "proj-upcoming-heading"):
        assert f'id="{heading}"' not in body


@pytest.mark.django_db
def test_summary_activity_lists_reviews_verifications_and_who_completed(client, user, other_user):
    from apps.tasks.models import KanbanBoard, Task
    from apps.tasks.services import close_task, mark_own_done, mark_reviewed, verify_part

    project = _make_project(user)
    lead = ProjectMember.objects.filter(project=project, users=user).first()  # Alice
    bob = ProjectMember.objects.create(project=project)
    bob.users.add(other_user)
    carol_user = get_user_model().objects.create_user(
        email="carol@example.com", password="pw-123456789", name="Carol"
    )
    carol = ProjectMember.objects.create(project=project)
    carol.users.add(carol_user)
    col = KanbanBoard.objects.create(project=project, name="Doing", container_order=0)

    reviewed = Task.objects.create(
        project=project, created_by=lead, container=col,
        container_task_order=0, name="Reviewed one",
    )
    reviewed.assigned_members.add(bob, carol)
    reviewed.reviewers.add(lead)
    bob_log = mark_own_done(task=reviewed, member=bob)
    carol_log = mark_own_done(task=reviewed, member=carol)

    by_hand = Task.objects.create(
        project=project, created_by=lead, container=col,
        container_task_order=1, name="Closed by hand",
    )
    close_task(task=by_hand, closed_by=lead)

    url = reverse("projects:detail", kwargs={"pk": project.pk})
    client.force_login(user)

    # Every part reviewed: a review line each, and the task stays open — only
    # a Lead completes a task.
    mark_reviewed(task_log=bob_log, reviewer=lead)
    mark_reviewed(task_log=carol_log, reviewer=lead)
    reviewed.refresh_from_db()
    assert reviewed.status == "NOT_COMPLETED"

    verify_part(task_log=bob_log, lead=lead)
    response = client.get(url)
    lines = [
        (e["who"], e["verb"], e["task"].name)
        for e in response.context["summary"]["activity"] if e["kind"] == "task"
    ]
    assert lines[0] == ("Alice", "verified Bob's part on", "Reviewed one")
    assert ("Alice", "reviewed Carol's part on", "Reviewed one") in lines
    assert ("Alice", "completed", "Reviewed one") not in lines
    assert ("Alice", "completed", "Closed by hand") in lines

    body = response.content.decode()
    assert "verified Bob&#x27;s part on" in body
    assert "reviewed Bob&#x27;s part on" in body


@pytest.mark.django_db
def test_summary_number_cards_open_their_tabs(client, user):
    project = _make_project(user)
    client.force_login(user)
    body = client.get(reverse("projects:detail", kwargs={"pk": project.pk})).content.decode()
    board = reverse("tasks:board", kwargs={"project_pk": project.pk})
    for url in (
        board,                                  # Tasks
        f"{board}?filter=overdue",              # Overdue: the board, overdue only
        reverse("projects:join_requests", kwargs={"pk": project.pk}),
        reverse("projects:members", kwargs={"pk": project.pk}),
        reverse("wiki:list", kwargs={"project_pk": project.pk}),
    ):
        assert f'<a href="{url}"\n   class="brand-card p-4' in body, url
    # The wiki's number is labelled "Wiki", not "Wiki pages".
    assert ">Wiki pages</span>" not in body
    assert ">Wiki</span>" in body


@pytest.mark.django_db
def test_overview_is_one_full_width_column(client, user):
    """About, then the Wiki, then Recent activity, each the full width."""
    project = _make_project(user)
    client.force_login(user)
    body = client.get(reverse("projects:detail", kwargs={"pk": project.pk})).content.decode()
    assert "proj-columns" not in body
    assert (
        body.index('id="proj-about-heading"')
        < body.index('id="proj-wiki-heading"')
        < body.index('id="proj-activity-heading"')
    )


@pytest.mark.django_db
def test_summary_is_a_report_not_a_workbench(client, user):
    """Adding and editing live on the tabs; the summary only reports."""
    project = _make_project(user)
    client.force_login(user)
    body = client.get(reverse("projects:detail", kwargs={"pk": project.pk})).content.decode()
    assert reverse("tasks:create", kwargs={"project_pk": project.pk}) not in body
    assert reverse("wiki:create", kwargs={"project_pk": project.pk}) not in body
    assert "Delete task confirmation" not in body


@pytest.mark.django_db
def test_overview_wiki_card_lists_the_pages_put_on_the_overview(client, user):
    """Only the pages a lead put on the Overview, in one list, the most
    recently updated first; a locked page carries a lock."""
    from apps.wiki.services import create_page, update_page

    project = _make_project(user)
    on = {"show_on_overview": True}
    create_page(project=project, actor=user, title="Team agreement", locked=True, **on)
    create_page(project=project, actor=user, title="Data protocol", locked=True, **on)
    notes = create_page(project=project, actor=user, title="Meeting notes", **on)
    create_page(project=project, actor=user, title="Glossary", **on)
    create_page(project=project, actor=user, title="Scratchpad")          # not on it
    create_page(project=project, actor=user, title="Old rules", locked=True)  # nor this
    update_page(page=notes, actor=user, title=notes.title, content_markdown="Updated.")

    client.force_login(user)
    response = client.get(reverse("projects:detail", kwargs={"pk": project.pk}))
    wiki = response.context["summary"]["wiki"]
    assert [page.title for page in wiki["pages"]] == [
        "Meeting notes", "Glossary", "Data protocol", "Team agreement",
    ]

    body = response.content.decode()
    card = body[body.index('id="proj-wiki-heading"'):]
    card = card[: card.index("</section>")]
    assert "Key pages" not in card and "Recently updated" not in card
    # One plain list, no rail.
    assert card.count("<ul") == 1 and "wiki-rail" not in card
    order = [card.index(f">{t}</a>") for t in ("Meeting notes", "Glossary", "Data protocol", "Team agreement")]
    assert order == sorted(order)
    assert card.count('aria-label="Locked page"') == 2
    assert "Scratchpad" not in card and "Old rules" not in card
    # No dates, no rules between rows; the wiki holds more, so "See all".
    assert "Oct" not in card and "divide-y" not in card
    assert "See all" in card


@pytest.mark.django_db
def test_overview_wiki_card_lists_every_page_put_on_the_overview(client, user):
    from apps.wiki.services import create_page

    project = _make_project(user)
    create_page(
        project=project, actor=user, title="Team agreement", locked=True, show_on_overview=True
    )
    for title in ("One", "Two", "Three", "Four", "Five", "Six"):
        create_page(project=project, actor=user, title=title, show_on_overview=True)

    client.force_login(user)
    url = reverse("projects:detail", kwargs={"pk": project.pk})
    response = client.get(url)
    titles = [page.title for page in response.context["summary"]["wiki"]["pages"]]
    # No cap, and newest update first: being locked does not move a page up.
    assert titles == ["Six", "Five", "Four", "Three", "Two", "One", "Team agreement"]

    card = _wiki_card(response.content.decode())
    assert all(f">{t}</a>" in card for t in titles)
    # Every page in the wiki is already listed: nothing more to see.
    assert "See all" not in card

    # A page not on the Overview: "See all" opens the Wiki for it.
    create_page(project=project, actor=user, title="Scratchpad")
    card = _wiki_card(client.get(url).content.decode())
    wiki_url = reverse("wiki:list", kwargs={"project_pk": project.pk})
    assert "Scratchpad" not in card
    assert f'<a href="{wiki_url}"' in card and "See all" in card
    # A plain button at the top right, beside the title, above the list.
    assert f'<a href="{wiki_url}" class="btn btn-sm btn-ghost">See all</a>' in card
    assert card.index("See all") < card.index("<ul")


def _wiki_card(body: str) -> str:
    """The Overview's Wiki card alone, out of the whole page."""
    card = body[body.index('id="proj-wiki-heading"'):]
    return card[: card.index("</section>")]


@pytest.mark.django_db
def test_overview_wiki_card_when_nothing_is_on_the_overview_or_written(client, user, other_user):
    from apps.wiki.services import create_page, update_page

    project = _make_project(user)
    url = reverse("projects:detail", kwargs={"pk": project.pk})
    client.force_login(user)
    body = client.get(url).content.decode()
    assert "No wiki pages yet." in body and "Go to the Wiki" in body

    # Pages, none on the Overview: the card says so, and a lead is told how
    # to put one there; a member is not.
    notes = create_page(project=project, actor=user, title="Notes")
    lead_body = client.get(url).content.decode()
    assert "No wiki pages are on the Overview yet." in lead_body
    assert "Show on Project Overview" in lead_body
    assert ">Notes</a>" not in _wiki_card(lead_body)

    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    client.force_login(other_user)
    member_body = client.get(url).content.decode()
    assert "No wiki pages are on the Overview yet." in member_body
    assert "Show on Project Overview" not in member_body

    # A lead puts it on the Overview: every member now sees it listed.
    update_page(
        page=notes, actor=user, title=notes.title, content_markdown="", show_on_overview=True
    )
    assert ">Notes</a>" in _wiki_card(client.get(url).content.decode())


@pytest.mark.django_db
def test_overview_wiki_follows_who_can_see_each_page(client, user, other_user):
    """A leads-only page is on a lead's Overview only — not on the card, in
    Recent activity or in the Wiki count for anyone else — and a visitor to
    a public project sees just the public pages put on the Overview."""
    from apps.wiki.models import WikiVisibility
    from apps.wiki.services import create_page

    project = _make_project(user)
    project.is_public = True
    project.save()
    on = {"show_on_overview": True}
    create_page(project=project, actor=user, title="Budget", visibility=WikiVisibility.LEADS, **on)
    create_page(project=project, actor=user, title="Protocol", **on)
    create_page(project=project, actor=user, title="About us", visibility=WikiVisibility.PUBLIC, **on)
    create_page(project=project, actor=user, title="Press kit", visibility=WikiVisibility.PUBLIC)
    url = reverse("projects:detail", kwargs={"pk": project.pk})

    client.force_login(user)
    lead = client.get(url)
    assert {p.title for p in lead.context["summary"]["wiki"]["pages"]} == {
        "Budget", "Protocol", "About us",
    }
    assert lead.context["wiki_page_count"] == 4

    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    client.force_login(other_user)
    contributor = client.get(url)
    assert {p.title for p in contributor.context["summary"]["wiki"]["pages"]} == {
        "Protocol", "About us",
    }
    assert contributor.context["wiki_page_count"] == 3
    assert "Budget" not in contributor.content.decode()  # not in Recent activity either

    # A visitor: not a member of this public project.
    member.delete()
    ProjectPrivilege.objects.filter(project=project, user=other_user).delete()
    visitor = client.get(url)
    assert [p.title for p in visitor.context["public_wiki_pages"]] == ["About us"]
    body = visitor.content.decode()
    assert 'id="proj-public-wiki-heading"' in body and ">About us</a>" in body
    assert "Protocol" not in body and "Budget" not in body and "Press kit" not in body


@pytest.mark.django_db
def test_members_table_hides_the_roles_column_by_default(client, user):
    """Roles is hidden for now: no header, no cells, and the (i) stops
    promising them. The column comes back with SHOW_MEMBER_ROLES."""
    from django.test import override_settings

    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    client.force_login(user)
    url = reverse("projects:members", kwargs={"pk": project.pk})

    content = client.get(url).content.decode()
    thead = content[content.index("<thead") : content.index("</thead>")]
    assert "Roles" not in thead
    assert "privilege and roles" not in content
    assert "Privilege" in thead

    with override_settings(SHOW_MEMBER_ROLES=True):
        content = client.get(url).content.decode()
    thead = content[content.index("<thead") : content.index("</thead>")]
    assert "Roles" in thead
    assert "privilege and roles" in content


@pytest.mark.django_db
def test_members_table_sorts_by_every_header_but_actions(client, user, other_user):
    """Name and Privilege are sort buttons, Actions is not, and each row
    carries what it sorts by — Privilege as a rank, Lead first."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    client.force_login(user)

    content = client.get(
        reverse("projects:members", kwargs={"pk": project.pk})
    ).content.decode()
    for key in ("name", "privilege"):
        assert f"@click=\"sortBy('{key}')\"" in content, key
    assert "sortBy('email')" not in content, "the email has no column to sort"
    assert "sortBy('roles')" not in content, "Roles is hidden, so is its sort"
    assert "sortBy('actions')" not in content
    assert '<th scope="col" class="text-right">Actions</th>' in content

    row = content[content.index(f'id="member-row-{member.pk}"') :]
    row = row[: row.index(">")]
    assert f'data-order="{member.pk}"' in row
    assert 'data-sort-privilege="1"' in row
    assert 'data-sort-privilege="0"' in content, "the Lead ranks first"

    with override_settings(SHOW_MEMBER_ROLES=True):
        content = client.get(
            reverse("projects:members", kwargs={"pk": project.pk})
        ).content.decode()
    assert "@click=\"sortBy('roles')\"" in content


@pytest.mark.django_db
def test_join_requests_table_sorts_by_every_header_but_actions(client, user, other_user):
    """User, Requested as, Message and Submitted are sort buttons, Actions is
    not, and each row carries what it sorts by."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    jr = JoinRequest.objects.create(
        project=project, user=other_user, message="Keen To Help",
        requested_privilege=MemberPrivileges.CONTRIBUTOR,
    )
    client.force_login(user)

    content = client.get(
        reverse("projects:join_requests", kwargs={"pk": project.pk})
    ).content.decode()
    for key in ("user", "privilege", "message", "submitted"):
        assert f"@click=\"sortBy('{key}')\"" in content, key
    assert '<th scope="col" class="text-right">Actions</th>' in content
    row = content[content.index('data-order="0"') :]
    row = row[: row.index(">")]
    assert 'data-sort-privilege="1"' in row
    assert 'data-sort-message="keen to help"' in row
    assert f'data-sort-submitted="{int(jr.created_at.timestamp())}"' in row


@pytest.mark.django_db
def test_invitation_requests_table_sorts_by_every_header_but_actions(client, user):
    """Invited, Privilege, Sent by and Date are sort buttons, Actions is not,
    and each row carries what it sorts by."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    invitation = Invitation.objects.create(
        project=project, email="Eve@Example.org",
        privilege=MemberPrivileges.PROJECT_MANAGER, invitation_code="sortme",
    )
    client.force_login(user)

    content = client.get(
        reverse("projects:project_invitations", kwargs={"pk": project.pk})
    ).content.decode()
    for key in ("invited", "privilege", "sender", "date"):
        assert f"@click=\"sortBy('{key}')\"" in content, key
    assert '<th scope="col" class="text-right">Actions</th>' in content
    row = content[content.index('data-order="0"') :]
    row = row[: row.index(">")]
    assert 'data-sort-invited="eve@example.org"' in row
    assert 'data-sort-privilege="0"' in row
    assert f'data-sort-date="{int(invitation.created_at.timestamp())}"' in row


@pytest.mark.django_db
def test_pending_requests_tables_show_privilege_as_plain_text(client, user, other_user):
    """Requested as and Privilege read as plain text, not a pill."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    JoinRequest.objects.create(
        project=project, user=other_user,
        requested_privilege=MemberPrivileges.PROJECT_MANAGER,
    )
    Invitation.objects.create(
        project=project, email="eve@example.org",
        privilege=MemberPrivileges.CONTRIBUTOR, invitation_code="plain",
    )
    client.force_login(user)

    for name, label in (("join_requests", "Lead"), ("project_invitations", "Member")):
        content = client.get(reverse(f"projects:{name}", kwargs={"pk": project.pk})).content.decode()
        tbody = content[content.index("<tbody") : content.index("</tbody>")]
        assert f'<td class="text-xs text-brand-text/80">{label}</td>' in tbody, name
        assert "rounded-full" not in tbody, f"{name} still draws a pill"


@pytest.mark.django_db
def test_members_table_puts_the_email_under_the_name(client, user, other_user):
    """No Email column: a person's email sits under their name, and a person
    with no name, whose email already stands in for it, is not shown twice."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    client.force_login(user)

    content = client.get(
        reverse("projects:members", kwargs={"pk": project.pk})
    ).content.decode()
    thead = content[content.index("<thead") : content.index("</thead>")]
    assert "Email" not in thead
    row = content[content.index(f'id="member-row-{member.pk}"') :]
    row = row[: row.index("</tr>")]
    name_cell = row[row.index("<td") : row.index("</td>")]
    assert other_user.name in name_cell
    assert f'<div class="text-xs text-brand-text/60">{other_user.email}</div>' in name_cell

    other_user.name = ""
    other_user.save(update_fields=["name"])
    content = client.get(
        reverse("projects:members", kwargs={"pk": project.pk})
    ).content.decode()
    row = content[content.index(f'id="member-row-{member.pk}"') :]
    name_cell = row[row.index("<td") : row.index("</td>")]
    assert name_cell.count(other_user.email) == 1


@pytest.mark.django_db
def test_members_table_shows_privilege_as_plain_text(client, user, other_user):
    """The Privilege cell reads as plain text, for a Lead too: no inline
    select, no pill."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    client.force_login(user)

    content = client.get(
        reverse("projects:members", kwargs={"pk": project.pk})
    ).content.decode()
    tbody = content[content.index("<tbody") : content.index("</tbody>")]
    assert '<td class="text-xs text-brand-text/80">Member</td>' in tbody
    assert '<td class="text-xs text-brand-text/80">Lead</td>' in tbody
    assert "<select" not in tbody
    assert "rounded-full text-xs font-medium" not in tbody


@pytest.mark.django_db
def test_members_table_offers_modify_privilege_to_a_lead(client, user, other_user):
    """A Lead gets Modify privilege under Actions, opening one shared modal;
    the last Lead's own row has it disabled; a Member gets neither."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    url = reverse("projects:members", kwargs={"pk": project.pk})
    update_url = reverse(
        "projects:update_privilege", kwargs={"pk": project.pk, "member_pk": member.pk}
    )

    client.force_login(user)
    content = client.get(url).content.decode()
    assert 'id="privilege-modal"' in content
    assert '<option value="PROJECT_MANAGER">Lead</option>' in content
    row = content[content.index(f'id="member-row-{member.pk}"') :]
    row = row[: row.index("</tr>")]
    assert f"openPrivilegeModal('{update_url}', 'Bob', 'CONTRIBUTOR', 'member-row-{member.pk}')" in row
    own = content[content.index("member-row-") :]
    own = own[: own.index("</tr>")]
    assert "Modify privilege" in own and "openPrivilegeModal" not in own, "the last Lead cannot step down"

    client.force_login(other_user)
    content = client.get(url).content.decode()
    assert "Modify privilege" not in content
    assert 'id="privilege-modal"' not in content


@pytest.mark.django_db
def test_members_table_action_buttons_styles(client, user, other_user):
    """Modify privilege looks like Resend on Pending invitation requests, a
    plain navy button; Remove is red text, like Cancel there."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    client.force_login(user)

    content = client.get(
        reverse("projects:members", kwargs={"pk": project.pk})
    ).content.decode()
    row = content[content.index(f'id="member-row-{member.pk}"') :]
    row = row[: row.index("</tr>")]
    red = 'class="btn btn-sm btn-ghost text-error hover:bg-error/10"'
    modify = row[row.index("openPrivilegeModal") - 200 : row.index("openPrivilegeModal")]
    assert 'class="btn btn-sm btn-ghost text-brand-navy hover:bg-brand-lighter"' in modify, "Modify privilege looks like Resend"
    remove = row[row.index("openRemoveMemberModal") - 200 : row.index("openRemoveMemberModal")]
    assert red in remove, "Remove is red text, like Cancel"
    assert "bg-brand-coral" not in row


@pytest.mark.django_db
def test_join_requests_review_button_looks_like_resend(client, user, other_user):
    """Review on Pending join requests wears Resend's plain navy style."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    JoinRequest.objects.create(project=project, user=other_user)
    client.force_login(user)

    content = client.get(
        reverse("projects:join_requests", kwargs={"pk": project.pk})
    ).content.decode()
    tbody = content[content.index("<tbody") : content.index("</tbody>")]
    assert 'class="btn btn-sm btn-ghost text-brand-navy hover:bg-brand-lighter"' in tbody
    assert "bg-brand-coral" not in tbody


@pytest.mark.django_db
def test_review_join_request_modal_is_the_plain_modal(client, user, other_user):
    """The Review modal has the Contributors modals' plain shape: a navy
    title, the request in a sentence, Cancel / Decline / Approve on the
    right — no icon badge, pill or close cross."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "Bird Song", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    jr = JoinRequest.objects.create(
        project=project, user=other_user, message="Keen to help",
        requested_privilege=MemberPrivileges.CONTRIBUTOR,
    )
    client.force_login(user)

    html = client.get(
        reverse(
            "projects:review_join_request",
            kwargs={"pk": project.pk, "join_request_pk": jr.pk},
        ),
        headers={"hx-request": "true"},
    ).content.decode()
    assert '<h3 class="text-lg font-semibold text-brand-navy">Review join request</h3>' in html
    assert "wants to join <strong>Bird Song</strong> as Member." in html
    assert "Keen to help" in html
    assert html.index(">Cancel<") < html.index(">Decline<") < html.index("Approve")
    assert "rounded-full" not in html and "btn-circle" not in html


@pytest.mark.django_db
def test_cancel_invitation_asks_first(client, user):
    """Cancel opens a confirmation, in the Remove modal's design, that says
    it cannot be undone and that a new invitation is needed to invite them
    again; the row itself no longer posts the cancel."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    invitation = Invitation.objects.create(
        project=project, email="eve@example.org",
        privilege=MemberPrivileges.CONTRIBUTOR, invitation_code="askfirst",
    )
    client.force_login(user)
    cancel_url = reverse(
        "projects:cancel_invitation",
        kwargs={"pk": project.pk, "invitation_pk": invitation.pk},
    )

    content = client.get(
        reverse("projects:project_invitations", kwargs={"pk": project.pk})
    ).content.decode()
    tbody = content[content.index("<tbody") : content.index("</tbody>")]
    assert f"openCancelInvitationModal('{cancel_url}', 'eve@example.org')" in tbody
    assert f'action="{cancel_url}"' not in tbody, "the row no longer cancels at once"
    modal = content[content.index('id="cancel-invitation-modal"') :]
    modal = modal[: modal.index("</dialog>")]
    assert 'class="modal-box border border-error/40"' in modal
    assert "This cannot be undone." in modal
    assert "a new invitation will need to be sent" in modal

    # The confirmation's form posts to the same endpoint, which still cancels.
    client.post(cancel_url)
    assert not Invitation.objects.filter(pk=invitation.pk).exists()


@pytest.mark.django_db
def test_members_table_shows_when_each_member_joined(client, user, other_user):
    """A sortable Joined column gives the date the membership was created,
    written as tasks write dates ("Mar 4, 2026")."""
    from datetime import datetime, timezone as dt_tz

    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    joined = datetime(2026, 3, 4, 12, 0, tzinfo=dt_tz.utc)
    ProjectMember.objects.filter(pk=member.pk).update(created_at=joined)
    client.force_login(user)

    content = client.get(
        reverse("projects:members", kwargs={"pk": project.pk})
    ).content.decode()
    thead = content[content.index("<thead") : content.index("</thead>")]
    assert "sortBy('joined')" in thead
    assert thead.index("Privilege") < thead.index("Joined") < thead.index("Actions")
    row = content[content.index(f'id="member-row-{member.pk}"') :]
    row = row[: row.index("</tr>")]
    assert f'data-sort-joined="{int(joined.timestamp())}"' in row
    assert '<td class="text-xs text-brand-text/70 whitespace-nowrap">Mar 4, 2026</td>' in row


@pytest.mark.django_db
def test_privilege_change_redraws_the_viewers_own_row_out_of_band(client, user, other_user):
    """Promoting someone frees the last Lead's own buttons at once, and
    demoting them again locks them: the viewer's row rides along out of band."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.CONTRIBUTOR
    )
    own = ProjectMember.objects.get(project=project, users=user)
    url = reverse(
        "projects:update_privilege", kwargs={"pk": project.pk, "member_pk": member.pk}
    )
    client.force_login(user)

    def own_row(html):
        start = html.index(f'id="member-row-{own.pk}"')
        return html[html.rindex("<tr", 0, start) : html.index("</tr>", start)]

    html = client.post(
        url, {"privilege": MemberPrivileges.PROJECT_MANAGER}, headers={"hx-request": "true"}
    ).content.decode()
    assert f'id="member-row-{member.pk}"' in html
    row = own_row(html)
    assert 'hx-swap-oob="true"' in row
    assert "openPrivilegeModal" in row and "openRemoveMemberModal" in row, "no longer the last Lead"

    html = client.post(
        url, {"privilege": MemberPrivileges.CONTRIBUTOR}, headers={"hx-request": "true"}
    ).content.decode()
    row = own_row(html)
    assert "openPrivilegeModal" not in row and "disabled" in row, "the last Lead again"


@pytest.mark.django_db
def test_removing_a_lead_redraws_the_viewers_own_row_out_of_band(client, user, other_user):
    """Removing the other Lead leaves the viewer the last one: the empty
    response that drops the row carries the viewer's row, now locked."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    member = ProjectMember.objects.create(project=project)
    member.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.PROJECT_MANAGER
    )
    own = ProjectMember.objects.get(project=project, users=user)
    client.force_login(user)

    html = client.post(
        reverse("projects:remove_member", kwargs={"pk": project.pk, "member_pk": member.pk}),
        headers={"hx-request": "true"},
    ).content.decode()
    assert f'id="member-row-{member.pk}"' not in html
    assert f'id="member-row-{own.pk}"' in html and 'hx-swap-oob="true"' in html
    assert "openRemoveMemberModal" not in html, "the viewer is the last Lead now"


@pytest.mark.django_db
def test_a_lead_stepping_down_reloads_the_page(client, user, other_user):
    """A Lead who demotes themselves gets the whole page reloaded (HX-Refresh)
    with a note, since nothing on it is theirs to act on any more."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    other = ProjectMember.objects.create(project=project)
    other.users.add(other_user)
    ProjectPrivilege.objects.create(
        project=project, user=other_user, privilege=MemberPrivileges.PROJECT_MANAGER
    )
    own = ProjectMember.objects.get(project=project, users=user)
    client.force_login(user)

    response = client.post(
        reverse("projects:update_privilege", kwargs={"pk": project.pk, "member_pk": own.pk}),
        {"privilege": MemberPrivileges.CONTRIBUTOR},
        headers={"hx-request": "true"},
    )
    assert response.status_code == 200
    assert response["HX-Refresh"] == "true"
    assert ProjectPrivilege.objects.get(project=project, user=user).privilege == MemberPrivileges.CONTRIBUTOR

    page = client.get(reverse("projects:members", kwargs={"pk": project.pk})).content.decode()
    assert "You are now a Member of this project." in page
    assert "Modify privilege" not in page


@pytest.mark.django_db
def test_people_with_accounts_link_to_their_profile(client, user, other_user):
    """On Contributors, Pending join requests and Pending invitation requests
    a name with an account behind it opens that profile; an invited email
    with no account stays plain text."""
    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    JoinRequest.objects.create(project=project, user=other_user)
    Invitation.objects.create(
        project=project, email=other_user.email, invited_user=other_user,
        privilege=MemberPrivileges.CONTRIBUTOR, invitation_code="has-account",
    )
    Invitation.objects.create(
        project=project, email="nobody@example.org",
        privilege=MemberPrivileges.CONTRIBUTOR, invitation_code="no-account",
    )
    client.force_login(user)

    def tbody(name):
        html = client.get(reverse(f"projects:{name}", kwargs={"pk": project.pk})).content.decode()
        return html[html.index("<tbody") : html.index("</tbody>")]

    members = tbody("members")
    assert f'href="{reverse("public_profile", kwargs={"pk": user.pk})}"' in members
    assert f'href="{reverse("public_profile", kwargs={"pk": other_user.pk})}"' in tbody("join_requests")
    invitations = tbody("project_invitations")
    assert f'href="{reverse("public_profile", kwargs={"pk": other_user.pk})}"' in invitations
    assert invitations.count('href="/people/') == 1, "the email with no account is not a link"
    assert "nobody@example.org" in invitations


@pytest.mark.django_db
def test_pending_requests_tables_write_dates_as_tasks_do(client, user, other_user):
    """Submitted (join requests) and Date (invitations) read "Mar 4, 2026"."""
    from datetime import datetime, timezone as dt_tz

    project = create_project_with_owner(
        user=user,
        form_data={"name": "P", "status": ProjectStatus.ACTIVE, "is_public": True},
    )
    when = datetime(2026, 3, 4, 12, 0, tzinfo=dt_tz.utc)
    jr = JoinRequest.objects.create(project=project, user=other_user)
    JoinRequest.objects.filter(pk=jr.pk).update(created_at=when)
    client.force_login(user)

    html = client.get(reverse("projects:join_requests", kwargs={"pk": project.pk})).content.decode()
    assert '<td class="text-xs text-brand-text/70 whitespace-nowrap">Mar 4, 2026</td>' in html
    assert "2026-03-04" not in html

    invitation = Invitation.objects.create(
        project=project, email="eve@example.org",
        privilege=MemberPrivileges.CONTRIBUTOR, invitation_code="dated",
    )
    Invitation.objects.filter(pk=invitation.pk).update(created_at=when)
    html = client.get(reverse("projects:project_invitations", kwargs={"pk": project.pk})).content.decode()
    assert '<td class="text-xs text-brand-text/70 whitespace-nowrap">Mar 4, 2026</td>' in html
    assert "2026-03-04" not in html
