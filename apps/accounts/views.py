"""Views for the accounts app.

Server-rendered, HTMX-friendly auth flows using Django's built-in session
authentication. ORCID social auth is handled by ``django-allauth`` (see
``apps.accounts.adapters`` for the link-if-logged-in behaviour).
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import CharField, Count, Q, Value
from django.db.models.functions import Coalesce, Concat, Lower, NullIf, Trim
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.encoding import force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, FormView, ListView, UpdateView
from django_countries import countries

from apps.audit.services import AuditAction, log_action
from apps.contributions.services import (
    get_contribution_tiles,
    get_user_contributions,
)
from apps.projects.services import get_user_public_projects

from .emails import send_confirmation_email, send_password_reset_email, send_welcome_email
from .lockout import unlock_with_token
from .forms import (
    ChangePasswordForm,
    LoginForm,
    PasswordResetRequestForm,
    ProfileForm,
    ResendConfirmationForm,
    SetPasswordForm,
    SignupForm,
)
from .throttle import is_throttled, record_attempt
from .tokens import email_confirmation_token, password_reset_token

User = get_user_model()


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------
class SignupView(FormView):
    template_name = "accounts/signup.html"
    form_class = SignupForm
    success_url = reverse_lazy("accounts:confirmation_sent")

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect(reverse_lazy("core:home"))
        return super().dispatch(request, *args, **kwargs)

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        # Record first so the current attempt counts, then check.
        # ``is_throttled`` uses strict ``>`` so THRESHOLD submissions pass
        # and the (THRESHOLD+1)-th is rejected.
        record_attempt(request, AuditAction.SIGNUP_ATTEMPTED)
        if is_throttled(request, AuditAction.SIGNUP_ATTEMPTED):
            form = self.get_form()
            form.add_error(
                None,
                "Too many signup attempts from this network. "
                "Please try again in a few minutes.",
            )
            return self.render_to_response(
                self.get_context_data(form=form), status=429
            )
        return super().post(request, *args, **kwargs)

    def form_valid(self, form: SignupForm) -> HttpResponse:
        user = form.save()
        log_action(AuditAction.USER_SIGNUP, actor=user, entity=user)
        # Mandatory email confirmation (SECURITY_AUDIT.md H1): the account
        # cannot log in until this link is followed, so there is no
        # auto-login here — see ``LoginForm.clean`` and ``EmailConfirmView``.
        try:
            send_confirmation_email(user, request=self.request)
        except Exception:  # noqa: BLE001 — email failures shouldn't break signup
            pass
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------
class LoginView(FormView):
    template_name = "accounts/login.html"
    form_class = LoginForm
    success_url = reverse_lazy("core:home")

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect(self.success_url)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self) -> dict:
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def form_valid(self, form: LoginForm) -> HttpResponse:
        user = form.get_user()
        login(self.request, user)
        # Honour ``?next=`` if present and safe (same-origin only).
        next_url = self.request.GET.get("next") or self.request.POST.get("next")
        if next_url and next_url.startswith("/") and not next_url.startswith("//"):
            return HttpResponseRedirect(next_url)
        return super().form_valid(form)


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    """POST-only logout. CSRF is enforced by Django's middleware.

    Honours a same-site ``next`` (e.g. the sign-in page with an invitation
    link to return to); anything else lands on the home page.
    """
    logout(request)
    next_url = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return HttpResponseRedirect(next_url)
    return redirect("/")


# ---------------------------------------------------------------------------
# Email confirmation (mandatory — SECURITY_AUDIT.md H1)
# ---------------------------------------------------------------------------
def confirmation_sent(request: HttpRequest) -> HttpResponse:
    return render(request, "accounts/confirmation_sent.html")


def email_confirm(request: HttpRequest, uidb64: str, token: str) -> HttpResponse:
    """Consume a signup confirmation link.

    On success the account is marked confirmed and the user is logged in
    immediately — the same "prove receipt, then land in the app" flow as
    ``password_reset_confirm``/``lockout_unlock``.
    """
    user = _user_from_uidb64(uidb64)
    success = bool(
        user is not None
        and user.email_confirmed_at is None
        and email_confirmation_token.check_token(user, token)
    )
    if success:
        user.email_confirmed_at = timezone.now()
        user.save(update_fields=["email_confirmed_at", "updated_at"])
        log_action(AuditAction.EMAIL_CONFIRMED, actor=user, entity=user)
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        try:
            send_welcome_email(user, request=request)
        except Exception:  # noqa: BLE001 — email failures shouldn't block confirmation
            pass
        messages.success(request, "Your email is confirmed. Welcome to i-connects!")
    return render(request, "accounts/email_confirm.html", {"success": success})


class ResendConfirmationView(FormView):
    template_name = "accounts/resend_confirmation.html"
    form_class = ResendConfirmationForm
    success_url = reverse_lazy("accounts:confirmation_sent")

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        record_attempt(request, AuditAction.EMAIL_CONFIRMATION_RESEND_ATTEMPTED)
        if is_throttled(request, AuditAction.EMAIL_CONFIRMATION_RESEND_ATTEMPTED):
            form = self.get_form()
            form.add_error(
                None,
                "Too many requests from this network. "
                "Please try again in a few minutes.",
            )
            return self.render_to_response(
                self.get_context_data(form=form), status=429
            )
        return super().post(request, *args, **kwargs)

    def form_valid(self, form: ResendConfirmationForm) -> HttpResponse:
        email = form.cleaned_data["email"]
        # Same response either way, to avoid account enumeration.
        user = User.objects.filter(email__iexact=email).first()
        if user is not None and user.email_confirmed_at is None:
            try:
                send_confirmation_email(user, request=self.request)
            except Exception:  # noqa: BLE001 — swallow to avoid leaking info
                pass
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Forgot / reset password
# ---------------------------------------------------------------------------
class PasswordResetRequestView(FormView):
    template_name = "accounts/password_reset_request.html"
    form_class = PasswordResetRequestForm
    success_url = reverse_lazy("accounts:password_reset_sent")

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        # Record first so the current attempt counts, then check.
        # ``is_throttled`` uses strict ``>`` so THRESHOLD submissions pass
        # and the (THRESHOLD+1)-th is rejected.
        record_attempt(request, AuditAction.PASSWORD_RESET_ATTEMPTED)
        if is_throttled(request, AuditAction.PASSWORD_RESET_ATTEMPTED):
            form = self.get_form()
            form.add_error(
                None,
                "Too many password reset attempts from this network. "
                "Please try again in a few minutes.",
            )
            return self.render_to_response(
                self.get_context_data(form=form), status=429
            )
        return super().post(request, *args, **kwargs)

    def form_valid(self, form: PasswordResetRequestForm) -> HttpResponse:
        email = form.cleaned_data["email"]
        # Always return the same response regardless of whether the email
        # matches a user, to avoid account enumeration.
        user = User.objects.filter(email__iexact=email).first()
        if user is not None:
            log_action(AuditAction.PASSWORD_RESET_REQUESTED, actor=user, entity=user)
            try:
                send_password_reset_email(user, request=self.request)
            except Exception:  # noqa: BLE001 — swallow to avoid leaking info
                pass
        return super().form_valid(form)


def password_reset_sent(request: HttpRequest) -> HttpResponse:
    return render(request, "accounts/password_reset_sent.html")


def _user_from_uidb64(uidb64: str):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        return User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return None


class PasswordResetConfirmView(FormView):
    template_name = "accounts/password_reset_confirm.html"
    form_class = SetPasswordForm
    success_url = reverse_lazy("accounts:password_reset_done")

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        self.user = _user_from_uidb64(kwargs.get("uidb64", ""))
        self.token = kwargs.get("token", "")
        self.valid_link = bool(
            self.user is not None
            and password_reset_token.check_token(self.user, self.token)
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self) -> dict:
        kwargs = super().get_form_kwargs()
        # Always pass a User instance so the form's password validators run.
        kwargs["user"] = self.user or User()
        return kwargs

    def get_context_data(self, **kwargs) -> dict:
        ctx = super().get_context_data(**kwargs)
        ctx["valid_link"] = self.valid_link
        return ctx

    def form_valid(self, form: SetPasswordForm) -> HttpResponse:
        if not self.valid_link:
            return self.render_to_response(self.get_context_data(form=form))
        user = form.save()
        log_action(AuditAction.PASSWORD_RESET_COMPLETED, actor=user, entity=user)
        login(self.request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(self.request, "Your password has been reset.")
        return super().form_valid(form)

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if not self.valid_link:
            return self.render_to_response(self.get_context_data(form=self.get_form()))
        return super().post(request, *args, **kwargs)


def password_reset_done(request: HttpRequest) -> HttpResponse:
    return render(request, "accounts/password_reset_done.html")


# ---------------------------------------------------------------------------
# Lockout unlock (email-link landing)
# ---------------------------------------------------------------------------
def lockout_unlock(request: HttpRequest, token: str) -> HttpResponse:
    """Consume a lockout unlock token and re-enable the account.

    On success the user is NOT auto-logged-in (the lockout was triggered
    because someone was guessing their password; forcing a fresh sign-in
    is the safer default).
    """
    user = unlock_with_token(token)
    return render(
        request,
        "accounts/lockout_unlock.html",
        {"user": user, "success": user is not None},
    )


# ---------------------------------------------------------------------------
# Change password (authenticated)
# ---------------------------------------------------------------------------
@method_decorator(login_required, name="dispatch")
class PasswordChangeView(FormView):
    template_name = "accounts/password_change.html"
    form_class = ChangePasswordForm
    success_url = reverse_lazy("accounts:profile")

    def get_form_kwargs(self) -> dict:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form: ChangePasswordForm) -> HttpResponse:
        user = form.save()
        log_action(AuditAction.PASSWORD_CHANGED, actor=user, entity=user)
        # Keep the current session alive after the password hash changes.
        update_session_auth_hash(self.request, user)
        messages.success(self.request, "Password updated.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
class ProfileView(LoginRequiredMixin, UpdateView):
    template_name = "accounts/profile.html"
    form_class = ProfileForm
    success_url = reverse_lazy("accounts:profile")

    def get_object(self, queryset=None):  # noqa: D401
        return self.request.user

    def form_valid(self, form: ProfileForm) -> HttpResponse:
        response = super().form_valid(form)
        log_action(
            AuditAction.PROFILE_UPDATED,
            actor=self.request.user,
            entity=self.request.user,
        )
        messages.success(self.request, "Profile updated.")
        return response


# ---------------------------------------------------------------------------
# Public profile (read-only view of another researcher)
# ---------------------------------------------------------------------------
class PublicProfileView(LoginRequiredMixin, DetailView):
    """Read-only researcher profile at ``/people/<pk>/``.

    Login-gated so contribution data isn't scraped anonymously. A per-user
    ``is_public_profile`` toggle is v2.
    """

    model = User
    pk_url_kwarg = "pk"
    context_object_name = "profile_user"
    template_name = "accounts/public_profile.html"

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        profile_user = self.object
        tiles = get_contribution_tiles(profile_user)
        context["contribution_tiles"] = tiles
        context["contribution_total"] = sum(count for _label, count in tiles)
        context["recent_contributions"] = get_user_contributions(profile_user)[:15]
        context["public_projects"] = get_user_public_projects(profile_user)
        context["is_self"] = (
            self.request.user.is_authenticated
            and self.request.user.pk == profile_user.pk
        )
        return context


# ---------------------------------------------------------------------------
# Discover people — the researcher directory
#
# Everyone on the platform, in one searchable list. Login-gated, matching
# the profile it links to: the directory exists so researchers here can
# find each other, not so names and affiliations can be scraped anonymously.
# ---------------------------------------------------------------------------

# A person's name as it is both shown and sorted: their full name, falling
# back to the account name they signed up with, and finally to their email —
# the same precedence ``contributions.services.user_display_name`` uses, so a
# name never sorts under one string while reading as another. ``given_name``
# and ``family_name`` are blank-not-null, so an empty pair concatenates to a
# blank string rather than to NULL, and ``NullIf`` is what moves it along to
# the next fallback.
_FULL_NAME = Trim(Concat("given_name", Value(" "), "family_name"))
_SORT_NAME = Lower(
    Coalesce(
        NullIf(_FULL_NAME, Value("")),
        NullIf("name", Value("")),
        "email",
        # ``email`` is an EmailField and the rest are CharFields, which
        # Coalesce won't reconcile on its own.
        output_field=CharField(),
    )
)
# Institution is nullable *and* blankable, so both spellings of "not set"
# have to collapse to NULL for ``nulls_last`` to put them all at the end.
_SORT_INSTITUTION = Lower(
    NullIf(Trim(Coalesce("institution", Value(""))), Value(""))
)


def _build_people_summary(count, q, institution, country_name, sort_label):
    """Human-readable summary of the active filters, or None if there are none."""
    desc = "people"
    if institution:
        desc += f" at {institution}"
    if country_name:
        desc += f" in {country_name}"
    if q:
        desc += f' matching “{q}”'
    if not (q or institution or country_name or sort_label):
        return None
    summary = f"{count} result{'s' if count != 1 else ''} for {desc}"
    if sort_label:
        summary += f", sorted by {sort_label}"
    return summary


class DiscoverPeopleView(LoginRequiredMixin, ListView):
    """Directory of everyone on the platform — searchable, filterable, sortable.

    Deactivated accounts are left out: they can't be collaborated with, and
    listing them would make the directory a record of who used to be here.
    """

    model = User
    template_name = "accounts/discover_people.html"
    context_object_name = "people"
    paginate_by = 24

    # Name first: a directory is read alphabetically far more often than it
    # is read by signup date. Each entry is (ordering terms, summary label);
    # the institution sorts fall back to the name so that everyone at one
    # institution reads alphabetically rather than in signup order.
    _SORTS = {
        "name_asc": ((_SORT_NAME.asc(),), None),
        "name_desc": ((_SORT_NAME.desc(),), "name Z–A"),
        # Everyone without an institution sorts last either way, so switching
        # direction reorders the named institutions without dragging the
        # blanks to the top.
        "institution_asc": (
            (_SORT_INSTITUTION.asc(nulls_last=True), _SORT_NAME.asc()),
            "institution A–Z",
        ),
        "institution_desc": (
            (_SORT_INSTITUTION.desc(nulls_last=True), _SORT_NAME.asc()),
            "institution Z–A",
        ),
        "newest": (("-created_at",), "newest members first"),
        "oldest": (("created_at",), "oldest members first"),
    }
    _DEFAULT_SORT = "name_asc"

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["accounts/partials/people_results.html"]
        return [self.template_name]

    def _current_sort(self) -> str:
        sort = self.request.GET.get("sort", "")
        return sort if sort in self._SORTS else self._DEFAULT_SORT

    def _base_queryset(self):
        return User.objects.filter(is_active=True)

    def _apply_filters(self, qs):
        """Search + institution/country facets.

        Every field touched here lives on ``User``, so none of it fans the
        rows out — which is what keeps the public-project count below honest
        and lets the ordering stay a plain ``order_by``.
        """
        institution = self.request.GET.get("institution", "").strip()
        if institution:
            qs = qs.filter(institution__iexact=institution)

        country = self.request.GET.get("country", "").strip()
        if country:
            qs = qs.filter(country=country)

        q = self.request.GET.get("q", "").strip()
        if q:
            # Country is stored as its two-letter code, so a search for
            # "Canada" only matches once the name is resolved back to codes.
            country_codes = [
                code for code, name in countries if q.lower() in str(name).lower()
            ]
            matches = (
                Q(given_name__icontains=q)
                | Q(family_name__icontains=q)
                | Q(name__icontains=q)
                | Q(institution__icontains=q)
                | Q(department__icontains=q)
                | Q(position__icontains=q)
                | Q(city__icontains=q)
                | Q(orcid__icontains=q)
            )
            if country_codes:
                matches |= Q(country__in=country_codes)
            qs = qs.filter(matches)
        return qs

    def get_queryset(self):
        qs = self._apply_filters(self._base_queryset()).annotate(
            public_project_count=Count(
                "project_privileges",
                filter=Q(project_privileges__project__is_public=True),
                distinct=True,
            ),
        )
        order, _label = self._SORTS[self._current_sort()]
        # ``id`` breaks ties so pagination can't show the same person twice.
        return qs.order_by(*order, "id")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        current_sort = self._current_sort()
        current_q = self.request.GET.get("q", "").strip()
        current_institution = self.request.GET.get("institution", "").strip()
        current_country = self.request.GET.get("country", "").strip()

        context["is_htmx_request"] = bool(self.request.headers.get("HX-Request"))
        context["current_sort"] = current_sort
        context["current_q"] = current_q
        context["current_institution"] = current_institution
        context["current_country"] = current_country

        # Facet options come from the data, so neither dropdown ever offers a
        # value that would return an empty page.
        people = User.objects.filter(is_active=True)
        context["institutions"] = list(
            people.exclude(institution__isnull=True)
            .exclude(institution="")
            .order_by("institution")
            .values_list("institution", flat=True)
            .distinct()
        )
        country_names = {
            code: str(countries.name(code))
            for code in people.exclude(country="")
            .values_list("country", flat=True)
            .distinct()
        }
        context["countries"] = sorted(
            ((code, name) for code, name in country_names.items() if name),
            key=lambda pair: pair[1],
        )

        params = self.request.GET.copy()
        params.pop("page", None)
        context["filter_params"] = params.urlencode()

        context["total_people_count"] = context["paginator"].count
        context["results_summary"] = _build_people_summary(
            context["paginator"].count,
            current_q,
            current_institution,
            country_names.get(current_country, ""),
            self._SORTS[current_sort][1],
        )
        return context
