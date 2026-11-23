"""Grid-building for the task calendar view (year / month / week / day).

Buckets tasks (already filtered to those with a calendar-relevant date) by
date, using each task's ``deadline`` if set, otherwise its ``start_date``,
each read on the calendar of the task's own timezone (``Task.timezone``) —
so a task due 11:00 PM in Edmonton sits on that day wherever the server is.
Weeks start on Monday throughout.

Tasks with *both* ``start_date`` and ``deadline`` set, spanning more than one
day, are treated as ranges: month/week views draw them as lane-stacked bars
spanning the days they cover (``build_month_grid`` / ``build_week_grid``),
while the year view and day agenda (which have no room for bars) simply mark
every day the range touches.
"""

from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.utils import timezone

from apps.tasks.timezones import get_zone

VIEWS = ("year", "month", "week", "day")


@dataclass
class CalendarDay:
    date: date
    in_current_month: bool
    is_today: bool
    tasks: list = field(default_factory=list)


@dataclass
class DayAgendaItem:
    task: object
    is_range: bool
    range_start: date | None = None
    range_end: date | None = None


def _to_local_date(dt, zone=None) -> date:
    """``dt``'s date in ``zone`` (an IANA name; the server's when empty)."""
    if not timezone.is_aware(dt):
        return dt.date()
    return dt.astimezone(get_zone(zone)).date() if zone else timezone.localtime(dt).date()


def _task_zone(task):
    return getattr(task, "timezone", None)


def task_calendar_date(task) -> date | None:
    """Return the date, in the task's zone, it should be plotted on, or
    ``None``."""
    dt = task.deadline or task.start_date
    if dt is None:
        return None
    return _to_local_date(dt, _task_zone(task))


def task_range(task) -> tuple[date, date] | None:
    """Return ``(start, end)`` inclusive local dates if a task spans more
    than one day (both ``start_date`` and ``deadline`` set, on different
    calendar days), else ``None``.
    """
    if task.start_date is None or task.deadline is None:
        return None
    start = _to_local_date(task.start_date, _task_zone(task))
    end = _to_local_date(task.deadline, _task_zone(task))
    if start == end:
        return None
    if start > end:
        start, end = end, start
    return start, end


def _fmt_date(day: date, *, with_year: bool) -> str:
    return f"{day.strftime('%b')} {day.day}, {day.year}" if with_year else f"{day.strftime('%b')} {day.day}"


def task_date_label(task, today: date | None = None) -> dict:
    """One task's dates, formatted the same way everywhere they are shown.

    Returns ``{"short": ..., "full": ...}``. ``short`` is what a card or row
    displays and drops the year for dates in the current year; ``full``
    always spells the year out and is used as the hover title.

    A task with both dates shows the span ("Mar 10 – Mar 12"); one with a
    single date says which date it is, so a bare day is never ambiguous.
    Both are "" when the task has no dates at all.
    """
    empty = {"short": "", "full": ""}
    zone = _task_zone(task)
    start = _to_local_date(task.start_date, zone) if task.start_date else None
    end = _to_local_date(task.deadline, zone) if task.deadline else None
    if start is None and end is None:
        return empty
    if today is None:
        today = timezone.localdate()

    if start is not None and end is not None:
        if start > end:
            start, end = end, start
        if start == end:
            return {
                "short": f"Due {_fmt_date(end, with_year=end.year != today.year)}",
                "full": f"Due {_fmt_date(end, with_year=True)}",
            }
        # A span inside one year carries the year once, on the end date.
        if start.year == end.year:
            short_start = _fmt_date(start, with_year=False)
            short_end = _fmt_date(end, with_year=end.year != today.year)
        else:
            short_start = _fmt_date(start, with_year=True)
            short_end = _fmt_date(end, with_year=True)
        return {
            "short": f"{short_start} – {short_end}",
            "full": f"{_fmt_date(start, with_year=True)} – {_fmt_date(end, with_year=True)}",
        }

    day = end if end is not None else start
    prefix = "Due" if end is not None else "Starts"
    return {
        "short": f"{prefix} {_fmt_date(day, with_year=day.year != today.year)}",
        "full": f"{prefix} {_fmt_date(day, with_year=True)}",
    }


def _sort_key(task):
    return task.deadline or task.start_date


def bucket_tasks_by_day(tasks) -> dict:
    """Group every task onto each day it's active: a single day for
    point-in-time tasks, every day in its range for ranged tasks. Used
    where a full spanning-bar render isn't practical (year-view dots,
    day-view agenda).
    """
    by_day: dict = {}
    for task in tasks:
        rng = task_range(task)
        if rng is not None:
            start, end = rng
            day = start
            while day <= end:
                by_day.setdefault(day, []).append(task)
                day += timedelta(days=1)
        else:
            day = task_calendar_date(task)
            if day is not None:
                by_day.setdefault(day, []).append(task)
    for day_tasks in by_day.values():
        day_tasks.sort(key=_sort_key)
    return by_day


def bucket_single_day_tasks(tasks) -> dict:
    """Like :func:`bucket_tasks_by_day` but skips ranged tasks — those are
    drawn as spanning bars instead of day-cell pills.
    """
    by_day: dict = {}
    for task in tasks:
        if task_range(task) is not None:
            continue
        day = task_calendar_date(task)
        if day is not None:
            by_day.setdefault(day, []).append(task)
    for day_tasks in by_day.values():
        day_tasks.sort(key=_sort_key)
    return by_day


def _week_lanes(week_dates: list[date], ranged_tasks) -> list[list[dict]]:
    """Lane-stack ranged tasks overlapping ``week_dates`` (7 consecutive
    days) into non-overlapping rows, clipped to this week's bounds.

    Each bar dict has ``task``, ``start_col``/``end_col`` (1-7, the day
    columns it spans within this week), and ``is_range_start``/
    ``is_range_end`` (whether the task's actual start/end falls in this
    week, vs. continuing from/into an adjacent week).
    """
    week_start, week_end = week_dates[0], week_dates[-1]
    bars = []
    for task in ranged_tasks:
        start, end = task_range(task)
        if end < week_start or start > week_end:
            continue
        clipped_start = max(start, week_start)
        clipped_end = min(end, week_end)
        bars.append(
            {
                "task": task,
                "start_col": (clipped_start - week_start).days + 1,
                "end_col": (clipped_end - week_start).days + 1,
                "is_range_start": clipped_start == start,
                "is_range_end": clipped_end == end,
            }
        )
    bars.sort(key=lambda b: (b["start_col"], b["start_col"] - b["end_col"]))

    lane_ends: list[int] = []
    for bar in bars:
        placed = False
        for lane_idx, last_end in enumerate(lane_ends):
            if bar["start_col"] > last_end:
                lane_ends[lane_idx] = bar["end_col"]
                bar["lane"] = lane_idx
                placed = True
                break
        if not placed:
            bar["lane"] = len(lane_ends)
            lane_ends.append(bar["end_col"])

    lanes: list[list[dict]] = [[] for _ in lane_ends]
    for bar in bars:
        lanes[bar["lane"]].append(bar)
    return lanes


def build_month_grid(year: int, month: int, tasks) -> list[dict]:
    """Build a week-by-week grid for ``year``/``month``.

    Each week is ``{"days": [CalendarDay, ...7...], "lanes": [[bar, ...], ...]}``
    — ``days`` carries only non-ranged tasks (rendered as day-cell pills),
    ``lanes`` carries lane-stacked range bars spanning this week's columns.
    ``tasks`` should already be filtered to tasks with a calendar date set.
    """
    tasks = list(tasks)
    ranged_tasks = [t for t in tasks if task_range(t) is not None]
    by_day = bucket_single_day_tasks(tasks)
    today = timezone.localdate()
    cal = _calendar.Calendar(firstweekday=0)  # Monday
    weeks = []
    for week_dates in cal.monthdatescalendar(year, month):
        days = [
            CalendarDay(
                date=day,
                in_current_month=day.month == month,
                is_today=day == today,
                tasks=by_day.get(day, []),
            )
            for day in week_dates
        ]
        weeks.append({"days": days, "lanes": _week_lanes(week_dates, ranged_tasks)})
    return weeks


def build_year_grid(year: int, tasks) -> list[dict]:
    """Build 12 mini month grids (one per month) for ``year``.

    Unlike :func:`build_month_grid`, mini calendars are too small for bars,
    so every day a ranged task touches just gets a dot (via
    :func:`bucket_tasks_by_day`).
    """
    by_day = bucket_tasks_by_day(tasks)
    today = timezone.localdate()
    cal = _calendar.Calendar(firstweekday=0)
    months = []
    for month in range(1, 13):
        weeks = []
        for week_dates in cal.monthdatescalendar(year, month):
            days = [
                CalendarDay(
                    date=day,
                    in_current_month=day.month == month,
                    is_today=day == today,
                    tasks=by_day.get(day, []),
                )
                for day in week_dates
            ]
            weeks.append(days)
        months.append(
            {
                "month": month,
                "label": date(year, month, 1).strftime("%B"),
                "first_day": date(year, month, 1),
                "weeks": weeks,
            }
        )
    return months


def build_week_grid(anchor: date, tasks) -> dict:
    """Build the 7 days (Mon–Sun) of the week containing ``anchor``, plus
    lane-stacked range bars spanning that week.

    Returns ``{"days": [CalendarDay, ...7...], "lanes": [[bar, ...], ...]}``,
    same shape as one week of :func:`build_month_grid`.
    """
    tasks = list(tasks)
    start = anchor - timedelta(days=anchor.weekday())
    week_dates = [start + timedelta(days=offset) for offset in range(7)]
    ranged_tasks = [t for t in tasks if task_range(t) is not None]
    by_day = bucket_single_day_tasks(tasks)
    today = timezone.localdate()
    days = [
        CalendarDay(
            date=day,
            in_current_month=True,
            is_today=day == today,
            tasks=by_day.get(day, []),
        )
        for day in week_dates
    ]
    return {"days": days, "lanes": _week_lanes(week_dates, ranged_tasks)}


def build_day_tasks(anchor: date, tasks) -> list[DayAgendaItem]:
    """Return the tasks active on exactly ``anchor``, sorted by time, each
    wrapped with its range info (if any) for display.
    """
    items = []
    for task in bucket_tasks_by_day(tasks).get(anchor, []):
        rng = task_range(task)
        items.append(
            DayAgendaItem(
                task=task,
                is_range=rng is not None,
                range_start=rng[0] if rng else None,
                range_end=rng[1] if rng else None,
            )
        )
    return items


def parse_view_date(raw: str | None) -> date:
    """Parse a ``YYYY-MM-DD`` date param, defaulting to today on any error."""
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return timezone.localdate()


def clamp_month(year: int, month: int) -> tuple[int, int]:
    """Normalize an out-of-range month (e.g. month=13) by rolling into year."""
    month0 = month - 1
    year += month0 // 12
    month = month0 % 12 + 1
    return year, month


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    return clamp_month(year, month + delta)


def shift_date(view: str, anchor: date, delta: int) -> date:
    """Move ``anchor`` by one unit of ``view`` (year/month/week/day) * delta."""
    if view == "year":
        return date(anchor.year + delta, 1, 1)
    if view == "month":
        year, month = shift_month(anchor.year, anchor.month, delta)
        return date(year, month, 1)
    if view == "week":
        return anchor + timedelta(weeks=delta)
    if view == "day":
        return anchor + timedelta(days=delta)
    raise ValueError(f"Unknown calendar view: {view!r}")
