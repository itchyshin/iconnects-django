"""Accounts app: custom User, Blitz-compatible Session/Token, and dashboard Widgets."""

from __future__ import annotations

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django_countries.fields import CountryField

from apps.core.enums import EmailFrequency, UserRole, WidgetSize


class UserManager(BaseUserManager):
    """Manager for the custom User model that uses email as the login field."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address.")
        email = self.normalize_email(email)
        # Derive a username if one isn't provided explicitly.
        username = extra_fields.pop("username", None) or email
        user = self.model(email=email, username=username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", UserRole.ADMIN)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    """Custom user model ported from Prisma ``User``.

    Django's built-in ``password`` field replaces Prisma's ``hashedPassword``.
    Email is the login field; ``username`` is retained for admin display and
    backwards compatibility with the Blitz.js data model.
    """

    # Override AbstractUser's email to be unique + required.
    email = models.EmailField(unique=True)

    # Profile
    name = models.CharField(max_length=255, blank=True)
    given_name = models.CharField(max_length=255, blank=True)
    family_name = models.CharField(max_length=255, blank=True)
    pronouns = models.CharField(max_length=64, null=True, blank=True)
    orcid = models.CharField(max_length=64, unique=True, null=True, blank=True)
    position = models.CharField(max_length=255, null=True, blank=True)
    department = models.CharField(max_length=255, null=True, blank=True)
    institution = models.CharField(max_length=255, null=True, blank=True)
    city = models.CharField(max_length=255, null=True, blank=True)
    country = CountryField(blank=True, blank_label="(Select country)")
    language = models.CharField(max_length=16, default="en-US")
    gravatar = models.CharField(max_length=255, null=True, blank=True)
    tooltips = models.BooleanField(default=True)
    tos = models.BooleanField(null=True, blank=True)

    # Role / permissions (application-level, separate from Django's is_staff).
    role = models.CharField(
        max_length=32,
        choices=UserRole.choices,
        default=UserRole.USER,
    )

    # Email notification preferences.
    email_project_activity_frequency = models.CharField(
        max_length=16,
        choices=EmailFrequency.choices,
        default=EmailFrequency.NEVER,
    )
    email_overdue_task_frequency = models.CharField(
        max_length=16,
        choices=EmailFrequency.choices,
        default=EmailFrequency.NEVER,
    )
    # Sent the moment a Lead assigns a task, unlike the digests above. On by
    # default: for members who rarely log in it is how they hear of new work.
    email_task_assigned = models.BooleanField(default=True)
    # Sent the moment someone tags this person in a task comment (@Name).
    # On by default for the same reason: a tag is a direct request for them.
    email_comment_mention = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Account lockout (Ultra Plan §7): set when the user has exceeded the
    # failed-login threshold. While non-null, login is rejected until the
    # user consumes a ``LockoutToken`` via the email-unlock flow.
    locked_at = models.DateTimeField(null=True, blank=True)

    # Mandatory email confirmation (closes SECURITY_AUDIT.md H1): set once
    # the user follows the confirmation link mailed at signup. While null,
    # ``LoginForm`` refuses to authenticate the account regardless of
    # password correctness — see ``apps.accounts.forms.LoginForm.clean``.
    email_confirmed_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    objects = UserManager()

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return self.email

    @property
    def is_locked_out(self) -> bool:
        return self.locked_at is not None

    @property
    def is_email_confirmed(self) -> bool:
        return self.email_confirmed_at is not None

    @property
    def display_name(self) -> str:
        """How this person is named anywhere they are shown or listed.

        Their full name, falling back to the account name they signed up
        with, and finally to their email — an account always has one, so
        this never comes back blank and no row ever renders nameless.
        """
        full = f"{(self.given_name or '').strip()} {(self.family_name or '').strip()}".strip()
        return full or self.name or self.email


class Session(models.Model):
    """Blitz.js session table, ported 1:1 for data compatibility.

    Django's native session framework is preferred for new code; this model
    exists so a future data migration from Blitz can land cleanly.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    handle = models.CharField(max_length=255, unique=True)
    hashed_session_token = models.CharField(max_length=255, null=True, blank=True)
    anti_csrf_token = models.CharField(max_length=255, null=True, blank=True)
    public_data = models.TextField(null=True, blank=True)
    private_data = models.TextField(null=True, blank=True)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="sessions",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user"])]

    def __str__(self) -> str:
        return self.handle


class Token(models.Model):
    """Blitz.js token table (password reset, email verification, etc.)."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    hashed_token = models.CharField(max_length=255)
    type = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    sent_to = models.CharField(max_length=254)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="tokens")

    class Meta:
        ordering = ["-created_at"]
        unique_together = [["hashed_token", "type"]]
        indexes = [models.Index(fields=["user"])]

    def __str__(self) -> str:
        return f"{self.type} token for {self.sent_to}"


class LockoutToken(models.Model):
    """One-time unlock token mailed to a user whose account got locked.

    Per Ultra Plan §7: after 10 failed login attempts, the account is locked
    and the user must confirm an email link before they can log in again.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="lockout_tokens")
    token_hash = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user"])]

    def __str__(self) -> str:
        return f"LockoutToken for {self.user_id}"

    @property
    def is_usable(self) -> bool:
        from django.utils import timezone

        return self.used_at is None and self.expires_at > timezone.now()


class Widget(models.Model):
    """User-level dashboard widget configuration."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="widgets")
    type = models.CharField(max_length=64)  # LastProject, Notifications, OverdueTask, UpcomingTask
    show = models.BooleanField(default=True)
    position = models.IntegerField()
    size = models.CharField(
        max_length=16,
        choices=WidgetSize.choices,
        default=WidgetSize.MEDIUM,
    )

    class Meta:
        ordering = ["position"]
        unique_together = [["user", "type"]]
        indexes = [models.Index(fields=["user"])]

    def __str__(self) -> str:
        return f"{self.type} widget for {self.user_id}"
