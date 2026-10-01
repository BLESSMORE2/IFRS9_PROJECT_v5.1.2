from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from .access_logs import close_user_session_log, maybe_sweep_idle_user_sessions
from .models import UserAccessLog
from .security import password_change_required
from .runtime import (
    MICROSOFT_AUTH_VERIFIED_AT_KEY,
    get_system_settings,
    microsoft_auth_is_available,
    read_session_timestamp,
)

SESSION_END_NOTICE_COOKIE = "nexa_session_end"


class RuntimeSessionControlMiddleware:
    SESSION_STARTED_AT_KEY = "users_session_started_at"
    LAST_ACTIVITY_AT_KEY = "users_last_activity_at"
    PASSIVE_PATH_PREFIXES = (
        "/static/",
        "/media/",
        "/favicon.ico",
        "/ifrs9/api/",
        "/scorecard/api/",
        "/api/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        maybe_sweep_idle_user_sessions()

        if request.user.is_authenticated:
            runtime_settings = get_system_settings()
            now = timezone.now()

            started_at = self._read_timestamp(request.session.get(self.SESSION_STARTED_AT_KEY))
            last_activity_at = self._read_timestamp(request.session.get(self.LAST_ACTIVITY_AT_KEY))

            if started_at is None:
                request.session[self.SESSION_STARTED_AT_KEY] = now.isoformat()
                started_at = now

            if last_activity_at is None:
                last_activity_at = started_at or now
                request.session[self.LAST_ACTIVITY_AT_KEY] = last_activity_at.isoformat()

            idle_timeout = runtime_settings.idle_timeout_minutes
            if idle_timeout and (now - last_activity_at).total_seconds() >= idle_timeout * 60:
                close_user_session_log(request, UserAccessLog.END_REASON_IDLE_TIMEOUT)
                logout(request)
                return self._redirect_to_login_with_next(
                    request,
                    UserAccessLog.END_REASON_IDLE_TIMEOUT,
                )

            absolute_timeout = runtime_settings.absolute_session_timeout_minutes
            if absolute_timeout and (now - started_at).total_seconds() > absolute_timeout * 60:
                close_user_session_log(request, UserAccessLog.END_REASON_ABSOLUTE_TIMEOUT)
                logout(request)
                return self._redirect_to_login_with_next(
                    request,
                    UserAccessLog.END_REASON_ABSOLUTE_TIMEOUT,
                )

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

            if self._is_interactive_activity_request(request):
                request.session[self.LAST_ACTIVITY_AT_KEY] = now.isoformat()

        return self.get_response(request)

    @staticmethod
    def _read_timestamp(value):
        return read_session_timestamp(value)

    @classmethod
    def _is_interactive_activity_request(cls, request):
        if request.path.startswith(cls.PASSIVE_PATH_PREFIXES):
            return False

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return False

        fetch_dest = (request.headers.get("Sec-Fetch-Dest") or "").lower()
        if fetch_dest in {"empty", "image", "script", "style", "font"}:
            return False

        accept_header = request.headers.get("Accept") or ""
        if "application/json" in accept_header.lower():
            return False

        return request.method in {"GET", "POST", "PUT", "PATCH", "DELETE"}

    @staticmethod
    def _redirect_to_login_with_next(request, session_end_reason=None):
        next_target = request.get_full_path()
        query_params = {}
        if next_target:
            query_params["next"] = next_target
        if session_end_reason:
            query_params["session_end"] = session_end_reason
        if query_params:
            response = redirect(f"{reverse('login')}?{urlencode(query_params)}")
        else:
            response = redirect("login")
        if session_end_reason:
            response.set_cookie(
                SESSION_END_NOTICE_COOKIE,
                session_end_reason,
                max_age=120,
                httponly=True,
                samesite="Lax",
            )
        return response

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
