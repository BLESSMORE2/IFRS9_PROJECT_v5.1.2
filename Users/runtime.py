from dataclasses import dataclass

from django.conf import settings as django_settings
from django.core.cache import cache
from django.db import DatabaseError
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from Loan_management_and_LLFP.package_runtime import (
    get_ifrs9_package_status,
    get_scorecard_package_status,
)

from .models import SystemModule, SystemSetting, UserModuleAccess


DEFAULT_MODULES = [
    {
        "code": "IFRS9",
        "name": "IFRS 9",
        "route_name": "ifrs9_home",
        "icon_class": "fas fa-chart-line",
        "accent_class": "ifrs9",
        "description": "Credit risk, staging, ECL, and reporting",
        "display_order": 10,
    },
    {
        "code": "SCORECARD",
        "name": "Scorecard",
        "route_name": "scorecard:scorecard_dashboard",
        "icon_class": "fas fa-calculator",
        "accent_class": "scorecard",
        "description": "Scoring, score templates, and score operations",
        "display_order": 20,
    },
]

MODULE_CACHE_KEY = "users_active_modules_bootstrap_v1"
MICROSOFT_AUTH_VERIFIED_AT_KEY = "users_microsoft_auth_verified_at"
MICROSOFT_AUTH_VERIFIED_EMAIL_KEY = "users_microsoft_auth_verified_email"


@dataclass
class FallbackSystemSettings:
    idle_timeout_minutes: int = 15
    absolute_session_timeout_minutes: int = 480
    default_landing_rule: str = SystemSetting.LANDING_RULE_LAUNCHER
    failed_login_limit: int = 3
    lockout_duration_minutes: int = 60
    enable_self_profile_edit: bool = True
    enable_self_password_change: bool = True
    password_expiry_days: int = 90
    password_history_count: int = 5
    password_policy: str = SystemSetting.PASSWORD_POLICY_STANDARD
    enable_microsoft_authentication: bool = False
    microsoft_auth_mode: str = SystemSetting.MICROSOFT_AUTH_MODE_SIMULATED
    microsoft_auth_on_login: bool = True
    microsoft_auth_on_password_change: bool = False
    microsoft_auth_enforce_periodically: bool = False
    microsoft_auth_recheck_days: int = 30

    def get_default_landing_rule_display(self):
        if self.default_landing_rule == SystemSetting.LANDING_RULE_DIRECT:
            return "Open directly when only one module is available"
        return "Always show module launcher"

    def get_password_policy_display(self):
        labels = dict(SystemSetting.PASSWORD_POLICY_CHOICES)
        return labels.get(self.password_policy, self.password_policy)


def ensure_default_modules():
    try:
        if cache.get(MODULE_CACHE_KEY):
            return

        for module_def in DEFAULT_MODULES:
            SystemModule.objects.get_or_create(
                code=module_def["code"],
                defaults=module_def,
            )

        cache.set(MODULE_CACHE_KEY, True, 300)
    except DatabaseError:
        return


def get_system_settings():
    try:
        return SystemSetting.load()
    except DatabaseError:
        return FallbackSystemSettings()


def microsoft_auth_is_available(runtime_settings=None):
    runtime_settings = runtime_settings or get_system_settings()
    return bool(runtime_settings.enable_microsoft_authentication)


def microsoft_auth_uses_authenticator_app_mode(runtime_settings=None):
    runtime_settings = runtime_settings or get_system_settings()
    return bool(runtime_settings.enable_microsoft_authentication)


def read_session_timestamp(value):
    if not value:
        return None

    parsed = parse_datetime(value)
    if parsed is None:
        return None

    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())

    return parsed


def clear_runtime_caches():
    cache.delete(SystemSetting.CACHE_KEY)
    cache.delete(MODULE_CACHE_KEY)


def apply_runtime_security_settings():
    runtime_settings = get_system_settings()
    django_settings.AXES_FAILURE_LIMIT = runtime_settings.failed_login_limit
    django_settings.AXES_COOLOFF_TIME = runtime_settings.lockout_duration_minutes / 60.0
    django_settings.AXES_LOCKOUT_PARAMETERS = ["username"]
    django_settings.AXES_USERNAME_CALLABLE = "Users.axes_helpers.get_axes_username"
    django_settings.AXES_ENABLE_ACCESS_FAILURE_LOG = True
    django_settings.AXES_RESET_ON_SUCCESS = True
    return runtime_settings


def _module_code(module_or_code):
    if isinstance(module_or_code, str):
        return module_or_code.upper()
    return str(getattr(module_or_code, "code", "") or "").upper()


def _module_is_available(module_or_code):
    code = _module_code(module_or_code)
    if code == "IFRS9":
        return get_ifrs9_package_status()["usable"]
    if code == "SCORECARD":
        return get_scorecard_package_status()["usable"]
    return True


def _module_to_launcher_card(module):
    try:
        target_url = reverse(module.route_name)
    except NoReverseMatch:
        target_url = "#"

    return {
        "id": getattr(module, "pk", None),
        "code": module.code,
        "name": module.name,
        "url": target_url,
        "status": "Available",
        "icon": getattr(module, "icon_class", "") or "fas fa-layer-group",
        "accent": getattr(module, "accent_class", "") or "",
        "description": getattr(module, "description", ""),
    }


def _default_module_cards():
    class ModuleStub:
        def __init__(self, payload):
            self.pk = payload["code"]
            self.code = payload["code"]
            self.name = payload["name"]
            self.route_name = payload["route_name"]
            self.icon_class = payload["icon_class"]
            self.accent_class = payload["accent_class"]
            self.description = payload["description"]

    return [
        _module_to_launcher_card(ModuleStub(module_def))
        for module_def in DEFAULT_MODULES
        if _module_is_available(module_def["code"])
    ]


def get_visible_modules_for_user(user):
    try:
        ensure_default_modules()
        module_qs = SystemModule.objects.filter(is_active=True).order_by("display_order", "name")

        if user.is_superuser:
            return [
                _module_to_launcher_card(module)
                for module in module_qs
                if _module_is_available(module)
            ]

        access_rules_exist = UserModuleAccess.objects.filter(module__is_active=True).exists()
        if not access_rules_exist:
            return [
                _module_to_launcher_card(module)
                for module in module_qs
                if _module_is_available(module)
            ]

        visible_module_ids = (
            UserModuleAccess.objects.filter(
                can_view=True,
                user=user,
                module__is_active=True,
            )
            .values_list("module_id", flat=True)
            .distinct()
        )

        visible_modules = module_qs.filter(pk__in=visible_module_ids)
        return [
            _module_to_launcher_card(module)
            for module in visible_modules
            if _module_is_available(module)
        ]
    except DatabaseError:
        return _default_module_cards()


def get_post_login_redirect(user):
    modules = get_visible_modules_for_user(user)
    runtime_settings = get_system_settings()

    if (
        runtime_settings.default_landing_rule == SystemSetting.LANDING_RULE_DIRECT
        and len(modules) == 1
        and modules[0]["url"] != "#"
    ):
        return modules[0]["url"]

    return reverse("modules_home")
