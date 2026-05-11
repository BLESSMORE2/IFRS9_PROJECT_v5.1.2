import base64
import io
import os
import urllib.parse
from time import perf_counter

import pyotp
import qrcode
from django import forms as django_forms
from django.contrib import messages, auth
from django.contrib.auth import login,logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.db import DatabaseError
from django.db.utils import ConnectionHandler
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from .forms import (
    AuthenticatorResetForm,
    CustomPasswordChangeForm,
    SystemSettingsForm,
    UserModuleAccessForm,
    UserWorkspaceCreationForm,
    UserRoleAssignmentForm,
)
from .models import CustomUser, SystemModule, SystemSetting, UserModuleAccess
from .backends import resolve_login_user
from .security import password_change_required, password_policy_requirements
from django.urls import reverse
from django.conf import settings
from IFRS9.Functions_view.audit import save_audit_trail
from Loan_management_and_LLFP.runtime_database_config import (
    get_runtime_database_config_path,
    load_runtime_database_config,
    save_runtime_dr_database_config,
    save_runtime_database_config,
)
from Loan_management_and_LLFP.package_runtime import (
    get_ifrs9_package_status,
    get_scorecard_package_status,
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


def get_app_version():
    """Return current app version from IFRS9 AppVersion table, or None if not available."""
    try:
        from IFRS9.models import AppVersion
        v = AppVersion.objects.filter(is_current=True).first()
        if v:
            return v.version
        v = AppVersion.objects.order_by('-id').first()
        return v.version if v else None
    except Exception:
        return None


MICROSOFT_AUTH_PURPOSE_KEY = "users_microsoft_auth_purpose"
MICROSOFT_AUTH_NEXT_KEY = "users_microsoft_auth_next"
MICROSOFT_AUTH_PENDING_USER_ID_KEY = "users_pending_microsoft_auth_user_id"
WORKSPACE_POPUP_SESSION_KEY = "users_workspace_popup_mode"
WORKSPACE_POPUP_WINDOW_NAME = "nexaWorkspaceWindow"


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


 

def is_package_expired_or_missing():
    """Return True when the packaged IFRS9 app is unavailable for use."""
    return not get_ifrs9_package_status()["usable"]


PACKAGE_EXPIRED = is_package_expired_or_missing()



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
        _set_workspace_popup_mode(request, True)
        user = auth.authenticate(request, username=identifier, email=identifier, password=password)
        if user is not None:
            if microsoft_login_available and runtime_settings.microsoft_auth_on_login and authenticator_app_mode:
                request.session[MICROSOFT_AUTH_PENDING_USER_ID_KEY] = user.pk
                request.session[MICROSOFT_AUTH_PURPOSE_KEY] = "login"
                request.session[MICROSOFT_AUTH_NEXT_KEY] = next_target or ""
                return redirect(f"{reverse('microsoft_auth_start')}?{urllib.parse.urlencode({'popup': '1'})}")

            auth.login(request, user)
            _clear_microsoft_verification(request)
            _clear_pending_microsoft_auth(request)
            _set_workspace_popup_mode(request, True)

            if password_change_required(user, runtime_settings):
                if user.must_change_password:
                    messages.warning(request, "You must change your password before continuing.")
                else:
                    messages.warning(request, "Your password has expired. Please set a new password to continue.")
                return redirect("change_password")

            return redirect(next_target or get_post_login_redirect(user))
        else:
            if identifier and resolve_login_user(identifier) is None:
                messages.error(request, "No account found for that email or username.")
            else:
                messages.error(request, "Incorrect password.")
            if next_target:
                return redirect(f"{reverse('login_popup')}?{urllib.parse.urlencode({'next': next_target})}")
            return redirect("login_popup")

    return render(
        request,
        "users/login.html",
        {
            "app_version": get_app_version(),
            "next_url": _get_safe_next_value(request.GET.get("next")),
            "popup_mode": True,
            "login_form_action": reverse("login_popup"),
            "workspace_popup_window_name": WORKSPACE_POPUP_WINDOW_NAME,
            "microsoft_login_available": microsoft_login_available,
            "microsoft_login_required": microsoft_login_available and runtime_settings.microsoft_auth_on_login,
            "microsoft_authenticator_app_mode": authenticator_app_mode,
            "microsoft_login_url": f"{reverse('microsoft_auth_start')}?{urllib.parse.urlencode({'purpose': 'login', 'next': _get_safe_next_value(request.GET.get('next')) or '', 'popup': '1'})}",
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
    ifrs9_status = get_ifrs9_package_status()
    if not ifrs9_status["usable"]:
        messages.error(request, ifrs9_status["message"])

    scorecard_status = get_scorecard_package_status()
    if not scorecard_status["usable"]:
        messages.error(request, scorecard_status["message"])

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
    form.fields["groups"].queryset = Group.objects.order_by("name")

    if request.method == "POST":
        if not _can_manage_users(request.user):
            messages.error(request, "You do not have permission to create users.")
            return redirect("user_settings_add_user")

        form = UserWorkspaceCreationForm(request.POST)
        form.fields["groups"].queryset = Group.objects.order_by("name")
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
            "recent_users": recent_users,
            "role_count": Group.objects.count(),
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
        users_qs = CustomUser.objects.order_by("name", "surname", "email")
        groups_qs = Group.objects.order_by("name")
        modules_qs = SystemModule.objects.filter(is_active=True).order_by("display_order", "name")
        users_qs.exists()
        groups_qs.exists()
        modules_qs.exists()
        UserModuleAccess.objects.exists()
    except DatabaseError:
        messages.error(
            request,
            "The new Users settings tables are not available yet. Apply the latest Users migration first.",
        )
        return redirect("modules_home")

    selected_user = None
    requested_user_id = request.GET.get("user")

    if requested_user_id:
        selected_user = users_qs.filter(pk=requested_user_id).first()
    if selected_user is None:
        selected_user = users_qs.first()

    role_form = UserRoleAssignmentForm()
    module_form = UserModuleAccessForm()

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "assign_user_roles":
            if not _can_manage_user_roles(request.user):
                messages.error(request, "You do not have permission to change user-role assignments.")
                return redirect("user_settings_roles")

            role_form = UserRoleAssignmentForm(request.POST)
            if role_form.is_valid():
                target_user = role_form.cleaned_data["user"]
                target_user.groups.set(role_form.cleaned_data["groups"])
                messages.success(request, f"Updated role membership for {target_user.email}.")
                selected_user = target_user
                redirect_url = reverse("user_settings_roles")
                query = [f"user={target_user.pk}"]
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
                return redirect(f"{redirect_url}?user={target_user.pk}")
        else:
            role_form = UserRoleAssignmentForm()
            module_form = UserModuleAccessForm()

    role_form.fields["user"].queryset = users_qs
    role_form.fields["groups"].queryset = groups_qs
    module_form.fields["user"].queryset = users_qs
    module_form.fields["modules"].queryset = modules_qs

    if selected_user:
        role_form.initial["user"] = selected_user
        role_form.initial["groups"] = selected_user.groups.all()
        module_form.initial["user"] = selected_user
        module_form.initial["modules"] = list(
            modules_qs.filter(user_access_entries__user=selected_user, user_access_entries__can_view=True).values_list(
                "id", flat=True
            )
        )

    role_rows = []
    for group in groups_qs:
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

    user_rows = []
    for user in users_qs:
        modules = get_visible_modules_for_user(user)
        user_rows.append(
            {
                "user": user,
                "group_names": list(user.groups.order_by("name").values_list("name", flat=True)),
                "module_names": [module["name"] for module in modules],
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
            "role_rows": role_rows,
            "user_rows": user_rows,
            "selected_user": selected_user,
            "can_manage_user_roles": _can_manage_user_roles(request.user),
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
                return redirect(f"{reverse('user_settings_system')}?tab=database")

            if action == "save_dr_database_config":
                if dr_payload["enabled"]:
                    backend_state["dr_database"] = dr_payload
                    backend_state["dr_test_result"] = _test_dr_database_connection(dr_payload)
                    if not backend_state["dr_test_result"]["ok"]:
                        messages.error(request, "DR database settings were not saved because the connection test failed.")
                        action = "dr_test_complete"

                if action != "dr_test_complete":
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
def user_settings_authenticator_view(request):
    if not _can_view_authenticator_settings(request.user):
        messages.error(request, "You do not have permission to view authenticator settings.")
        return redirect("modules_home")

    runtime_settings = get_system_settings()
    admin_form = None
    selected_user = request.user

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
    _clear_pending_microsoft_auth(request)
    _clear_microsoft_verification(request)
    _clear_microsoft_oauth_handshake(request)
    _set_workspace_popup_mode(request, False)
    logout(request)
    messages.success(request, "Successfully logged out.")
    if popup_mode:
        return redirect(reverse('login_popup'))
    return redirect(reverse('login'))


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
    if _can_view_system_settings(request.user):
        nav_items.append(
            {
                "label": "System Settings",
                "icon": "fas fa-sliders-h",
                "url": reverse("user_settings_system"),
                "key": "system_settings",
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
        login(request, user)
        _clear_pending_microsoft_auth(request)
        messages.success(request, "Microsoft Authenticator verification completed successfully.")
        if password_change_required(user, runtime_settings):
            if user.must_change_password:
                messages.warning(request, "You must change your password before continuing.")
            else:
                messages.warning(request, "Your password has expired. Please set a new password to continue.")
            return redirect(next_target or reverse("change_password"))
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
        "runtime_settings": runtime_settings,
        "app_version": get_app_version(),
        "challenge_mode": "enroll" if enroll_mode else "verify",
        "qr_code_data_uri": qr_code_data_uri,
        "manual_secret": manual_secret,
        "user_email": user.email,
    }


def _build_qr_code_data_uri(payload):
    qr_image = qrcode.make(payload)
    image_buffer = io.BytesIO()
    qr_image.save(image_buffer, format="PNG")
    encoded = base64.b64encode(image_buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{encoded}"

