"""Populate a fresh database with a realistic demo scenario.

    python manage.py seed_demo

Creates three users (a PM, a contributor, a verifier), one project with a
kanban board, a handful of tasks spanning several CRediT roles, and
runs them through the mark-done → close lifecycle so the contributions
pages have real data to show. Safe to re-run: the command is idempotent
and uses ``get_or_create`` throughout.

Intended for evaluation and demos, NOT production. All accounts use the
password ``demopass``.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.enums import (
    ContributionType,
    MemberPrivileges,
    ProjectStatus,
    TaskStatus,
)
from apps.projects.models import (
    ContributionTracking,
    ProjectMember,
    ProjectPrivilege,
)
from apps.projects.services import create_project_with_owner
from apps.tasks.models import KanbanBoard, Task
from apps.tasks.services import close_task, mark_own_done, verify_part

User = get_user_model()


DEMO_PROJECT_NAME = "Demo: bird song meta-analysis"
DEMO_PASSWORD = "demopass"  # noqa: S105 — demo data only

DEMO_USERS = [
    # (email, name, privilege)
    ("alice@iconnects.test", "Alice Chen (PM)", MemberPrivileges.PROJECT_MANAGER),
    ("bob@iconnects.test", "Bob Patel (contributor)", MemberPrivileges.CONTRIBUTOR),
    ("cara@iconnects.test", "Cara Nguyen (contributor)", MemberPrivileges.CONTRIBUTOR),
]

#: ``closed`` says how far down the lifecycle to take each task:
#: ``True`` marked done and closed (so it carries credit), ``False``
#: marked done but left open, ``None`` untouched.
DEMO_TASKS = [
    # (name, assignee_email, contribution_type, closed?)
    ("Screen abstracts batch 1", "bob@iconnects.test", ContributionType.DATA_CURATION, True),
    ("Screen abstracts batch 2", "cara@iconnects.test", ContributionType.DATA_CURATION, True),
    ("Extract effect sizes", "bob@iconnects.test", ContributionType.DATA_CURATION, True),
    ("Validate 20% random sample", "cara@iconnects.test", ContributionType.VALIDATION, True),
    ("Draft methods section", "alice@iconnects.test", ContributionType.WRITING_ORIGINAL_DRAFT, True),
    ("Code preregistration", "alice@iconnects.test", ContributionType.PROJECT_ADMINISTRATION, True),
    # One open task and one done-but-open, for realism on the dashboard widgets.
    ("Review R analysis script", "bob@iconnects.test", ContributionType.VALIDATION, None),
    ("Write discussion draft", "cara@iconnects.test", ContributionType.WRITING_REVIEW_EDITING, False),
]


class Command(BaseCommand):
    help = (
        "Seed a demo project + users + tasks + contributions. "
        "Idempotent; safe to run multiple times. Not for production."
    )

    def handle(self, *args, **options) -> None:
        from django.conf import settings
        if not settings.DEBUG:
            raise CommandError(
                "seed_demo is dev/demo-only and refuses to run when DEBUG=False."
            )
        users_by_email = {}
        for email, name, _priv in DEMO_USERS:
            user, created = User.objects.get_or_create(
                email=email,
                defaults={"name": name, "username": email},
            )
            if created:
                user.set_password(DEMO_PASSWORD)
                user.save()
                self.stdout.write(f"  + created user {email}")
            # Sign-in refuses an unconfirmed address, and a demo inbox
            # receives nothing. Confirming here also repairs demo users
            # seeded before confirmation was required.
            if user.email_confirmed_at is None:
                user.email_confirmed_at = timezone.now()
                user.save(update_fields=["email_confirmed_at"])
            users_by_email[email] = user

        alice = users_by_email["alice@iconnects.test"]

        # Project — owned by Alice (who is auto-added as PM).
        project = (
            ProjectPrivilege.objects.filter(
                user=alice, project__name=DEMO_PROJECT_NAME
            )
            .select_related("project")
            .first()
        )
        if project is not None:
            project = project.project
            self.stdout.write(f"  . project '{DEMO_PROJECT_NAME}' already exists")
        else:
            project = create_project_with_owner(
                user=alice,
                form_data={
                    "name": DEMO_PROJECT_NAME,
                    "description": (
                        "A worked example showing a meta-analysis of urban bird song. "
                        "All data is synthetic."
                    ),
                    "status": ProjectStatus.ACTIVE,
                    "is_public": True,
                    "contribution_tracking": ContributionTracking.CREDIT,
                },
            )
            self.stdout.write(f"  + created project '{DEMO_PROJECT_NAME}'")

        # Add Bob and Cara as contributors.
        for email, _name, priv in DEMO_USERS:
            if email == alice.email:
                continue
            user = users_by_email[email]
            # One membership each — get_or_create(project=...) alone would
            # match the owner's row and pile every demo user into it, so the
            # whole board ended up assigned to (and credited to) all three.
            member = ProjectMember.objects.filter(project=project, users=user).first()
            if member is None:
                member = ProjectMember.objects.create(project=project)
                member.users.add(user)
            ProjectPrivilege.objects.get_or_create(
                user=user, project=project, defaults={"privilege": priv}
            )

        # Kanban board column.
        column, _ = KanbanBoard.objects.get_or_create(
            project=project, name="Backlog", defaults={"container_order": 0}
        )

        # Tasks.
        alice_member = ProjectMember.objects.filter(project=project, users=alice).first()
        next_order = Task.objects.filter(container=column).count()
        for name, assignee_email, ctype, closed in DEMO_TASKS:
            if Task.objects.filter(project=project, name=name).exists():
                continue
            assignee_user = users_by_email[assignee_email]
            assignee_member = ProjectMember.objects.filter(
                project=project, users=assignee_user
            ).first()
            task = Task.objects.create(
                project=project,
                created_by=alice_member,
                container=column,
                container_task_order=next_order,
                name=name,
                contribution_type=ctype,
                status=TaskStatus.NOT_COMPLETED,
            )
            next_order += 1
            task.assigned_members.add(assignee_member)

            if closed is None:
                # Leave open — will show up under "My active tasks" for the assignee.
                continue

            log = mark_own_done(task=task, member=assignee_member)
            if closed:
                # Verifying is the ledger event: this is what writes the
                # credit. Closing only marks the task completed.
                verify_part(task_log=log, lead=alice_member)
                close_task(task=task, closed_by=alice_member)
            # closed=False means we stop after the assignee's own mark —
            # done, but still waiting on the lead to verify it and close it.

            self.stdout.write(f"  + task '{name}' ({assignee_email}, {ctype})")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write("")
        self.stdout.write("Sign in as any of these to explore:")
        for email, name, priv in DEMO_USERS:
            self.stdout.write(f"  {email}  ({name})")
        self.stdout.write(f"  password: {DEMO_PASSWORD}")
        self.stdout.write("")
