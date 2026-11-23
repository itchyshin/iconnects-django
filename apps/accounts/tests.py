"""Smoke tests for the accounts app."""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.urls import reverse

from apps.accounts.models import Widget


@pytest.mark.django_db
def test_signup_creates_unconfirmed_user_and_does_not_log_in(client):
    """POST /accounts/signup/ creates the user but does NOT log them in.

    Mandatory email confirmation (SECURITY_AUDIT.md H1): a fresh signup
    can't authenticate until the confirmation link is followed.
    """
    url = reverse("accounts:signup")
    response = client.post(
        url,
        {
            "given_name": "New",
            "family_name": "Bie",
            "email": "newbie@test.com",
            "email_confirm": "newbie@test.com",
            "password": "supersecure123",
            "password_confirm": "supersecure123",
            "agree_terms": "on",
        },
    )
    assert response.status_code in (301, 302)
    User = get_user_model()
    user = User.objects.get(email="newbie@test.com")
    assert user.email_confirmed_at is None
    # Session should NOT be authenticated.
    assert "_auth_user_id" not in client.session
    # A confirmation email went out, not a welcome email.
    assert len(mail.outbox) == 1
    assert "confirm" in mail.outbox[0].subject.lower()


def _confirm_url_from_email(message) -> str:
    body = message.body
    match = re.search(r"http\S*/accounts/confirm/\S+/\S+/", body)
    assert match, f"No confirmation URL found in email body: {body!r}"
    return match.group(0)


@pytest.mark.django_db
def test_confirming_email_logs_in_and_allows_login(client):
    """Following the confirmation link marks the account confirmed, logs
    the user in immediately, and lets them log in normally afterwards."""
    url = reverse("accounts:signup")
    client.post(
        url,
        {
            "given_name": "New",
            "family_name": "Bie",
            "email": "newbie2@test.com",
            "email_confirm": "newbie2@test.com",
            "password": "supersecure123",
            "password_confirm": "supersecure123",
            "agree_terms": "on",
        },
    )
    confirm_url = _confirm_url_from_email(mail.outbox[0])

    response = client.get(confirm_url)
    assert response.status_code == 200
    assert b"confirmed" in response.content.lower()
    assert "_auth_user_id" in client.session

    User = get_user_model()
    user = User.objects.get(email="newbie2@test.com")
    assert user.email_confirmed_at is not None

    # A welcome email is sent once confirmed.
    assert any("welcome" in m.subject.lower() for m in mail.outbox)

    # And they can now log in normally.
    client.logout()
    response = client.post(
        reverse("accounts:login"),
        {"email": "newbie2@test.com", "password": "supersecure123"},
    )
    assert response.status_code in (301, 302)
    assert "_auth_user_id" in client.session


@pytest.mark.django_db
def test_unconfirmed_user_cannot_log_in_with_correct_password(client):
    User = get_user_model()
    user = User.objects.create_user(email="pending@test.com", password="correct-horse-staple")
    response = client.post(
        reverse("accounts:login"),
        {"email": user.email, "password": "correct-horse-staple"},
    )
    assert "_auth_user_id" not in client.session
    assert "confirm" in response.content.decode().lower()


@pytest.mark.django_db
def test_confirm_rejects_bogus_token(client):
    User = get_user_model()
    user = User.objects.create_user(email="pending2@test.com", password="password123")
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    response = client.get(
        reverse("accounts:email_confirm", kwargs={"uidb64": uidb64, "token": "bogus-token"})
    )
    assert response.status_code == 200
    assert b"not valid" in response.content.lower()
    user.refresh_from_db()
    assert user.email_confirmed_at is None
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_resend_confirmation_sends_new_email_for_unconfirmed_account(client):
    User = get_user_model()
    User.objects.create_user(email="pending3@test.com", password="password123")
    response = client.post(
        reverse("accounts:resend_confirmation"), {"email": "pending3@test.com"}
    )
    assert response.status_code in (301, 302)
    assert len(mail.outbox) == 1
    assert "confirm" in mail.outbox[0].subject.lower()


@pytest.mark.django_db
def test_resend_confirmation_does_not_leak_account_existence(client):
    """Same redirect whether or not the email matches an account."""
    response = client.post(
        reverse("accounts:resend_confirmation"), {"email": "nobody@test.com"}
    )
    assert response.status_code in (301, 302)
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_signup_creates_default_widgets(client):
    """The signup post_save signal seeds the 10-widget dashboard defaults."""
    client.post(
        reverse("accounts:signup"),
        {
            "given_name": "",
            "family_name": "Widgets",
            "email": "widgets@test.com",
            "email_confirm": "widgets@test.com",
            "password": "supersecure123",
            "password_confirm": "supersecure123",
            "agree_terms": "on",
        },
    )
    User = get_user_model()
    user = User.objects.get(email="widgets@test.com")
    assert Widget.objects.filter(user=user).count() == 10


@pytest.mark.django_db
def test_login_valid_credentials(client, user):
    response = client.post(
        reverse("accounts:login"),
        {"email": user.email, "password": "password123"},
    )
    assert response.status_code in (301, 302)
    assert "_auth_user_id" in client.session


@pytest.mark.django_db
def test_login_invalid_credentials(client, user):
    response = client.post(
        reverse("accounts:login"),
        {"email": user.email, "password": "wrong-password"},
    )
    assert response.status_code == 200
    # Session should NOT have an authenticated user.
    assert "_auth_user_id" not in client.session
    # Form should surface an error.
    content = response.content.decode()
    assert "Invalid email or password" in content or "error" in content.lower()


@pytest.mark.django_db
def test_logout_requires_post(client_logged_in):
    url = reverse("accounts:logout")
    # GET is not allowed.
    get_response = client_logged_in.get(url)
    assert get_response.status_code == 405
    # POST actually logs the user out.
    post_response = client_logged_in.post(url)
    assert post_response.status_code in (301, 302)
    assert "_auth_user_id" not in client_logged_in.session


@pytest.mark.django_db
@pytest.mark.parametrize(
    "next_url,expected",
    [
        ("/accounts/login/?next=/invites/abc/", "/accounts/login/?next=/invites/abc/"),
        ("https://evil.example/", "/"),
        ("//evil.example/", "/"),
        ("", "/"),
    ],
)
def test_logout_follows_only_same_site_next(client_logged_in, next_url, expected):
    response = client_logged_in.post(reverse("accounts:logout"), {"next": next_url})
    assert response.status_code == 302
    assert response["Location"] == expected
    assert "_auth_user_id" not in client_logged_in.session


@pytest.mark.django_db
def test_profile_requires_login(client):
    response = client.get(reverse("accounts:profile"))
    # Unauthenticated -> redirected to login.
    assert response.status_code in (301, 302)
    assert "/accounts/login" in response["Location"]


# ---------------------------------------------------------------------------
# People discovery — directory + researcher profile
# ---------------------------------------------------------------------------


@pytest.fixture
def researchers(db):
    """Three people with enough profile filled in to search and facet on."""
    User = get_user_model()
    return {
        "nakagawa": User.objects.create_user(
            email="shinichi@ualberta.ca",
            password="password123",
            given_name="Shinichi",
            family_name="Nakagawa",
            pronouns="he/him",
            orcid="0000-0002-7765-5182",
            position="Professor",
            institution="University of Alberta",
            city="Edmonton",
            country="CA",
        ),
        "lagisz": User.objects.create_user(
            email="malgorzata@unsw.edu.au",
            password="password123",
            given_name="Malgorzata",
            family_name="Lagisz",
            institution="UNSW Sydney",
            city="Sydney",
            country="AU",
        ),
        "abbott": User.objects.create_user(
            email="abbott@ualberta.ca",
            password="password123",
            given_name="Ada",
            family_name="Abbott",
            institution="University of Alberta",
            city="Edmonton",
            country="CA",
        ),
    }


def _names_on_page(response) -> list[str]:
    """Directory names in the order the page lists them."""
    import re

    html = response.content.decode()
    return re.findall(r'leading-snug break-words">\s*(.*?)\s*</h2>', html)


@pytest.mark.django_db
def test_discover_people_requires_login(client):
    response = client.get(reverse("accounts:discover_people"))
    assert response.status_code in (301, 302)
    assert "/accounts/login" in response["Location"]


@pytest.mark.django_db
def test_discover_people_lists_everyone(client_logged_in, researchers):
    response = client_logged_in.get(reverse("accounts:discover_people"))
    assert response.status_code == 200
    names = _names_on_page(response)
    # The three researchers plus the logged-in ``user`` fixture.
    assert set(names) == {
        "Shinichi Nakagawa",
        "Malgorzata Lagisz",
        "Ada Abbott",
        "Alice",
    }


@pytest.mark.django_db
def test_discover_people_excludes_deactivated_accounts(client_logged_in, researchers):
    lagisz = researchers["lagisz"]
    lagisz.is_active = False
    lagisz.save(update_fields=["is_active"])
    names = _names_on_page(client_logged_in.get(reverse("accounts:discover_people")))
    assert "Malgorzata Lagisz" not in names


@pytest.mark.django_db
@pytest.mark.parametrize(
    "query,expected",
    [
        ("nakagawa", {"Shinichi Nakagawa"}),
        ("UNSW", {"Malgorzata Lagisz"}),
        ("Edmonton", {"Shinichi Nakagawa", "Ada Abbott"}),
        ("0000-0002-7765-5182", {"Shinichi Nakagawa"}),
        # Country is stored as a code, so the name has to resolve back to it.
        ("Australia", {"Malgorzata Lagisz"}),
    ],
)
def test_discover_people_search(client_logged_in, researchers, query, expected):
    response = client_logged_in.get(
        reverse("accounts:discover_people"), {"q": query}
    )
    assert set(_names_on_page(response)) == expected


@pytest.mark.django_db
def test_discover_people_search_does_not_match_email(client_logged_in, researchers):
    """The directory is not an email lookup — searching one finds nobody."""
    response = client_logged_in.get(
        reverse("accounts:discover_people"), {"q": "shinichi@ualberta.ca"}
    )
    assert _names_on_page(response) == []
    assert response.context["paginator"].count == 0


@pytest.mark.django_db
def test_discover_people_filters_by_institution(client_logged_in, researchers):
    response = client_logged_in.get(
        reverse("accounts:discover_people"),
        {"institution": "University of Alberta"},
    )
    assert set(_names_on_page(response)) == {"Shinichi Nakagawa", "Ada Abbott"}


@pytest.mark.django_db
def test_discover_people_filters_by_country(client_logged_in, researchers):
    response = client_logged_in.get(
        reverse("accounts:discover_people"), {"country": "AU"}
    )
    assert set(_names_on_page(response)) == {"Malgorzata Lagisz"}


@pytest.mark.django_db
def test_discover_people_facets_offer_only_values_in_use(client_logged_in, researchers):
    context = client_logged_in.get(reverse("accounts:discover_people")).context
    assert context["institutions"] == ["UNSW Sydney", "University of Alberta"]
    assert context["countries"] == [("AU", "Australia"), ("CA", "Canada")]


@pytest.mark.django_db
def test_discover_people_sorts_by_name(client_logged_in, researchers):
    url = reverse("accounts:discover_people")
    # Default is A–Z. "Alice" is the ``user`` fixture, named only by ``name``.
    assert _names_on_page(client_logged_in.get(url)) == [
        "Ada Abbott",
        "Alice",
        "Malgorzata Lagisz",
        "Shinichi Nakagawa",
    ]
    descending = _names_on_page(client_logged_in.get(url, {"sort": "name_desc"}))
    assert descending == [
        "Shinichi Nakagawa",
        "Malgorzata Lagisz",
        "Alice",
        "Ada Abbott",
    ]


@pytest.mark.django_db
def test_discover_people_sorts_by_institution_with_blanks_last(
    client_logged_in, researchers
):
    """Alice has no institution, so she sorts last in either direction."""
    url = reverse("accounts:discover_people")
    ascending = _names_on_page(client_logged_in.get(url, {"sort": "institution_asc"}))
    descending = _names_on_page(client_logged_in.get(url, {"sort": "institution_desc"}))
    # "University of Alberta" < "UNSW Sydney" once case is folded away.
    assert ascending[:2] == ["Ada Abbott", "Shinichi Nakagawa"]
    assert ascending[2] == "Malgorzata Lagisz"
    assert ascending[-1] == "Alice"
    assert descending[0] == "Malgorzata Lagisz"
    assert descending[-1] == "Alice"


@pytest.mark.django_db
def test_discover_people_unknown_sort_falls_back_to_default(
    client_logged_in, researchers
):
    response = client_logged_in.get(
        reverse("accounts:discover_people"), {"sort": "; DROP TABLE"}
    )
    assert response.status_code == 200
    assert response.context["current_sort"] == "name_asc"


@pytest.mark.django_db
def test_discover_people_htmx_returns_results_partial(client_logged_in, researchers):
    response = client_logged_in.get(
        reverse("accounts:discover_people"),
        {"q": "nakagawa"},
        headers={"HX-Request": "true"},
    )
    html = response.content.decode()
    assert "Shinichi Nakagawa" in html
    # A fragment, not the whole page, with the count badge swapped out of band.
    assert "<html" not in html
    assert 'id="people-total-badge" hx-swap-oob="true"' in html


@pytest.mark.django_db
def test_discover_people_card_links_to_profile(client_logged_in, researchers):
    response = client_logged_in.get(reverse("accounts:discover_people"))
    expected = reverse("public_profile", kwargs={"pk": researchers["nakagawa"].pk})
    assert f'href="{expected}"' in response.content.decode()


@pytest.mark.django_db
def test_public_profile_shows_identity_fields(client_logged_in, researchers):
    nakagawa = researchers["nakagawa"]
    html = client_logged_in.get(
        reverse("public_profile", kwargs={"pk": nakagawa.pk})
    ).content.decode()
    assert "Shinichi Nakagawa" in html
    assert "he/him" in html
    assert "0000-0002-7765-5182" in html
    assert "University of Alberta" in html
    assert "Edmonton" in html
    assert "Canada" in html


@pytest.mark.django_db
def test_public_profile_lists_only_public_projects(client_logged_in, researchers):
    from apps.projects.services import create_project_with_owner

    nakagawa = researchers["nakagawa"]
    create_project_with_owner(
        user=nakagawa, form_data={"name": "Open Meta-Analysis", "is_public": True}
    )
    create_project_with_owner(
        user=nakagawa, form_data={"name": "Embargoed Draft", "is_public": False}
    )
    html = client_logged_in.get(
        reverse("public_profile", kwargs={"pk": nakagawa.pk})
    ).content.decode()
    assert "Open Meta-Analysis" in html
    assert "Embargoed Draft" not in html


@pytest.mark.django_db
def test_public_profile_hides_email_from_others(client_logged_in, researchers):
    """Only the owner sees their own address on the profile."""
    nakagawa = researchers["nakagawa"]
    url = reverse("public_profile", kwargs={"pk": nakagawa.pk})
    assert nakagawa.email not in client_logged_in.get(url).content.decode()

    client_logged_in.force_login(nakagawa)
    assert nakagawa.email in client_logged_in.get(url).content.decode()


@pytest.mark.django_db
def test_directory_counts_only_public_projects(client_logged_in, researchers):
    from apps.projects.services import create_project_with_owner

    nakagawa = researchers["nakagawa"]
    create_project_with_owner(
        user=nakagawa, form_data={"name": "Open One", "is_public": True}
    )
    create_project_with_owner(
        user=nakagawa, form_data={"name": "Private One", "is_public": False}
    )
    response = client_logged_in.get(
        reverse("accounts:discover_people"), {"q": "nakagawa"}
    )
    person = response.context["people"][0]
    assert person.public_project_count == 1


@pytest.mark.django_db
def test_display_name_falls_back_to_account_name_then_email():
    User = get_user_model()
    full = User(given_name="Ada", family_name="Abbott", name="ignored", email="a@b.c")
    assert full.display_name == "Ada Abbott"
    partial = User(given_name="", family_name="Abbott", email="a@b.c")
    assert partial.display_name == "Abbott"
    account_name = User(name="Alice", email="a@b.c")
    assert account_name.display_name == "Alice"
    bare = User(email="a@b.c")
    assert bare.display_name == "a@b.c"


@pytest.mark.django_db
def test_discover_people_search_and_facets_combine(client_logged_in, researchers):
    """Search narrows within the chosen facets rather than replacing them."""
    response = client_logged_in.get(
        reverse("accounts:discover_people"),
        {"q": "Edmonton", "institution": "University of Alberta", "sort": "name_desc"},
    )
    assert _names_on_page(response) == ["Shinichi Nakagawa", "Ada Abbott"]
    assert "University of Alberta" in response.context["results_summary"]
    assert "sorted by name Z–A" in response.context["results_summary"]


@pytest.mark.django_db
def test_profile_recent_contributions_name_the_role_not_the_task(
    client_logged_in, researchers
):
    """A task name can carry detail the person hasn't published; the role can't."""
    from django.utils import timezone

    from apps.contributions.models import ContributionRecord
    from apps.projects.models import ProjectMember
    from apps.projects.services import create_project_with_owner
    from apps.tasks.models import KanbanBoard, Task

    nakagawa = researchers["nakagawa"]
    project = create_project_with_owner(
        user=nakagawa,
        form_data={"name": "Open Meta-Analysis", "is_public": True,
                   "contribution_tracking": "CREDIT"},
    )
    column = KanbanBoard.objects.create(project=project, name="Todo", container_order=0)
    member = ProjectMember.objects.filter(project=project, users=nakagawa).first()
    task = Task.objects.create(
        project=project,
        created_by=member,
        container=column,
        container_task_order=0,
        name="Unblind the pilot cohort",
    )
    ContributionRecord.objects.create(
        task=task, user=nakagawa, project=project,
        contribution_type="DATA_CURATION", closed_at=timezone.now(),
    )

    html = client_logged_in.get(
        reverse("public_profile", kwargs={"pk": nakagawa.pk})
    ).content.decode()
    assert "Data curation" in html
    assert "Open Meta-Analysis" in html
    assert "Unblind the pilot cohort" not in html


@pytest.mark.django_db
def test_profile_contribution_tiles_are_ordered_with_other_last(
    client_logged_in, researchers
):
    context = client_logged_in.get(
        reverse("public_profile", kwargs={"pk": researchers["nakagawa"].pk})
    ).context
    labels = [label for label, _count in context["contribution_tiles"]]
    assert labels[0] == "Conceptualization"
    assert labels[-1] == "Other"


@pytest.mark.django_db
def test_profile_names_the_person_once(client_logged_in, researchers):
    """The identity card carries the name and the eyebrow; no page header."""
    nakagawa = researchers["nakagawa"]
    html = client_logged_in.get(
        reverse("public_profile", kwargs={"pk": nakagawa.pk})
    ).content.decode()

    # Once on the page itself — the <title> keeps its own copy.
    body = html.split("</head>", 1)[1]
    assert body.count("Shinichi Nakagawa") == 1
    # The label sits inside the card, above the name it labels.
    assert "Researcher profile" in body
    assert body.index("Researcher profile") < body.index("Shinichi Nakagawa")
