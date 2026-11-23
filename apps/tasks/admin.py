from django.contrib import admin

from .models import AssigneeContributionType, KanbanBoard, Milestone, Task, TaskLog


@admin.register(KanbanBoard)
class KanbanBoardAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "project", "container_order", "created_at")
    search_fields = ("name", "project__name")


@admin.register(Milestone)
class MilestoneAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "project", "start_date", "end_date", "created_at")
    search_fields = ("name", "description", "project__name")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "project", "container", "status", "is_priority", "deadline", "timezone", "created_at")
    search_fields = ("name", "description", "project__name")
    list_filter = ("status", "is_priority", "auto_assign_new", "anonymous")


@admin.register(TaskLog)
class TaskLogAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "assigned_to", "completed_by", "status", "completed_as", "created_at")
    search_fields = ("task__name",)
    list_filter = ("status", "completed_as", "approved")


@admin.register(AssigneeContributionType)
class AssigneeContributionTypeAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "member", "contribution_type", "set_by", "updated_at")
    search_fields = ("task__name",)
    list_filter = ("contribution_type",)
