import logging
from datetime import timedelta

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe
from django.views.generic import TemplateView

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditLog
from apps.contributions.models import ContributionRecord
from apps.core.enums import TaskStatus, UserRole
from apps.projects.models import Project
from apps.tasks.models import Task

logger = logging.getLogger(__name__)


def home(request):
    """Landing page for the Django MVP.

    Authenticated users are bounced to their project dashboard; anonymous
    visitors see the marketing/landing page.
    """
    if request.user.is_authenticated:
        return redirect("projects:dashboard")
    return render(request, "core/home.html")


@never_cache
@require_safe
def healthz(request):
    """Health probe for the Docker health check and external uptime monitors.

    Answers ``200`` only when this process is serving *and* can run a query
    against the database; ``503`` when that query fails, so a web container
    whose Postgres connection is gone is reported unhealthy rather than
    "up". Deliberately cheap and dependency-free: one round trip, no ORM,
    no template, no session. Redis is not checked here on purpose -- a
    broker outage breaks background work, not page serving, and the celery
    service's own health check (``celery inspect ping``) catches it.

    The JSON content type also keeps allauth's ``AccountMiddleware`` from
    touching the session on the way out, so a probe never writes anything.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 -- any failure here means "not healthy"
        logger.exception("healthz: database check failed")
        return JsonResponse({"status": "unhealthy", "database": "error"}, status=503)
    return JsonResponse({"status": "ok", "database": "ok"})


class TermsOfUseView(TemplateView):
    """Static Terms of Use page (content TBD)."""

    template_name = "core/terms_of_use.html"


class PrivacyPolicyView(TemplateView):
    """Static Privacy Policy page (content TBD)."""

    template_name = "core/privacy_policy.html"


class AdminConsoleView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """Platform-admin tile view at ``/admin/console/``.

    This is the custom staff surface (Ultra Plan §A3), not Django's built-in
    admin. Only ``User.role == ADMIN`` is allowed through.
    """

    template_name = "core/admin_console.html"

    def test_func(self) -> bool:
        user = self.request.user
        return user.is_authenticated and user.role == UserRole.ADMIN

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()
        week_ago = now - timedelta(days=7)

        # Users
        ctx["user_count"] = User.objects.count()
        ctx["active_user_count"] = User.objects.filter(is_active=True).count()
        ctx["locked_user_count"] = User.objects.filter(locked_at__isnull=False).count()
        ctx["new_user_count_7d"] = User.objects.filter(created_at__gte=week_ago).count()

        # Projects
        ctx["project_count"] = Project.objects.count()
        ctx["public_project_count"] = Project.objects.filter(is_public=True).count()
        # The Project model has no ``archived_at`` field in the current MVP
        # schema; surface 0 so the template can render uniformly without a
        # FieldError if the tile is ever shown.
        project_has_archived_at = any(
            f.name == "archived_at" for f in Project._meta.get_fields()
        )
        if project_has_archived_at:
            ctx["archived_project_count"] = Project.objects.filter(
                archived_at__isnull=False
            ).count()
        else:
            ctx["archived_project_count"] = 0

        # Tasks
        ctx["task_count"] = Task.objects.count()
        ctx["open_task_count"] = Task.objects.exclude(status=TaskStatus.COMPLETED).count()
        ctx["completed_task_count"] = Task.objects.filter(status=TaskStatus.COMPLETED).count()
        ctx["overdue_task_count"] = Task.objects.overdue(now).count()

        # Contributions
        ctx["contribution_count"] = ContributionRecord.objects.count()

        # Recent governance events (last 10 of each)
        base_qs = AuditLog.objects.select_related("actor")
        ctx["recent_signups"] = list(
            base_qs.filter(action=AuditAction.USER_SIGNUP)[:10]
        )
        ctx["recent_failed_logins"] = list(
            base_qs.filter(action=AuditAction.USER_LOGIN_FAILED)[:10]
        )
        ctx["recent_lockouts"] = list(
            base_qs.filter(action=AuditAction.ACCOUNT_LOCKED_OUT)[:10]
        )

        return ctx
