from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Session, Token, User, Widget


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = (
        "id", "email", "username", "name", "role", "is_staff",
        "email_confirmed_at", "created_at",
    )
    search_fields = ("email", "username", "name", "orcid")
    list_filter = ("role", "is_staff", "is_superuser", "is_active")
    ordering = ("id",)

    fieldsets = BaseUserAdmin.fieldsets + (
        (
            "i-connects profile",
            {
                "fields": (
                    "name",
                    "given_name",
                    "family_name",
                    "pronouns",
                    "orcid",
                    "position",
                    "department",
                    "institution",
                    "city",
                    "country",
                    "language",
                    "gravatar",
                    "tooltips",
                    "tos",
                    "role",
                    "email_project_activity_frequency",
                    "email_overdue_task_frequency",
                    "email_task_assigned",
                    "email_comment_mention",
                    "email_confirmed_at",
                )
            },
        ),
    )


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ("id", "handle", "user", "expires_at", "created_at")
    search_fields = ("handle", "user__email")
    list_filter = ("expires_at",)


@admin.register(Token)
class TokenAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "sent_to", "user", "expires_at", "created_at")
    search_fields = ("sent_to", "user__email", "type")
    list_filter = ("type",)


@admin.register(Widget)
class WidgetAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "type", "position", "size", "show")
    search_fields = ("type", "user__email")
    list_filter = ("type", "size", "show")
