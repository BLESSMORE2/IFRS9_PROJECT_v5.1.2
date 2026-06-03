from django.conf import settings
from django.contrib import messages
from django.shortcuts import render
from django.shortcuts import redirect
from django.urls import reverse

from .package_runtime import (
    DEFAULT_SCORECARD_SUBSCRIPTION_MESSAGE,
    DEFAULT_SUBSCRIPTION_MESSAGE,
    get_ifrs9_package_status,
    get_scorecard_package_status,
)


WORKSPACE_POPUP_SESSION_KEY = "users_workspace_popup_mode"
WORKSPACE_POPUP_WINDOW_NAME = "nexaWorkspaceWindow"
ADMIN_POPUP_QUERY_KEY = "popup"


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        csp_directives = {
            "default-src": ["'self'"],
            "base-uri": ["'self'"],
            "form-action": ["'self'"],
            "frame-ancestors": ["'self'"],
            "object-src": ["'none'"],
            "script-src": ["'self'", "'unsafe-inline'"],
            "style-src": ["'self'", "'unsafe-inline'"],
            "img-src": ["'self'", "data:", "blob:"],
            "font-src": ["'self'", "data:"],
            "connect-src": ["'self'"],
        }

        csp_value = "; ".join(
            f"{directive} {' '.join(values)}" for directive, values in csp_directives.items()
        )

        response.headers.setdefault("Content-Security-Policy", csp_value)
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=(), payment=(), usb=()")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")

        if response.headers.get("Access-Control-Allow-Origin") == "*" and request.path.startswith(settings.STATIC_URL):
            del response.headers["Access-Control-Allow-Origin"]

        response.headers.pop("X-Powered-By", None)
        return response


class AdminWorkspacePopupMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/ifrs-admin/"):
            if request.GET.get(ADMIN_POPUP_QUERY_KEY) == "1":
                request.session[WORKSPACE_POPUP_SESSION_KEY] = True
                return self.get_response(request)

            if not request.session.get(WORKSPACE_POPUP_SESSION_KEY):
                query_params = request.GET.copy()
                query_params[ADMIN_POPUP_QUERY_KEY] = "1"
                admin_target = f"{request.path}?{query_params.urlencode()}"
                return render(
                    request,
                    "users/workspace_launcher.html",
                    {
                        "workspace_popup_window_name": WORKSPACE_POPUP_WINDOW_NAME,
                        "workspace_popup_url": admin_target,
                        "workspace_target_url": admin_target,
                        "workspace_already_authenticated": True,
                        "workspace_launcher_heading": "Launch secure admin",
                        "workspace_launcher_copy": "The IFRS administration panel will open inside the dedicated workspace window.",
                        "workspace_launcher_status": "The launcher will automatically try to open the admin workspace window as soon as this page loads.",
                        "workspace_launcher_button": "Open Admin Window",
                    },
                )

        return self.get_response(request)


class Ifrs9AvailabilityMiddleware:
    """
    Keep users on the shared launcher when packaged apps are missing or expired,
    while still allowing any healthy packaged modules to keep working.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        exempt_paths = {
            reverse("login"),
            reverse("logout"),
            reverse("modules_home"),
            reverse("modules_home_alias"),
        }
        allowed_prefixes = (
            "/login/",
            "/logout/",
            "/modules/",
            "/settings/",
            "/update-profile/",
            "/change-password/",
            "/password-change-done/",
            "/ifrs-admin/",
            "/swagger",
            "/redoc/",
        )

        if request.path.startswith(settings.STATIC_URL) or request.path.startswith(settings.MEDIA_URL):
            return self.get_response(request)

        scorecard_status = get_scorecard_package_status()
        if request.path.startswith("/scorecard/"):
            if not scorecard_status["usable"]:
                message = scorecard_status["message"] or DEFAULT_SCORECARD_SUBSCRIPTION_MESSAGE
                messages.error(request, message)
                if request.user.is_authenticated:
                    return redirect("modules_home")
                return redirect("login")
            return self.get_response(request)

        if request.path in exempt_paths or request.path.startswith(allowed_prefixes):
            return self.get_response(request)

        ifrs9_status = get_ifrs9_package_status()
        if not ifrs9_status["usable"]:
            message = ifrs9_status["message"] or DEFAULT_SUBSCRIPTION_MESSAGE
            messages.error(request, message)
            if request.user.is_authenticated:
                return redirect("modules_home")
            return redirect("login")

        return self.get_response(request)
