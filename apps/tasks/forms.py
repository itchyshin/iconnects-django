"""Forms for tasks, milestones, kanban columns, and task logs."""

from __future__ import annotations

import json

from django import forms
from django.db.models import Min
from django.utils.html import format_html

from apps.core.enums import (
    CompletedAs,
    ContributionType,
    TaskStatus,
    credit_role_choices,
)
from apps.projects.models import ProjectMember
from apps.tasks.models import KanbanBoard, Milestone, Task, TaskLog
from apps.tasks.timezones import (
    DEFAULT_DUE_TIME,
    DEFAULT_START_TIME,
    DateTimeSplitWidget,
    TimezoneField,
    ZonedDateTimeField,
    canonical,
    carried_own_date,
    default_zone_name,
    format_when,
    get_zone,
    picker_value,
)


_daisy_input = "input input-bordered w-full"
_daisy_textarea = "textarea textarea-bordered w-full"
_daisy_select = "select select-bordered w-full"


def member_label(member: ProjectMember) -> str:
    """Render a project member as ``"Given Name Last Name (email)"``.

    Falls back to the member's name (or the user's ``name``/email) when the
    structured given/family names are not populated.
    """

    user = member.users.order_by("given_name", "family_name").first()
    if user is None:
        return member.name or f"Member #{member.pk}"
    first = (user.given_name or "").strip()
    last = (user.family_name or "").strip()
    display = f"{first} {last}".strip()
    if not display:
        display = (user.name or user.email).strip()
    return f"{display} ({user.email})"


class MemberMultiSelect(forms.SelectMultiple):
    """A DaisyUI/Alpine.js multi-select dropdown of project members.

    Renders a collapsible dropdown of checkboxes (one per member) plus an
    "All" option that selects every member — leads included, since leads
    are project members carrying the project-manager privilege. Each
    checkbox submits under the field name, so server-side parsing stays
    identical to a native ``<select multiple>``.
    """

    def render(self, name, value, attrs=None, renderer=None):
        selected = {str(v) for v in (value or [])}
        all_ids = []
        members_data = []
        for option_value, option_label in self.choices:
            val = str(option_value)
            all_ids.append(val)
            members_data.append({"id": val, "label": str(option_label)})

        x_data = json.dumps({
            "ids": sorted(selected),
            "allIds": all_ids,
            "search": "",
            "members": members_data,
        })

        # Built on the native <details>/<summary> disclosure widget so the
        # toggle works in every browser (notably Safari, where click handlers
        # on <div>/<button> proxies are unreliable) — and even if Alpine fails
        # to load. Alpine only manages the checkbox state + summary label; the
        # checkboxes are real form inputs, so submission works regardless.
        # The dropdown opens upward (bottom-full) for better readability in modals.
        return format_html(
            '<details x-data="{}" class="dropdown w-full">'
            '<summary class="select select-bordered w-full flex items-center justify-between '
            'cursor-pointer text-left font-normal">'
            '<span x-text="ids.length ? (ids.length === allIds.length ? '
            "'All members' : ids.length + ' selected') : 'Select members'\">Select members</span>"
            "</summary>"
            '<div class="dropdown-content absolute left-0 bottom-full z-30 bg-base-100 rounded-box shadow '
            'border border-base-300 w-full mb-1 p-2 max-h-64 overflow-y-auto">'
            '<input type="text" x-model="search" placeholder="Search members…" '
            '@click.stop '
            'class="input input-sm input-bordered w-full mb-2">'
            '<label class="label cursor-pointer justify-start gap-2 py-1" x-show="!search">'
            '<input type="checkbox" class="checkbox checkbox-sm" '
            ':checked="allIds.length && ids.length === allIds.length" '
            '@change="ids = $event.target.checked ? [...allIds] : []">'
            '<span class="label-text font-semibold">All</span>'
            "</label>"
            '<div class="divider my-0" x-show="!search"></div>'
            '<template x-for="member in members.filter('
            "m => !search || m.label.toLowerCase().includes(search.toLowerCase())"
            ')" :key="member.id">'
            '<label class="label cursor-pointer justify-start gap-2 py-1">'
            '<input type="checkbox" name="{}" :value="member.id" x-model="ids" '
            'class="checkbox checkbox-sm">'
            '<span class="label-text" x-text="member.label"></span>'
            "</label>"
            "</template>"
            "</div>"
            "</details>",
            x_data,
            name,
        )


def _when(value, zone=None) -> str:
    """``value`` in ``zone`` (the server's when None), with the zone's
    abbreviation."""
    return format_when(value, zone)


def start_after_deadline_problems(rows) -> list[str]:
    """One sentence per member whose own start would fall after their deadline.

    The one rule for a member's dates: when they have a start date of their
    own, the deadline they end up with (their own, else the task's) must be
    on or after it. A member who follows the task's start is not checked —
    their own deadline may be earlier than the task's start or due date, to
    suit collaborators in other timezones. Callers pass only members with an
    own start.

    ``rows`` are ``(member, start, deadline)`` — the dates each member would
    end up with, their own where they have one, else the task's — or
    ``(member, start, deadline, start zone, deadline zone)``, naming the
    zone each date is shown in. Dates are compared as instants, so a start
    and deadline in different zones compare correctly. A start equal to the
    deadline is fine, and a missing date on either side has nothing to
    compare. A start must never come after the deadline, for the task or
    for any member (see ``TaskForm.clean`` and ``AssigneeDatesForm.clean``).
    """
    problems = []
    for member, start, deadline, *zones in rows:
        if start is None or deadline is None or start <= deadline:
            continue
        start_zone, deadline_zone = zones if zones else (None, None)
        problems.append(
            f"{member.display_name} would start {_when(start, start_zone)}, "
            f"after their deadline ({_when(deadline, deadline_zone)})."
        )
    return problems


_DATE_INPUT_FORMATS = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]


class TaskForm(forms.ModelForm):
    """Create / update a Task.

    The ``project`` kwarg scopes the choices for ``container``,
    ``assigned_members``, and ``reviewers`` to the active project. The
    ``container`` field is optional at the form level so inline creation
    from a column can pre-fill it without surfacing the field in the UI.
    """

    # Both are read and shown in the task's timezone (``timezone`` below),
    # not the server's: see ``_init_timezone``.
    #
    # Each is a day picker and a time picker (``DateTimeSplitWidget``); the
    # time starts on 9:00 AM for the start date and 5:00 PM for the due
    # date, so picking only a day gives those times.
    deadline = ZonedDateTimeField(
        required=False,
        input_formats=_DATE_INPUT_FORMATS,
        widget=DateTimeSplitWidget(
            DEFAULT_DUE_TIME, attrs={"class": f"{_daisy_input} px-3"}, time_label="Due time"
        ),
    )
    start_date = ZonedDateTimeField(
        required=False,
        input_formats=_DATE_INPUT_FORMATS,
        widget=DateTimeSplitWidget(
            DEFAULT_START_TIME, attrs={"class": f"{_daisy_input} px-3"}, time_label="Start time"
        ),
    )
    # The IANA zone governing the start and due dates. The picker starts on
    # the Lead's system timezone for a new task and on the saved zone when
    # editing (``_components/timezone_select.html``); left empty (no
    # script), a new task takes the server's zone and an edit keeps its own.
    timezone = TimezoneField(label="Timezone")
    # Asked only when editing a task some assignees have their own start
    # date / deadline on, and only acted on when the task's matching date
    # changes: what happens to those members' own dates. See
    # ``_init_individual_dates``.
    individual_start_dates = forms.ChoiceField(
        required=False,
        choices=[
            ("shift", "Shift them by the same amount"),
            ("keep", "Keep their dates as they are"),
            ("clear", "Remove them, so they follow the task's start date"),
        ],
        widget=forms.RadioSelect,
    )
    individual_deadlines = forms.ChoiceField(
        required=False,
        choices=[
            ("shift", "Shift them by the same amount"),
            ("keep", "Keep their dates as they are"),
            ("clear", "Remove them, so they follow the task's due date"),
        ],
        widget=forms.RadioSelect,
    )

    # Task date field -> the choice field asking about members' own dates.
    INDIVIDUAL_DATE_CHOICES = {
        "start_date": "individual_start_dates",
        "deadline": "individual_deadlines",
    }

    class Meta:
        model = Task
        fields = [
            "name",
            "description",
            "container",
            "assigned_members",
            "requires_review",
            "reviewers",
            "start_date",
            "deadline",
            "timezone",
            "contribution_type",
            "is_priority",
        ]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": _daisy_input,
                "maxlength": "255",
                "@input": "nameCharCount = $event.target.value.length",
            }),
            "description": forms.Textarea(attrs={
                "class": _daisy_textarea,
                "rows": 4,
                "maxlength": "5000",
                "@input": "descCharCount = $event.target.value.length",
            }),
            "container": forms.Select(attrs={"class": _daisy_select}),
            "assigned_members": MemberMultiSelect(),
            "reviewers": MemberMultiSelect(),
            # Drawn as a switch in the Review panel (task_form.html).
            "requires_review": forms.CheckboxInput(
                attrs={"class": "toggle toggle-sm toggle-primary mt-0.5", "x-model": "requiresReview"}
            ),
            "contribution_type": forms.Select(attrs={"class": _daisy_select}),
            # Visually hidden: task_form.html draws it as a flag toggle button.
            "is_priority": forms.CheckboxInput(attrs={"class": "sr-only"}),
        }

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.fields["name"].label = "Task name"
        self.fields["assigned_members"].label = "Assignees"
        self.fields["reviewers"].label = "Reviewers"
        self.fields["requires_review"].label = "Require review"
        self.fields["requires_review"].help_text = (
            "Each assignee's submission has to be signed off by a reviewer "
            "before the task counts as done."
        )
        self.fields["deadline"].label = "Due date"
        self.fields["is_priority"].label = "Mark as high priority"
        self.fields["container"].label = "Kanban column"
        self.fields["container"].required = True
        # Prompt text shown for the unselected (blank) choice.
        self.fields["container"].empty_label = "Select Kanban column"
        # Show only the container's name in the dropdown (e.g. "In Progress"),
        # not the model's default "{name} (project {id})" representation.
        self.fields["container"].label_from_instance = lambda obj: obj.name
        self._init_task_type(project)
        self._init_timezone()
        self._init_individual_dates()
        if project is not None:
            self.fields["container"].queryset = KanbanBoard.objects.filter(project=project)
            members = (
                ProjectMember.objects.filter(project=project, deleted=False)
                .annotate(sort_name=Min("users__given_name"))
                .order_by("sort_name")
            )
            # Render members as "Given Name Last Name (email)", sorted by given name.
            for field_name in ("assigned_members", "reviewers"):
                self.fields[field_name].queryset = members
                self.fields[field_name].label_from_instance = member_label

    def _init_task_type(self, project):
        """Offer the CRediT roles as the task's type — on CRediT projects only.

        The type is what a contribution is recorded under when the task
        closes, so it is meaningless anywhere else: on a project tracking
        contributions any other way the field is dropped from the form
        entirely and the task keeps whatever it already has.
        """
        if project is None or not project.tracks_credit:
            self.fields.pop("contribution_type", None)
            return
        field = self.fields["contribution_type"]
        field.label = "Task type"
        field.help_text = (
            "The CRediT role this task's contributors are credited with "
            "when their part is verified. A Lead can give an assignee a "
            "different role of their own from the task's completion table."
        )
        current = self.instance.contribution_type if self.instance.pk else None
        choices = credit_role_choices(current)
        # "Other" is ours rather than CRediT's, but a task that fits none of
        # the 14 roles needs somewhere to go, so it is always offered — last,
        # where the app's ordering puts the catch-all.
        if ContributionType.OTHER not in {value for value, _label in choices}:
            choices.append((ContributionType.OTHER, ContributionType.OTHER.label))
        field.choices = [("", "No task type")] + choices
        field.required = False
        # A new task still opens on "No task type". The field inherits the
        # model default, "Other", which is now in the list and would
        # otherwise come up preselected; both save as "Other".
        if not self.instance.pk and "contribution_type" not in self.initial:
            field.initial = ""

    def _init_timezone(self):
        """Read and show the date fields in the task's zone.

        ``original_zone_name`` is the zone the task had before this edit
        (None for a new task). The zone the dates are read in is the one
        submitted with them, so a Lead who changes the zone keeps the clock
        times they typed, now in the new zone; one left empty or unknown
        falls back to the task's zone, or for a new task the server's.
        """
        self.original_zone_name = self.instance.timezone if self.instance.pk else None
        fallback = self.original_zone_name or default_zone_name()
        submitted = (
            canonical(self.data.get(self.add_prefix("timezone"))) if self.is_bound else None
        )
        self.zone_name = submitted or fallback
        zone = get_zone(self.zone_name)
        for name in ("start_date", "deadline"):
            self.fields[name].zone = zone
        field = self.fields["timezone"]
        field.help_text = "The start and due dates are in this timezone."
        if not self.instance.pk and "timezone" not in self.initial:
            # Empty: the picker fills in the Lead's system timezone.
            self.initial["timezone"] = ""

    def clean_timezone(self):
        """An empty zone keeps the task's, or for a new task takes the
        server's."""
        return self.cleaned_data.get("timezone") or self.original_zone_name or default_zone_name()

    @property
    def new_zone_name(self) -> str:
        cleaned = getattr(self, "cleaned_data", {}) or {}
        return cleaned.get("timezone") or self.zone_name

    def zone_changed(self) -> bool:
        """Whether this edit moves the task to another timezone."""
        return bool(
            self.original_zone_name
            and canonical(self.new_zone_name) != canonical(self.original_zone_name)
        )

    def _init_individual_dates(self):
        """Offer each individual-dates choice when editing a task some members
        have that kind of own date on; drop the field otherwise.

        ``individual_rows[field]`` lists ``(member, own date)`` for the panel.
        Shifting is preselected where it can apply (the task has that date
        now, so there is an amount to shift by); otherwise keeping them is.
        """
        entries = (
            list(
                self.instance.assignee_deadlines.select_related("member")
                .prefetch_related("member__users")
                .order_by("id")
            )
            if self.instance.pk
            else []
        )
        self.individual_entries = entries
        self.original_dates = {}
        self.individual_rows = {}
        for date_field, choice_field in self.INDIVIDUAL_DATE_CHOICES.items():
            original = getattr(self.instance, date_field) if self.instance.pk else None
            self.original_dates[date_field] = original
            # (member, own date, the zone it is shown in, its clock time
            # there as the date picker writes it — for the shift preview).
            rows = sorted(
                (
                    (
                        e.member,
                        getattr(e, date_field),
                        e.timezone or self.original_zone_name,
                        picker_value(
                            getattr(e, date_field),
                            e.timezone or self.original_zone_name,
                        ),
                    )
                    for e in entries
                    if getattr(e, date_field) is not None
                ),
                key=lambda row: row[1],
            )
            self.individual_rows[date_field] = rows
            if not rows:
                self.fields.pop(choice_field, None)
                continue
            field = self.fields[choice_field]
            field.label = (
                "Individual start dates" if date_field == "start_date" else "Individual deadlines"
            )
            if choice_field not in self.initial:
                field.initial = "shift" if original else "keep"

    # Read by the template and the view.
    @property
    def original_start_date(self):
        return self.original_dates.get("start_date")

    @property
    def original_deadline(self):
        return self.original_dates.get("deadline")

    @property
    def original_start_input(self):
        return picker_value(self.original_start_date, self.original_zone_name)

    @property
    def original_deadline_input(self):
        return picker_value(self.original_deadline, self.original_zone_name)

    @property
    def individual_start_rows(self):
        return self.individual_rows.get("start_date", [])

    @property
    def individual_deadline_rows(self):
        return self.individual_rows.get("deadline", [])

    @staticmethod
    def _to_minute(value):
        return value.replace(second=0, microsecond=0) if value else None

    def _resulting_own_date(self, entry, date_field: str, new_value):
        """The own ``date_field`` ``entry``'s member would have after this
        edit: shifted, kept or removed as the Lead chose, when the task's
        date moved; as it is otherwise. A member whose dates follow the
        task's zone keeps their clock times when the zone changes. See
        ``timezones.carried_own_date``, which the save applies too."""
        choice_field = self.INDIVIDUAL_DATE_CHOICES[date_field]
        moved = choice_field in self.fields and self.date_changed(date_field)
        return carried_own_date(
            getattr(entry, date_field),
            own_zone=entry.timezone,
            old_task_zone=self.original_zone_name or self.new_zone_name,
            new_task_zone=self.new_zone_name,
            old_value=self.original_dates[date_field],
            new_value=new_value,
            choice=(self.cleaned_data.get(choice_field) or "keep") if moved else "keep",
            moved=moved,
        )

    def date_changed(self, date_field: str) -> bool:
        """Whether this edit moves, sets or clears the task's ``date_field``.

        Compared to the minute, the picker's precision, so a date stored
        with seconds is not "changed" by a re-save.
        """
        if not hasattr(self, "cleaned_data") or date_field not in self.cleaned_data:
            return False
        return self._to_minute(self.cleaned_data[date_field]) != self._to_minute(
            self.original_dates.get(date_field)
        )

    def changed_individual_dates(self):
        """``(date field, choice)`` for each task date this edit moved that
        some members have their own of — what the view carries over."""
        return [
            (date_field, self.cleaned_data.get(choice_field) or "keep")
            for date_field, choice_field in self.INDIVIDUAL_DATE_CHOICES.items()
            if choice_field in self.fields and self.date_changed(date_field)
        ]

    def _check_member_date_order(self, cleaned) -> None:
        """No member with a start of their own may end up starting after
        their deadline.

        Checked when this edit moves the task's start or due date (or its
        zone), on the dates each member still on the task would end up with:
        their own, shifted, kept or removed as the Lead chose, else the
        task's new ones. Only members left with an own start are checked
        (``start_after_deadline_problems``): one following the task's start
        may have an own deadline before it. The error goes on the panels the
        Lead answered (or, where no panel applies — say the task's due date
        moved before a member's own start — on the task date that moved), so
        it shows where the choice is made.
        """
        changed = [f for f in ("start_date", "deadline") if self.date_changed(f)]
        watched = [
            "start_date", "deadline", "timezone", *self.INDIVIDUAL_DATE_CHOICES.values()
        ]
        if (
            not (changed or self.zone_changed())
            or not self.individual_entries
            or any(name in self.errors for name in watched)
        ):
            return
        start = cleaned.get("start_date")
        deadline = cleaned.get("deadline")
        assigned = cleaned.get("assigned_members")
        assigned_ids = None if assigned is None else {m.pk for m in assigned}
        rows = []
        for entry in self.individual_entries:
            if assigned_ids is not None and entry.member_id not in assigned_ids:
                continue  # taken off the task: their own dates go with them
            own_start = self._resulting_own_date(entry, "start_date", start)
            if own_start is None:
                continue  # follows the task's start: nothing to check
            own_deadline = self._resulting_own_date(entry, "deadline", deadline)
            own_zone = entry.timezone or self.new_zone_name
            # Both shown in the zone the member's own dates are in.
            rows.append((entry.member, own_start, own_deadline or deadline, own_zone, own_zone))
        problems = start_after_deadline_problems(rows)
        if not problems:
            return
        message = " ".join(problems) + (
            " A start date must be on or before the deadline: choose differently "
            "for their own dates, or change the task's dates."
        )
        targets = [
            self.INDIVIDUAL_DATE_CHOICES[f]
            for f in changed
            if self.INDIVIDUAL_DATE_CHOICES[f] in self.fields
        ] or changed or ["timezone"]
        for target in targets:
            self.add_error(target, message)

    def clean_contribution_type(self):
        """An unpicked task type falls back to the model's own default."""
        return self.cleaned_data.get("contribution_type") or ContributionType.OTHER

    def clean(self):
        """A task cannot be due before it starts, and review needs a reviewer.

        The date rule matches ``MilestoneForm`` below; equal dates are fine,
        and either date on its own is unconstrained.

        Requiring review with nobody named to give it would leave a task
        nobody could finish, so the two are kept together: ticking the box
        asks for at least one reviewer, and leaving it unticked clears any
        who were named before.
        """
        cleaned = super().clean()
        start = cleaned.get("start_date")
        deadline = cleaned.get("deadline")
        if start and deadline and start > deadline:
            self.add_error(
                "start_date", "Start date must be before or equal to the due date."
            )

        # Moving the start or due date of a task some members have their own
        # of asks the Lead what happens to those, every time.
        names = {"start_date": "start date", "deadline": "deadline"}
        task_names = {"start_date": "start date", "deadline": "due date"}
        for date_field, choice_field in self.INDIVIDUAL_DATE_CHOICES.items():
            if choice_field not in self.fields or not self.date_changed(date_field):
                continue
            choice = cleaned.get(choice_field)
            new_value = cleaned.get(date_field)
            if not choice:
                self.add_error(
                    choice_field,
                    f"Some assignees have their own {names[date_field]}. Choose "
                    "whether to shift, keep or remove them.",
                )
            elif choice == "shift" and not (new_value and self.original_dates[date_field]):
                self.add_error(
                    choice_field,
                    "There is no change of date to shift by: the task "
                    + (
                        f"had no {task_names[date_field]}."
                        if new_value
                        else f"no longer has a {task_names[date_field]}."
                    )
                    + f" Keep or remove the individual {names[date_field]}s instead.",
                )

        self._check_member_date_order(cleaned)

        if cleaned.get("requires_review"):
            if not cleaned.get("reviewers"):
                self.add_error(
                    "reviewers",
                    "Pick at least one reviewer, or untick Require review.",
                )
        else:
            cleaned["reviewers"] = self.fields["reviewers"].queryset.none()
        return cleaned


class MilestoneForm(forms.ModelForm):
    start_date = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local", "class": _daisy_input}
        ),
    )
    end_date = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local", "class": _daisy_input}
        ),
    )

    class Meta:
        model = Milestone
        fields = ["name", "description", "start_date", "end_date"]
        widgets = {
            "name": forms.TextInput(attrs={"class": _daisy_input}),
            "description": forms.Textarea(attrs={"class": _daisy_textarea, "rows": 3}),
        }

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        if start and end and start > end:
            self.add_error("start_date", "Start date must be before or equal to end date.")
        return cleaned


class KanbanBoardForm(forms.ModelForm):
    class Meta:
        model = KanbanBoard
        fields = ["name"]
        widgets = {"name": forms.TextInput(attrs={"class": _daisy_input})}


class TaskAssignForm(forms.Form):
    """Re-assign a task's members."""

    assigned_members = forms.ModelMultipleChoiceField(
        queryset=ProjectMember.objects.none(),
        required=False,
        widget=forms.SelectMultiple(
            attrs={"class": "select select-bordered w-full h-32"}
        ),
    )

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        if project is not None:
            members = (
                ProjectMember.objects.filter(project=project, deleted=False)
                .annotate(sort_name=Min("users__given_name"))
                .order_by("sort_name")
            )
            # Render members as "First Name Last Name (email)", sorted by given name.
            self.fields["assigned_members"].queryset = members
            self.fields["assigned_members"].label_from_instance = member_label


class TaskLogForm(forms.ModelForm):
    """Record a manual TaskLog entry."""

    class Meta:
        model = TaskLog
        fields = ["status", "completed_as", "metadata"]
        widgets = {
            "status": forms.Select(
                choices=TaskStatus.choices, attrs={"class": _daisy_select}
            ),
            "completed_as": forms.Select(
                choices=CompletedAs.choices, attrs={"class": _daisy_select}
            ),
            "metadata": forms.Textarea(attrs={"class": _daisy_textarea, "rows": 2}),
        }


class TaskMilestoneForm(forms.Form):
    """Assign a task to a milestone (or clear it)."""

    milestone = forms.ModelChoiceField(
        queryset=Milestone.objects.none(),
        required=False,
        empty_label="— No milestone —",
        widget=forms.Select(attrs={"class": _daisy_select}),
    )

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        if project is not None:
            self.fields["milestone"].queryset = Milestone.objects.filter(project=project)


class AssigneeDatesForm(forms.Form):
    """A Lead's own start date and/or deadline for one assignee, or for every
    assignee not yet done (``views.TaskMemberDatesView``). The dialog starts
    both on the task's dates; a date sent back at the task's moment counts
    as not set.

    Each date may be earlier or later than the task's. For one member
    (``scope="member"``) the dialog shows their current own dates, so an
    empty date means "follow the task's" and both empty removes them. For
    everyone not yet done (``scope="pending"``) an empty date means "leave
    each member's as it is", so at least one is needed.

    The one rule: a start date of the member's own must not fall after the
    deadline they end up with (their own, else the task's), compared as
    moments and reported in the member's timezone. Their deadline may be
    earlier than the task's start or due date, and may be in the past.

    ``timezone`` governs the member's own dates: the dates are read in it,
    and shown in it afterwards. Empty means "Same as task": the task's zone.
    A zone for a member with no own dates, sent with no dates, is refused:
    it would have nothing to govern.
    For everyone not done, it becomes each member's zone; a date left empty
    keeps its moment in time and is shown in that zone from then on.
    """

    # A day and a time each (``DateTimeSplitWidget``), as on the task form.
    start_date = ZonedDateTimeField(
        required=False, input_formats=_DATE_INPUT_FORMATS,
        widget=DateTimeSplitWidget(DEFAULT_START_TIME),
    )
    deadline = ZonedDateTimeField(
        required=False, input_formats=_DATE_INPUT_FORMATS,
        widget=DateTimeSplitWidget(DEFAULT_DUE_TIME),
    )
    # The zone governing the member's own dates. Empty ("Same as task")
    # follows the task's zone. The dialog starts on the Lead's system
    # timezone unless the member already has their own dates.
    timezone = TimezoneField()
    reason = forms.CharField(required=False, max_length=500)

    def __init__(
        self, *args, task, scope="member", current=None, pending=(), member=None,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.task = task
        self.scope = scope
        self.member = member  # scope "member": the assignee, for messages
        self.current = current  # the member's AssigneeDeadline, if any
        # scope "pending": (member, their AssigneeDeadline or None) for each
        # assignee not yet done — whose dates a date left empty keeps.
        self.pending = list(pending)
        # The dates are read in the zone submitted with them, else the
        # task's.
        submitted = canonical(self.data.get(self.add_prefix("timezone")))
        self.zone_name = submitted or task.timezone
        for name in ("start_date", "deadline"):
            self.fields[name].zone = get_zone(self.zone_name)

    @staticmethod
    def _minute(value):
        return value.replace(second=0, microsecond=0) if value else None

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        # The dialog starts a date the member has no own value for (and, for
        # everyone not done, both dates) on the task's; one sent back at the
        # task's moment is not a date of their own. For one member it
        # follows the task's; for everyone not done it keeps each member's
        # own, as an empty one does.
        for name in ("start_date", "deadline"):
            value, task_value = cleaned.get(name), getattr(self.task, name)
            if (
                value is not None
                and task_value is not None
                and self._minute(value) == self._minute(task_value)
            ):
                cleaned[name] = None
        start = cleaned.get("start_date")
        deadline = cleaned.get("deadline")
        if self.scope == "pending" and start is None and deadline is None:
            raise forms.ValidationError(
                "Change the start date or the deadline: a date left at the "
                "task's keeps each member's own."
            )
        # A member's zone is kept with their own dates and governs only
        # those, so one with no own dates, given none here, has nothing for
        # it to apply to: say so rather than save nothing. (A member who
        # has own dates and is sent none has them removed, as ever.)
        if (
            self.scope == "member"
            and self.current is None
            and start is None
            and deadline is None
            and cleaned.get("timezone")
        ):
            name = self.member.display_name if self.member is not None else "this member"
            raise forms.ValidationError(
                f"Set a start date or deadline for {name} first \u2014 a "
                "contributor's timezone applies to their own dates."
            )
        # The one rule: a start of the member's own must be on or before the
        # deadline they end up with. For one member an empty date follows
        # the task's; for everyone not done it is left as each member has it
        # (their own, else the task's). Shown in the member's timezone.
        if self.scope == "pending":
            rows = []
            # Every member gets the dialog's zone, so their dates read in it.
            zone = self.zone_name
            for member, entry in self.pending:
                own_start = start or (entry and entry.start_date)
                if not own_start:
                    continue  # follows the task's start
                own_deadline = deadline or (entry and entry.deadline)
                rows.append((
                    member, own_start, own_deadline or self.task.deadline, zone, zone,
                ))
            problems = start_after_deadline_problems(rows)
            if problems:
                self.add_error(
                    "start_date" if start is not None else "deadline",
                    " ".join(problems)
                    + " A start date must be on or before the deadline.",
                )
        elif start is not None:
            effective_deadline = deadline or self.task.deadline
            if effective_deadline is not None and start > effective_deadline:
                self.add_error(
                    "start_date",
                    "The start date ("
                    + _when(start, self.zone_name)
                    + ") must be on or before the deadline ("
                    + _when(effective_deadline, self.zone_name)
                    + ").",
                )
        return cleaned

