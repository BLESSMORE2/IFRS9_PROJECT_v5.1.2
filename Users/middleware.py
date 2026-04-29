from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from .security import password_change_required
from .runtime import (
    MICROSOFT_AUTH_VERIFIED_AT_KEY,
    get_system_settings,
    microsoft_auth_is_available,
    read_session_timestamp,
)


class RuntimeSessionControlMiddleware:
    SESSION_STARTED_AT_KEY = "users_session_started_at"
    LAST_ACTIVITY_AT_KEY = "users_last_activity_at"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            runtime_settings = get_system_settings()
            now = timezone.now()

            started_at = self._read_timestamp(request.session.get(self.SESSION_STARTED_AT_KEY))
            last_activity_at = self._read_timestamp(request.session.get(self.LAST_ACTIVITY_AT_KEY))

            if started_at is None:
                request.session[self.SESSION_STARTED_AT_KEY] = now.isoformat()
                started_at = now

            if last_activity_at is None:
                request.session[self.LAST_ACTIVITY_AT_KEY] = now.isoformat()
                last_activity_at = now

            idle_timeout = runtime_settings.idle_timeout_minutes
            if idle_timeout and (now - last_activity_at).total_seconds() > idle_timeout * 60:
                logout(request)
                messages.warning(
                    request,
                    "You were logged out automatically after being inactive for too long.",
                )
                return self._redirect_to_login_with_next(request)

            absolute_timeout = runtime_settings.absolute_session_timeout_minutes
            if absolute_timeout and (now - started_at).total_seconds() > absolute_timeout * 60:
                logout(request)
                messages.warning(
                    request,
                    "Your session reached the maximum allowed duration and has been closed.",
                )
                return self._redirect_to_login_with_next(request)

            if self._should_force_password_change(request, runtime_settings):
                messages.warning(
                    request,
                    "You need to change your password before continuing.",
                )
                return redirect("change_password")

            if self._should_reverify_with_microsoft(request, runtime_settings, now):
                query_string = urlencode(
                    {
                        "purpose": "session_recheck",
                        "next": request.get_full_path(),
                    }
                )
                return redirect(f"{reverse('microsoft_auth_start')}?{query_string}")

            request.session[self.LAST_ACTIVITY_AT_KEY] = now.isoformat()

        return self.get_response(request)

    @staticmethod
    def _read_timestamp(value):
        return read_session_timestamp(value)

    @staticmethod
    def _redirect_to_login_with_next(request):
        next_target = request.get_full_path()
        if next_target:
            query_string = urlencode({"next": next_target})
            return redirect(f"{reverse('login')}?{query_string}")
        return redirect("login")

    @classmethod
    def _should_reverify_with_microsoft(cls, request, runtime_settings, now):
        if not microsoft_auth_is_available(runtime_settings):
            return False

        if not runtime_settings.microsoft_auth_enforce_periodically:
            return False

        exempt_paths = {
            reverse("login"),
            reverse("logout"),
            reverse("microsoft_auth_start"),
            reverse("user_settings_authenticator"),
        }
        if request.path in exempt_paths:
            return False

        verified_at = cls._read_timestamp(request.session.get(MICROSOFT_AUTH_VERIFIED_AT_KEY))
        if verified_at is None:
            return True

        recheck_days = max(int(runtime_settings.microsoft_auth_recheck_days or 0), 1)
        return (now - verified_at).total_seconds() > recheck_days * 86400

    @staticmethod
    def _should_force_password_change(request, runtime_settings):
        exempt_paths = {
            reverse("change_password"),
            reverse("password_change_done"),
            reverse("logout"),
            reverse("microsoft_auth_start"),
            reverse("user_settings_authenticator"),
        }
        if request.path in exempt_paths:
            return False

        return password_change_required(request.user, runtime_settings)
