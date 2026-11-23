"""Comment form."""

from __future__ import annotations

from django import forms
from django.db.models.functions import Lower

from apps.comments.models import Comment
from apps.comments.services import emails_tagged_in, tagged_in

# The comment box posts one of these per person picked from its @ list.
MENTION_FIELD = "mentioned_user_ids"


class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ["content"]
        widgets = {
            # One row. DaisyUI's .textarea carries min-height: 3rem, so the
            # box lands at the same 48px as a single-line .input rather than
            # the 74px two rows of its 2.0 line-height produced. It is still
            # a textarea: newlines and the drag handle both work.
            "content": forms.Textarea(
                attrs={
                    "class": "textarea textarea-bordered w-full",
                    "rows": 1,
                    "placeholder": "Add a comment",
                }
            ),
        }

    def __init__(self, *args, mentionable=None, author_user=None, **kwargs):
        """``mentionable`` is the queryset of users who may be tagged (see
        ``services.mentionable_users``); without it no one is. ``author_user``
        is the person writing, who is never tagged — there is no need to tell
        someone what they just wrote."""
        super().__init__(*args, **kwargs)
        self.mentionable = mentionable
        self.author_user = author_user
        self.mentioned_users: list = []

    def _submitted_mention_ids(self) -> set[int]:
        if not self.is_bound:
            return set()
        getlist = getattr(self.data, "getlist", None)
        raw = getlist(MENTION_FIELD) if getlist else self.data.get(MENTION_FIELD) or []
        if isinstance(raw, (str, int)):
            raw = [raw]
        ids = set()
        for value in raw:
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                continue
        return ids

    def clean(self):
        """Work out who the comment tags, quietly leaving out the rest.

        Two ways in, both limited to ``mentionable`` (the project's
        contributors):

        * a pick from the @ list, which sent the person's id, stands while
          their ``@Name`` (or ``@email``) is still in the text;
        * ``@email`` typed out stands on its own — an address names exactly
          one person, so it needs no pick.

        Anything else — a name deleted from the box after it was picked,
        someone who is not a contributor, an id that was never valid, the
        author tagging themselves — is not worth refusing the comment over,
        so it is left out rather than raised as an error.
        """
        cleaned = super().clean()
        if self.mentionable is None:
            return cleaned
        content = cleaned.get("content") or ""
        ids = self._submitted_mention_ids()
        emails = emails_tagged_in(content)
        tagged = {}
        if ids:
            for u in self.mentionable.filter(pk__in=ids):
                if tagged_in(content, u):
                    tagged[u.pk] = u
        if emails:
            for u in self.mentionable.annotate(email_lower=Lower("email")).filter(
                email_lower__in=emails
            ):
                tagged[u.pk] = u
        if self.author_user is not None:
            tagged.pop(self.author_user.pk, None)
        self.mentioned_users = list(tagged.values())
        return cleaned
