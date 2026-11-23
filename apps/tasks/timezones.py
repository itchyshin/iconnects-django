"""Timezones for task dates.

A task's start and due dates are stored as instants (UTC, ``USE_TZ``), and
each task names the IANA timezone that governs them (``Task.timezone``): the
Lead's typed clock time is read in it, and the dates are shown in it, with
the zone's abbreviation, wherever they appear. A Lead can give an assignee
their own zone along with their own dates (``AssigneeDeadline.timezone``);
it governs that member's own dates, and a date they follow from the task
stays in the task's zone.

Wherever a zone is picked, the picker starts on the user's system timezone
(read in the browser, see ``_components/timezone_select.html``) unless one
is already saved. Without script the field comes back empty and the
server's ``settings.TIME_ZONE`` is used, as before zones existed.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone as dj_timezone

# Regions offered in the picker. Everything else in the tz database (the
# old "US/Eastern" style names, "EST5EDT", "Etc/GMT+7" …) is still accepted
# when submitted, but not listed.
_REGIONS = (
    "Africa",
    "America",
    "Antarctica",
    "Arctic",
    "Asia",
    "Atlantic",
    "Australia",
    "Europe",
    "Indian",
    "Pacific",
)

# Old names some browsers still report (Chrome's ICU in particular), mapped
# to the current name the picker lists. Anything not here that the tz
# database knows is still accepted as it is.
ALIASES = {
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Catamarca": "America/Argentina/Catamarca",
    "America/Cordoba": "America/Argentina/Cordoba",
    "America/Fort_Wayne": "America/Indiana/Indianapolis",
    "America/Godthab": "America/Nuuk",
    "America/Indianapolis": "America/Indiana/Indianapolis",
    "America/Jujuy": "America/Argentina/Jujuy",
    "America/Knox_IN": "America/Indiana/Knox",
    "America/Louisville": "America/Kentucky/Louisville",
    "America/Mendoza": "America/Argentina/Mendoza",
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Chongqing": "Asia/Shanghai",
    "Asia/Chungking": "Asia/Shanghai",
    "Asia/Dacca": "Asia/Dhaka",
    "Asia/Harbin": "Asia/Shanghai",
    "Asia/Istanbul": "Europe/Istanbul",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Macao": "Asia/Macau",
    "Asia/Rangoon": "Asia/Yangon",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Thimbu": "Asia/Thimphu",
    "Asia/Ujung_Pandang": "Asia/Makassar",
    "Asia/Ulan_Bator": "Asia/Ulaanbaatar",
    "Atlantic/Faeroe": "Atlantic/Faroe",
    "Europe/Kiev": "Europe/Kyiv",
    "Europe/Uzhgorod": "Europe/Kyiv",
    "Europe/Zaporozhye": "Europe/Kyiv",
    "Pacific/Enderbury": "Pacific/Kanton",
    "Pacific/Ponape": "Pacific/Pohnpei",
    "Pacific/Truk": "Pacific/Chuuk",
    "Etc/GMT": "UTC",
    "Etc/UCT": "UTC",
    "Etc/Universal": "UTC",
    "Etc/UTC": "UTC",
    "Etc/Zulu": "UTC",
    "GMT": "UTC",
    "UCT": "UTC",
    "Universal": "UTC",
    "Zulu": "UTC",
}

PICKER_FORMAT = "%Y-%m-%dT%H:%M"

# The time a new task's start and due dates take when only a day is picked.
DEFAULT_START_TIME = time(9, 0)
DEFAULT_DUE_TIME = time(17, 0)
WHEN_FORMAT = "%b %-d, %Y %-I:%M %p"


@lru_cache(maxsize=1)
def _known() -> frozenset[str]:
    return frozenset(available_timezones())


def default_zone_name() -> str:
    """The zone used when none is chosen: the server's ``TIME_ZONE``."""
    return settings.TIME_ZONE


def canonical(name) -> str | None:
    """``name`` as the picker lists it, or None when it is empty or not a
    timezone the tz database knows."""
    name = (name or "").strip()
    if not name:
        return None
    name = ALIASES.get(name, name)
    return name if name in _known() or name == "UTC" else None


def get_zone(name=None) -> ZoneInfo:
    """The ``ZoneInfo`` for ``name``, or the default zone when it is empty
    or unknown — so a bad value never stops a page from rendering."""
    for candidate in (canonical(name), default_zone_name(), "UTC"):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return ZoneInfo("UTC")  # pragma: no cover


def validate_timezone(value) -> None:
    if canonical(value) is None:
        raise ValidationError(f"{value!r} is not a known timezone.")


# ---------------------------------------------------------------------------
# Clock time <-> instant
# ---------------------------------------------------------------------------
def to_wall(value: datetime | None, zone) -> datetime | None:
    """``value`` as clock time in ``zone``, with no zone attached."""
    if value is None:
        return None
    if dj_timezone.is_naive(value):
        return value
    return value.astimezone(_zone(zone)).replace(tzinfo=None)


def from_wall(value: datetime | None, zone) -> datetime | None:
    """Clock time ``value`` read in ``zone``, as an aware datetime."""
    if value is None:
        return None
    if dj_timezone.is_aware(value):
        return value
    return value.replace(tzinfo=_zone(zone))


def rezone(value: datetime | None, old_zone, new_zone) -> datetime | None:
    """The instant with ``value``'s clock time in ``old_zone``, read in
    ``new_zone`` instead — the same "5:00 PM", in another zone."""
    return from_wall(to_wall(value, old_zone), new_zone)


def wall_delta(old, old_zone, new, new_zone) -> timedelta:
    """How far a date moved on the clock: from ``old`` in ``old_zone`` to
    ``new`` in ``new_zone``. Zero when only the zone changed and the clock
    time was kept; a day, not 23 or 25 hours, across a daylight-saving
    change."""
    return to_wall(new, new_zone) - to_wall(old, old_zone)


def shift_wall(value: datetime, delta: timedelta, zone) -> datetime:
    """``value`` moved by ``delta`` on the clock in ``zone``, so 5:00 PM stays
    5:00 PM across a daylight-saving change."""
    return from_wall(to_wall(value, zone) + delta, zone)


def carried_own_date(
    own: datetime | None,
    *,
    own_zone: str,
    old_task_zone: str,
    new_task_zone: str,
    old_value: datetime | None = None,
    new_value: datetime | None = None,
    choice: str = "keep",
    moved: bool = False,
) -> datetime | None:
    """The own date a member ends up with after a Lead edits the task.

    ``own_zone`` is the member's own zone ("" when their dates follow the
    task's zone). When the task's zone changes, such a member's own dates
    keep their clock time in the new zone, just as the task's do. When the
    task's date of the same kind ``moved`` (``old_value`` -> ``new_value``),
    the Lead's ``choice`` applies: ``shift`` moves the own date by the same
    amount on the clock, in the zone governing it; ``clear`` removes it;
    ``keep`` leaves it. ``TaskForm`` checks the result and
    ``services.apply_task_date_change`` writes it, so the two agree.
    """
    if own is None:
        return None
    zone_changed = canonical(old_task_zone) != canonical(new_task_zone)
    if not own_zone and zone_changed:
        own = rezone(own, old_task_zone, new_task_zone)
    if not moved:
        return own
    if choice == "clear":
        return None
    if choice == "shift" and old_value is not None and new_value is not None:
        delta = wall_delta(old_value, old_task_zone, new_value, new_task_zone)
        if delta:
            return shift_wall(own, delta, own_zone or new_task_zone)
    return own


def picker_value(value: datetime | None, zone) -> str:
    """A date as a ``datetime-local`` input takes it, in ``zone``."""
    return to_wall(value, zone).strftime(PICKER_FORMAT) if value else ""


def abbreviation(value: datetime, zone) -> str:
    """The zone's short name at ``value`` ("MST", "MDT", or "+08" where the
    tz database has no letters for it)."""
    return value.astimezone(_zone(zone)).tzname() or ""


def format_when(value: datetime | None, zone, fmt: str = WHEN_FORMAT) -> str:
    """``value`` in ``zone``, with the zone's abbreviation:
    "Nov 30, 2026 5:00 PM MST"."""
    if value is None:
        return ""
    local = value.astimezone(_zone(zone))
    return f"{local.strftime(fmt)} {local.tzname() or ''}".rstrip()


def _zone(zone) -> ZoneInfo:
    return zone if isinstance(zone, ZoneInfo) else get_zone(zone)


# ---------------------------------------------------------------------------
# The picker
# ---------------------------------------------------------------------------
def _offset_label(offset: timedelta) -> str:
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "−"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{mins:02d}"


# Extra words a country can be searched by, beyond its name and code.
COUNTRY_SYNONYMS = {
    "US": "USA America",
    "GB": "UK Britain England Scotland Wales",
    "AE": "UAE Emirates",
    "KR": "South Korea",
    "KP": "North Korea",
    "RU": "Russia",
    "NL": "Holland",
    "CZ": "Czechia",
}


def _tab_rows(filename: str) -> list[list[str]]:
    """The rows of one of the tz database's ``.tab`` files: from the
    ``tzdata`` package, else the system's copy. Empty when neither has it."""
    text = ""
    try:
        from importlib.resources import files

        text = files("tzdata.zoneinfo").joinpath(filename).read_text(encoding="utf-8")
    except Exception:
        try:
            with open(f"/usr/share/zoneinfo/{filename}", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return []
    return [
        line.split("\t") for line in text.splitlines() if line and not line.startswith("#")
    ]


@lru_cache(maxsize=1)
def _zone_places() -> dict[str, tuple[str, str]]:
    """Each zone the picker lists, as ``{zone: (country code, note)}``: the
    tz database's ``zone.tab``, one entry per zone, with notes such as
    "Mountain - AB, BC(E)". Falls back to every zone in the listed regions,
    with no country, when the table is missing."""
    places = {
        row[2]: (row[0], row[3] if len(row) > 3 else "")
        for row in _tab_rows("zone.tab")
        if len(row) >= 3 and row[2] in _known()
    }
    if not places:
        places = {
            name: ("", "")
            for name in _known()
            if name.partition("/")[0] in _REGIONS and "/" in name and name not in ALIASES
        }
    return places


@lru_cache(maxsize=1)
def _tab_country_names() -> dict[str, str]:
    return {row[0]: row[1] for row in _tab_rows("iso3166.tab") if len(row) >= 2}


def _country_name(code: str) -> str:
    if not code:
        return ""
    try:
        from django_countries import countries

        name = countries.name(code)
    except Exception:  # pragma: no cover - settings not ready
        name = ""
    return str(name or _tab_country_names().get(code, code))


def _city(zone: str) -> str:
    return zone.rsplit("/", 1)[-1].replace("_", " ")


def _option(zone: str, now: datetime) -> dict:
    """One picker entry: the zone, how it is labelled, and the words it is
    found by (city, country, its code and other names, the zone's name and
    note, its abbreviation and offset)."""
    if zone == "UTC":
        code, note, city, country = "", "", "UTC", "Coordinated Universal Time"
    else:
        code, note = _zone_places().get(zone, ("", ""))
        city, country = _city(zone), _country_name(code)
    local = now.astimezone(ZoneInfo(zone))
    offset = local.utcoffset() or timedelta(0)
    offset_label = _offset_label(offset)
    abbr = local.tzname() or ""
    label = f"{city}, {country}" if country and country != city else city
    minutes = int(offset.total_seconds() // 60)
    hours, mins = divmod(abs(minutes), 60)
    sign = "+" if minutes >= 0 else "-"
    search = " ".join(
        part
        for part in (
            city,
            country,
            code,
            COUNTRY_SYNONYMS.get(code, ""),
            # The tz database's own name for the country, without its
            # bracketed parts ("Virgin Islands (UK)" is not the UK).
            re.sub(r"\s*\(.*?\)", "", _tab_country_names().get(code, "")),
            zone.replace("_", " "),
            zone,
            note,
            abbr if abbr[:1].isalpha() else "",
            offset_label.replace("\u2212", "-"),
            f"utc{sign}{hours}" + (f":{mins:02d}" if mins else ""),
            f"gmt{sign}{hours}" + (f":{mins:02d}" if mins else ""),
        )
        if part
    )
    # The tz database's notes sometimes open with the abbreviation
    # ("CST - AB, BC(E)"); it is shown once.
    if abbr and note.startswith(f"{abbr} - "):
        note = note[len(abbr) + 3:]
    detail = " \u00b7 ".join(
        dict.fromkeys(
            part
            for part in (abbr if abbr[:1].isalpha() else "", note, zone)
            if part and part != city
        )
    )
    return {
        "value": zone,
        "label": label,
        "offset": offset_label,
        "display": f"({offset_label}) {label}",
        "detail": detail,
        "search": search,
        "sort": (minutes, label.lower()),
    }


@lru_cache(maxsize=4)
def _options(hour_key: str) -> tuple:
    now = dj_timezone.now()
    options = [_option(zone, now) for zone in ["UTC", *_zone_places()]]
    return tuple(sorted(options, key=lambda option: option["sort"]))


def timezone_choices(extra: str | None = None) -> list[dict]:
    """The picker's entries, ordered by their offset right now, then by
    name. Each is a dict: ``value`` (the IANA name), ``label`` ("Edmonton,
    Canada"), ``offset`` ("UTC\u221206:00"), ``display`` (both, as the
    picker shows its choice), ``detail`` (abbreviation, the tz database's
    note and the zone's name) and ``search`` (the words it is found by).

    ``extra``, a saved zone the list leaves out (an old "US/Eastern" style
    name), is offered too, so opening a form never quietly changes it."""
    options = list(_options(dj_timezone.now().strftime("%Y%m%d%H")))
    if extra and extra not in {option["value"] for option in options}:
        try:
            options.insert(0, _option(extra, dj_timezone.now()))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return options


def short_zone_label(name, at: datetime | None = None) -> str:
    """A zone in a few words, for tight spaces: its city and its offset
    ``at`` (now by default), "Tokyo (UTC+9)", "Kolkata (UTC+5:30)",
    "UTC"."""
    zone = get_zone(name)
    key = getattr(zone, "key", "UTC")
    if key == "UTC":
        return "UTC"
    offset = (at or dj_timezone.now()).astimezone(zone).utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    hours, mins = divmod(abs(minutes), 60)
    sign = "+" if minutes >= 0 else "\u2212"
    return f"{_city(key)} (UTC{sign}{hours}" + (f":{mins:02d}" if mins else "") + ")"


def timezone_option(value) -> dict | None:
    """The picker's entry for ``value`` (as in :func:`timezone_choices`), for
    the form to show the saved zone before the full list has loaded. None
    when ``value`` is empty or not a known zone."""
    if not value or canonical(value) is None:
        return None
    for option in _options(dj_timezone.now().strftime("%Y%m%d%H")):
        if option["value"] == value:
            return option
    try:
        return _option(value, dj_timezone.now())
    except (ZoneInfoNotFoundError, ValueError):
        return None


class TimezoneField(forms.CharField):
    """A timezone picked from the list (``_components/timezone_select.html``
    draws it). Cleans to the canonical name, or "" when left empty; a name
    the tz database does not know is refused."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("max_length", 64)
        super().__init__(*args, **kwargs)

    def clean(self, value):
        value = super().clean(value)
        if not value:
            return ""
        name = canonical(value)
        if name is None:
            raise ValidationError("Choose a timezone from the list.")
        return name


class ZonedDateTimeField(forms.DateTimeField):
    """A ``datetime-local`` date read and shown in ``zone`` rather than the
    server's timezone. The form sets ``zone`` (a ``ZoneInfo``) before it is
    rendered or cleaned."""

    zone = None

    def prepare_value(self, value):
        if isinstance(value, datetime) and dj_timezone.is_aware(value) and self.zone:
            return to_wall(value, self.zone)
        return super().prepare_value(value)

    def to_python(self, value):
        with dj_timezone.override(self.zone or dj_timezone.get_current_timezone()):
            return super().to_python(value)


class DateTimeSplitWidget(forms.MultiWidget):
    """A day picker and a time picker side by side (``<input type=date>`` and
    ``<input type=time>``), which the form reads as one clock time,
    "YYYY-MM-DDTHH:MM".

    While no day is set the time is empty too. Picking a day fills in
    ``default_time`` if the time is still empty (9:00 AM for a start date,
    5:00 PM for a due date; ``data-default-time`` on the time picker, read
    by ``window.dateTimeDefaults`` in base.html), and clearing the day
    clears the time. An empty day means no date, whatever the time says; a
    day sent with no time takes ``default_time``. A single
    ``<name>`` value ("YYYY-MM-DDTHH:MM", or a bare "YYYY-MM-DD", which also
    takes ``default_time``) is read when the two parts are not sent.
    """

    def __init__(self, default_time: time, attrs=None, time_label: str = "Time"):
        self.default_time = default_time
        base = dict(attrs or {})
        widgets = (
            forms.DateInput(attrs={**base, "type": "date"}, format="%Y-%m-%d"),
            forms.TimeInput(
                attrs={
                    **base,
                    "type": "time",
                    "aria-label": time_label,
                    "data-default-time": default_time.strftime("%H:%M"),
                },
                format="%H:%M",
            ),
        )
        super().__init__(widgets)

    def decompress(self, value):
        default = self.default_time.strftime("%H:%M")
        if isinstance(value, datetime):
            return [value.strftime("%Y-%m-%d"), value.strftime("%H:%M")]
        if isinstance(value, date):
            return [value.strftime("%Y-%m-%d"), default]
        if isinstance(value, str) and value:
            day, _, clock = value.replace(" ", "T").partition("T")
            return [day, clock[:5] or default]
        return ["", ""]

    def _join(self, day: str, clock: str) -> str:
        day, clock = (day or "").strip(), (clock or "").strip()
        if not day:
            return ""
        return f"{day}T{clock or self.default_time.strftime('%H:%M')}"

    def value_from_datadict(self, data, files, name):
        if f"{name}_0" in data:
            return self._join(data.get(f"{name}_0"), data.get(f"{name}_1"))
        value = data.get(name)
        if isinstance(value, str) and len(value.strip()) == 10:  # a bare day
            return self._join(value, "")
        return value

    def value_omitted_from_data(self, data, files, name):
        return name not in data and super().value_omitted_from_data(data, files, name)
