"""Template filters for task assignee/reviewer names and date labels."""

from __future__ import annotations

from django import template

from apps.core.linkify import linkify
from apps.tasks.calendar import task_date_label
from apps.tasks.timezones import get_zone

register = template.Library()


@register.filter
def link_urls(text):
    """``text`` escaped, with its web addresses as links (``apps.core.linkify``).

    Safe output, so it goes before ``linebreaks`` — which leaves safe text
    unescaped and only adds the paragraphs: ``{{ x|link_urls|linebreaks }}``.
    """
    return linkify(text)


@register.simple_tag
def timezone_option(value):
    """The timezone picker's entry for one zone (see
    ``apps.tasks.timezones.timezone_option``), or None when ``value`` is
    empty or unknown."""
    from apps.tasks.timezones import timezone_option as option

    return option(value)


@register.simple_tag
def timezone_aliases_json():
    """Old zone names browsers may report, mapped to the listed ones, as
    JSON for the picker's script."""
    import json

    from apps.tasks.timezones import ALIASES

    return json.dumps(ALIASES)


@register.simple_tag
def default_timezone_name():
    from apps.tasks.timezones import default_zone_name

    return default_zone_name()


@register.filter
def task_when(task):
    """A task's dates with their times, in the task's own timezone, for the
    task card: "Nov 15, 2026 9:00 AM – Nov 30, 2026 5:00 PM MST". The
    zone's abbreviation is given once, or on each date when they differ (a
    span across a daylight-saving change); a span within one day names the
    day once ("Nov 15, 2026 9:00 AM – 5:00 PM MST"). A task with one date
    says which it is ("Due Nov 30, 2026 5:00 PM MST"). Empty for a task
    with no dates."""
    day_fmt, time_fmt = "%b %-d, %Y", "%-I:%M %p"
    zone = task.tz
    start = task.start_date.astimezone(zone) if task.start_date else None
    end = task.deadline.astimezone(zone) if task.deadline else None
    if start is None and end is None:
        return ""
    if start is None or end is None:
        when = end or start
        prefix = "Due" if end is not None else "Starts"
        return f"{prefix} {when:{day_fmt}} {when:{time_fmt}} {when.tzname()}"
    if start > end:
        start, end = end, start
    same_abbr = start.tzname() == end.tzname()
    first = f"{start:{day_fmt}} {start:{time_fmt}}" + ("" if same_abbr else f" {start.tzname()}")
    if start.date() == end.date():
        second = f"{end:{time_fmt}}"
    else:
        second = f"{end:{day_fmt}} {end:{time_fmt}}"
    return f"{first} \u2013 {second} {end.tzname()}"


@register.filter
def in_zone(value, zone):
    """``value`` in ``zone`` (an IANA name; empty or unknown falls back to
    the server's), for the ``date`` filter to format as it stands rather
    than in the server's zone: ``{{ task.deadline|in_zone:task.timezone|date:"M j, Y g:i A T" }}``
    — "T" adds the zone's abbreviation."""
    from django.templatetags.tz import do_timezone

    if not value:
        return value
    return do_timezone(value, get_zone(zone))


@register.filter
def member_name_summary(members, short_limit=2):
    """Return {'full': all names comma-separated, 'short': first short_limit
    names + an "and N others" overflow suffix}, for a list/queryset of
    ``ProjectMember`` objects.
    """
    members = list(members or [])
    if not members:
        return {"full": "", "short": ""}

    names = [m.display_name for m in members]
    full = ", ".join(names)

    visible = names[:short_limit]
    overflow = len(names) - len(visible)
    short = ", ".join(visible)
    if overflow:
        short += f" and {overflow} other{'' if overflow == 1 else 's'}"

    return {"full": full, "short": short}


@register.filter
def task_dates(task):
    """``{'short': ..., 'full': ...}`` for a task's date span — see
    ``apps.tasks.calendar.task_date_label``. Rendered by
    ``_components/task_dates.html``."""
    return task_date_label(task)


@register.filter
def due_hint(task, member=None):
    """How long an open task has left, beside its dates: "4 days left",
    "1 day left" or "Due today". Empty for a task with no deadline, one
    already completed, and one past due — the Overdue (or Extended) pill
    says that.

    Pass the viewer's ``ProjectMember`` (``task|due_hint:project_member``):
    when a Lead gave them their own dates, the hint speaks to those instead,
    since the dates beside it are the task's — "Your start Nov 20", then
    "Your deadline Nov 25 · 4 days left" or "Your deadline passed Nov 12",
    joined with a middle dot.
    """
    from django.utils import timezone

    from apps.core.enums import TaskStatus

    if task.status == TaskStatus.COMPLETED:
        return ""
    own_start = own_deadline = None
    if member and getattr(member, "pk", None):
        own_start = task.personal_start_dates().get(member.pk)
        own_deadline = task.personal_deadlines().get(member.pk)
    parts = []
    if own_start is not None:
        start_zone = task.start_zone_for(member)
        parts.append(f"Your start {own_start.astimezone(start_zone):%b %-d}")
    if own_deadline is not None:
        zone = task.deadline_zone_for(member)
        date = f"{own_deadline.astimezone(zone):%b %-d}"
        if own_deadline < timezone.now():
            parts.append(f"Your deadline passed {date}")
        else:
            parts.append(f"Your deadline {date} · {_days_left(own_deadline, zone)}")
    elif task.deadline and not task.is_past_due:
        parts.append(_days_left(task.deadline, task.tz))
    return " · ".join(parts)


def _days_left(deadline, zone) -> str:
    """Days until ``deadline``, counted on the calendar of ``zone`` — the
    zone the deadline is set in, so "Due today" means its own today."""
    from django.utils import timezone

    days = (timezone.localdate(deadline, zone) - timezone.localdate(timezone=zone)).days
    if days <= 0:
        return "Due today"
    return f"{days} day{'' if days == 1 else 's'} left"
