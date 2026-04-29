from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from .package_runtime import (
    DEFAULT_SCORECARD_SUBSCRIPTION_MESSAGE,
    DEFAULT_SUBSCRIPTION_MESSAGE,
    get_ifrs9_package_status,
    get_scorecard_package_status,
)


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
