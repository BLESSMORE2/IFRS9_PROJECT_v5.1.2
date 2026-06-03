from dataclasses import dataclass

from django.apps import apps
from django.conf import settings as django_settings
from django.core.cache import cache
from django.db import DatabaseError
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

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
VISIBLE_MODULES_CACHE_PREFIX = "users_visible_modules_v2"
VISIBLE_MODULES_CACHE_SECONDS = 60
MICROSOFT_AUTH_VERIFIED_AT_KEY = "users_microsoft_auth_verified_at"
MICROSOFT_AUTH_VERIFIED_EMAIL_KEY = "users_microsoft_auth_verified_email"
PACKAGE_BACKED_MODULES = {
    "IFRS9": ("IFRS9_PACKAGE_AVAILABLE", "IFRS9"),
    "SCORECARD": ("SCORECARD_PACKAGE_AVAILABLE", "scorecard"),
}


def _module_availability_flags():
    flags = {}
    for module_code, (setting_name, app_label) in PACKAGE_BACKED_MODULES.items():
        configured_value = getattr(django_settings, setting_name, None)
        if configured_value is None:
            flags[module_code] = apps.is_installed(app_label)
        else:
            flags[module_code] = bool(configured_value)
    return flags


def _module_is_available(module_code):
    availability = _module_availability_flags()
    return availability.get(str(module_code or "").upper(), True)


def _module_availability_signature():
    availability = _module_availability_flags()
    return ":".join(f"{code}={int(available)}" for code, available in sorted(availability.items()))


def _filter_available_module_queryset(module_qs):
    unavailable_codes = [
        code
        for code, available in _module_availability_flags().items()
        if not available
    ]
    if unavailable_codes:
        return module_qs.exclude(code__in=unavailable_codes)
    return module_qs



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
    password_expiry_warning_days: int = 7
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
        if user and getattr(user, "is_authenticated", False):
            cache_key = f"{VISIBLE_MODULES_CACHE_PREFIX}:{_module_availability_signature()}:{getattr(user, 'pk', 'anon')}:{int(bool(getattr(user, 'is_superuser', False)))}"
            cached_modules = cache.get(cache_key)
            if cached_modules is not None:
                return cached_modules
        else:
            cache_key = None

        ensure_default_modules()
        module_qs = _filter_available_module_queryset(SystemModule.objects.filter(is_active=True)).order_by("display_order", "name")

        if user.is_superuser:
            modules = [_module_to_launcher_card(module) for module in module_qs]
            if cache_key:
                cache.set(cache_key, modules, VISIBLE_MODULES_CACHE_SECONDS)
            return modules

        access_rules_exist = UserModuleAccess.objects.filter(module__is_active=True).exists()
        if not access_rules_exist:
            modules = [_module_to_launcher_card(module) for module in module_qs]
            if cache_key:
                cache.set(cache_key, modules, VISIBLE_MODULES_CACHE_SECONDS)
            return modules

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
        modules = [_module_to_launcher_card(module) for module in visible_modules]
        if cache_key:
            cache.set(cache_key, modules, VISIBLE_MODULES_CACHE_SECONDS)
        return modules
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
