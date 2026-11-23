"""Signal handlers for the tasks app.

What a Lead sets for one assignee — their individual dates
(``AssigneeDeadline``) and their own contribution type
(``AssigneeContributionType``) — belongs to the assignment, so it goes when
the member comes off the task — however that happens: the assign control,
the task form, a CSV import or the reverse side of the relation.
"""

from __future__ import annotations

from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from apps.tasks.models import AssigneeContributionType, AssigneeDeadline, Task

_PER_ASSIGNEE_MODELS = (AssigneeDeadline, AssigneeContributionType)


@receiver(m2m_changed, sender=Task.assigned_members.through)
def drop_deadlines_of_unassigned(sender, instance, action, reverse, pk_set, **kwargs):
    for model in _PER_ASSIGNEE_MODELS:
        if action == "post_remove" and pk_set:
            if reverse:  # instance is a ProjectMember; pk_set holds task ids
                model.objects.filter(member=instance, task_id__in=pk_set).delete()
            else:
                model.objects.filter(task=instance, member_id__in=pk_set).delete()
        elif action == "post_clear":
            if reverse:
                model.objects.filter(member=instance).delete()
            else:
                model.objects.filter(task=instance).delete()
