"""Template helpers for comments."""

from __future__ import annotations

from django import template
from django.urls import reverse
from django.utils.html import escape

from apps.comments.services import mention_pattern
from apps.core.linkify import linkify

register = template.Library()


def _tagger(comment):
    """A function that escapes plain text and marks the tags in it."""
    users = [m.user for m in comment.mentions.all()]
    pattern = mention_pattern(
        [escape(u.display_name) for u in users], [escape(u.email) for u in users]
    )
    if pattern is None:
        return escape

    by_name: dict[str, list] = {}
    by_email = {}
    for u in users:
        by_name.setdefault(str(escape(u.display_name)), []).append(u)
        by_email[str(escape(u.email)).lower()] = u

    def chip(match):
        tag = match.group(0)
        said = tag[1:]
        named = by_name.get(said, [])
        user = by_email.get(said.lower()) or (named[0] if len(named) == 1 else None)
        if user is None:
            return f'<span class="comment-mention">{tag}</span>'
        href = reverse("public_profile", kwargs={"pk": user.pk})
        return f'<a href="{href}" class="comment-mention">{tag}</a>'

    return lambda plain: pattern.sub(chip, str(escape(plain)))


@register.filter
def comment_body(comment):
    """The comment's text, escaped, with its web addresses and tags linked.

    Web addresses (``apps.core.linkify``) open in a new tab, after the "Leaving i-CONNECTS"
    dialog (base.html) has asked — ``data-external-link``. They are found
    first, in the text as typed, so an ``@`` inside one
    (``https://x.com/@reefs``) is never read as a tag.

    A tag is ``@Name`` or ``@email`` of someone actually tagged
    (``comment.mentions``): "@everyone" typed by hand, or a name whose tag
    was dropped, stays plain text. Each tag links to the tagged person's
    profile. Two people tagged under one name (two Ben Okafors, both picked)
    leave that name unsure of whom it means, so it is highlighted but not
    linked; their addresses, being unique, always are. ``.comment-mention``
    (base.html) is the same chip the comment box paints while typing.

    Everything the author typed is escaped before it is emitted, including
    inside an ``href``. Newlines are left as they are for
    ``whitespace-pre-line`` to render.
    """
    return linkify(comment.content, between=_tagger(comment))
