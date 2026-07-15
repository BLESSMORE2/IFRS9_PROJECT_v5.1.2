import base64
import csv
from datetime import timedelta
import importlib
import io
import os
import sys
import sysconfig
import urllib.parse
from time import perf_counter

import pyotp
import qrcode
from django import forms as django_forms
from django.apps import apps
from django.contrib import messages, auth
from django.contrib.auth import login,logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.db.utils import ConnectionHandler
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.db.models import Count, Q
from .forms import (
    AuthenticatorResetForm,
    CustomPasswordChangeForm,
    SystemSettingsForm,
    UserModuleAccessForm,
    UserWorkspaceCreationForm,
    UserWorkspacePasswordResetForm,
    UserRoleAssignmentForm,
    UserRoleGroupAssignmentForm,
    UserRoleGroupForm,
)
from .models import CustomUser, SystemModule, SystemSetting, UserModuleAccess, UserAccessLog, UserRoleGroup
from .backends import resolve_login_user
from .access_logs import begin_user_session_log, close_user_session_log
from .security import (
    password_change_required,
    password_expiry_reminder_due,
    password_policy_requirements,
)
from django.urls import reverse
from django.conf import settings
try:
    from IFRS9.signals import PACKAGE_EXPIRED
except ModuleNotFoundError:
    PACKAGE_EXPIRED = not any(
        app_name == "IFRS9" or app_name.startswith("IFRS9.")
        for app_name in settings.INSTALLED_APPS
    )

try:
    from IFRS9.Functions_view.audit import save_audit_trail
except ModuleNotFoundError:
    def save_audit_trail(*args, **kwargs):
        return None

from Loan_management_and_LLFP.runtime_database_config import (
    get_runtime_database_config_path,
    load_runtime_database_config,
    save_runtime_dr_database_config,
    save_runtime_database_config,
)
from .runtime import (
    MICROSOFT_AUTH_VERIFIED_AT_KEY,
    MICROSOFT_AUTH_VERIFIED_EMAIL_KEY,
    apply_runtime_security_settings,
    clear_runtime_caches,
    ensure_default_modules,
    get_post_login_redirect,
    get_system_settings,
    get_visible_modules_for_user,
    microsoft_auth_uses_authenticator_app_mode,
    microsoft_auth_is_available,
    read_session_timestamp,
)
from .versioning import get_current_application_version

try:
    from axes.models import AccessAttempt, AccessFailureLog
except Exception:  # pragma: no cover - graceful fallback if axes is unavailable
    AccessAttempt = None
    AccessFailureLog = None


SCORECARD_ROLE_PREFIXES = (
    "Basel Score",
    "Basel Template",
    "IFRS9 Score",
    "IFRS9 Template",
    "Scorecard",
    "Branch ",
    "Customer ",
    "Document ",
    "Email ",
    "External Import",
    "Historical Score",
    "Notification",
    "Support Documents",
)
IFRS9_ROLE_PREFIXES = (
    "API",
    "Audit Trail",
    "Data -",
    "EAD & Cashflows",
    "IFRS9 Configuration",
    "IFRS9 Results",
    "IFRS9 Supporting Data",
    "LGD Configuration",
    "Operations",
    "Probability Configuration",
    "Reports",
    "Staging",
)
ROLE_SECTION_META = {
    "ifrs9": {
        "title": "IFRS 9 Roles",
        "subtitle": "Configuration, API, staging, ECL reporting, LGD, PD, EAD, and operations permissions.",
        "icon": "fas fa-chart-line",
        "accent": "ifrs9",
    },
    "scorecard": {
        "title": "Scorecard Roles",
        "subtitle": "Score templates, makers, checkers, customers, documents, branches, and notifications.",
        "icon": "fas fa-clipboard-check",
        "accent": "scorecard",
    },
    "platform": {
        "title": "Platform & Settings Roles",
        "subtitle": "Shared administration, workspace, permission, audit, and security-control roles.",
        "icon": "fas fa-shield-alt",
        "accent": "platform",
    },
}

SCORECARD_ROLE_DESCRIPTIONS = {
    "Scorecard Administrator": "Full scorecard access across workspace, API, templates, scores, customers, documents, email, and permission settings.",
    "Workspace Access": "Open the scorecard workspace and use branch switching.",
    "API Viewer": "Review API health, import history, and sync monitoring pages.",
    "API Endpoint Manager": "Create, edit, and retire API endpoint definitions.",
    "API Operator": "Run API tests, manual imports, retries, and main customer sync operations.",
    "API Scheduler Manager": "Maintain API schedules and automation timing.",
    "API Settings Manager": "Maintain API configuration and main customer auto-sync settings.",
    "API Administrator": "Full API control across settings, endpoints, schedules, and operations.",
    "External Import Operator": "Upload, map, validate, and import external Basel, IFRS9, and historical score files.",
    "Basel Template Viewer": "Review Basel scorecard templates, builder structure, and grade bands without editing them.",
    "Basel Template Manager": "Create and maintain Basel templates, including builder flow, sections, drivers, attributes, options, and grade bands.",
    "Basel Template Checker": "Review, approve, return, and reopen Basel template workflow items.",
    "Basel Template Administrator": "Full Basel template control across the builder, setup pages, and approval workflow.",
    "IFRS9 Template Viewer": "Review IFRS9 template structures and builder layout without changing them.",
    "IFRS9 Template Manager": "Create and maintain IFRS9 templates, including builder flow, sections, drivers, attributes, and options.",
    "IFRS9 Template Checker": "Review, approve, return, and reopen IFRS9 template workflow items.",
    "IFRS9 Template Administrator": "Full IFRS9 template control across the builder, setup pages, and approval workflow.",
    "Basel Score Viewer": "Review Basel score submissions, comparisons, history, and exports.",
    "Basel Score Maker": "Create, edit, submit, and manage Basel score evaluations.",
    "Basel Score Reopen Administrator": "Reopen approved Basel evaluations for correction or rework.",
    "Basel Score Checker": "Review, approve, and return Basel score evaluations.",
    "Basel Score Administrator": "Full Basel score control across creation, review, and reopen actions.",
    "IFRS9 Score Viewer": "Review IFRS9 score submissions, comparisons, history, and exports.",
    "IFRS9 Score Maker": "Create, edit, submit, and manage IFRS9 score forms.",
    "IFRS9 Score Reopen Administrator": "Reopen approved IFRS9 score forms for correction or rework.",
    "IFRS9 Score Checker": "Review, approve, and return IFRS9 score evaluations.",
    "IFRS9 Score Administrator": "Full IFRS9 score control across creation, review, and reopen actions.",
    "IFRS9 Supporting Data Manager": "Maintain branch-scoped collateral and payment schedule staging records for active IFRS9 loans.",
    "IFRS9 Results Viewer": "Open branch-scoped IFRS9 results extracts and ECL summary reporting workspaces.",
    "Historical Score Viewer": "Review branch-scoped historical score snapshots and download filtered historical score extracts.",
    "Customer Viewer": "Review customer directories, exports, and score gap lists.",
    "Customer Manager": "Create and maintain scorecard customer records.",
    "Branch Viewer": "Open the branch master workspace in read-only mode.",
    "Branch Manager": "Maintain branch master data.",
    "Document Viewer": "Open questionnaire and scorecard document libraries.",
    "Document Manager": "Upload and maintain scorecard document records.",
    "Notification Viewer": "Read scorecard alerts and open notification details.",
    "Email Viewer": "Open email configuration, templates, and delivery history in read-only mode.",
    "Email Manager": "Maintain email configuration, templates, and retry actions.",
    "Audit Trail Viewer": "Open the standalone scorecard audit trail page and review scorecard-wide activity history.",
    "Permission Viewer": "Review the permission dashboard, user access summary, matrix, and workflow rules.",
    "Permission Manager": "Assign scorecard permission roles, branch access, and workflow auto-approval rules.",
}
SCORECARD_ROLE_NAMES = frozenset(SCORECARD_ROLE_DESCRIPTIONS)


def _role_area_name(group_name):
    area = group_name
    for suffix in (
        " Reopen Administrator",
        " Administrator",
        " Manager",
        " Operator",
        " Viewer",
        " Checker",
        " Maker",
        " Exporter",
        " Canceller",
        " Executor",
        " Monitor",
        " Creator",
        " Deleter",
        " Editor",
        " Loader",
        " Admin",
    ):
        if area.endswith(suffix):
            area = area[: -len(suffix)]
            break
    return area.replace(" - ", " ").strip() or group_name


def _fallback_role_description(group_name, section_key):
    area = _role_area_name(group_name)
    if group_name.endswith(("Admin", "Administrator")):
        action = f"Full administrative control for {area}."
    elif group_name.endswith("Viewer"):
        action = f"Read-only access to view {area} workspaces, records, and reports."
    elif group_name.endswith("Manager"):
        action = f"Create, update, and maintain {area} configuration and operational records."
    elif group_name.endswith("Operator"):
        action = f"Run controlled operational actions for {area}."
    elif group_name.endswith("Exporter"):
        action = f"Download and export {area} report outputs."
    elif group_name.endswith("Checker"):
        action = f"Review, approve, return, or reject {area} workflow items."
    elif group_name.endswith("Maker"):
        action = f"Create, edit, and submit {area} records for review."
    elif group_name.endswith("Executor"):
        action = f"Execute approved {area} processes."
    elif group_name.endswith("Monitor"):
        action = f"Monitor {area} process progress and execution status."
    elif group_name.endswith("Canceller"):
        action = f"Cancel eligible {area} processes when operationally required."
    elif group_name.endswith("Creator"):
        action = f"Create new {area} setup records."
    elif group_name.endswith("Editor"):
        action = f"Edit existing {area} setup records."
    elif group_name.endswith("Deleter"):
        action = f"Delete eligible {area} records where permissions allow."
    elif group_name.endswith("Loader"):
        action = f"Load and maintain data for {area}."
    else:
        action = f"Access and use {area} features."

    if section_key == "ifrs9":
        return f"{action} Applies inside the IFRS 9 module."
    if section_key == "scorecard":
        return f"{action} Applies inside the Scorecard module."
    return f"{action} Applies to shared platform settings and security controls."


def _role_section_key(group_name):
    if group_name in SCORECARD_ROLE_NAMES:
        return "scorecard"
    if group_name.startswith(SCORECARD_ROLE_PREFIXES):
        return "scorecard"
    if group_name.startswith(IFRS9_ROLE_PREFIXES):
        return "ifrs9"
    return "platform"


def _build_role_sections(groups, selected_group_ids=None, role_rows=None):
    selected_group_ids = {str(group_id) for group_id in (selected_group_ids or [])}
    row_lookup = {row["group"].id: row for row in (role_rows or [])}
    section_map = {
        "ifrs9": [],
        "scorecard": [],
        "platform": [],
    }
    for group in groups:
        section_key = _role_section_key(group.name)
        section_map[section_key].append(
            {
                "group": group,
                "checked": str(group.id) in selected_group_ids,
                "directory_row": row_lookup.get(group.id),
                "description": SCORECARD_ROLE_DESCRIPTIONS.get(
                    group.name,
                    _fallback_role_description(group.name, section_key),
                ),
            }
        )

    sections = []
    for section_key in ("ifrs9", "scorecard", "platform"):
        roles = section_map[section_key]
        if not roles:
            continue
        section = dict(ROLE_SECTION_META[section_key])
        section.update(
            {
                "key": section_key,
                "roles": roles,
                "count": len(roles),
                "selected_count": sum(1 for role in roles if role["checked"]),
            }
        )
        sections.append(section)
    return sections


def _apply_user_role_groups(target_user, role_groups):
    role_ids = set()
    for role_group in role_groups:
        role_ids.update(role_group.roles.values_list("id", flat=True))

    if not role_ids:
        return 0

    roles = list(Group.objects.filter(id__in=role_ids))
    target_user.groups.add(*roles)
    return len(roles)


def get_app_version():
    """Return the current platform launcher version."""
    return get_current_application_version()


def _render_lockout_response(request, target_user=None, permanent_lock=False):
    popup_mode = _workspace_popup_enabled(request)
    lockout_until = getattr(target_user, "lockout_until", None) if target_user is not None else None
    lockout_remaining_seconds = 0
    if lockout_until:
        lockout_remaining_seconds = max(int((lockout_until - timezone.now()).total_seconds()), 0)
    return render(
        request,
        "axes/lockout.html",
        {
            "app_version": get_app_version(),
            "popup_mode": popup_mode,
            "permanent_lock": permanent_lock,
            "lockout_until": lockout_until,
            "lockout_remaining_seconds": lockout_remaining_seconds,
        },
        status=429,
    )


MICROSOFT_AUTH_PURPOSE_KEY = "users_microsoft_auth_purpose"
MICROSOFT_AUTH_NEXT_KEY = "users_microsoft_auth_next"
MICROSOFT_AUTH_PENDING_USER_ID_KEY = "users_pending_microsoft_auth_user_id"
PASSWORD_EXPIRY_REMINDER_SESSION_KEY = "users_password_expiry_reminder"
WORKSPACE_POPUP_SESSION_KEY = "users_workspace_popup_mode"
WORKSPACE_POPUP_WINDOW_NAME = "nexaWorkspaceWindow"


def _clear_password_expiry_reminder(request):
    request.session.pop(PASSWORD_EXPIRY_REMINDER_SESSION_KEY, None)


def _queue_password_expiry_reminder(request, user, runtime_settings):
    _clear_password_expiry_reminder(request)
    reminder_due, days_left = password_expiry_reminder_due(user, runtime_settings)
    if not reminder_due or days_left is None:
        return

    request.session[PASSWORD_EXPIRY_REMINDER_SESSION_KEY] = {
        "days_left": int(days_left),
        "can_change_password": bool(getattr(runtime_settings, "enable_self_password_change", True)),
    }


def _set_workspace_popup_mode(request, enabled=True):
    if enabled:
        request.session[WORKSPACE_POPUP_SESSION_KEY] = True
    else:
        request.session.pop(WORKSPACE_POPUP_SESSION_KEY, None)


def _workspace_popup_enabled(request):
    return bool(request.session.get(WORKSPACE_POPUP_SESSION_KEY))


def _workspace_launcher_target(user, next_target=""):
    if next_target:
        return next_target
    if user and getattr(user, "is_authenticated", False):
        return get_post_login_redirect(user)
    return reverse("login_popup")

def login_view(request):
    """Login view with expiry check."""
    # Invalidate caches immediately before checking package status
    importlib.invalidate_caches()
    runtime_settings = apply_runtime_security_settings()
    microsoft_login_available = microsoft_auth_is_available(runtime_settings)
    authenticator_app_mode = microsoft_auth_uses_authenticator_app_mode(runtime_settings)
    # If the package is missing/expired, disallow logging in
    # if PACKAGE_EXPIRED:
    #     messages.error(request, "🔴 Your license expired ,you no longer have access to this application please contact the product owner.")
    #     return render(request, "users/login.html") 


    
    if request.method == "POST":
        identifier = (request.POST.get("login_identifier") or request.POST.get("email") or "").strip()
        password = request.POST.get("password", None)
        next_target = _get_safe_next_value(request.POST.get("next"))
        target_user = resolve_login_user(identifier) if identifier else None
        if target_user and _is_user_permanently_locked(target_user):
            return _render_lockout_response(request, target_user, permanent_lock=True)
        if target_user and _is_user_in_custom_lockout(target_user):
            return _render_lockout_response(request, target_user)
        user = auth.authenticate(request, username=identifier, email=identifier, password=password)
        if user is not None:
            if microsoft_login_available and runtime_settings.microsoft_auth_on_login and authenticator_app_mode:
                _reset_user_failed_login_state(user)
                _clear_password_expiry_reminder(request)
                request.session[MICROSOFT_AUTH_PENDING_USER_ID_KEY] = user.pk
                request.session[MICROSOFT_AUTH_PURPOSE_KEY] = "login"
                request.session[MICROSOFT_AUTH_NEXT_KEY] = next_target or ""
                return redirect("microsoft_auth_start")

            _reset_user_failed_login_state(user)
            auth.login(request, user)
            begin_user_session_log(request, user)
            _clear_microsoft_verification(request)
            _clear_pending_microsoft_auth(request)

            if password_change_required(user, runtime_settings):
                if user.must_change_password:
                    messages.warning(request, "You must change your password before continuing.")
                else:
                    messages.warning(request, "Your password has expired. Please set a new password to continue.")
                return redirect("change_password")

            _queue_password_expiry_reminder(request, user, runtime_settings)
            return redirect(next_target or get_post_login_redirect(user))
        else:
            if target_user is not None:
                lockout_state = _register_failed_login_attempt(target_user, runtime_settings)
                if lockout_state == "permanent":
                    return _render_lockout_response(request, target_user, permanent_lock=True)
                if lockout_state == "temporary":
                    return _render_lockout_response(request, target_user)
            if identifier and target_user is None:
                messages.error(request, "No account found for that email or username.")
            else:
                messages.error(request, "Incorrect password.")
            if next_target:
                return redirect(f"{reverse('login')}?{urllib.parse.urlencode({'next': next_target})}")
            return redirect("login")

    return render(
        request,
        "users/login.html",
        {
            "app_version": get_app_version(),
            "next_url": _get_safe_next_value(request.GET.get("next")),
            "microsoft_login_available": microsoft_login_available,
            "microsoft_login_required": microsoft_login_available and runtime_settings.microsoft_auth_on_login,
            "microsoft_authenticator_app_mode": authenticator_app_mode,
            "microsoft_login_url": f"{reverse('microsoft_auth_start')}?{urllib.parse.urlencode({'purpose': 'login', 'next': _get_safe_next_value(request.GET.get('next')) or ''})}",
        },
    )


def login_view(request):
    runtime_settings = apply_runtime_security_settings()
    next_target = _get_safe_next_value(request.GET.get("next") or request.POST.get("next"))
    popup_login_url = reverse("login_popup")
    popup_query = urllib.parse.urlencode({"next": next_target}) if next_target else ""
    popup_url = f"{popup_login_url}?{popup_query}" if popup_query else popup_login_url

    return render(
        request,
        "users/workspace_launcher.html",
        {
            "app_version": get_app_version(),
            "next_url": next_target,
            "workspace_popup_window_name": WORKSPACE_POPUP_WINDOW_NAME,
            "workspace_popup_url": popup_url,
            "workspace_target_url": _workspace_launcher_target(request.user, next_target),
            "workspace_already_authenticated": bool(request.user.is_authenticated),
            "microsoft_login_available": microsoft_auth_is_available(runtime_settings),
        },
    )


def login_popup_view(request):
    """Popup login view for the dedicated workspace window."""
    runtime_settings = apply_runtime_security_settings()
    microsoft_login_available = microsoft_auth_is_available(runtime_settings)
    authenticator_app_mode = microsoft_auth_uses_authenticator_app_mode(runtime_settings)

    if request.method == "POST":
        identifier = (request.POST.get("login_identifier") or request.POST.get("email") or "").strip()
        password = request.POST.get("password", None)
        next_target = _get_safe_next_value(request.POST.get("next"))
        target_user = resolve_login_user(identifier) if identifier else None
        _set_workspace_popup_mode(request, True)
        if target_user and _is_user_permanently_locked(target_user):
            return _render_lockout_response(request, target_user, permanent_lock=True)
        if target_user and _is_user_in_custom_lockout(target_user):
            return _render_lockout_response(request, target_user)
        user = auth.authenticate(
            request,
            username=identifier,
            email=identifier,
            password=password,
            resolved_user=target_user,
        )
        if user is not None:
            if microsoft_login_available and runtime_settings.microsoft_auth_on_login and authenticator_app_mode:
                _reset_user_failed_login_state(user)
                _clear_password_expiry_reminder(request)
                request.session[MICROSOFT_AUTH_PENDING_USER_ID_KEY] = user.pk
                request.session[MICROSOFT_AUTH_PURPOSE_KEY] = "login"
                request.session[MICROSOFT_AUTH_NEXT_KEY] = next_target or ""
                return redirect(f"{reverse('microsoft_auth_start')}?{urllib.parse.urlencode({'popup': '1'})}")

            _reset_user_failed_login_state(user)
            auth.login(request, user)
            begin_user_session_log(request, user)
            _clear_microsoft_verification(request)
            _clear_pending_microsoft_auth(request)
            _set_workspace_popup_mode(request, True)

            if password_change_required(user, runtime_settings):
                if user.must_change_password:
                    messages.warning(request, "You must change your password before continuing.")
                else:
                    messages.warning(request, "Your password has expired. Please set a new password to continue.")
                return redirect("change_password")

            _queue_password_expiry_reminder(request, user, runtime_settings)
            return redirect(next_target or get_post_login_redirect(user))
        else:
            if target_user is not None:
                lockout_state = _register_failed_login_attempt(target_user, runtime_settings)
                if lockout_state == "permanent":
                    return _render_lockout_response(request, target_user, permanent_lock=True)
                if lockout_state == "temporary":
                    return _render_lockout_response(request, target_user)
            if identifier and target_user is None:
                messages.error(request, "No account found for that email or username.")
            else:
                messages.error(request, "Incorrect password.")
            if next_target:
                return redirect(f"{reverse('login_popup')}?{urllib.parse.urlencode({'next': next_target})}")
            return redirect("login_popup")

    next_url = _get_safe_next_value(request.GET.get("next"))
    _set_workspace_popup_mode(request, True)
    microsoft_query = {"purpose": "login", "next": next_url or "", "popup": "1"}
    return render(
        request,
        "users/login.html",
        {
            "app_version": get_app_version(),
            "next_url": next_url,
            "popup_mode": True,
            "login_form_action": reverse("login_popup"),
            "microsoft_login_available": microsoft_login_available,
            "microsoft_login_required": microsoft_login_available and runtime_settings.microsoft_auth_on_login,
            "microsoft_authenticator_app_mode": authenticator_app_mode,
            "microsoft_login_url": f"{reverse('microsoft_auth_start')}?{urllib.parse.urlencode(microsoft_query)}",
        },
    )


def microsoft_auth_start_view(request):
    runtime_settings = get_system_settings()
    if request.GET.get("popup") == "1":
        _set_workspace_popup_mode(request, True)
    if not microsoft_auth_is_available(runtime_settings):
        messages.error(
            request,
            "Microsoft authentication is not fully configured yet. Complete the Microsoft Auth settings first.",
        )
        return redirect("login" if not request.user.is_authenticated else "modules_home")

    purpose = (request.GET.get("purpose") or request.session.get(MICROSOFT_AUTH_PURPOSE_KEY) or "login").strip().lower()
    if purpose not in {"login", "password_change", "session_recheck"}:
        purpose = "login"

    if purpose != "login" and not request.user.is_authenticated:
        messages.error(request, "Please sign in before using Microsoft verification for this action.")
        return redirect("login")

    if request.method == "POST":
        return _handle_authenticator_app_challenge(request, runtime_settings, purpose)

    next_target = _get_safe_next_value(request.GET.get("next"))
    if next_target:
        request.session[MICROSOFT_AUTH_NEXT_KEY] = next_target
    request.session[MICROSOFT_AUTH_PURPOSE_KEY] = purpose
    return render(
        request,
        "users/microsoft_authenticator_challenge.html",
        _build_authenticator_challenge_context(request, runtime_settings, purpose),
    )

@login_required
def modules_home_view(request):
    modules = get_visible_modules_for_user(request.user)

    return render(
        request,
        "users/modules_home.html",
        {
            "modules": modules,
            "app_version": get_app_version(),
            "can_open_users_settings": _can_open_users_settings(request.user),
        },
    )


@login_required
def user_settings_view(request):
    if _can_view_add_users(request.user):
        return redirect("user_settings_add_user")
    if _can_view_authenticator_settings(request.user):
        return redirect("user_settings_authenticator")
    if _can_view_user_roles(request.user):
        return redirect("user_settings_roles")
    if _can_view_access_logs(request.user):
        return redirect("user_settings_access_logs")
    if _can_view_system_settings(request.user):
        return redirect("user_settings_system")

    messages.error(request, "You do not have access to the settings workspace.")
    return redirect("modules_home")


@login_required
def user_settings_add_user_view(request):
    if not _can_view_add_users(request.user):
        messages.error(request, "You do not have permission to add users.")
        return redirect("modules_home")

    form = UserWorkspaceCreationForm()
    password_reset_form = UserWorkspacePasswordResetForm()
    form.fields["groups"].queryset = Group.objects.order_by("name")
    form.fields["user_groups"].queryset = UserRoleGroup.objects.filter(is_active=True).order_by("name")

    if request.method == "POST":
        if not _can_manage_users(request.user):
            messages.error(request, "You do not have permission to manage users.")
            return redirect("user_settings_add_user")

        action = request.POST.get("action") or "create_user"
        if action == "reset_user_password":
            password_reset_form = UserWorkspacePasswordResetForm(request.POST)
            if password_reset_form.is_valid():
                target_user = password_reset_form.save()
                _reset_user_failed_login_state(target_user)
                save_audit_trail(
                    request.user,
                    "CustomUser",
                    "password_reset",
                    target_user.pk,
                    f"Reset password for user {target_user.email} from the shared Users workspace.",
                )
                messages.success(
                    request,
                    f"Password reset successfully for {target_user.email}. The user must change it at next login.",
                )
                return redirect("user_settings_add_user")
            messages.error(request, "The password was not reset. Please correct the highlighted fields and try again.")
        else:
            form = UserWorkspaceCreationForm(request.POST)
            form.fields["groups"].queryset = Group.objects.order_by("name")
            form.fields["user_groups"].queryset = UserRoleGroup.objects.filter(is_active=True).order_by("name")
            if form.is_valid():
                new_user = form.save()
                save_audit_trail(
                    request.user,
                    "CustomUser",
                    "create",
                    new_user.pk,
                    f"Created user {new_user.email} from the shared Users workspace.",
                )
                messages.success(request, f"Created user {new_user.email} successfully.")
                return redirect("user_settings_add_user")
            messages.error(request, "The user was not created. Please correct the highlighted fields and try again.")

    recent_users = CustomUser.objects.order_by("-date_joined", "-id")[:8]
    context = _build_settings_context(
        request,
        active_section="add_users",
        page_title="Add Users",
        page_intro="Create shared user accounts, set an initial password, and assign the first access roles from one place.",
    )
    context.update(
        {
            "new_user_form": form,
            "password_reset_form": password_reset_form,
            "recent_users": recent_users,
            "role_count": Group.objects.count(),
            "user_group_count": UserRoleGroup.objects.count(),
            "user_count": CustomUser.objects.count(),
            "can_manage_users": _can_manage_users(request.user),
            "password_policy_spec": password_policy_requirements(get_system_settings()),
        }
    )
    return render(request, "users/settings_workspace.html", context)


@login_required
def user_settings_roles_view(request):
    if not _can_view_user_roles(request.user):
        messages.error(request, "You do not have permission to view user-role settings.")
        return redirect("modules_home")

    try:
        ensure_default_modules()
        _safe_ensure_access_log_security_roles()
        users_qs = CustomUser.objects.order_by("name", "surname", "email")
        groups_qs = Group.objects.order_by("name")
        modules_qs = SystemModule.objects.filter(is_active=True).order_by("display_order", "name")
        role_group_qs = UserRoleGroup.objects.prefetch_related("roles", "users").order_by("name")
        users_qs.exists()
        groups_qs.exists()
        modules_qs.exists()
        role_group_qs.exists()
        UserModuleAccess.objects.exists()
    except DatabaseError:
        messages.error(
            request,
            "The new Users settings tables are not available yet. Apply the latest Users migration first.",
        )
        return redirect("modules_home")

    selected_user = None
    requested_user_id = request.GET.get("user")
    active_user_roles_tab = request.GET.get("tab") or "user-access"
    user_access_search = (request.GET.get("user_search") or "").strip()
    requested_page_size = request.GET.get("user_page_size") or "20"
    user_access_page_size = int(requested_page_size) if requested_page_size in {"10", "20", "50", "100"} else 20
    scorecard_branch_access_available = False
    scorecard_branch_model = None
    scorecard_user_branch_access_model = None
    available_scorecard_branches = []
    available_scorecard_branch_ids = set()

    if requested_user_id:
        selected_user = users_qs.filter(pk=requested_user_id).first()

    if apps.is_installed("scorecard"):
        try:
            scorecard_branch_model = apps.get_model("scorecard", "BankBranch")
            scorecard_user_branch_access_model = apps.get_model("scorecard", "ScorecardUserBranchAccess")
            available_scorecard_branches = list(
                scorecard_branch_model.objects.all().order_by("bank_name", "branch_name")
            )
            available_scorecard_branch_ids = {
                branch.pk for branch in available_scorecard_branches
            }
            scorecard_branch_access_available = True
        except (LookupError, DatabaseError):
            scorecard_branch_access_available = False

    role_form = UserRoleAssignmentForm()
    module_form = UserModuleAccessForm()
    role_group_form = UserRoleGroupForm()
    role_group_assignment_form = UserRoleGroupAssignmentForm()

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "assign_user_roles":
            if not _can_manage_user_roles(request.user):
                messages.error(request, "You do not have permission to change user-role assignments.")
                return redirect("user_settings_roles")

            role_form = UserRoleAssignmentForm(request.POST)
            if role_form.is_valid():
                target_user = role_form.cleaned_data["user"]
                selected_branch_ids = {
                    int(branch_id)
                    for branch_id in request.POST.getlist("scorecard_branch_ids")
                    if branch_id.isdigit() and int(branch_id) in available_scorecard_branch_ids
                }

                with transaction.atomic():
                    target_user.groups.set(role_form.cleaned_data["groups"])
                    if (
                        scorecard_branch_access_available
                        and request.POST.get("scorecard_branch_access_present") == "1"
                    ):
                        scorecard_user_branch_access_model.objects.filter(user=target_user).exclude(
                            branch_id__in=selected_branch_ids
                        ).delete()
                        existing_branch_ids = set(
                            scorecard_user_branch_access_model.objects.filter(
                                user=target_user,
                                branch_id__in=selected_branch_ids,
                            ).values_list("branch_id", flat=True)
                        )
                        scorecard_user_branch_access_model.objects.bulk_create(
                            [
                                scorecard_user_branch_access_model(
                                    user=target_user,
                                    branch=branch,
                                )
                                for branch in available_scorecard_branches
                                if branch.pk in selected_branch_ids
                                and branch.pk not in existing_branch_ids
                            ]
                        )

                success_message = f"Updated role membership for {target_user.email}."
                if scorecard_branch_access_available:
                    success_message = (
                        f"Updated role membership and Scorecard branch access for "
                        f"{target_user.email}."
                    )
                messages.success(request, success_message)
                selected_user = target_user
                redirect_url = reverse("user_settings_roles")
                query = [f"user={target_user.pk}", "tab=user-assignment"]
                return redirect(f"{redirect_url}?{'&'.join(query)}")
        elif action == "assign_user_modules":
            if not _can_manage_user_roles(request.user):
                messages.error(request, "You do not have permission to change user module access.")
                return redirect("user_settings_roles")

            module_form = UserModuleAccessForm(request.POST)
            module_form.fields["user"].queryset = users_qs
            module_form.fields["modules"].queryset = modules_qs
            if module_form.is_valid():
                target_user = module_form.cleaned_data["user"]
                selected_module_ids = set(module_form.cleaned_data["modules"].values_list("id", flat=True))
                UserModuleAccess.objects.filter(user=target_user, module__in=modules_qs).exclude(
                    module_id__in=selected_module_ids
                ).delete()
                for module in modules_qs:
                    if module.id in selected_module_ids:
                        UserModuleAccess.objects.update_or_create(
                            user=target_user,
                            module=module,
                            defaults={"can_view": True},
                        )
                clear_runtime_caches()
                messages.success(request, f"Updated launcher access for {target_user.email}.")
                selected_user = target_user
                redirect_url = reverse("user_settings_roles")
                return redirect(f"{redirect_url}?user={target_user.pk}&tab=user-modules")
        elif action == "save_user_role_group":
            active_user_roles_tab = "user-groups"
            if not _can_manage_user_roles(request.user):
                messages.error(request, "You do not have permission to create or change user groups.")
                return redirect("user_settings_roles")

            role_group_id = request.POST.get("role_group_id")
            role_group_instance = None
            if role_group_id:
                role_group_instance = role_group_qs.filter(pk=role_group_id).first()
                if role_group_instance is None:
                    messages.error(request, "The selected user group could not be found.")
                    return redirect(f"{reverse('user_settings_roles')}?tab=user-groups")

            role_group_form = UserRoleGroupForm(request.POST, instance=role_group_instance)
            role_group_form.fields["roles"].queryset = groups_qs
            if role_group_form.is_valid():
                user_group = role_group_form.save(commit=False)
                if user_group.pk is None:
                    user_group.created_by = request.user
                    audit_action = "create"
                else:
                    audit_action = "update"
                user_group.save()
                role_group_form.save_m2m()

                applied_users = 0
                for bundled_user in user_group.users.all():
                    _apply_user_role_groups(bundled_user, [user_group])
                    applied_users += 1

                save_audit_trail(
                    request.user,
                    "UserRoleGroup",
                    audit_action,
                    user_group.pk,
                    f"Saved user group '{user_group.name}' with {user_group.roles.count()} role(s).",
                )
                suffix = f" Applied to {applied_users} linked user(s)." if applied_users else ""
                messages.success(request, f"Saved user group '{user_group.name}'.{suffix}")
                return redirect(f"{reverse('user_settings_roles')}?tab=user-groups")
        elif action == "assign_user_role_groups":
            active_user_roles_tab = "user-groups"
            if not _can_manage_user_roles(request.user):
                messages.error(request, "You do not have permission to assign user groups.")
                return redirect("user_settings_roles")

            role_group_assignment_form = UserRoleGroupAssignmentForm(request.POST)
            role_group_assignment_form.fields["user"].queryset = users_qs
            role_group_assignment_form.fields["user_groups"].queryset = role_group_qs.filter(is_active=True)
            if role_group_assignment_form.is_valid():
                target_user = role_group_assignment_form.cleaned_data["user"]
                selected_role_groups = role_group_assignment_form.cleaned_data["user_groups"]
                target_user.access_role_groups.set(selected_role_groups)
                applied_role_count = _apply_user_role_groups(target_user, selected_role_groups)
                clear_runtime_caches()
                save_audit_trail(
                    request.user,
                    "UserRoleGroup",
                    "update",
                    target_user.pk,
                    f"Assigned {selected_role_groups.count()} user group(s) to {target_user.email}.",
                )
                messages.success(
                    request,
                    f"Updated user groups for {target_user.email}. {applied_role_count} bundled role(s) are now applied.",
                )
                selected_user = target_user
                return redirect(f"{reverse('user_settings_roles')}?user={target_user.pk}&tab=user-groups")
        else:
            role_form = UserRoleAssignmentForm()
            module_form = UserModuleAccessForm()
            role_group_form = UserRoleGroupForm()
            role_group_assignment_form = UserRoleGroupAssignmentForm()

    role_form.fields["user"].queryset = users_qs
    role_form.fields["groups"].queryset = groups_qs
    module_form.fields["user"].queryset = users_qs
    module_form.fields["modules"].queryset = modules_qs
    role_group_form.fields["roles"].queryset = groups_qs
    role_group_assignment_form.fields["user"].queryset = users_qs
    role_group_assignment_form.fields["user_groups"].queryset = role_group_qs.filter(is_active=True)
    role_form.fields["user"].widget.attrs.update(
        {
            "data-role-user-select": "true",
            "data-user-search-select": "true",
            "data-user-search-placeholder": "Type email, name, or surname",
        }
    )
    module_form.fields["user"].widget.attrs.update(
        {
            "data-module-user-select": "true",
            "data-user-search-select": "true",
            "data-user-search-placeholder": "Type email, name, or surname",
        }
    )
    role_group_assignment_form.fields["user"].widget.attrs.update(
        {
            "data-role-group-user-select": "true",
            "data-user-search-select": "true",
            "data-user-search-placeholder": "Type email, name, or surname",
        }
    )
    groups_list = list(groups_qs)
    role_group_list = list(role_group_qs)

    if selected_user:
        role_form.initial["user"] = selected_user
        role_form.initial["groups"] = selected_user.groups.all()
        module_form.initial["user"] = selected_user
        module_form.initial["modules"] = list(
            modules_qs.filter(user_access_entries__user=selected_user, user_access_entries__can_view=True).values_list(
                "id", flat=True
            )
        )
        role_group_assignment_form.initial["user"] = selected_user
        role_group_assignment_form.initial["user_groups"] = list(
            selected_user.access_role_groups.filter(is_active=True).values_list("id", flat=True)
        )

    if request.method == "POST" and request.POST.get("action") == "assign_user_roles":
        selected_group_ids = request.POST.getlist("groups")
    elif selected_user:
        selected_group_ids = selected_user.groups.values_list("id", flat=True)
    else:
        selected_group_ids = []

    role_rows = []
    for group in groups_list:
        member_emails = list(
            group.customuser_groups.order_by("email").values_list("email", flat=True)
        )
        role_rows.append(
            {
                "group": group,
                "member_count": len(member_emails),
                "member_emails": member_emails,
            }
        )
    role_sections = _build_role_sections(groups_list, selected_group_ids, role_rows)
    selected_scorecard_branch_ids = set()
    if scorecard_branch_access_available:
        if request.method == "POST" and request.POST.get("action") == "assign_user_roles":
            selected_scorecard_branch_ids = {
                int(branch_id)
                for branch_id in request.POST.getlist("scorecard_branch_ids")
                if branch_id.isdigit() and int(branch_id) in available_scorecard_branch_ids
            }
        elif selected_user:
            selected_scorecard_branch_ids = set(
                scorecard_user_branch_access_model.objects.filter(
                    user=selected_user
                ).values_list("branch_id", flat=True)
            )

    if request.method == "POST" and request.POST.get("action") == "assign_user_role_groups":
        selected_role_group_ids = {str(group_id) for group_id in request.POST.getlist("user_groups")}
    elif selected_user:
        selected_role_group_ids = {
            str(group_id)
            for group_id in selected_user.access_role_groups.values_list("id", flat=True)
        }
    else:
        selected_role_group_ids = set()

    role_group_rows = []
    for role_group in role_group_list:
        role_ids = ",".join(str(role.id) for role in role_group.roles.all())
        role_group_rows.append(
            {
                "group": role_group,
                "role_count": role_group.roles.count(),
                "user_count": role_group.users.count(),
                "role_names": list(role_group.roles.order_by("name").values_list("name", flat=True)),
                "selected": str(role_group.id) in selected_role_group_ids,
                "role_ids": role_ids,
            }
        )

    user_access_qs = users_qs.prefetch_related("groups")
    if user_access_search:
        user_access_qs = user_access_qs.filter(
            Q(email__icontains=user_access_search)
            | Q(name__icontains=user_access_search)
            | Q(surname__icontains=user_access_search)
        )
    user_access_paginator = Paginator(user_access_qs, user_access_page_size)
    user_access_page = user_access_paginator.get_page(request.GET.get("user_page"))
    visible_user_ids = [user.pk for user in user_access_page.object_list]

    scorecard_branches_by_user = {}
    if scorecard_branch_access_available:
        branch_assignments = (
            scorecard_user_branch_access_model.objects.select_related("branch")
            .filter(user_id__in=visible_user_ids)
            .order_by("branch__branch_name")
        )
        for assignment in branch_assignments:
            scorecard_branches_by_user.setdefault(assignment.user_id, []).append(
                {
                    "name": assignment.branch.branch_name,
                    "code": assignment.branch.branch_code,
                }
            )

    user_rows = []
    for user in user_access_page.object_list:
        modules = get_visible_modules_for_user(user)
        user_rows.append(
            {
                "user": user,
                "group_names": sorted(group.name for group in user.groups.all()),
                "module_names": [module["name"] for module in modules],
                "scorecard_branches": scorecard_branches_by_user.get(user.pk, []),
            }
        )

    context = _build_settings_context(
        request,
        active_section="user_roles",
        page_title="User Access",
        page_intro="Manage user roles and assign which modules each user can open after login.",
    )
    context.update(
        {
            "role_form": role_form,
            "module_form": module_form,
            "module_count": modules_qs.count(),
            "user_group_count": len(role_group_rows),
            "role_group_form": role_group_form,
            "role_group_assignment_form": role_group_assignment_form,
            "role_group_rows": role_group_rows,
            "active_user_roles_tab": active_user_roles_tab,
            "role_rows": role_rows,
            "role_sections": role_sections,
            "user_rows": user_rows,
            "user_count": users_qs.count(),
            "user_access_search": user_access_search,
            "user_access_page_size": user_access_page_size,
            "user_access_page": user_access_page,
            "selected_user": selected_user,
            "can_manage_user_roles": _can_manage_user_roles(request.user),
            "scorecard_branch_access_available": scorecard_branch_access_available,
            "available_scorecard_branches": available_scorecard_branches,
            "selected_scorecard_branch_ids": selected_scorecard_branch_ids,
        }
    )
    return render(request, "users/settings_workspace.html", context)


@login_required
def user_settings_system_view(request):
    if not _can_view_system_settings(request.user):
        messages.error(request, "You do not have permission to view system settings.")
        return redirect("modules_home")

    runtime_settings = get_system_settings()
    backend_state = _load_database_backend_state()
    system_active_tab = _normalize_system_settings_tab(request.GET.get("tab"))
    system_settings_available = hasattr(runtime_settings, "_meta")
    form = SystemSettingsForm(instance=runtime_settings) if system_settings_available else None

    if request.method == "POST":
        action = request.POST.get("action", "save_runtime_controls")
        if action == "save_database_backend":
            if not _can_manage_system_settings(request.user):
                messages.error(request, "You do not have permission to change the database backend setting.")
                return redirect("user_settings_system")

            selected_vendor = request.POST.get("database_vendor", "").strip().lower()
            if selected_vendor not in backend_state["supported_vendors"]:
                messages.error(request, "Please choose a valid database backend.")
                return redirect("user_settings_system")

            previous_vendor = backend_state["saved_database_vendor"]
            previous_backend = backend_state["saved_functions_backend"]
            save_runtime_database_config(
                settings.BASE_DIR,
                selected_vendor,
                backend_state["supported_vendors"],
                functions_db_backend=selected_vendor,
            )
            save_audit_trail(
                request.user,
                "RuntimeDatabaseConfig",
                "update",
                selected_vendor,
                (
                    f"Updated runtime database backend setting from the Users system settings workspace. "
                    f"Previous database vendor: {previous_vendor}. "
                    f"Previous functions backend: {previous_backend}. "
                    f"New database vendor: {selected_vendor}. "
                    f"New functions backend: {selected_vendor}. "
                    f"Config file: {get_runtime_database_config_path(settings.BASE_DIR).name}. "
                    f"Application restart required for the new database backend to take effect."
                ),
            )
            messages.success(
                request,
                f"Database backend saved as {selected_vendor}. Restart the application for the new database backend to take effect.",
            )
            return redirect(f"{reverse('user_settings_system')}?tab=database")

        if action in {"save_dr_database_config", "test_dr_database_config"}:
            if not (request.user.is_superuser or request.user.has_perm("Users.can_manage_settings_database")):
                messages.error(request, "You do not have permission to change DR database settings.")
                return redirect("user_settings_system")

            system_active_tab = "system-database"
            current_dr_config = backend_state.get("dr_database", {})
            dr_payload = _dr_database_payload_from_post(request.POST, current_dr_config)

            if action == "test_dr_database_config":
                backend_state["dr_database"] = dr_payload
                backend_state["dr_test_result"] = _test_dr_database_connection(dr_payload)
                if backend_state["dr_test_result"]["ok"]:
                    messages.success(request, "DR database connectivity test succeeded. Review the result below, then save when ready.")
                else:
                    messages.error(request, "DR database connectivity test failed. Review the result below before saving.")
                action = "dr_test_complete"

            if action == "save_dr_database_config" and dr_payload["enabled"] and not (dr_payload["host"] and dr_payload["name"]):
                messages.error(request, "Please enter the DR server IP/host and DR database name before enabling DR.")
                return redirect("user_settings_system")

            if action == "save_dr_database_config":
                if dr_payload["enabled"]:
                    backend_state["dr_database"] = dr_payload
                    backend_state["dr_test_result"] = _test_dr_database_connection(dr_payload)
                    if not backend_state["dr_test_result"]["ok"]:
                        messages.error(request, "DR database settings were not saved because the connection test failed.")
                        action = "dr_test_complete"

                if action == "dr_test_complete":
                    pass
                else:
                    save_runtime_dr_database_config(
                        settings.BASE_DIR,
                        dr_payload,
                        backend_state["saved_database_vendor"],
                        backend_state["supported_vendors"],
                    )
                    save_audit_trail(
                        request.user,
                        "RuntimeDatabaseConfig",
                        "update",
                        "dr",
                        (
                            "Updated DR database configuration from System Settings. "
                            f"Enabled: {dr_payload['enabled']}. "
                            f"Host: {dr_payload['host'] or 'not set'}. "
                            f"Database: {dr_payload['name'] or 'not set'}. "
                            f"Backup method: {dr_payload['backup_method']}. "
                            "Application restart is required before the DR connection is registered in Django DATABASES."
                        ),
                    )
                    messages.success(
                        request,
                        "DR database settings saved. Restart the application so the DR connection becomes active in Django settings.",
                    )
                    return redirect(f"{reverse('user_settings_system')}?tab=database")

        if action == "dr_test_complete":
            pass
        elif not system_settings_available:
            messages.error(
                request,
                "The shared Users system settings tables are not available yet. Apply the latest Users migration first.",
            )
            return redirect("user_settings_system")

        elif not _can_manage_system_settings(request.user):
            messages.error(request, "You do not have permission to change system settings.")
            return redirect("user_settings_system")

        else:
            form = SystemSettingsForm(
                _merge_system_settings_post_data(request.POST, runtime_settings),
                instance=runtime_settings,
            )
            if form.is_valid():
                saved_settings = form.save(commit=False)
                saved_settings.microsoft_auth_mode = SystemSetting.MICROSOFT_AUTH_MODE_SIMULATED
                saved_settings.microsoft_tenant_id = ""
                saved_settings.microsoft_client_id = ""
                saved_settings.microsoft_redirect_uri = ""
                saved_settings.save()
                clear_runtime_caches()
                apply_runtime_security_settings()
                messages.success(request, "System settings were updated successfully.")
                return redirect("user_settings_system")
    context = _build_settings_context(
        request,
        active_section="system_settings",
        page_title="System Settings",
        page_intro="Control idle logout, session duration, login behavior, and self-service account controls from one shared workspace.",
    )
    context.update(
        {
            "system_form": form,
            "runtime_settings": runtime_settings,
            "system_settings_available": system_settings_available,
            "backend_state": backend_state,
            "system_active_tab": system_active_tab,
            "can_manage_system_settings": _can_manage_system_settings(request.user),
            "can_manage_database_setting": request.user.is_superuser or request.user.has_perm("Users.can_manage_settings_database"),
        }
    )
    return render(request, "users/settings_workspace.html", context)


@login_required
def user_settings_audit_trail_view(request):
    if not _can_view_settings_audit_trail(request.user):
        messages.error(request, "You do not have permission to view audit trail settings.")
        return redirect("modules_home")

    audit_trail_options = _build_settings_audit_trail_options(request.user)
    context = _build_settings_context(
        request,
        active_section="audit_trail",
        page_title="Audit Trail",
        page_intro="Choose the module audit register you want to inspect from one controlled settings workspace.",
    )
    context.update(
        {
            "audit_trail_options": audit_trail_options,
            "audit_trail_option_count": len(audit_trail_options),
        }
    )
    return render(request, "users/settings_workspace.html", context)


@login_required
def user_settings_access_logs_view(request):
    if not _can_view_access_logs(request.user):
        messages.error(request, "You do not have permission to view access logs.")
        return redirect("modules_home")

    try:
        _safe_ensure_access_log_security_roles()
    except DatabaseError:
        pass

    access_log_view = _normalize_access_log_view(request.GET.get("log_view"))
    selected_user_id = (request.GET.get("user") or "").strip()
    selected_access_log_user = _get_selected_access_log_user(selected_user_id)
    selected_end_reason = (request.GET.get("end_reason") or "").strip()
    search_query = (request.GET.get("search") or request.GET.get("username") or "").strip()
    selected_user_activity_state = (request.GET.get("user_activity") or "").strip().lower()

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "clear_access_attempts":
            if not _can_clear_access_attempts(request.user):
                messages.error(request, "You do not have permission to clear access attempts.")
                return redirect(_build_access_logs_view_url("attempts", selected_user_id=selected_user_id, search_query=search_query))
            if AccessAttempt is None:
                messages.warning(request, "Access attempt logs are not available in the current environment.")
                return redirect(_build_access_logs_view_url("attempts", selected_user_id=selected_user_id, search_query=search_query))
            try:
                attempts_qs = AccessAttempt.objects.order_by("-attempt_time", "-id")
                attempts_qs = _filter_access_username_queryset_by_user(attempts_qs, selected_access_log_user)
                if search_query:
                    attempts_qs = attempts_qs.filter(username__icontains=search_query)
                deleted_count, _ = attempts_qs.delete()
                save_audit_trail(
                    request.user,
                    "AccessAttempt",
                    "delete",
                    None,
                    "Cleared access attempts from the Access Logs settings workspace."
                    if not search_query
                    else f"Cleared filtered access attempts matching '{search_query}'.",
                )
                if search_query:
                    messages.success(request, f"Cleared {deleted_count} access-attempt records matching '{search_query}'.")
                else:
                    messages.success(request, f"Cleared {deleted_count} access-attempt records.")
            except DatabaseError:
                messages.warning(request, "Access attempt logs could not be cleared from the current database.")
            return redirect(_build_access_logs_view_url("attempts", selected_user_id=selected_user_id, search_query=search_query))
        if action == "reset_user_lockout":
            if not _can_reset_user_lockout(request.user):
                messages.error(request, "You do not have permission to reset user lockout state.")
                return redirect(
                    _build_access_logs_view_url(
                        "users",
                        search_query=search_query,
                        user_activity_state=selected_user_activity_state,
                    )
                )
            target_user_id = (request.POST.get("target_user_id") or "").strip()
            target_user = CustomUser.objects.filter(pk=target_user_id).first()
            if target_user is None:
                messages.warning(request, "The selected user could not be found.")
                return redirect(
                    _build_access_logs_view_url(
                        "users",
                        search_query=search_query,
                        user_activity_state=selected_user_activity_state,
                    )
                )

            _release_user_lockout_by_admin(target_user)
            save_audit_trail(
                request.user,
                "CustomUser",
                "update",
                target_user.pk,
                f"Released lockout state for user {target_user.email} from the Access Logs workspace and armed permanent lock on the next failed password while preserving history.",
            )
            messages.success(
                request,
                f"Released {target_user.email}. History was preserved, and the next failed password will require another administrator reset.",
            )
            return redirect(
                _build_access_logs_view_url(
                    "users",
                    search_query=search_query,
                    user_activity_state=selected_user_activity_state,
                )
            )

    session_log_available = True
    attempt_log_available = True
    failure_log_available = True
    user_roster_tracking_available = True
    history_log_available = True

    session_rows = []
    attempt_rows = []
    failure_rows = []
    user_roster_rows = []
    access_history_rows = []
    session_metrics = {
        "total_sessions": 0,
        "active_sessions": 0,
        "manual_sessions": 0,
        "timeout_sessions": 0,
    }
    attempt_metrics = {
        "total_attempts": 0,
        "unique_usernames": 0,
        "latest_failures": 0,
    }
    failure_metrics = {
        "total_failures": 0,
        "locked_out_failures": 0,
        "unique_usernames": 0,
    }
    user_roster_metrics = {
        "total_users": 0,
        "currently_active_users": 0,
        "never_logged_in_users": 0,
    }
    history_metrics = {
        "events_in_scope": 0,
        "session_events": 0,
        "attempt_events": 0,
        "failure_events": 0,
    }

    try:
        access_logs_qs = UserAccessLog.objects.select_related("user").order_by("-login_time", "-id")
        if selected_access_log_user:
            access_logs_qs = access_logs_qs.filter(user=selected_access_log_user)
        if selected_end_reason:
            access_logs_qs = access_logs_qs.filter(end_reason=selected_end_reason)

        session_rows = list(access_logs_qs[:120])
        session_metrics = UserAccessLog.objects.aggregate(
            total_sessions=Count("id"),
            active_sessions=Count("id", filter=Q(end_reason=UserAccessLog.END_REASON_ACTIVE)),
            manual_sessions=Count("id", filter=Q(end_reason=UserAccessLog.END_REASON_MANUAL_LOGOUT)),
            timeout_sessions=Count(
                "id",
                filter=Q(end_reason__in=[UserAccessLog.END_REASON_IDLE_TIMEOUT, UserAccessLog.END_REASON_ABSOLUTE_TIMEOUT]),
            ),
        )
    except DatabaseError:
        session_log_available = False
        if access_log_view == "sessions":
            messages.warning(
                request,
                "Session log storage is not available yet in this database. Apply the latest Users migration to enable session history.",
            )

    if AccessAttempt is None:
        attempt_log_available = False
    else:
        try:
            attempt_qs = AccessAttempt.objects.order_by("-attempt_time", "-id")
            attempt_qs = _filter_access_username_queryset_by_user(attempt_qs, selected_access_log_user)
            if search_query:
                attempt_qs = attempt_qs.filter(username__icontains=search_query)
            attempt_rows = list(attempt_qs[:120])
            attempt_metrics = {
                "total_attempts": AccessAttempt.objects.count(),
                "unique_usernames": AccessAttempt.objects.exclude(username__isnull=True).exclude(username="").values("username").distinct().count(),
                "latest_failures": AccessAttempt.objects.aggregate(max_failures=Count("id", filter=Q(failures_since_start__gt=0))).get("max_failures", 0),
            }
        except DatabaseError:
            attempt_log_available = False
            if access_log_view == "attempts":
                messages.warning(
                    request,
                    "Access attempt logs are not available in the current database yet.",
                )

    if AccessFailureLog is None:
        failure_log_available = False
    else:
        try:
            failure_qs = AccessFailureLog.objects.order_by("-attempt_time", "-id")
            failure_qs = _filter_access_username_queryset_by_user(failure_qs, selected_access_log_user)
            if search_query:
                failure_qs = failure_qs.filter(username__icontains=search_query)
            failure_rows = list(failure_qs[:120])
            failure_metrics = {
                "total_failures": AccessFailureLog.objects.count(),
                "locked_out_failures": AccessFailureLog.objects.filter(locked_out=True).count(),
                "unique_usernames": AccessFailureLog.objects.exclude(username__isnull=True).exclude(username="").values("username").distinct().count(),
            }
        except DatabaseError:
            failure_log_available = False
            if access_log_view == "failures":
                messages.warning(
                    request,
                    "Access failure logs are not available in the current database yet.",
                )

    user_roster_base_qs = CustomUser.objects.all().order_by("email")
    if selected_access_log_user:
        user_roster_base_qs = user_roster_base_qs.filter(pk=selected_access_log_user.pk)
    if search_query:
        user_roster_base_qs = user_roster_base_qs.filter(
            Q(email__icontains=search_query)
            | Q(name__icontains=search_query)
            | Q(surname__icontains=search_query)
            | Q(department__icontains=search_query)
        )

    try:
        user_roster_base_qs = user_roster_base_qs.annotate(
            active_session_count=Count(
                "access_log_entries",
                filter=Q(
                    access_log_entries__end_reason=UserAccessLog.END_REASON_ACTIVE,
                    access_log_entries__logout_time__isnull=True,
                ),
                distinct=True,
            )
        )
        if selected_user_activity_state == "active":
            user_roster_base_qs = user_roster_base_qs.filter(active_session_count__gt=0)
        elif selected_user_activity_state == "inactive":
            user_roster_base_qs = user_roster_base_qs.filter(active_session_count=0)
        elif selected_user_activity_state == "never_logged":
            user_roster_base_qs = user_roster_base_qs.filter(last_login__isnull=True)

        user_roster_metrics = {
            "total_users": user_roster_base_qs.count(),
            "currently_active_users": user_roster_base_qs.filter(active_session_count__gt=0).count(),
            "never_logged_in_users": user_roster_base_qs.filter(last_login__isnull=True).count(),
        }
        user_roster_rows = list(user_roster_base_qs[:200])
    except DatabaseError:
        user_roster_tracking_available = False
        if selected_user_activity_state == "never_logged":
            user_roster_base_qs = user_roster_base_qs.filter(last_login__isnull=True)
        elif selected_user_activity_state in {"active", "inactive"}:
            user_roster_base_qs = user_roster_base_qs.none()

        user_roster_metrics = {
            "total_users": user_roster_base_qs.count(),
            "currently_active_users": 0,
            "never_logged_in_users": user_roster_base_qs.filter(last_login__isnull=True).count(),
        }
        user_roster_rows = list(user_roster_base_qs[:200])
        if access_log_view == "users":
            messages.warning(
                request,
                "Current-session tracking is not available yet in this database. User account details are still shown below.",
            )

    history_session_rows = []
    history_attempt_rows = []
    history_failure_rows = []

    try:
        history_session_qs = UserAccessLog.objects.select_related("user").order_by("-login_time", "-id")
        if selected_access_log_user:
            history_session_qs = history_session_qs.filter(user=selected_access_log_user)
        if search_query:
            history_session_qs = history_session_qs.filter(
                Q(user__email__icontains=search_query)
                | Q(user__name__icontains=search_query)
                | Q(user__surname__icontains=search_query)
            )
        history_session_rows = list(history_session_qs[:120])
    except DatabaseError:
        if access_log_view == "history" and not AccessAttempt and not AccessFailureLog:
            history_log_available = False

    if AccessAttempt is not None:
        try:
            history_attempt_qs = AccessAttempt.objects.order_by("-attempt_time", "-id")
            history_attempt_qs = _filter_access_username_queryset_by_user(history_attempt_qs, selected_access_log_user)
            if search_query:
                history_attempt_qs = history_attempt_qs.filter(username__icontains=search_query)
            history_attempt_rows = list(history_attempt_qs[:120])
        except DatabaseError:
            if access_log_view == "history" and not session_log_available and AccessFailureLog is None:
                history_log_available = False

    if AccessFailureLog is not None:
        try:
            history_failure_qs = AccessFailureLog.objects.order_by("-attempt_time", "-id")
            history_failure_qs = _filter_access_username_queryset_by_user(history_failure_qs, selected_access_log_user)
            if search_query:
                history_failure_qs = history_failure_qs.filter(username__icontains=search_query)
            history_failure_rows = list(history_failure_qs[:120])
        except DatabaseError:
            if access_log_view == "history" and not session_log_available and AccessAttempt is None:
                history_log_available = False

    for row in history_session_rows:
        access_history_rows.append(
            {
                "event_time": row.login_time,
                "event_type": "Session",
                "actor": row.user.email,
                "secondary": f"{row.user.name} {row.user.surname}".strip(),
                "outcome": row.get_end_reason_display(),
                "status_class": "status-active" if row.end_reason == UserAccessLog.END_REASON_ACTIVE else ("status-manual" if row.end_reason == UserAccessLog.END_REASON_MANUAL_LOGOUT else "status-timeout"),
                "details": f"Started from {row.ip_address or 'unknown IP'}",
                "source": "Authenticated Session",
            }
        )
        if row.logout_time:
            access_history_rows.append(
                {
                    "event_time": row.logout_time,
                    "event_type": "Session End",
                    "actor": row.user.email,
                    "secondary": f"{row.user.name} {row.user.surname}".strip(),
                    "outcome": row.get_end_reason_display(),
                    "status_class": "status-manual" if row.end_reason == UserAccessLog.END_REASON_MANUAL_LOGOUT else "status-timeout",
                    "details": f"Session duration {row.duration_display}",
                    "source": "Authenticated Session",
                }
            )

    for row in history_attempt_rows:
        access_history_rows.append(
            {
                "event_time": row.attempt_time,
                "event_type": "Access Attempt",
                "actor": row.username or "-",
                "secondary": row.ip_address or "-",
                "outcome": "Failed Attempt" if (row.failures_since_start or 0) > 0 else "Attempt Recorded",
                "status_class": "status-timeout" if (row.failures_since_start or 0) > 0 else "",
                "details": f"Failures since start: {row.failures_since_start}",
                "source": row.path_info or "-",
            }
        )

    for row in history_failure_rows:
        access_history_rows.append(
            {
                "event_time": row.attempt_time,
                "event_type": "Access Failure",
                "actor": row.username or "-",
                "secondary": row.ip_address or "-",
                "outcome": "Locked Out" if row.locked_out else "Failed Sign-In",
                "status_class": "status-timeout",
                "details": row.user_agent or "Failure recorded by access-control layer",
                "source": row.path_info or "-",
            }
        )

    access_history_rows.sort(
        key=lambda item: (item["event_time"] is not None, item["event_time"]),
        reverse=True,
    )
    access_history_rows = access_history_rows[:220]
    history_metrics = {
        "events_in_scope": len(access_history_rows),
        "session_events": sum(1 for item in access_history_rows if item["event_type"] in {"Session", "Session End"}),
        "attempt_events": sum(1 for item in access_history_rows if item["event_type"] == "Access Attempt"),
        "failure_events": sum(1 for item in access_history_rows if item["event_type"] == "Access Failure"),
    }
    if access_log_view == "history" and not access_history_rows and not (session_log_available or attempt_log_available or failure_log_available):
        history_log_available = False

    context = _build_settings_context(
        request,
        active_section="access_logs",
        page_title="Access Logs",
        page_intro="Review authenticated sessions, monitor access attempts, inspect failed sign-ins, review user login status, and open one combined access history whenever you need an audit extract.",
    )
    context.update(
        {
            "access_log_view": access_log_view,
            "session_log_rows": session_rows,
            "attempt_log_rows": attempt_rows,
            "failure_log_rows": failure_rows,
            "user_roster_rows": user_roster_rows,
            "access_history_rows": access_history_rows,
            "access_log_user_choices": CustomUser.objects.order_by("email"),
            "selected_access_log_user": selected_user_id,
            "selected_access_log_end_reason": selected_end_reason,
            "access_log_search_query": search_query,
            "selected_user_activity_state": selected_user_activity_state,
            "session_log_metrics": session_metrics,
            "attempt_log_metrics": attempt_metrics,
            "failure_log_metrics": failure_metrics,
            "user_roster_metrics": user_roster_metrics,
            "history_metrics": history_metrics,
            "access_log_reason_choices": UserAccessLog.END_REASON_CHOICES,
            "user_roster_activity_choices": (
                ("all", "All users"),
                ("active", "Currently active"),
                ("inactive", "Not currently active"),
                ("never_logged", "Never logged in"),
            ),
            "access_log_now": timezone.now(),
            "session_log_available": session_log_available,
            "attempt_log_available": attempt_log_available,
            "failure_log_available": failure_log_available,
            "user_roster_tracking_available": user_roster_tracking_available,
            "history_log_available": history_log_available,
            "can_clear_access_attempts": _can_clear_access_attempts(request.user),
            "can_reset_user_lockout": _can_reset_user_lockout(request.user),
            "access_log_export_url": _build_access_logs_export_url(
                access_log_view,
                selected_user_id=selected_user_id,
                selected_end_reason=selected_end_reason,
                search_query=search_query,
                user_activity_state=selected_user_activity_state,
            ),
        }
    )
    return render(request, "users/settings_workspace.html", context)


@login_required
def user_settings_access_logs_download_view(request):
    if not _can_view_access_logs(request.user):
        messages.error(request, "You do not have permission to download access logs.")
        return redirect("modules_home")

    access_log_view = _normalize_access_log_view(request.GET.get("log_view"))
    selected_user_id = (request.GET.get("user") or "").strip()
    selected_access_log_user = _get_selected_access_log_user(selected_user_id)
    selected_end_reason = (request.GET.get("end_reason") or "").strip()
    search_query = (request.GET.get("search") or request.GET.get("username") or "").strip()
    selected_user_activity_state = (request.GET.get("user_activity") or "").strip().lower()

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{access_log_view}_access_logs.csv"'
    writer = csv.writer(response)

    try:
        if access_log_view == "sessions":
            queryset = UserAccessLog.objects.select_related("user").order_by("-login_time", "-id")
            if selected_access_log_user:
                queryset = queryset.filter(user=selected_access_log_user)
            if selected_end_reason:
                queryset = queryset.filter(end_reason=selected_end_reason)

            writer.writerow(["User", "Email", "Login Time", "Logout Time", "Session Duration (seconds)", "Session End", "IP Address", "User Agent"])
            for row in queryset[:5000]:
                writer.writerow([
                    f"{row.user.name} {row.user.surname}".strip(),
                    row.user.email,
                    row.login_time.strftime("%Y-%m-%d %H:%M:%S") if row.login_time else "",
                    row.logout_time.strftime("%Y-%m-%d %H:%M:%S") if row.logout_time else "",
                    row.session_duration_seconds if row.session_duration_seconds is not None else "",
                    row.get_end_reason_display(),
                    row.ip_address or "",
                    row.user_agent or "",
                ])
        elif access_log_view == "attempts" and AccessAttempt is not None:
            queryset = AccessAttempt.objects.order_by("-attempt_time", "-id")
            queryset = _filter_access_username_queryset_by_user(queryset, selected_access_log_user)
            if search_query:
                queryset = queryset.filter(username__icontains=search_query)

            writer.writerow(["Username", "Attempt Time", "Failures Since Start", "IP Address", "Path", "User Agent"])
            for row in queryset[:5000]:
                writer.writerow([
                    row.username or "",
                    row.attempt_time.strftime("%Y-%m-%d %H:%M:%S") if row.attempt_time else "",
                    row.failures_since_start,
                    row.ip_address or "",
                    row.path_info or "",
                    row.user_agent or "",
                ])
        elif access_log_view == "failures" and AccessFailureLog is not None:
            queryset = AccessFailureLog.objects.order_by("-attempt_time", "-id")
            queryset = _filter_access_username_queryset_by_user(queryset, selected_access_log_user)
            if search_query:
                queryset = queryset.filter(username__icontains=search_query)

            writer.writerow(["Username", "Failure Time", "Locked Out", "IP Address", "Path", "User Agent"])
            for row in queryset[:5000]:
                writer.writerow([
                    row.username or "",
                    row.attempt_time.strftime("%Y-%m-%d %H:%M:%S") if row.attempt_time else "",
                    "Yes" if row.locked_out else "No",
                    row.ip_address or "",
                    row.path_info or "",
                    row.user_agent or "",
                ])
        elif access_log_view == "history":
            history_export_rows = []

            session_queryset = UserAccessLog.objects.select_related("user").order_by("-login_time", "-id")
            if selected_access_log_user:
                session_queryset = session_queryset.filter(user=selected_access_log_user)
            if search_query:
                session_queryset = session_queryset.filter(
                    Q(user__email__icontains=search_query)
                    | Q(user__name__icontains=search_query)
                    | Q(user__surname__icontains=search_query)
                )
            for row in session_queryset[:2000]:
                history_export_rows.append([
                    row.login_time.strftime("%Y-%m-%d %H:%M:%S") if row.login_time else "",
                    "Session",
                    row.user.email,
                    f"{row.user.name} {row.user.surname}".strip(),
                    row.get_end_reason_display(),
                    f"Started from {row.ip_address or 'unknown IP'}",
                    "Authenticated Session",
                ])
                if row.logout_time:
                    history_export_rows.append([
                        row.logout_time.strftime("%Y-%m-%d %H:%M:%S") if row.logout_time else "",
                        "Session End",
                        row.user.email,
                        f"{row.user.name} {row.user.surname}".strip(),
                        row.get_end_reason_display(),
                        f"Session duration {row.duration_display}",
                        "Authenticated Session",
                    ])

            if AccessAttempt is not None:
                attempt_queryset = AccessAttempt.objects.order_by("-attempt_time", "-id")
                attempt_queryset = _filter_access_username_queryset_by_user(attempt_queryset, selected_access_log_user)
                if search_query:
                    attempt_queryset = attempt_queryset.filter(username__icontains=search_query)
                for row in attempt_queryset[:2000]:
                    history_export_rows.append([
                        row.attempt_time.strftime("%Y-%m-%d %H:%M:%S") if row.attempt_time else "",
                        "Access Attempt",
                        row.username or "",
                        row.ip_address or "",
                        "Failed Attempt" if (row.failures_since_start or 0) > 0 else "Attempt Recorded",
                        f"Failures since start: {row.failures_since_start}",
                        row.path_info or "",
                    ])

            if AccessFailureLog is not None:
                failure_queryset = AccessFailureLog.objects.order_by("-attempt_time", "-id")
                failure_queryset = _filter_access_username_queryset_by_user(failure_queryset, selected_access_log_user)
                if search_query:
                    failure_queryset = failure_queryset.filter(username__icontains=search_query)
                for row in failure_queryset[:2000]:
                    history_export_rows.append([
                        row.attempt_time.strftime("%Y-%m-%d %H:%M:%S") if row.attempt_time else "",
                        "Access Failure",
                        row.username or "",
                        row.ip_address or "",
                        "Locked Out" if row.locked_out else "Failed Sign-In",
                        row.user_agent or "Failure recorded by access-control layer",
                        row.path_info or "",
                    ])

            history_export_rows.sort(key=lambda row: row[0], reverse=True)
            writer.writerow(["Event Time", "Event Type", "Actor", "Secondary", "Outcome", "Details", "Source"])
            for row in history_export_rows[:5000]:
                writer.writerow(row)
        elif access_log_view == "users":
            queryset = CustomUser.objects.all().order_by("email")
            if selected_access_log_user:
                queryset = queryset.filter(pk=selected_access_log_user.pk)
            if search_query:
                queryset = queryset.filter(
                    Q(email__icontains=search_query)
                    | Q(name__icontains=search_query)
                    | Q(surname__icontains=search_query)
                    | Q(department__icontains=search_query)
                )

            roster_tracking_available = True
            try:
                queryset = queryset.annotate(
                    active_session_count=Count(
                        "access_log_entries",
                        filter=Q(
                            access_log_entries__end_reason=UserAccessLog.END_REASON_ACTIVE,
                            access_log_entries__logout_time__isnull=True,
                        ),
                        distinct=True,
                    )
                )
                if selected_user_activity_state == "active":
                    queryset = queryset.filter(active_session_count__gt=0)
                elif selected_user_activity_state == "inactive":
                    queryset = queryset.filter(active_session_count=0)
                elif selected_user_activity_state == "never_logged":
                    queryset = queryset.filter(last_login__isnull=True)
            except DatabaseError:
                roster_tracking_available = False
                if selected_user_activity_state == "never_logged":
                    queryset = queryset.filter(last_login__isnull=True)
                elif selected_user_activity_state in {"active", "inactive"}:
                    queryset = queryset.none()

            writer.writerow(["Name", "Email", "Department", "Account Status", "Currently Logged In", "Last Login", "Joined"])
            for row in queryset[:5000]:
                if roster_tracking_available:
                    current_status = "Yes" if getattr(row, "active_session_count", 0) > 0 else "No"
                else:
                    current_status = "Unavailable"
                writer.writerow([
                    f"{row.name} {row.surname}".strip(),
                    row.email,
                    row.department or "",
                    "Active" if row.is_active else "Disabled",
                    current_status,
                    row.last_login.strftime("%Y-%m-%d %H:%M:%S") if row.last_login else "",
                    row.date_joined.strftime("%Y-%m-%d %H:%M:%S") if row.date_joined else "",
                ])
        else:
            messages.warning(request, "The requested access-log dataset is not available for download.")
            return redirect("user_settings_access_logs")
    except DatabaseError:
        messages.warning(request, "The selected access-log dataset is not available in the current database yet.")
        return redirect("user_settings_access_logs")

    return response


@login_required
def user_settings_authenticator_view(request):
    if not _can_view_authenticator_settings(request.user):
        messages.error(request, "You do not have permission to view authenticator settings.")
        return redirect("modules_home")

    runtime_settings = get_system_settings()
    admin_form = None
    selected_user = request.user
    requested_user_id = ""

    if _can_manage_authenticator_resets(request.user):
        admin_form = AuthenticatorResetForm()
        requested_user_id = request.GET.get("user")
        if requested_user_id:
            selected_user = CustomUser.objects.filter(pk=requested_user_id).first() or request.user

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "reconnect_my_authenticator":
            if not microsoft_auth_is_available(runtime_settings):
                messages.error(request, "Microsoft Authenticator is currently disabled by system settings.")
                return redirect("user_settings_authenticator")

            _reset_user_authenticator(request.user)
            _clear_microsoft_verification(request)
            _clear_pending_microsoft_auth(request)
            save_audit_trail(
                request.user,
                "MicrosoftAuthenticator",
                "update",
                request.user.pk,
                f"User reset and reconnected their own Microsoft Authenticator device for {request.user.email}.",
            )
            request.session[MICROSOFT_AUTH_PURPOSE_KEY] = "session_recheck"
            request.session[MICROSOFT_AUTH_NEXT_KEY] = reverse("user_settings_authenticator")
            messages.info(request, "Scan the QR code to reconnect your new phone to Microsoft Authenticator.")
            return redirect("microsoft_auth_start")

        if action == "reset_user_authenticator":
            if not _can_manage_authenticator_resets(request.user):
                messages.error(request, "You do not have permission to reset another user's authenticator device.")
                return redirect("user_settings_authenticator")

            admin_form = AuthenticatorResetForm(request.POST)
            if admin_form.is_valid():
                selected_user = admin_form.cleaned_data["user"]
                _reset_user_authenticator(selected_user)
                save_audit_trail(
                    request.user,
                    "MicrosoftAuthenticator",
                    "update",
                    selected_user.pk,
                    f"Administrator {request.user.email} reset Microsoft Authenticator enrollment for {selected_user.email}.",
                )
                if selected_user.pk == request.user.pk:
                    _clear_microsoft_verification(request)
                    _clear_pending_microsoft_auth(request)
                messages.success(request, f"Reset Microsoft Authenticator enrollment for {selected_user.email}.")
                return redirect(f"{reverse('user_settings_authenticator')}?user={selected_user.pk}")

    if admin_form is not None:
        admin_form.fields["user"].queryset = CustomUser.objects.order_by("email")
        admin_form.fields["user"].widget.attrs.update(
            {
                "data-user-search-select": "true",
                "data-user-search-placeholder": "Type email, name, or surname",
            }
        )
        if requested_user_id:
            admin_form.initial["user"] = selected_user

    enrolled_user_count = CustomUser.objects.filter(microsoft_authenticator_enabled=True).count()
    context = _build_settings_context(
        request,
        active_section="authenticator",
        page_title="Authenticator",
        page_intro="Reconnect your Microsoft Authenticator phone, review your enrollment status, or reset another user's device if you manage access.",
    )
    context.update(
        {
            "runtime_settings": runtime_settings,
            "authenticator_feature_enabled": microsoft_auth_is_available(runtime_settings),
            "my_authenticator_enabled": request.user.microsoft_authenticator_enabled,
            "my_authenticator_confirmed_at": request.user.microsoft_authenticator_confirmed_at,
            "my_authenticator_verified_at": read_session_timestamp(request.session.get(MICROSOFT_AUTH_VERIFIED_AT_KEY)),
            "can_manage_authenticator_resets": _can_manage_authenticator_resets(request.user),
            "authenticator_admin_form": admin_form,
            "selected_authenticator_user": selected_user,
            "selected_authenticator_enabled": getattr(selected_user, "microsoft_authenticator_enabled", False),
            "selected_authenticator_confirmed_at": getattr(selected_user, "microsoft_authenticator_confirmed_at", None),
            "enrolled_user_count": enrolled_user_count,
        }
    )
    return render(request, "users/settings_workspace.html", context)


@login_required
def update_profile(request):
    runtime_settings = get_system_settings()
    if not runtime_settings.enable_self_profile_edit:
        messages.error(request, "Profile editing is currently disabled by system settings.")
        return redirect("modules_home")

    user = request.user

    if request.method == 'POST':
        name = request.POST.get('name', user.name)
        surname = request.POST.get('surname', user.surname)
        phone_number = request.POST.get('phone_number', user.phone_number)
        gender = request.POST.get('gender', user.gender)

        # Update user fields
        user.name = name
        user.surname = surname
        user.phone_number = phone_number
        user.gender = gender
        user.save()

        messages.success(request, 'Your profile has been updated successfully.')
        return redirect('modules_home')

    return render(request, 'users/update_profile.html', {
        'user': user,
    })



@login_required
def change_password(request):
    runtime_settings = get_system_settings()
    forced_change_required = password_change_required(request.user, runtime_settings)

    if not runtime_settings.enable_self_password_change and not forced_change_required:
        messages.error(request, "Password changes are currently disabled by system settings.")
        return redirect("modules_home")

    if _microsoft_password_change_required(request, runtime_settings):
        messages.info(
            request,
            "Microsoft verification is required before you can change your password.",
        )
        query_string = urllib.parse.urlencode(
            {
                "purpose": "password_change",
                "next": reverse("change_password"),
            }
        )
        return redirect(f"{reverse('microsoft_auth_start')}?{query_string}")

    if request.method == 'POST':
        form = CustomPasswordChangeForm(request.POST, user=request.user)
        if form.is_valid():
            form.save()
            _clear_password_expiry_reminder(request)
            messages.success(request, 'Your password has been changed successfully.')
            return redirect('modules_home')
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = CustomPasswordChangeForm(user=request.user)

    return render(
        request,
        'users/change_password.html',
        {
            'form': form,
            'forced_change_required': forced_change_required,
            'runtime_settings': runtime_settings,
        },
    )


@login_required
def password_change_done(request):
    return render(request, 'users/password_change_done.html')

def custom_logout_view(request):
    popup_mode = _workspace_popup_enabled(request)
    if request.user.is_authenticated:
        close_user_session_log(request, UserAccessLog.END_REASON_MANUAL_LOGOUT)
    _clear_password_expiry_reminder(request)
    _clear_pending_microsoft_auth(request)
    _clear_microsoft_verification(request)
    _clear_microsoft_oauth_handshake(request)
    _set_workspace_popup_mode(request, False)
    logout(request)
    messages.success(request, "Successfully logged out.")
    if popup_mode:
        return redirect(reverse('login_popup'))
    return redirect(reverse('login'))


def _is_user_in_custom_lockout(user, current_time=None):
    if not user:
        return False
    current_time = current_time or timezone.now()
    lockout_until = getattr(user, "lockout_until", None)
    return bool(lockout_until and lockout_until > current_time)


def _is_user_permanently_locked(user):
    if not user:
        return False
    return bool(getattr(user, "permanently_locked", False))


def _reset_user_failed_login_state(user):
    if not user:
        return
    user.failed_login_attempts = 0
    user.lockout_until = None
    user.lock_immediately_on_next_failure = False
    user.permanently_locked = False
    user.save(update_fields=["failed_login_attempts", "lockout_until", "lock_immediately_on_next_failure", "permanently_locked"])


def _release_user_lockout_by_admin(user):
    if not user:
        return
    user.failed_login_attempts = 0
    user.lockout_until = None
    user.lock_immediately_on_next_failure = True
    user.permanently_locked = False
    user.save(update_fields=["failed_login_attempts", "lockout_until", "lock_immediately_on_next_failure", "permanently_locked"])


def _register_failed_login_attempt(user, runtime_settings):
    if not user:
        return None

    current_time = timezone.now()
    failure_limit = max(int(getattr(runtime_settings, "failed_login_limit", 3) or 3), 1)
    lockout_minutes = max(int(getattr(runtime_settings, "lockout_duration_minutes", 60) or 60), 1)
    lockout_expires_at = current_time + timedelta(minutes=lockout_minutes)

    if getattr(user, "lock_immediately_on_next_failure", False):
        user.failed_login_attempts = failure_limit
        user.lockout_until = None
        user.lock_immediately_on_next_failure = False
        user.permanently_locked = True
        user.save(update_fields=["failed_login_attempts", "lockout_until", "lock_immediately_on_next_failure", "permanently_locked"])
        return "permanent"

    prior_lockout_until = getattr(user, "lockout_until", None)
    prior_failed_attempts = int(getattr(user, "failed_login_attempts", 0) or 0)
    if prior_failed_attempts >= failure_limit and prior_lockout_until and prior_lockout_until <= current_time:
        user.failed_login_attempts = failure_limit
        user.lockout_until = None
        user.lock_immediately_on_next_failure = False
        user.permanently_locked = True
        user.save(update_fields=["failed_login_attempts", "lockout_until", "lock_immediately_on_next_failure", "permanently_locked"])
        return "permanent"

    next_attempt_total = int(getattr(user, "failed_login_attempts", 0) or 0) + 1
    user.failed_login_attempts = next_attempt_total
    if next_attempt_total >= failure_limit:
        user.lockout_until = lockout_expires_at
        user.lock_immediately_on_next_failure = False
        user.permanently_locked = False
        user.save(update_fields=["failed_login_attempts", "lockout_until", "lock_immediately_on_next_failure", "permanently_locked"])
        return "temporary"

    user.save(update_fields=["failed_login_attempts"])
    return None


def _can_view_user_roles(user):
    return user.is_superuser or user.has_perm("Users.can_view_settings_user_roles") or user.has_perm("Users.can_assign_settings_roles")


def _can_manage_user_roles(user):
    return user.is_superuser or user.has_perm("Users.can_assign_settings_roles")


def _can_view_add_users(user):
    return _can_manage_users(user)


def _can_manage_users(user):
    return user.is_superuser or user.has_perm("Users.can_assign_settings_roles") or user.has_perm("Users.can_manage_settings_database")


def _can_view_system_settings(user):
    return user.is_superuser or user.has_perm("Users.can_view_settings_database") or user.has_perm("Users.can_manage_settings_database")


def _can_manage_system_settings(user):
    return user.is_superuser or user.has_perm("Users.can_manage_settings_database")


def _can_view_access_logs(user):
    return _can_view_system_settings(user)


def _can_view_settings_audit_trail(user):
    return _can_view_system_settings(user)


def _build_settings_audit_trail_options(user):
    module_options = []
    if apps.is_installed("IFRS9"):
        module_options.append(
            {
                "key": "ifrs9",
                "title": "IFRS 9 Audit Trail",
                "label": "IFRS 9",
                "icon": "fas fa-chart-line",
                "badge": "IFRS 9 module",
                "url": "/ifrs9/settings/audit-trail/",
                "description": "Review configuration, reporting, API, staging, ECL, LGD, PD, EAD, and operations activity captured inside the IFRS 9 workspace.",
                "can_open": user.is_superuser or user.has_perm("IFRS9.can_view_audit_trail"),
                "permission_hint": "Requires IFRS 9 audit trail viewer permission.",
            }
        )
    if apps.is_installed("scorecard"):
        module_options.append(
            {
                "key": "scorecard",
                "title": "Scorecard Audit Trail",
                "label": "Scorecard",
                "icon": "fas fa-clipboard-check",
                "badge": "Scorecard module",
                "url": "/scorecard/settings/permissions/audit/",
                "description": "Review score templates, maker-checker actions, customer scoring, documents, branches, emails, API, and scorecard settings activity.",
                "can_open": user.is_superuser or user.has_perm("scorecard.view_scorecard_audit_trail"),
                "permission_hint": "Requires scorecard audit trail viewer permission.",
            }
        )
    return module_options


def _can_clear_access_attempts(user):
    return user.is_superuser or user.has_perm("Users.can_clear_access_attempts")


def _can_reset_user_lockout(user):
    return _can_manage_system_settings(user)


def _normalize_access_log_view(value):
    value = (value or "").strip().lower()
    return value if value in {"sessions", "attempts", "failures", "users", "history"} else "sessions"


def _get_selected_access_log_user(selected_user_id):
    if not selected_user_id:
        return None
    try:
        return CustomUser.objects.filter(pk=selected_user_id).first()
    except (TypeError, ValueError):
        return None


def _filter_access_username_queryset_by_user(queryset, selected_user):
    if not selected_user:
        return queryset
    email = (getattr(selected_user, "email", "") or "").strip()
    if not email:
        return queryset.none()
    return queryset.filter(username__iexact=email)


def _build_access_logs_view_url(
    log_view,
    selected_user_id="",
    selected_end_reason="",
    search_query="",
    user_activity_state="",
):
    params = {"log_view": _normalize_access_log_view(log_view)}
    if selected_user_id:
        params["user"] = selected_user_id
    if selected_end_reason:
        params["end_reason"] = selected_end_reason
    if search_query:
        params["search"] = search_query
    if user_activity_state:
        params["user_activity"] = user_activity_state
    return f"/settings/access-logs/?{urllib.parse.urlencode(params)}"


def _build_access_logs_export_url(
    log_view,
    selected_user_id="",
    selected_end_reason="",
    search_query="",
    user_activity_state="",
):
    params = {"log_view": _normalize_access_log_view(log_view)}
    if selected_user_id:
        params["user"] = selected_user_id
    if selected_end_reason:
        params["end_reason"] = selected_end_reason
    if search_query:
        params["search"] = search_query
    if user_activity_state:
        params["user_activity"] = user_activity_state
    return f"/settings/access-logs/download/?{urllib.parse.urlencode(params)}"


def _safe_ensure_access_log_security_roles():
    helper = globals().get("_ensure_access_log_security_roles")
    if callable(helper):
        return helper()

    content_type = ContentType.objects.get_for_model(CustomUser)
    permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename="can_clear_access_attempts",
        defaults={"name": "Can clear access attempts"},
    )
    cleanup_group, _ = Group.objects.get_or_create(name="Access Attempt Cleanup")
    cleanup_group.permissions.add(permission)


def _ensure_access_log_security_roles():
    content_type = ContentType.objects.get_for_model(CustomUser)
    permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename="can_clear_access_attempts",
        defaults={"name": "Can clear access attempts"},
    )
    cleanup_group, _ = Group.objects.get_or_create(name="Access Attempt Cleanup")
    cleanup_group.permissions.add(permission)


def _can_view_authenticator_settings(user):
    return user.is_authenticated


def _can_manage_authenticator_resets(user):
    return _can_manage_system_settings(user)


def _can_open_users_settings(user):
    return user.is_authenticated


def _build_settings_context(request, active_section, page_title, page_intro):
    nav_items = []
    if _can_view_add_users(request.user):
        nav_items.append(
            {
                "label": "Add Users",
                "icon": "fas fa-user-plus",
                "url": reverse("user_settings_add_user"),
                "key": "add_users",
            }
        )
    if _can_view_authenticator_settings(request.user):
        nav_items.append(
            {
                "label": "Authenticator",
                "icon": "fas fa-mobile-screen-button",
                "url": reverse("user_settings_authenticator"),
                "key": "authenticator",
            }
        )
    if _can_view_user_roles(request.user):
        nav_items.append(
            {
                "label": "User Access",
                "icon": "fas fa-users-cog",
                "url": reverse("user_settings_roles"),
                "key": "user_roles",
            }
        )
    if _can_view_access_logs(request.user):
        nav_items.append(
            {
                "label": "Access Logs",
                "icon": "fas fa-clock-rotate-left",
                "url": reverse("user_settings_access_logs"),
                "key": "access_logs",
            }
        )
    if _can_view_system_settings(request.user):
        nav_items.append(
            {
                "label": "System Settings",
                "icon": "fas fa-sliders-h",
                "url": reverse("user_settings_system"),
                "key": "system_settings",
            }
        )
    if _can_view_settings_audit_trail(request.user):
        nav_items.append(
            {
                "label": "Audit Trail",
                "icon": "fas fa-clipboard-list",
                "url": reverse("user_settings_audit_trail"),
                "key": "audit_trail",
            }
        )

    return {
        "app_version": get_app_version(),
        "settings_nav_items": nav_items,
        "active_section": active_section,
        "page_title": page_title,
        "page_intro": page_intro,
        "can_open_users_settings": _can_open_users_settings(request.user),
    }


def _supported_database_choices():
    return tuple(getattr(settings, "SUPPORTED_DATABASE_VENDORS", ("oracle", "mssql", "postgresql")))


def _load_database_backend_state():
    supported_vendors = _supported_database_choices()
    fallback_vendor = getattr(settings, "DATABASE_VENDOR", supported_vendors[0])
    runtime_config = load_runtime_database_config(
        settings.BASE_DIR,
        fallback_vendor,
        supported_vendors,
    )
    effective_vendor = getattr(settings, "DATABASE_VENDOR", runtime_config["database_vendor"])
    effective_functions_backend = getattr(
        settings,
        "FUNCTIONS_DB_BACKEND",
        runtime_config["functions_db_backend"],
    )
    return {
        "supported_vendors": supported_vendors,
        "saved_database_vendor": runtime_config["database_vendor"],
        "saved_functions_backend": runtime_config["functions_db_backend"],
        "effective_database_vendor": effective_vendor,
        "effective_functions_backend": effective_functions_backend,
        "config_path": runtime_config["path"],
        "config_source": getattr(settings, "DATABASE_RUNTIME_CONFIG_SOURCE", runtime_config["source"]),
        "dr_database": runtime_config.get("dr_database", {}),
        "dr_connection_active": "dr" in getattr(settings, "DATABASES", {}),
        "dr_backup_methods": (
            ("sql_server_native", "SQL Server native backup/restore"),
            ("scheduled_copy", "Scheduled table copy into DR database"),
            ("manual_copy", "Manual copy on demand"),
        ),
        "dr_backup_frequencies": (
            ("manual", "Manual only"),
            ("hourly", "Hourly"),
            ("daily", "Daily"),
            ("weekly", "Weekly"),
        ),
        "dr_table_scopes": (
            ("full_database", "Full database"),
            ("ifrs9_core", "IFRS9 core tables only"),
            ("staging_and_reporting", "Staging and reporting tables"),
        ),
    }


def _normalize_system_settings_tab(value):
    value = (value or "").strip().lower().replace("_", "-")
    aliases = {
        "overview": "system-overview",
        "session": "system-session",
        "security": "system-security",
        "self-service": "system-self-service",
        "password": "system-password",
        "microsoft-auth": "system-microsoft-auth",
        "database": "system-database",
    }
    if value in aliases.values():
        return value
    return aliases.get(value, "system-overview")


def _dr_database_payload_from_post(post_data, current_config):
    posted_password = (post_data.get("dr_password") or "").strip()
    return {
        "enabled": post_data.get("dr_enabled") == "on",
        "engine": (post_data.get("dr_engine") or "mssql").strip(),
        "name": (post_data.get("dr_name") or "").strip(),
        "user": (post_data.get("dr_user") or "").strip(),
        "password": posted_password or current_config.get("password", ""),
        "host": (post_data.get("dr_host") or "").strip(),
        "port": (post_data.get("dr_port") or "1433").strip(),
        "driver": (post_data.get("dr_driver") or "ODBC Driver 17 for SQL Server").strip(),
        "extra_params": (post_data.get("dr_extra_params") or "Encrypt=no;TrustServerCertificate=yes").strip(),
        "backup_method": (post_data.get("dr_backup_method") or "sql_server_native").strip(),
        "backup_frequency": (post_data.get("dr_backup_frequency") or "daily").strip(),
        "backup_window": (post_data.get("dr_backup_window") or "22:00").strip(),
        "table_scope": (post_data.get("dr_table_scope") or "full_database").strip(),
    }


def _dr_database_engine_name(engine):
    engine = (engine or "mssql").strip().lower()
    if engine == "postgresql":
        return "django.db.backends.postgresql"
    if engine == "oracle":
        return "django.db.backends.oracle"
    return "mssql"


def _dr_database_settings_from_payload(dr_payload):
    return {
        "ENGINE": _dr_database_engine_name(dr_payload.get("engine")),
        "NAME": dr_payload.get("name", ""),
        "USER": dr_payload.get("user", ""),
        "PASSWORD": dr_payload.get("password", ""),
        "HOST": dr_payload.get("host", ""),
        "PORT": dr_payload.get("port", "1433"),
        "OPTIONS": {
            "driver": dr_payload.get("driver", "ODBC Driver 17 for SQL Server"),
            "extra_params": dr_payload.get("extra_params", "Encrypt=no;TrustServerCertificate=yes"),
        },
    }


def _test_dr_database_connection(dr_payload):
    required_fields = {
        "host": "DR server IP / host",
        "name": "DR database name",
    }
    missing = [label for key, label in required_fields.items() if not dr_payload.get(key)]
    if missing:
        return {
            "ok": False,
            "title": "Connection details incomplete",
            "message": "Please enter " + ", ".join(missing) + " before testing.",
            "duration": None,
        }

    started_at = perf_counter()
    handler = ConnectionHandler(
        {
            "default": settings.DATABASES["default"],
            "dr_test": _dr_database_settings_from_payload(dr_payload),
        }
    )
    connection = None
    try:
        connection = handler["dr_test"]
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return {
            "ok": True,
            "title": "DR database reachable",
            "message": "Connection test succeeded. You can now save these DR database settings.",
            "duration": round(perf_counter() - started_at, 2),
        }
    except Exception as exc:
        return {
            "ok": False,
            "title": "DR database not reachable",
            "message": str(exc),
            "duration": round(perf_counter() - started_at, 2),
        }
    finally:
        if connection is not None:
            connection.close()


def _merge_system_settings_post_data(post_data, runtime_settings):
    payload = post_data.copy()
    baseline_form = SystemSettingsForm(instance=runtime_settings)

    for field_name, field in baseline_form.fields.items():
        if field_name in payload:
            continue

        current_value = getattr(runtime_settings, field_name, None)
        if isinstance(field, django_forms.BooleanField):
            if current_value:
                payload[field_name] = "on"
            continue

        if current_value is None:
            continue

        payload[field_name] = str(current_value)

    return payload


def _reset_user_authenticator(user):
    user.microsoft_authenticator_secret = ""
    user.microsoft_authenticator_enabled = False
    user.microsoft_authenticator_confirmed_at = None
    user.save(
        update_fields=[
            "microsoft_authenticator_secret",
            "microsoft_authenticator_enabled",
            "microsoft_authenticator_confirmed_at",
        ]
    )


def _get_safe_next_value(value):
    if value and url_has_allowed_host_and_scheme(
        value,
        allowed_hosts={host for host in settings.ALLOWED_HOSTS if host} | {"127.0.0.1", "localhost"},
        require_https=False,
    ):
        return value
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return ""


def _mark_microsoft_verified(request, email_address):
    now = timezone.now().isoformat()
    request.session[MICROSOFT_AUTH_VERIFIED_AT_KEY] = now
    request.session[MICROSOFT_AUTH_VERIFIED_EMAIL_KEY] = email_address


def _clear_microsoft_verification(request):
    request.session.pop(MICROSOFT_AUTH_VERIFIED_AT_KEY, None)
    request.session.pop(MICROSOFT_AUTH_VERIFIED_EMAIL_KEY, None)


def _clear_microsoft_oauth_handshake(request):
    request.session.pop(MICROSOFT_AUTH_PURPOSE_KEY, None)
    request.session.pop(MICROSOFT_AUTH_NEXT_KEY, None)


def _clear_pending_microsoft_auth(request):
    request.session.pop(MICROSOFT_AUTH_PENDING_USER_ID_KEY, None)
    request.session.pop(MICROSOFT_AUTH_PURPOSE_KEY, None)
    request.session.pop(MICROSOFT_AUTH_NEXT_KEY, None)


def _has_fresh_microsoft_verification(request, runtime_settings):
    if not microsoft_auth_is_available(runtime_settings):
        return False

    verified_email = request.session.get(MICROSOFT_AUTH_VERIFIED_EMAIL_KEY, "")
    if not request.user.is_authenticated or request.user.email.lower() != verified_email.lower():
        return False

    verified_at = read_session_timestamp(request.session.get(MICROSOFT_AUTH_VERIFIED_AT_KEY))
    if verified_at is None:
        return False

    if runtime_settings.microsoft_auth_enforce_periodically:
        max_age_days = max(int(runtime_settings.microsoft_auth_recheck_days or 0), 1)
        return (timezone.now() - verified_at).total_seconds() <= max_age_days * 86400

    return True


def _microsoft_password_change_required(request, runtime_settings):
    return (
        microsoft_auth_is_available(runtime_settings)
        and runtime_settings.microsoft_auth_on_password_change
        and not _has_fresh_microsoft_verification(request, runtime_settings)
    )


def _handle_authenticator_app_challenge(request, runtime_settings, purpose):
    user = _resolve_microsoft_auth_target_user(request, purpose)
    if user is None:
        messages.error(request, "The Microsoft Authenticator verification session has expired.")
        return redirect("login" if purpose == "login" else "modules_home")

    next_target = _get_safe_next_value(request.POST.get("next")) or request.session.get(MICROSOFT_AUTH_NEXT_KEY, "")
    token = (request.POST.get("token") or "").strip().replace(" ", "")
    enroll_mode = request.POST.get("challenge_mode") == "enroll" or not user.microsoft_authenticator_enabled

    if not token:
        messages.error(request, "Enter the 6-digit code from the Microsoft Authenticator app.")
        return render(
            request,
            "users/microsoft_authenticator_challenge.html",
            _build_authenticator_challenge_context(
                request,
                runtime_settings,
                purpose,
                next_target=next_target,
                force_enroll=enroll_mode,
            ),
        )

    secret = _ensure_microsoft_authenticator_secret(user)
    totp = pyotp.TOTP(secret)
    if not totp.verify(token, valid_window=1):
        messages.error(request, "The verification code is invalid or has expired. Please try again.")
        return render(
            request,
            "users/microsoft_authenticator_challenge.html",
            _build_authenticator_challenge_context(
                request,
                runtime_settings,
                purpose,
                next_target=next_target,
                force_enroll=enroll_mode,
            ),
        )

    if enroll_mode or not user.microsoft_authenticator_enabled:
        user.microsoft_authenticator_enabled = True
        user.microsoft_authenticator_confirmed_at = timezone.now()
        user.save(update_fields=["microsoft_authenticator_secret", "microsoft_authenticator_enabled", "microsoft_authenticator_confirmed_at"])

    _mark_microsoft_verified(request, user.email)
    save_audit_trail(
        user,
        "MicrosoftAuthenticator",
        "update",
        user.pk,
        f"User completed Microsoft Authenticator verification for purpose '{purpose}'.",
    )

    if purpose == "login":
        user.backend = "Users.backends.CaseInsensitiveEmailOrAliasBackend"
        _reset_user_failed_login_state(user)
        login(request, user)
        begin_user_session_log(request, user)
        _set_workspace_popup_mode(request, True)
        _clear_pending_microsoft_auth(request)
        messages.success(request, "Microsoft Authenticator verification completed successfully.")
        if password_change_required(user, runtime_settings):
            if user.must_change_password:
                messages.warning(request, "You must change your password before continuing.")
            else:
                messages.warning(request, "Your password has expired. Please set a new password to continue.")
            return redirect(next_target or reverse("change_password"))
        _queue_password_expiry_reminder(request, user, runtime_settings)
        return redirect(next_target or get_post_login_redirect(user))

    messages.success(request, "Microsoft Authenticator verification completed successfully.")
    if purpose == "password_change":
        return redirect(next_target or reverse("change_password"))
    return redirect(next_target or reverse("modules_home"))


def _resolve_microsoft_auth_target_user(request, purpose):
    if purpose == "login":
        pending_user_id = request.session.get(MICROSOFT_AUTH_PENDING_USER_ID_KEY)
        if not pending_user_id:
            return None
        return CustomUser.objects.filter(pk=pending_user_id).first()

    if request.user.is_authenticated:
        return request.user

    return None


def _ensure_microsoft_authenticator_secret(user):
    if user.microsoft_authenticator_secret:
        return user.microsoft_authenticator_secret

    user.microsoft_authenticator_secret = pyotp.random_base32()
    user.save(update_fields=["microsoft_authenticator_secret"])
    return user.microsoft_authenticator_secret


def _build_authenticator_challenge_context(request, runtime_settings, purpose, next_target="", force_enroll=False):
    user = _resolve_microsoft_auth_target_user(request, purpose)
    if user is None:
        return {
            "purpose": purpose,
            "next_url": next_target,
            "cancel_url": _get_microsoft_auth_cancel_url(request, purpose),
            "app_version": get_app_version(),
            "challenge_mode": "verify",
        }

    next_target = next_target or request.session.get(MICROSOFT_AUTH_NEXT_KEY, "") or _get_safe_next_value(request.GET.get("next"))
    enroll_mode = force_enroll or not user.microsoft_authenticator_enabled
    otp_uri = ""
    qr_code_data_uri = ""
    manual_secret = ""

    if enroll_mode:
        secret = _ensure_microsoft_authenticator_secret(user)
        manual_secret = secret
        otp_uri = pyotp.TOTP(secret).provisioning_uri(
            name=user.email,
            issuer_name="Brain Nexus Solution",
        )
        qr_code_data_uri = _build_qr_code_data_uri(otp_uri)

    return {
        "purpose": purpose,
        "next_url": next_target,
        "cancel_url": _get_microsoft_auth_cancel_url(request, purpose),
        "runtime_settings": runtime_settings,
        "app_version": get_app_version(),
        "challenge_mode": "enroll" if enroll_mode else "verify",
        "qr_code_data_uri": qr_code_data_uri,
        "manual_secret": manual_secret,
        "user_email": user.email,
    }


def _get_microsoft_auth_cancel_url(request, purpose):
    if purpose == "password_change":
        return reverse("logout")
    if purpose == "login":
        return reverse("login_popup") if _workspace_popup_enabled(request) else reverse("login")
    return reverse("modules_home")


def _build_qr_code_data_uri(payload):
    qr_image = qrcode.make(payload)
    image_buffer = io.BytesIO()
    qr_image.save(image_buffer, format="PNG")
    encoded = base64.b64encode(image_buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{encoded}"
