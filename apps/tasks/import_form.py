"""Form for bulk CSV task import."""

from __future__ import annotations

from django import forms

from apps.tasks.timezones import TimezoneField


class TaskImportForm(forms.Form):
    csv_file = forms.FileField(
        label="CSV file",
        widget=forms.ClearableFileInput(
            attrs={
                "class": "file-input file-input-bordered w-full",
                "accept": ".csv,text/csv",
            }
        ),
        help_text=(
            "Columns: name (required), description, deadline (YYYY-MM-DD), "
            "contribution_type, column, assignee_emails (comma-separated), priority, "
            "timezone."
        ),
    )
    # The zone for rows without a ``timezone`` column of their own. The
    # picker starts on the Lead's system timezone; left empty, the server's.
    timezone = TimezoneField(
        label="Timezone",
        help_text="Deadlines are read in this timezone, unless a row names its own.",
    )
