"""Who may comment on a task, and tagging people (@Name) in a comment."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.comments.models import Comment, CommentMention
from apps.comments.services import can_comment, mentionable_users
from apps.comments.tasks import send_comment_mention_email_task
from apps.core.enums import MemberPrivileges, ProjectStatus
from apps.notifications.models import Notification
from apps.projects.models import ProjectMember, ProjectPrivilege
from apps.projects.services import create_project_with_owner
from apps.tasks.models import KanbanBoard, Task


@pytest.fixture(autouse=True)
def _site_url(settings):
    settings.SITE_URL = "https://iconnects.test"


def _user(email, given, family=""):
    return get_user_model().objects.create_user(
        email=email,
        password="password123",
        name=given,
        given_name=given,
        family_name=family,
        email_confirmed_at=timezone.now(),
    )


def _join(project, user, privilege=MemberPrivileges.CONTRIBUTOR):
    member = ProjectMember.objects.create(project=project)
    member.users.add(user)
    ProjectPrivilege.objects.create(project=project, user=user, privilege=privilege)
    return member


class Cast:
    """A task and everyone around it.

    ``creator`` is a Lead who made the task. ``assignee``, ``reviewer`` and
    ``bystander`` are plain members; only the first two are on the task.
    ``lead`` is a second Lead who is not on it. ``outsider`` is in no project.
    """

    def __init__(self, creator):
        self.creator = creator
        self.project = create_project_with_owner(
            user=creator,
            form_data={"name": "Reefs", "status": ProjectStatus.ACTIVE, "is_public": True},
        )
        self.creator_member = ProjectMember.objects.get(project=self.project, users=creator)
        column = KanbanBoard.objects.filter(project=self.project).first()
        self.task = Task.objects.create(
            project=self.project,
            created_by=self.creator_member,
            container=column,
            container_task_order=0,
            name="Count corals",
        )
        self.assignee = _user("asha@test.com", "Asha", "Rao")
        self.assignee_member = _join(self.project, self.assignee)
        self.reviewer = _user("ravi@test.com", "Ravi", "Shah")
        self.reviewer_member = _join(self.project, self.reviewer)
        self.bystander = _user("bea@test.com", "Bea", "Lund")
        self.bystander_member = _join(self.project, self.bystander)
        self.lead = _user("lena@test.com", "Lena", "Park")
        self.lead_member = _join(self.project, self.lead, MemberPrivileges.PROJECT_MANAGER)
        self.outsider = _user("otto@test.com", "Otto", "Berg")
        self.task.assigned_members.add(self.assignee_member)
        self.task.reviewers.add(self.reviewer_member)

    def create_url(self):
        return reverse("comments:create", kwargs={"task_pk": self.task.pk})

    def pane_url(self):
        return reverse(
            "tasks:detail_pane", kwargs={"project_pk": self.project.pk, "pk": self.task.pk}
        )

    def detail_url(self):
        return reverse(
            "tasks:detail", kwargs={"project_pk": self.project.pk, "pk": self.task.pk}
        )


@pytest.fixture
def cast(db, user):
    return Cast(user)


def _post(client, cast, as_user, content, mentioned=()):
    client.force_login(as_user)
    return client.post(
        cast.create_url(),
        {"content": content, "mentioned_user_ids": [u.pk for u in mentioned]},
    )


# ---------------------------------------------------------------------------
# Who may comment
# ---------------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("who", ["creator", "assignee", "reviewer"])
def test_participants_can_comment(client, cast, who):
    response = _post(client, cast, getattr(cast, who), "On it")
    assert response.status_code == 200
    assert cast.task.comments.filter(content="On it").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("who", ["bystander", "lead"])
def test_others_in_the_project_cannot(client, cast, who):
    """A plain member, and a Lead who is not on the task, can only read."""
    response = _post(client, cast, getattr(cast, who), "Me too")
    assert response.status_code == 403
    assert not cast.task.comments.exists()


@pytest.mark.django_db
def test_a_watcher_is_not_a_participant(client, cast):
    cast.task.watchers.add(cast.bystander_member)
    assert _post(client, cast, cast.bystander, "Watching").status_code == 403


@pytest.mark.django_db
def test_a_team_on_the_task_counts_for_each_of_its_users(cast):
    team = ProjectMember.objects.create(project=cast.project, name="Divers")
    team.users.add(cast.bystander)
    assert not can_comment(cast.bystander, cast.task)
    cast.task.assigned_members.add(team)
    assert can_comment(cast.bystander, cast.task)


@pytest.mark.django_db
def test_a_membership_that_left_no_longer_counts(cast):
    cast.assignee_member.deleted = True
    cast.assignee_member.save()
    assert not can_comment(cast.assignee, cast.task)


@pytest.mark.django_db
def test_the_box_is_drawn_only_for_participants(client, cast):
    """Others read the thread with no box, no send button, and nothing in
    their place."""
    Comment.objects.create(task=cast.task, author=cast.assignee_member, content="Status: done")
    box = f'hx-post="{cast.create_url()}"'
    for url in (cast.pane_url(), cast.detail_url()):
        client.force_login(cast.assignee)
        html = client.get(url).content.decode()
        assert box in html
        assert 'title="Comment"' in html  # the send button

        client.force_login(cast.bystander)
        html = client.get(url).content.decode()
        assert "Status: done" in html
        assert box not in html
        assert 'title="Comment"' not in html
        assert 'name="content"' not in html
        assert "can comment" not in html


@pytest.mark.django_db
def test_an_author_taken_off_the_task_cannot_edit_but_can_delete(client, cast):
    comment = Comment.objects.create(task=cast.task, author=cast.assignee_member, content="x")
    cast.task.assigned_members.remove(cast.assignee_member)
    client.force_login(cast.assignee)

    edit_url = reverse("comments:edit", kwargs={"pk": comment.pk})
    assert client.get(edit_url).status_code == 403
    assert client.post(edit_url, {"content": "y"}).status_code == 403
    comment.refresh_from_db()
    assert comment.content == "x"

    response = client.post(reverse("comments:delete", kwargs={"pk": comment.pk}))
    assert response.status_code == 204
    assert not Comment.objects.filter(pk=comment.pk).exists()


# ---------------------------------------------------------------------------
# Who may be tagged
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_only_contributors_can_be_tagged(cast):
    """The Contributors list — Leads and Members alike — and no one else."""
    tagged = set(mentionable_users(cast.project))
    assert {cast.creator, cast.assignee, cast.reviewer, cast.bystander, cast.lead} <= tagged
    assert cast.outsider not in tagged

    # A Lead privilege with no membership row is not on the Contributors list.
    privileged = _user("pia@test.com", "Pia", "Holm")
    ProjectPrivilege.objects.create(
        project=cast.project, user=privileged, privilege=MemberPrivileges.PROJECT_MANAGER
    )
    # Nor is someone who is only on a team-shaped membership.
    teamed = _user("tom@test.com", "Tom", "Ek")
    ProjectMember.objects.create(project=cast.project, name="Divers").users.add(teamed)

    tagged = set(mentionable_users(cast.project))
    assert privileged not in tagged
    assert teamed not in tagged


@pytest.mark.django_db
def test_people_who_left_or_are_inactive_cannot_be_tagged(cast):
    cast.bystander_member.deleted = True
    cast.bystander_member.save()
    cast.reviewer.is_active = False
    cast.reviewer.save()
    tagged = set(mentionable_users(cast.project))
    assert cast.bystander not in tagged
    assert cast.reviewer not in tagged


@pytest.mark.django_db
def test_the_at_list_shows_each_contributors_name_and_email(client, cast):
    """Names and emails, so two people with one name can be told apart — and
    never the person writing, nor anyone who is not a contributor."""
    client.force_login(cast.assignee)
    html = client.get(cast.pane_url()).content.decode()
    assert '"name": "Bea Lund", "email": "bea@test.com"' in html
    assert '"name": "Lena Park", "email": "lena@test.com"' in html
    assert "asha@test.com" not in html
    assert "otto@test.com" not in html
    assert 'x-text="c.email"' in html


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_tag_is_saved_and_highlighted(client, cast, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        response = _post(client, cast, cast.assignee, "@Bea Lund can you check?", [cast.bystander])
    assert response.status_code == 200
    comment = cast.task.comments.get()
    assert list(comment.mentions.values_list("user_id", flat=True)) == [cast.bystander.pk]
    html = response.content.decode()
    bea = reverse("public_profile", kwargs={"pk": cast.bystander.pk})
    assert f'<a href="{bea}" class="comment-mention">@Bea Lund</a> can you check?' in html


@pytest.mark.django_db
def test_a_tag_whose_name_was_deleted_from_the_text_is_dropped(client, cast):
    _post(client, cast, cast.assignee, "never mind", [cast.bystander])
    assert not CommentMention.objects.exists()


@pytest.mark.django_db
def test_a_prefix_of_a_longer_word_is_not_a_tag(client, cast):
    """Picking "Bea Lund" and then typing "@Bea Lundqvist" tags no one."""
    _post(client, cast, cast.assignee, "@Bea Lundqvist", [cast.bystander])
    assert not CommentMention.objects.exists()


@pytest.mark.django_db
def test_outsiders_bad_ids_and_yourself_are_dropped_not_refused(client, cast):
    client.force_login(cast.assignee)
    response = client.post(
        cast.create_url(),
        {
            "content": "@Otto Berg @otto@test.com @Asha Rao @asha@test.com @Ravi Shah",
            "mentioned_user_ids": [
                cast.outsider.pk,
                "nonsense",
                cast.assignee.pk,
                cast.reviewer.pk,
            ],
        },
    )
    assert response.status_code == 200
    comment = cast.task.comments.get()
    assert set(comment.mentions.values_list("user_id", flat=True)) == {cast.reviewer.pk}

    client.post(
        reverse("comments:edit", kwargs={"pk": comment.pk}),
        {"content": "@Asha Rao", "mentioned_user_ids": [cast.assignee.pk]},
    )
    assert not comment.mentions.exists()


@pytest.mark.django_db
def test_an_email_typed_out_tags_its_owner_without_a_pick(client, cast):
    """An address names one person, so the text alone is enough."""
    response = _post(client, cast, cast.assignee, "cc @BEA@test.com, and @lena@test.com.")
    assert response.status_code == 200
    comment = cast.task.comments.get()
    assert set(comment.mentions.values_list("user_id", flat=True)) == {
        cast.bystander.pk,
        cast.lead.pk,
    }
    html = response.content.decode()
    bea = reverse("public_profile", kwargs={"pk": cast.bystander.pk})
    lena = reverse("public_profile", kwargs={"pk": cast.lead.pk})
    assert f'<a href="{bea}" class="comment-mention">@BEA@test.com</a>,' in html
    assert f'<a href="{lena}" class="comment-mention">@lena@test.com</a>.' in html


@pytest.mark.django_db
def test_a_picked_person_can_be_written_as_their_email(client, cast):
    _post(client, cast, cast.assignee, "@bea@test.com see above", [cast.bystander])
    assert list(CommentMention.objects.values_list("user_id", flat=True)) == [cast.bystander.pk]


@pytest.mark.django_db
def test_an_address_that_only_starts_like_a_contributors_is_not_a_tag(client, cast):
    """Not typed out, and not left after picking Bea and lengthening it."""
    _post(
        client,
        cast,
        cast.assignee,
        "@bea@test.com.au @bea@test.community @bea@test.com-west bea@test.com",
        [cast.bystander],
    )
    assert not CommentMention.objects.exists()


@pytest.mark.django_db
def test_a_name_typed_by_hand_is_not_highlighted(cast):
    """Only tagged people are marked up, and the text stays escaped."""
    comment = Comment.objects.create(
        task=cast.task, author=cast.assignee_member, content="@Bea Lund <b>hi</b>"
    )
    html = render_to_string("comments/comment_item.html", {"comment": comment, "task": cast.task})
    assert "comment-mention" not in html
    assert "&lt;b&gt;hi&lt;/b&gt;" in html


# ---------------------------------------------------------------------------
# Telling them
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_tagged_person_is_notified_and_emailed(client, cast, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        _post(client, cast, cast.assignee, "@Lena Park please <b>approve</b>", [cast.lead])

    note = Notification.objects.for_user(cast.lead).get()
    assert note.headline == "Asha Rao mentioned you on Count corals"
    assert note.url == cast.detail_url()

    assert len(mail.outbox) == 1
    email = mail.outbox[0]
    assert email.to == ["lena@test.com"]
    assert email.subject == 'i-connects: Asha Rao mentioned you on "Count corals"'
    assert "https://iconnects.test" + cast.detail_url() in email.body
    assert "@Lena Park please <b>approve</b>" in email.body  # plain text
    html = email.alternatives[0][0]
    assert "&lt;b&gt;approve&lt;/b&gt;" in html
    assert "<b>approve</b>" not in html


@pytest.mark.django_db
def test_an_email_tag_sends_the_email(client, cast, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        _post(client, cast, cast.assignee, "@ravi@test.com over to you")
    assert [m.to for m in mail.outbox] == [["ravi@test.com"]]
    assert Notification.objects.for_user(cast.reviewer).exists()


@pytest.mark.django_db
def test_turning_the_email_off_keeps_the_notification(client, cast, django_capture_on_commit_callbacks):
    cast.lead.email_comment_mention = False
    cast.lead.save()
    with django_capture_on_commit_callbacks(execute=True):
        _post(client, cast, cast.assignee, "@Lena Park", [cast.lead])
    assert Notification.objects.for_user(cast.lead).exists()
    assert mail.outbox == []


@pytest.mark.django_db
def test_an_edit_emails_only_the_people_it_adds(client, cast, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        _post(client, cast, cast.assignee, "@Lena Park", [cast.lead])
    comment = cast.task.comments.get()
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            reverse("comments:edit", kwargs={"pk": comment.pk}),
            {
                "content": "@Lena Park and @Bea Lund",
                "mentioned_user_ids": [cast.lead.pk, cast.bystander.pk],
            },
        )
    assert response.status_code == 200
    assert [m.to for m in mail.outbox] == [["bea@test.com"]]
    assert set(comment.mentions.values_list("user_id", flat=True)) == {
        cast.lead.pk,
        cast.bystander.pk,
    }

    # Taking a tag out removes it, and tells no one.
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            reverse("comments:edit", kwargs={"pk": comment.pk}),
            {"content": "@Bea Lund", "mentioned_user_ids": [cast.bystander.pk]},
        )
    assert mail.outbox == []
    assert list(comment.mentions.values_list("user_id", flat=True)) == [cast.bystander.pk]


@pytest.mark.django_db
def test_the_email_rechecks_everything_when_it_runs(cast):
    comment = Comment.objects.create(task=cast.task, author=cast.assignee_member, content="@Bea Lund")
    CommentMention.objects.create(comment=comment, user=cast.bystander)

    # Left the project before the worker got to it.
    cast.bystander_member.deleted = True
    cast.bystander_member.save()
    send_comment_mention_email_task(comment.pk, [cast.bystander.pk])
    assert mail.outbox == []

    # Tag edited out.
    cast.bystander_member.deleted = False
    cast.bystander_member.save()
    send_comment_mention_email_task(comment.pk, [cast.lead.pk])
    assert mail.outbox == []

    # Comment deleted.
    comment_pk = comment.pk
    comment.delete()
    send_comment_mention_email_task(comment_pk, [cast.bystander.pk])
    assert mail.outbox == []


@pytest.mark.django_db
def test_the_preference_is_on_the_profile_page(client, user):
    client.force_login(user)
    html = client.get(reverse("accounts:profile")).content.decode()
    assert "Email me when I&#x27;m mentioned in a comment" in html


@pytest.mark.django_db
def test_the_box_posts_the_people_it_highlights(client, cast):
    """The ids the comment box posts are its highlighted tags (mentionBox's
    ``tagged``), announced to screen readers, and the chip style the box
    paints is the one a posted comment wears."""
    client.force_login(cast.assignee)
    html = client.get(cast.detail_url()).content.decode()
    assert '<template x-for="m in tagged" :key="m.id">' in html
    assert 'aria-live="polite" x-text="taggedSummary"' in html
    assert ".mention-backdrop mark {" in html
    assert ".comment-mention {" in html


# ---------------------------------------------------------------------------
# Names link to profiles
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_commenters_name_opens_their_profile(client, cast):
    Comment.objects.create(task=cast.task, author=cast.assignee_member, content="hi")
    asha = reverse("public_profile", kwargs={"pk": cast.assignee.pk})
    client.force_login(cast.bystander)
    for url in (cast.pane_url(), cast.detail_url()):
        html = client.get(url).content.decode()
        assert f'<a href="{asha}"\n               class="comment-author' in html
        assert ">Asha Rao</a>" in html


@pytest.mark.django_db
def test_a_name_two_tagged_people_share_is_highlighted_but_not_linked(cast):
    """Which of them "@Ben Okafor" means cannot be told, so it links to
    neither; each one's address still links to them."""
    ben1 = _user("ben1@test.com", "Ben", "Okafor")
    ben2 = _user("ben2@test.com", "Ben", "Okafor")
    _join(cast.project, ben1)
    _join(cast.project, ben2)
    comment = Comment.objects.create(
        task=cast.task,
        author=cast.assignee_member,
        content="@Ben Okafor and @Ben Okafor, i.e. @ben2@test.com",
    )
    CommentMention.objects.create(comment=comment, user=ben1)
    CommentMention.objects.create(comment=comment, user=ben2)
    html = render_to_string("comments/comment_item.html", {"comment": comment, "task": cast.task})
    assert html.count('<span class="comment-mention">@Ben Okafor</span>') == 2
    ben2_url = reverse("public_profile", kwargs={"pk": ben2.pk})
    assert f'<a href="{ben2_url}" class="comment-mention">@ben2@test.com</a>' in html


# ---------------------------------------------------------------------------
# Web addresses
# ---------------------------------------------------------------------------
def _body(cast, content, tagged=()):
    comment = Comment.objects.create(task=cast.task, author=cast.assignee_member, content=content)
    for user in tagged:
        CommentMention.objects.create(comment=comment, user=user)
    html = render_to_string("comments/comment_item.html", {"comment": comment, "task": cast.task})
    start = html.index(f'id="comment-body-{comment.pk}"')
    open_end = html.index(">", start) + 1
    return html[open_end : html.index("</div>", open_end)]


def _a(href, text=None):
    return (
        f'<a href="{href}" class="comment-link text-brand-navy underline underline-offset-2 '
        f'hover:text-brand-coral" target="_blank" rel="noopener noreferrer nofollow" '
        f"data-external-link>"
        f"{text or href}</a>"
    )


@pytest.mark.django_db
def test_a_web_address_opens_in_a_new_tab(cast):
    body = _body(cast, "Data: https://example.org/reef?site=4&year=2026")
    assert body == "Data: " + _a(
        "https://example.org/reef?site=4&amp;year=2026",
    )


@pytest.mark.django_db
def test_www_addresses_are_linked_over_https(cast):
    body = _body(cast, "See www.example.org/notes")
    assert body == "See " + _a("https://www.example.org/notes", "www.example.org/notes")


@pytest.mark.django_db
def test_the_sentences_punctuation_stays_out_of_the_link(cast):
    body = _body(cast, "Read https://example.org/a. Then (see https://example.org/b), and https://en.wikipedia.org/wiki/Reef_(ecology)!")
    assert _a("https://example.org/a") + ". Then (see " in body
    assert _a("https://example.org/b") + "), and " in body
    assert _a("https://en.wikipedia.org/wiki/Reef_(ecology)") + "!" in body


@pytest.mark.django_db
def test_only_web_addresses_are_linked(cast):
    """No javascript:, no bare file names or domains, no plain addresses."""
    body = _body(cast, "javascript:alert(1) notes.md example.org bea@test.com www. https://")
    assert "<a" not in body


@pytest.mark.django_db
def test_what_the_author_typed_cannot_break_out_of_a_link(cast):
    body = _body(cast, 'https://example.org/"onmouseover="alert(1) <b>x</b>')
    assert body.startswith(_a("https://example.org/") + "&quot;onmouseover=&quot;alert(1)")
    assert "&lt;b&gt;x&lt;/b&gt;" in body
    assert "<b>" not in body


@pytest.mark.django_db
def test_links_and_tags_side_by_side(cast):
    """An @ inside an address is part of it; a tag beside one is still a tag."""
    body = _body(cast, "@Bea Lund look at https://x.com/@Bea Lund", tagged=[cast.bystander])
    bea = reverse("public_profile", kwargs={"pk": cast.bystander.pk})
    assert body == (
        f'<a href="{bea}" class="comment-mention">@Bea Lund</a> look at '
        + _a("https://x.com/@Bea")
        + " Lund"
    )


@pytest.mark.django_db
def test_every_page_carries_the_leaving_dialog(client, cast):
    """Comment links are marked to ask first, and the task page has the
    dialog that asks: Return focused and on the right, Continue beside it."""
    Comment.objects.create(task=cast.task, author=cast.assignee_member, content="https://example.org")
    client.force_login(cast.bystander)
    html = client.get(cast.detail_url()).content.decode()
    assert "data-external-link>https://example.org</a>" in html
    dialog = html[html.index('<dialog id="external-link-modal"') :]
    dialog = dialog[: dialog.index("</dialog>")]
    assert "Leaving i-CONNECTS" in dialog
    assert "You are now leaving i-CONNECTS." in dialog
    assert "i-connects" not in dialog
    assert "We are not responsible for the content of external websites." in dialog
    assert dialog.index(">Continue</button>") < dialog.index(">Return</button>")
    assert 'id="external-link-return" class="btn btn-primary" autofocus' in dialog


@pytest.mark.django_db
def test_the_box_survives_the_pane_being_loaded_again(client, cast):
    """What keeps the comment box drawn when a pane loads over an older one.

    The textarea has an id, and HTMX resets the class and style of an element
    whose id was in the markup it swapped out. So the highlight layer's
    styles hang off the box's server-rendered wrapper, never a class added to
    the textarea, and the pane is emptied before each load.
    """
    client.force_login(cast.assignee)
    pane = client.get(cast.pane_url()).content.decode()
    assert '<div class="mention-field relative flex-1 min-w-0">' in pane

    page = client.get(cast.detail_url()).content.decode()
    assert ".mention-field textarea {" in page
    assert "mention-input" not in page
    assert 'target.id === "task-pane-body" && event.detail.shouldSwap' in page
    assert "target.replaceChildren();" in page
