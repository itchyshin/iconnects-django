"""Turning the web addresses in text people typed into links.

Shared by task comments (``comment_tags.comment_body``) and task
descriptions (``task_tags.link_urls``), so both link the same addresses the
same way, and both send the reader through the "Leaving i-CONNECTS" dialog
(base.html) — the ``data-external-link`` on every link — before an address
takes them off the site.
"""

from __future__ import annotations

import re
from typing import Callable

from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

# A web address: http(s):// or www., up to the next space or a character that
# cannot be in one. Other schemes (javascript:, data:, mailto:) are never
# linked. Not Django's urlize: it would also turn the address in a comment's
# @email tag into a mailto link, and a bare "notes.md" into a website.
URL = re.compile(r"\b(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)

# What is left must still be an address: the prefix, then something more.
_ADDRESS = re.compile(r"(?:https?://|www\.)[^\s.]", re.IGNORECASE)

# Punctuation that ends a sentence rather than the address it follows.
_TRAILING = ".,;:!?*_~"
_CLOSERS = {")": "(", "]": "["}

LINK_CLASS = "comment-link text-brand-navy underline underline-offset-2 hover:text-brand-coral"


def _trim(url: str) -> str:
    """``url`` without the punctuation that follows it in the sentence.

    A closing bracket stays when the address opened it — the Wikipedia
    "Foo_(bar)" kind — and goes when it closes a bracket around the address.
    """
    while url:
        last = url[-1]
        if last in _TRAILING:
            url = url[:-1]
        elif last in _CLOSERS and url.count(last) > url.count(_CLOSERS[last]):
            url = url[:-1]
        else:
            break
    return url


def link(url: str) -> str:
    """One address as a link that opens in a new tab, after the dialog asks."""
    href = url if url.lower().startswith(("http://", "https://")) else f"https://{url}"
    return (
        f'<a href="{escape(href)}" class="{LINK_CLASS}" '
        f'target="_blank" rel="noopener noreferrer nofollow" data-external-link>'
        f"{escape(url)}</a>"
    )


def linkify(text: str, between: Callable[[str], str] = escape) -> SafeString:
    """``text`` escaped, with its web addresses linked.

    Addresses are found in the text as typed. Everything between them goes
    through ``between``, which must escape what it is given — comments pass
    one that also marks their @tags, which is how an ``@`` inside an address
    is never read as a tag.
    """
    text = text or ""
    out, last = [], 0
    for match in URL.finditer(text):
        url = _trim(match.group(0))
        if not _ADDRESS.match(url):  # "www." or "https://" alone, once trimmed
            continue
        out.append(str(between(text[last : match.start()])))
        out.append(link(url))
        last = match.start() + len(url)
    out.append(str(between(text[last:])))
    return mark_safe("".join(out))
