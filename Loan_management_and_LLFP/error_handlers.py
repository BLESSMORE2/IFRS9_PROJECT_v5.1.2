from urllib.parse import urlencode, urlsplit

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils.http import url_has_allowed_host_and_scheme


def _safe_reverse(name, fallback):
    try:
        return reverse(name)
    except NoReverseMatch:
        return fallback


def _safe_redirect_target(request):
    referer = request.META.get("HTTP_REFERER", "")
    if referer and url_has_allowed_host_and_scheme(
        url=referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        referer_parts = urlsplit(referer)
        current_parts = urlsplit(request.build_absolute_uri())
        if referer_parts.path and referer_parts.path != current_parts.path:
            target = referer_parts.path
            if referer_parts.query:
                target = f"{target}?{referer_parts.query}"
            return target

    default_target = _safe_reverse(
        "modules_home",
        reverse("login") if not request.user.is_authenticated else "/",
    )

    if request.path.startswith("/scorecard/"):
        return _safe_reverse("scorecard:scorecard_dashboard", default_target)
    if request.path.startswith("/ifrs9/") or request.path.startswith("/dashboard/"):
        return _safe_reverse("dashboard", default_target)
    if request.user.is_authenticated:
        return _safe_reverse("modules_home", reverse("login"))
    return reverse("login")


def _login_view_name_for_request(request):
    if request.path.startswith("/scorecard/") or request.path.startswith("/ifrs9/"):
        return "login_popup"
    return "login"


def _login_redirect_url(request):
    login_name = _login_view_name_for_request(request)
    login_url = _safe_reverse(login_name, reverse("login"))
    next_target = request.get_full_path()
    if next_target and next_target != login_url:
        return f"{login_url}?{urlencode({'next': next_target})}"
    return login_url


def _wants_json_response(request):
    accept = (request.headers.get("Accept") or "").lower()
    requested_with = (request.headers.get("X-Requested-With") or "").lower()
    return requested_with == "xmlhttprequest" or "application/json" in accept


def csrf_failure(request, reason="", template_name=None):
    messages.error(
        request,
        "Your form session expired or the page was submitted twice. Please reopen the page and try again.",
    )
    return redirect(_safe_redirect_target(request))


def forbidden(request, exception=None):
    if not request.user.is_authenticated:
        message = "Your session expired. Please sign in again to continue."
        login_url = _login_redirect_url(request)
        if _wants_json_response(request):
            return JsonResponse(
                {
                    "detail": message,
                    "session_expired": True,
                    "login_url": login_url,
                },
                status=403,
            )
        messages.warning(request, message)
        return redirect(login_url)

    message = str(exception).strip() if exception else ""
    return render(
        request,
        "users/forbidden.html",
        {
            "page_title": "Access denied",
            "forbidden_message": message or "You do not have permission to access this page.",
        },
        status=403,
    )
