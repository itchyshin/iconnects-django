"""Timezones for task dates.

A Lead sets the timezone that governs a task's start and due dates
(``Task.timezone``), and may give an assignee their own zone with their own
dates (``AssigneeDeadline.timezone``). Dates are stored as instants; the
zone decides how the Lead's clock time is read and how dates are shown.
Every picker starts on the user's system timezone unless a zone is saved.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest
from django.template import Context, Template
from django.urls import reverse

from apps.audit.models import AuditAction, AuditLog
from apps.notifications.models import Notification
from apps.tasks.calendar import task_calendar_date, task_date_label
from apps.tasks.deadline_tests import _edit, _post, _setup
from apps.tasks.import_csv import import_rows
from apps.tasks.models import AssigneeDeadline, KanbanBoard, Task
from apps.tasks.services import set_member_dates
from apps.tasks.timezones import canonical, carried_own_date, format_when

DENVER = ZoneInfo("America/Denver")
TOKYO = ZoneInfo("Asia/Tokyo")
UTC = dt_timezone.utc


def _at(zone, *args):
    return datetime(*args, tzinfo=zone)


def _update_url(project, task):
    return reverse("tasks:update", kwargs={"project_pk": project.pk, "pk": task.pk})


def _create(client, project, **data):
    column = KanbanBoard.objects.filter(project=project).first()
    return client.post(
        reverse("tasks:create", kwargs={"project_pk": project.pk}),
        {"name": "Zoned", "container": column.pk, "contribution_type": "OTHER", **data},
    )


@pytest.fixture
def zoned(pm_user, user, other_user):
    """A task due Jan 15, 2027 5:00 PM in Denver; Ari follows the task's
    dates, Bo is assigned too. Returns (project, task, lead, ari, bo)."""
    project, task, lead, [ari, bo] = _setup(pm_user, user, other_user)
    task.timezone = "America/Denver"
    task.deadline = _at(DENVER, 2027, 1, 15, 17, 0)
    task.save()
    return project, task, lead, ari, bo


# ---------------------------------------------------------------------------
# The task's zone
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_task_dates_are_read_in_the_chosen_zone(client, pm_user, other_user):
    project, _task, _lead, _ = _setup(pm_user, other_user)
    client.force_login(pm_user)

    response = _create(
        client, project, timezone="America/Denver", deadline="2027-01-15T17:00"
    )

    assert response.status_code == 302
    task = Task.objects.get(name="Zoned")
    assert task.timezone == "America/Denver"
    assert task.deadline == datetime(2027, 1, 16, 0, 0, tzinfo=UTC)


@pytest.mark.django_db
def test_task_without_a_zone_takes_the_servers(client, settings, pm_user, other_user):
    project, _task, _lead, _ = _setup(pm_user, other_user)
    settings.TIME_ZONE = "America/Denver"
    client.force_login(pm_user)

    _create(client, project, deadline="2027-01-15T17:00")

    task = Task.objects.get(name="Zoned")
    assert task.timezone == "America/Denver"
    assert task.deadline == _at(DENVER, 2027, 1, 15, 17, 0)


@pytest.mark.django_db
def test_new_task_defaults_to_the_servers_zone(pm_user, other_user, settings):
    _project, task, _lead, _ = _setup(pm_user, other_user)
    assert task.timezone == settings.TIME_ZONE


@pytest.mark.django_db
def test_old_zone_names_are_accepted_and_unknown_ones_refused(client, pm_user, other_user):
    project, _task, _lead, _ = _setup(pm_user, other_user)
    client.force_login(pm_user)

    _create(client, project, name="Kolkata", timezone="Asia/Calcutta", deadline="2027-01-15T17:00")
    assert Task.objects.get(name="Kolkata").timezone == "Asia/Kolkata"

    response = _create(client, project, name="Nowhere", timezone="Mars/Olympus_Mons")
    assert response.status_code == 200
    assert "Choose a timezone from the list." in response.content.decode()
    assert not Task.objects.filter(name="Nowhere").exists()


@pytest.mark.django_db
def test_create_form_starts_on_the_system_timezone(client, pm_user, other_user):
    project, _task, _lead, _ = _setup(pm_user, other_user)
    client.force_login(pm_user)

    content = client.get(
        reverse("tasks:create", kwargs={"project_pk": project.pk}),
        HTTP_HX_REQUEST="true",
    ).content.decode()

    # Nothing saved: the picker's script picks the system timezone, and the
    # server's default is what is sent without script.
    assert "timezoneSelect(null)" in content
    assert "Server default (UTC)</option>" in content
    assert "Type to search for a city or country" in content
    # The full list is fetched, not written into the form.
    assert reverse("tasks:timezones") in content
    assert 'value="America/Denver"' not in content


@pytest.mark.django_db
def test_new_task_times_start_empty_and_know_their_defaults(client, pm_user, other_user):
    project, _task, _lead, _ = _setup(pm_user, other_user)
    client.force_login(pm_user)

    content = client.get(
        reverse("tasks:create", kwargs={"project_pk": project.pk}),
        HTTP_HX_REQUEST="true",
    ).content.decode()

    # No day yet, so no time either; picking a day fills in the default.
    assert 'type="date" name="start_date_0" value="" class=' in content
    assert 'type="time" name="start_date_1" value="" ' in content
    assert 'data-default-time="09:00"' in content
    assert 'type="date" name="deadline_0" value="" class=' in content
    assert 'type="time" name="deadline_1" value="" ' in content
    assert 'data-default-time="17:00"' in content


@pytest.mark.django_db
def test_picking_only_a_day_gives_the_default_times(client, pm_user, other_user):
    project, _task, _lead, _ = _setup(pm_user, other_user)
    client.force_login(pm_user)

    _create(
        client, project, name="Days only", timezone="America/Denver",
        start_date_0="2027-01-11", start_date_1="",
        deadline_0="2027-01-15", deadline_1="",
    )
    _create(
        client, project, name="Own times", timezone="America/Denver",
        start_date_0="2027-01-11", start_date_1="08:15",
        deadline_0="2027-01-15", deadline_1="12:00",
    )
    _create(
        client, project, name="No day", timezone="America/Denver",
        start_date_0="", start_date_1="09:00", deadline_0="", deadline_1="17:00",
    )

    days_only = Task.objects.get(name="Days only")
    assert days_only.start_date == _at(DENVER, 2027, 1, 11, 9, 0)
    assert days_only.deadline == _at(DENVER, 2027, 1, 15, 17, 0)
    own = Task.objects.get(name="Own times")
    assert own.start_date == _at(DENVER, 2027, 1, 11, 8, 15)
    assert own.deadline == _at(DENVER, 2027, 1, 15, 12, 0)
    # A time with no day sets no date.
    no_day = Task.objects.get(name="No day")
    assert (no_day.start_date, no_day.deadline) == (None, None)


@pytest.mark.django_db
def test_a_bare_day_sent_as_one_value_gets_the_default_time(client, pm_user, other_user):
    project, _task, _lead, _ = _setup(pm_user, other_user)
    client.force_login(pm_user)

    _create(
        client, project, name="Bare", timezone="America/Denver",
        start_date="2027-01-11", deadline="2027-01-15",
    )

    task = Task.objects.get(name="Bare")
    assert task.start_date == _at(DENVER, 2027, 1, 11, 9, 0)
    assert task.deadline == _at(DENVER, 2027, 1, 15, 17, 0)


@pytest.mark.django_db
def test_hidden_select_has_no_id_for_htmx_to_settle(client, pm_user, zoned):
    """htmx restores class and style on elements whose id is in both the old
    and the new content when it swaps; with an id, the select the search box
    hides would reappear after the completion table re-renders."""
    project, task, *_ = zoned
    client.force_login(pm_user)

    for url in (
        reverse("tasks:create", kwargs={"project_pk": project.pk}),
        reverse("tasks:status_block", kwargs={"pk": task.pk}) + "?pane=1",
    ):
        content = client.get(url, HTTP_HX_REQUEST="true").content.decode()
        assert '<select name="timezone" class="select' in content


@pytest.mark.django_db
def test_edit_form_shows_the_dates_in_the_tasks_zone(client, pm_user, zoned):
    project, task, *_ = zoned
    client.force_login(pm_user)

    content = client.get(_update_url(project, task), HTTP_HX_REQUEST="true").content.decode()

    assert 'name="deadline_0" value="2027-01-15"' in content
    assert 'name="deadline_1" value="17:00"' in content
    assert "timezoneSelect('America/Denver')" in content
    assert (
        '<option value="America/Denver" selected '
        'data-display="(UTC\u221207:00) Denver, United States of America">'
    ) in content
    assert "Server default" not in content


@pytest.mark.django_db
def test_changing_the_zone_keeps_the_clock_time(client, pm_user, zoned):
    project, task, _lead, ari, bo = zoned
    client.force_login(pm_user)

    response = _edit(
        client, project, task, [ari, bo],
        deadline="2027-01-15T17:00", timezone="America/Toronto",
    )

    assert response.status_code == 302
    task.refresh_from_db()
    assert task.timezone == "America/Toronto"
    assert task.deadline == _at(ZoneInfo("America/Toronto"), 2027, 1, 15, 17, 0)
    row = AuditLog.objects.filter(action=AuditAction.TASK_UPDATED).latest("id")
    assert row.metadata["timezone"] == "America/Toronto"
    assert row.metadata["previous_timezone"] == "America/Denver"


@pytest.mark.django_db
def test_changing_the_zone_carries_members_following_it(client, pm_user, zoned):
    """Ari's own deadline follows the task's zone, so it keeps its clock time
    in the new one; Bo's is in Tokyo time and stays as it is."""
    project, task, lead, ari, bo = zoned
    ari_own = set_member_dates(
        task=task, member=ari, lead=lead, deadline=_at(DENVER, 2027, 1, 20, 9, 0)
    )
    bo_own = set_member_dates(
        task=task, member=bo, lead=lead, timezone="Asia/Tokyo",
        deadline=_at(TOKYO, 2027, 1, 22, 9, 0),
    )
    client.force_login(pm_user)

    response = _edit(
        client, project, task, [ari, bo],
        deadline="2027-01-15T17:00", timezone="America/Toronto",
        individual_deadlines="shift",
    )

    assert response.status_code == 302
    ari_own.refresh_from_db()
    bo_own.refresh_from_db()
    assert ari_own.deadline == _at(ZoneInfo("America/Toronto"), 2027, 1, 20, 9, 0)
    assert ari_own.timezone == ""
    assert bo_own.deadline == _at(TOKYO, 2027, 1, 22, 9, 0)
    row = AuditLog.objects.filter(
        action=AuditAction.TASK_DEADLINE_SET, metadata__batch="task_timezone"
    ).get()
    assert row.metadata["assignee_member_id"] == ari.pk


@pytest.mark.django_db
def test_shift_keeps_the_clock_time_across_daylight_saving(client, pm_user, zoned):
    """Denver moves to daylight time on Mar 14, 2027: a week's shift is a
    week on the clock, so 5:00 PM stays 5:00 PM."""
    project, task, lead, ari, bo = zoned
    task.deadline = _at(DENVER, 2027, 3, 10, 17, 0)
    task.save()
    own = set_member_dates(
        task=task, member=ari, lead=lead, deadline=_at(DENVER, 2027, 3, 12, 17, 0)
    )
    client.force_login(pm_user)

    response = _edit(
        client, project, task, [ari, bo],
        deadline="2027-03-17T17:00", timezone="America/Denver",
        individual_deadlines="shift",
    )

    assert response.status_code == 302
    own.refresh_from_db()
    assert own.deadline == _at(DENVER, 2027, 3, 19, 17, 0)
    assert own.deadline.astimezone(UTC).hour == 23  # MDT, not MST


@pytest.mark.django_db
def test_changing_only_the_zone_asks_about_own_dates(client, pm_user, zoned):
    project, task, lead, ari, bo = zoned
    set_member_dates(
        task=task, member=bo, lead=lead, timezone="Asia/Tokyo",
        deadline=_at(TOKYO, 2027, 1, 22, 9, 0),
    )
    client.force_login(pm_user)

    response = _edit(
        client, project, task, [ari, bo],
        deadline="2027-01-15T17:00", timezone="America/Toronto",
    )

    assert response.status_code == 200
    assert "Choose whether to shift, keep or remove them." in response.content.decode()


def test_carried_own_date_rules():
    own = _at(TOKYO, 2027, 1, 22, 9, 0)
    old = _at(DENVER, 2027, 1, 15, 17, 0)
    new = _at(ZoneInfo("America/Toronto"), 2027, 1, 15, 17, 0)
    # A zone of their own: a change of the task's zone alone moves nothing.
    assert carried_own_date(
        own, own_zone="Asia/Tokyo", old_task_zone="America/Denver",
        new_task_zone="America/Toronto", old_value=old, new_value=new,
        choice="shift", moved=True,
    ) == own
    # Following the task's zone: same clock time, in the new zone.
    following = _at(DENVER, 2027, 1, 20, 9, 0)
    assert carried_own_date(
        following, own_zone="", old_task_zone="America/Denver",
        new_task_zone="America/Toronto",
    ) == _at(ZoneInfo("America/Toronto"), 2027, 1, 20, 9, 0)
    assert carried_own_date(
        own, own_zone="Asia/Tokyo", old_task_zone="UTC", new_task_zone="UTC",
        old_value=old, new_value=old + timedelta(days=1), choice="clear", moved=True,
    ) is None


# ---------------------------------------------------------------------------
# A member's own zone
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_member_dates_are_read_in_their_own_zone(client, pm_user, zoned):
    _project, task, _lead, ari, _bo = zoned
    client.force_login(pm_user)

    response = _post(
        client, task, member=ari.pk, timezone="Asia/Tokyo",
        deadline="2027-01-20T09:00",
    )

    assert response.status_code == 200
    own = AssigneeDeadline.objects.get(member=ari)
    assert own.timezone == "Asia/Tokyo"
    assert own.deadline == _at(TOKYO, 2027, 1, 20, 9, 0)
    row = AuditLog.objects.get(action=AuditAction.TASK_DEADLINE_SET)
    assert row.metadata["timezone"] == "Asia/Tokyo"
    assert row.metadata["previous_timezone"] is None
    assert row.metadata["task_timezone"] == "America/Denver"
    note = Notification.objects.for_user(ari.users.first()).latest("id")
    assert "Jan 20, 2027 9:00 AM JST" in note.message


@pytest.mark.django_db
def test_same_as_task_saves_no_zone_of_their_own(client, pm_user, zoned):
    _project, task, _lead, ari, _bo = zoned
    client.force_login(pm_user)

    _post(client, task, member=ari.pk, timezone="", deadline="2027-01-20T09:00")

    own = AssigneeDeadline.objects.get(member=ari)
    assert own.timezone == ""
    assert own.deadline == _at(DENVER, 2027, 1, 20, 9, 0)


@pytest.mark.django_db
def test_a_zone_alone_for_a_member_without_own_dates_is_refused(client, pm_user, zoned):
    """The zone governs only the member's own dates, so with none to govern
    the save is refused with a reason rather than quietly doing nothing."""
    _project, task, _lead, ari, _bo = zoned
    client.force_login(pm_user)

    response = _post(client, task, member=ari.pk, timezone="Asia/Tokyo", start_date="", deadline="")

    assert response.status_code == 400
    assert (
        f"Set a start date or deadline for {ari.display_name} first"
        in response.content.decode()
    )
    assert not AssigneeDeadline.objects.exists()


@pytest.mark.django_db
def test_emptying_both_own_dates_still_removes_them_whatever_the_zone(client, pm_user, zoned):
    _project, task, lead, ari, _bo = zoned
    set_member_dates(
        task=task, member=ari, lead=lead, timezone="Asia/Tokyo",
        deadline=_at(TOKYO, 2027, 1, 20, 9, 0),
    )
    client.force_login(pm_user)

    response = _post(client, task, member=ari.pk, timezone="Asia/Tokyo", start_date="", deadline="")

    assert response.status_code == 200
    assert not AssigneeDeadline.objects.exists()


@pytest.mark.django_db
def test_a_date_sent_back_at_the_tasks_moment_follows_the_task(client, pm_user, zoned):
    """The dialog starts a date the member has no own value for on the
    task's (in the zone picked); sent back unchanged, it is not theirs."""
    _project, task, _lead, ari, _bo = zoned
    task.start_date = _at(DENVER, 2027, 1, 11, 9, 0)
    task.save()
    client.force_login(pm_user)

    # The task's start, as it reads in Tokyo, beside an own deadline.
    response = _post(
        client, task, member=ari.pk, timezone="Asia/Tokyo",
        start_date_0="2027-01-12", start_date_1="01:00",
        deadline_0="2027-01-20", deadline_1="09:00",
    )

    assert response.status_code == 200
    own = AssigneeDeadline.objects.get(member=ari)
    assert own.start_date is None
    assert own.deadline == _at(TOKYO, 2027, 1, 20, 9, 0)


@pytest.mark.django_db
def test_task_dates_sent_back_unchanged_save_nothing(client, pm_user, zoned):
    _project, task, _lead, ari, _bo = zoned
    client.force_login(pm_user)

    response = _post(
        client, task, member=ari.pk, timezone="America/Denver",
        deadline_0="2027-01-15", deadline_1="17:00",
    )

    # Nothing of their own, so the zone has nothing to govern either.
    assert response.status_code == 400
    assert "first" in response.content.decode()
    assert not AssigneeDeadline.objects.exists()


@pytest.mark.django_db
def test_dates_dialog_knows_the_tasks_dates(client, pm_user, zoned):
    _project, task, *_ = zoned
    client.force_login(pm_user)

    content = client.get(
        reverse("tasks:status_block", kwargs={"pk": task.pk}) + "?pane=1"
    ).content.decode()

    due_ms = int(task.deadline.timestamp()) * 1000
    assert (
        "memberDatesDialog({ taskZone: 'America/Denver', taskStart: null, "
        f"taskDue: {due_ms} }})"
    ) in content
    for name in ("start_date_0", "start_date_1", "deadline_0", "deadline_1"):
        assert f'name="{name}"' in content


@pytest.mark.django_db
def test_changing_only_the_members_zone_is_saved(pm_user, zoned):
    _project, task, lead, ari, _bo = zoned
    when = _at(TOKYO, 2027, 1, 20, 9, 0)
    set_member_dates(task=task, member=ari, lead=lead, deadline=when, timezone="Asia/Tokyo")
    set_member_dates(task=task, member=ari, lead=lead, deadline=when, timezone="Europe/Paris")

    assert AssigneeDeadline.objects.get(member=ari).timezone == "Europe/Paris"
    assert AuditLog.objects.filter(action=AuditAction.TASK_DEADLINE_SET).count() == 2


@pytest.mark.django_db
def test_a_date_followed_from_the_task_stays_in_the_tasks_zone(pm_user, zoned):
    _project, task, lead, ari, bo = zoned
    set_member_dates(
        task=task, member=ari, lead=lead, timezone="Asia/Tokyo",
        start_date=_at(TOKYO, 2027, 1, 10, 9, 0),
    )
    task = Task.objects.prefetch_related("assignee_deadlines").get(pk=task.pk)

    assert task.start_zone_for(ari) == TOKYO
    assert task.deadline_zone_for(ari) == DENVER
    assert task.deadline_zone_for(bo) == DENVER


@pytest.mark.django_db
def test_removing_own_dates_removes_their_zone(client, pm_user, zoned):
    _project, task, lead, ari, _bo = zoned
    set_member_dates(
        task=task, member=ari, lead=lead, timezone="Asia/Tokyo",
        deadline=_at(TOKYO, 2027, 1, 20, 9, 0),
    )
    client.force_login(pm_user)

    _post(client, task, member=ari.pk, action="clear")

    assert not AssigneeDeadline.objects.exists()
    row = AuditLog.objects.get(action=AuditAction.TASK_DEADLINE_CLEARED)
    assert row.metadata["previous_timezone"] == "Asia/Tokyo"


@pytest.mark.django_db
def test_start_and_deadline_in_different_zones_compare_as_moments(client, pm_user, zoned):
    """The task is due Jan 15 5:00 PM MST (Jan 16 00:00 UTC). 8:00 AM Jan 16
    in Tokyo is an hour earlier; 10:00 AM is an hour later. The refusal
    states both in the member's timezone."""
    _project, task, _lead, ari, _bo = zoned
    client.force_login(pm_user)

    ok = _post(client, task, member=ari.pk, timezone="Asia/Tokyo", start_date="2027-01-16T08:00")
    assert ok.status_code == 200

    refused = _post(client, task, member=ari.pk, timezone="Asia/Tokyo", start_date="2027-01-16T10:00")
    assert refused.status_code == 400
    assert (
        "The start date (Jan 16, 2027 10:00 AM JST) must be on or before the "
        "deadline (Jan 16, 2027 9:00 AM JST)."
    ) in refused.content.decode()


@pytest.mark.django_db
def test_own_deadline_may_come_before_the_tasks_start(client, pm_user, zoned):
    """A collaborator far ahead in time may be given a deadline that falls
    before the task's start: with no start of their own, nothing is
    checked."""
    _project, task, _lead, ari, _bo = zoned
    task.start_date = _at(DENVER, 2027, 1, 15, 9, 0)  # Jan 16 01:00 in Tokyo
    task.save()
    client.force_login(pm_user)

    response = _post(
        client, task, member=ari.pk, timezone="Asia/Tokyo",
        deadline_0="2027-01-15", deadline_1="17:00",  # Jan 15 01:00 in Denver
    )

    assert response.status_code == 200
    own = AssigneeDeadline.objects.get(member=ari)
    assert own.deadline < task.start_date
    assert own.start_date is None


@pytest.mark.django_db
def test_set_dates_for_all_keeps_own_dates_left_at_the_tasks(client, pm_user, zoned):
    """The dialog starts on the task's dates for everyone not done; a date
    sent back at the task's moment keeps each member's own, and only a date
    the Lead changed is given to all."""
    _project, task, lead, ari, bo = zoned
    task.start_date = _at(DENVER, 2027, 1, 11, 9, 0)
    task.save()
    set_member_dates(task=task, member=bo, lead=lead, deadline=_at(DENVER, 2027, 1, 20, 17, 0))
    client.force_login(pm_user)

    # Start changed; deadline left at the task's (Jan 15 5:00 PM Denver).
    response = _post(
        client, task, scope="pending", timezone="America/Denver",
        start_date_0="2027-01-12", start_date_1="09:00",
        deadline_0="2027-01-15", deadline_1="17:00",
    )

    assert response.status_code == 200
    own = {d.member_id: d for d in AssigneeDeadline.objects.all()}
    assert own[ari.pk].start_date == _at(DENVER, 2027, 1, 12, 9, 0)
    assert own[ari.pk].deadline is None
    assert own[bo.pk].start_date == _at(DENVER, 2027, 1, 12, 9, 0)
    assert own[bo.pk].deadline == _at(DENVER, 2027, 1, 20, 17, 0)

    # Both left at the task's: nothing to give anyone.
    response = _post(
        client, task, scope="pending", timezone="America/Denver",
        start_date_0="2027-01-11", start_date_1="09:00",
        deadline_0="2027-01-15", deadline_1="17:00",
    )
    assert response.status_code == 400
    assert "Change the start date or the deadline" in response.content.decode()


@pytest.mark.django_db
def test_set_dates_for_all_gives_everyone_the_zone(client, pm_user, zoned):
    _project, task, _lead, ari, bo = zoned
    client.force_login(pm_user)

    _post(client, task, scope="pending", timezone="Asia/Tokyo", deadline="2027-01-20T09:00")

    assert set(AssigneeDeadline.objects.values_list("timezone", flat=True)) == {"Asia/Tokyo"}
    assert {d.deadline for d in AssigneeDeadline.objects.all()} == {_at(TOKYO, 2027, 1, 20, 9, 0)}


@pytest.mark.django_db
def test_dialog_starts_on_the_system_zone_unless_one_is_saved(client, pm_user, zoned):
    _project, task, lead, ari, bo = zoned
    set_member_dates(
        task=task, member=ari, lead=lead, timezone="Asia/Tokyo",
        deadline=_at(TOKYO, 2027, 1, 20, 9, 0),
    )
    client.force_login(pm_user)

    content = client.get(
        reverse("tasks:status_block", kwargs={"pk": task.pk}) + "?pane=1"
    ).content.decode()

    # Ari's own dates are shown and prefilled in Tokyo time…
    assert "start: '', value: '2027-01-20T09:00'" in content
    assert "timezone: 'Asia/Tokyo'" in content
    assert "<span data-member-timezone>Tokyo (UTC+9)</span>" in content
    # …Bo has none, so the dialog starts on the Lead's system timezone.
    assert "timezone: null" in content
    assert "Same as task (America/Denver)" in content
    assert "is due <span class=\"font-semibold text-brand-navy\">Jan 15, 2027 5:00 PM MST</span>" in content


@pytest.mark.django_db
def test_completion_table_names_each_members_timezone(client, pm_user, zoned):
    _project, task, lead, ari, bo = zoned
    set_member_dates(
        task=task, member=ari, lead=lead, timezone="Asia/Tokyo",
        deadline=_at(TOKYO, 2027, 1, 20, 9, 0),
    )
    client.force_login(pm_user)

    content = client.get(
        reverse("tasks:status_block", kwargs={"pk": task.pk}) + "?pane=1"
    ).content.decode()

    # Only what differs from the task: Ari's own deadline and zone, on one
    # line, the zone shortened; Bo follows the task, so nothing is shown.
    assert "Due Jan 20 &middot; <span data-member-timezone>Tokyo (UTC+9)</span></span>" in content
    assert content.count("data-member-dates") == 1


@pytest.mark.django_db
def test_nothing_shown_when_own_values_match_the_task(client, pm_user, zoned):
    """An own deadline at the task's moment, an own zone equal to the
    task's, an own type equal to the task's: none of it differs, so
    nothing is shown under the name."""
    from apps.tasks.services import set_member_contribution_type

    project, task, lead, ari, _bo = zoned
    project.contribution_tracking = "CREDIT"
    project.save()
    set_member_dates(
        task=task, member=ari, lead=lead, timezone="America/Denver",
        deadline=task.deadline,
    )
    set_member_contribution_type(
        task=task, member=ari, lead=lead, contribution_type=task.contribution_type,
    )
    client.force_login(pm_user)

    content = client.get(
        reverse("tasks:status_block", kwargs={"pk": task.pk}) + "?pane=1"
    ).content.decode()

    assert "data-member-dates" not in content
    assert "data-member-type-line" not in content
    # A Lead still gets the controls.
    assert f'data-set-dates="{ari.pk}"' in content
    assert ">Change dates<" in content


@pytest.mark.django_db
def test_an_own_start_shows_with_its_zone(client, pm_user, zoned):
    _project, task, lead, ari, _bo = zoned
    set_member_dates(
        task=task, member=ari, lead=lead, timezone="Asia/Tokyo",
        start_date=_at(TOKYO, 2027, 1, 10, 9, 0),
    )
    client.force_login(pm_user)

    content = client.get(
        reverse("tasks:status_block", kwargs={"pk": task.pk}) + "?pane=1"
    ).content.decode()

    assert "Starts Jan 10 &middot; <span data-member-timezone>Tokyo (UTC+9)</span>" in content


def test_short_zone_label():
    from apps.tasks.timezones import short_zone_label

    when = datetime(2027, 1, 15, tzinfo=UTC)
    assert short_zone_label("Asia/Tokyo", when) == "Tokyo (UTC+9)"
    assert short_zone_label("Asia/Kolkata", when) == "Kolkata (UTC+5:30)"
    assert short_zone_label("America/Denver", when) == "Denver (UTC\u22127)"
    assert short_zone_label("America/Argentina/Buenos_Aires", when) == "Buenos Aires (UTC\u22123)"
    assert short_zone_label("UTC", when) == "UTC"


# ---------------------------------------------------------------------------
# Showing dates
# ---------------------------------------------------------------------------


def test_format_when_names_the_zone():
    when = datetime(2027, 1, 16, 0, 0, tzinfo=UTC)
    assert format_when(when, "America/Denver") == "Jan 15, 2027 5:00 PM MST"
    assert format_when(when, "Asia/Tokyo") == "Jan 16, 2027 9:00 AM JST"


def test_in_zone_filter_formats_in_the_given_zone():
    when = datetime(2027, 1, 16, 0, 0, tzinfo=UTC)
    out = Template(
        '{% load task_tags %}{{ when|in_zone:zone|date:"M j, Y g:i A T" }}'
    ).render(Context({"when": when, "zone": "America/Denver"}))
    assert out == "Jan 15, 2027 5:00 PM MST"
    # An empty or unknown zone falls back to the server's (UTC in tests).
    out = Template(
        '{% load task_tags %}{{ when|in_zone:zone|date:"M j g:i A" }}'
    ).render(Context({"when": when, "zone": "Nowhere/Else"}))
    assert out == "Jan 16 12:00 AM"


@pytest.mark.django_db
def test_task_card_shows_the_times_and_the_timezone(client, pm_user, zoned):
    project, task, *_ = zoned
    task.start_date = _at(DENVER, 2027, 1, 11, 9, 0)
    task.save()
    client.force_login(pm_user)

    content = client.get(
        reverse("tasks:detail", kwargs={"project_pk": project.pk, "pk": task.pk})
    ).content.decode()

    assert "Jan 11, 2027 9:00 AM – Jan 15, 2027 5:00 PM MST" in content
    assert (
        "data-task-timezone>(UTC\u221207:00) Denver, United States of America</dd>"
    ) in content


def test_task_when_formats():
    from apps.tasks.templatetags.task_tags import task_when

    task = Task(timezone="America/Denver")
    assert task_when(task) == ""
    task.deadline = _at(DENVER, 2027, 1, 15, 17, 0)
    assert task_when(task) == "Due Jan 15, 2027 5:00 PM MST"
    task.start_date = _at(DENVER, 2027, 1, 15, 9, 0)
    assert task_when(task) == "Jan 15, 2027 9:00 AM – 5:00 PM MST"
    # Across the change to daylight time, each date names its own.
    task.start_date = _at(DENVER, 2027, 3, 10, 9, 0)
    task.deadline = _at(DENVER, 2027, 3, 19, 17, 0)
    assert task_when(task) == "Mar 10, 2027 9:00 AM MST – Mar 19, 2027 5:00 PM MDT"
    task.deadline = None
    assert task_when(task) == "Starts Mar 10, 2027 9:00 AM MST"


@pytest.mark.django_db
def test_calendar_places_a_task_on_its_own_day(pm_user, zoned):
    _project, task, *_ = zoned
    task.deadline = _at(DENVER, 2027, 1, 15, 23, 0)  # Jan 16 06:00 UTC

    assert task_calendar_date(task) == date(2027, 1, 15)
    assert task_date_label(task, today=date(2027, 1, 1))["short"] == "Due Jan 15"


@pytest.mark.django_db
def test_my_deadline_carries_the_zone_that_governs_it(pm_user, other_user, zoned):
    _project, task, lead, ari, bo = zoned
    set_member_dates(
        task=task, member=bo, lead=lead, timezone="Asia/Tokyo",
        deadline=_at(TOKYO, 2027, 1, 20, 9, 0),
    )

    mine = Task.objects.with_my_deadline(other_user).get(pk=task.pk)
    assert mine.my_deadline == _at(TOKYO, 2027, 1, 20, 9, 0)
    assert mine.my_deadline_tz == "Asia/Tokyo"
    theirs = Task.objects.with_my_deadline(ari.users.first()).get(pk=task.pk)
    assert theirs.my_deadline_tz == "America/Denver"


@pytest.mark.django_db
def test_assignment_email_states_the_deadline_in_the_tasks_zone(pm_user, zoned):
    from django.template.loader import render_to_string

    _project, task, *_ = zoned
    body = render_to_string(
        "emails/task_assigned/body.txt",
        {"entries": [{"task": task, "url": ""}], "user": {"email": "a@test.com"}},
    )
    assert "due Jan 15, 2027 5:00 PM MST" in body


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_import_reads_deadlines_in_the_rows_or_the_chosen_zone(pm_user, other_user):
    project, _task, _lead, _ = _setup(pm_user, other_user)
    rows = [
        {"name": "Row zone", "deadline": "2027-01-15 17:00", "timezone": "Asia/Tokyo"},
        {"name": "Form zone", "deadline": "2027-01-15"},
        {"name": "Bad zone", "deadline": "2027-01-15", "timezone": "Nowhere"},
    ]

    created, errors = import_rows(project, rows, user=pm_user, timezone="America/Denver")

    assert created == 2
    assert errors == ["row 4: unknown timezone 'Nowhere'"]
    row_zone = Task.objects.get(name="Row zone")
    assert (row_zone.timezone, row_zone.deadline) == ("Asia/Tokyo", _at(TOKYO, 2027, 1, 15, 17, 0))
    form_zone = Task.objects.get(name="Form zone")
    # A bare date is due at 5:00 PM, as on the task form.
    assert (form_zone.timezone, form_zone.deadline) == (
        "America/Denver", _at(DENVER, 2027, 1, 15, 17, 0)
    )


@pytest.mark.django_db
def test_zone_list_finds_zones_by_city_or_country(client, pm_user):
    client.force_login(pm_user)

    response = client.get(reverse("tasks:timezones"))

    assert response.status_code == 200
    assert "max-age=3600" in response["Cache-Control"]
    data = response.json()
    assert data["aliases"]["Asia/Calcutta"] == "Asia/Kolkata"
    by_zone = {o["value"]: o for o in data["options"]}
    edmonton = by_zone["America/Edmonton"]
    assert edmonton["label"] == "Edmonton, Canada"
    assert edmonton["display"].endswith(") Edmonton, Canada")
    for word in ("Edmonton", "Canada", "CA", "America/Edmonton"):
        assert word in edmonton["search"]
    # Countries are searchable by their other names too.
    assert "UK" in by_zone["Europe/London"]["search"].split()
    assert "USA" in by_zone["America/New_York"]["search"].split()
    # Ordered by offset, west to east.
    offsets = [o["offset"] for o in data["options"]]
    assert offsets.index(by_zone["America/Denver"]["offset"]) < offsets.index(
        by_zone["Asia/Tokyo"]["offset"]
    )


def test_canonical_maps_old_names():
    assert canonical("Asia/Calcutta") == "Asia/Kolkata"
    assert canonical("Europe/Kiev") == "Europe/Kyiv"
    assert canonical("America/Denver") == "America/Denver"
    assert canonical("Etc/Unknown") is None
    assert canonical("") is None
