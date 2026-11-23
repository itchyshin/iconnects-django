"""Who may comment on a task, who may be tagged in one, and telling them.

Commenting on a task is for the people working on it: the member who created
it, its assignees and its reviewers. Everyone else in the project can read
the thread but not add to it — which also means they cannot tag anyone.

Only the project's contributors can be tagged — the people on its
Contributors list, Leads and Members alike. A tag is written either as
``@Name``, picked from the comment box's @ list (which sends the person's id
alongside, see ``CommentForm``), or as ``@email``, which needs no picking:
emails are unique, so the address alone says who. A tagged contributor gets
an in-app notification and, unless they turned it off, an email.
"""

from __future__ import annotations

import re
from typing import Iterable

from django.contrib.auth import get_user_model
from django.db import transaction

from apps.projects.models import ProjectMember

from .models import Comment, CommentMention


# ---------------------------------------------------------------------------
# Who may comment
# ---------------------------------------------------------------------------
def task_participant_user_ids(task) -> set[int]:
    """Ids of the users who may comment on ``task``.

    Its creator, assignees and reviewers. Any of these may be a team-shaped
    membership, which counts for every user on it. A membership that has left
    the project (``deleted``) no longer counts.
    """
    member_ids = {task.created_by_id}
    member_ids.update(task.assigned_members.values_list("pk", flat=True))
    member_ids.update(task.reviewers.values_list("pk", flat=True))
    user_ids = set(
        ProjectMember.objects.filter(pk__in=member_ids, deleted=False).values_list(
            "users", flat=True
        )
    )
    user_ids.discard(None)  # a membership with no users left on it
    return user_ids


def can_comment(user, task) -> bool:
    """Whether ``user`` may post on, or edit their comments on, ``task``."""
    if not getattr(user, "is_authenticated", False):
        return False
    return user.pk in task_participant_user_ids(task)


# ---------------------------------------------------------------------------
# Who may be tagged
# ---------------------------------------------------------------------------
def mentionable_users(project):
    """The project's contributors: the only people a comment can tag.

    The active users on its Contributors list — each individual membership
    that has not left the project, Leads and Members alike. The same people
    can open the task the tag links to. Someone holding a Lead privilege with
    no membership row is not on that list and cannot be tagged, nor can the
    users of a team-shaped membership, who cannot open the project by it.
    """
    User = get_user_model()
    return (
        User.objects.filter(
            is_active=True,
            project_memberships__project=project,
            project_memberships__deleted=False,
            project_memberships__name__isnull=True,
        )
        .distinct()
        .order_by("given_name", "family_name", "name", "email")
    )


# ``@`` starting a word, then an address. The domain needs a dot followed by
# more of it, so the full stop that ends a sentence is not taken in.
EMAIL_TAG = re.compile(r"(?<!\S)@([\w.+'-]+@[\w-]+(?:\.[\w-]+)+)")

# After an address: not more of one. "@bea@x.org" is not a tag in
# "@bea@x.org.uk", nor in "@bea@x.org-west".
_EMAIL_END = r"(?![\w-]|\.[\w-])"


def mention_pattern(names: Iterable[str], emails: Iterable[str] = ()) -> re.Pattern | None:
    """A regex matching ``@<name>`` or ``@<email>`` for any of those given.

    Names are matched as written and as a whole word: longest first, so
    "@Ana Lima" is matched whole rather than as "@Ana" followed by " Lima",
    and the lookahead keeps "@Al" from matching the start of "@Alice".
    Emails are matched ignoring case, and not as the start of a longer one.
    """
    names = sorted({n for n in names if n}, key=len, reverse=True)
    emails = sorted({e for e in emails if e}, key=len, reverse=True)
    parts = []
    if names:
        parts.append("(?:" + "|".join(re.escape(n) for n in names) + r")(?!\w)")
    if emails:
        parts.append(
            "(?i:" + "|".join(re.escape(e) for e in emails) + ")" + _EMAIL_END
        )
    if not parts:
        return None
    return re.compile("@(?:" + "|".join(parts) + ")")


def tagged_in(content: str, user) -> bool:
    """Whether ``content`` still names ``user`` as ``@<display name>`` or
    ``@<email>``."""
    pattern = mention_pattern([user.display_name], [user.email])
    return bool(pattern and pattern.search(content or ""))


def emails_tagged_in(content: str) -> set[str]:
    """Every address written as a tag (``@someone@example.org``) in
    ``content``, lowercased."""
    return {m.lower() for m in EMAIL_TAG.findall(content or "")}


def sync_mentions(comment: Comment, users: Iterable) -> set[int]:
    """Make ``users`` the people tagged in ``comment``; return who is new.

    On a new comment that is everyone tagged. On an edit it is only the
    people the edit added, so fixing a typo does not email the people
    already tagged a second time. Tags the edit took out are removed.
    """
    wanted = {u.pk for u in users}
    existing = set(comment.mentions.values_list("user_id", flat=True))
    comment.mentions.exclude(user_id__in=wanted).delete()
    added = wanted - existing
    CommentMention.objects.bulk_create(
        [CommentMention(comment=comment, user_id=uid) for uid in added]
    )
    return added


# ---------------------------------------------------------------------------
# Telling them
# ---------------------------------------------------------------------------
def _queue_mention_email(comment_id: int, user_ids: list[int]):
    """Return an ``on_commit`` callback that queues the mention email.

    A named function rather than a ``functools.partial``, for the same reason
    as ``apps.tasks.services._queue_assignment_email``: Django logs a failing
    robust callback by its ``__qualname__``.
    """
    from apps.comments.tasks import send_comment_mention_email_task

    def queue_comment_mention_email() -> None:
        send_comment_mention_email_task.delay(comment_id, user_ids)

    return queue_comment_mention_email


def notify_mentioned_users(comment: Comment, user_ids: Iterable[int], *, mentioned_by=None) -> None:
    """Tell the people just tagged in ``comment``, in-app and by email.

    The in-app notification is written now. The email is queued for after the
    surrounding transaction commits, so the worker sees the saved comment;
    whether to send at all — the recipient's ``email_comment_mention``
    preference, and whether they can still open the task — is decided by the
    worker when it runs.
    """
    from apps.notifications.services import create_notification

    user_ids = set(user_ids)
    if mentioned_by is not None:
        user_ids.discard(mentioned_by.pk)
    if not user_ids:
        return
    task = comment.task
    who = mentioned_by.display_name if mentioned_by is not None else "Someone"
    create_notification(
        users=sorted(user_ids),
        title=f"{who} mentioned you on {task.name}",
        body=f"In project {task.project.name}.",
        project=task.project,
        route="tasks:detail",
        route_kwargs={"project_pk": task.project_id, "pk": task.pk},
    )
    transaction.on_commit(
        _queue_mention_email(comment.pk, sorted(user_ids)), robust=True
    )
