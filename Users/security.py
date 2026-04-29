from django.contrib.auth.hashers import check_password
from django.utils import timezone

from .models import PasswordHistory, SystemSetting
from .runtime import get_system_settings


def password_policy_requirements(runtime_settings=None):
    runtime_settings = runtime_settings or get_system_settings()
    policy = getattr(runtime_settings, "password_policy", SystemSetting.PASSWORD_POLICY_STANDARD)

    requirements = {
        "min_length": 0,
        "require_upper": False,
        "require_lower": False,
        "require_digit": False,
        "require_special": False,
        "label": "Basic",
    }

    if policy == SystemSetting.PASSWORD_POLICY_MINIMUM:
        requirements.update(
            {
                "min_length": 8,
                "label": "Minimum",
            }
        )
    elif policy == SystemSetting.PASSWORD_POLICY_STANDARD:
        requirements.update(
            {
                "min_length": 8,
                "require_upper": True,
                "require_lower": True,
                "require_digit": True,
                "label": "Standard",
            }
        )
    elif policy == SystemSetting.PASSWORD_POLICY_STRONG:
        requirements.update(
            {
                "min_length": 12,
                "require_upper": True,
                "require_lower": True,
                "require_digit": True,
                "require_special": True,
                "label": "Strong",
            }
        )
    elif policy == SystemSetting.PASSWORD_POLICY_ENTERPRISE:
        requirements.update(
            {
                "min_length": 14,
                "require_upper": True,
                "require_lower": True,
                "require_digit": True,
                "require_special": True,
                "label": "Enterprise",
            }
        )

    return requirements


def password_policy_help_text(runtime_settings=None):
    req = password_policy_requirements(runtime_settings)
    parts = []
    if req["min_length"] > 0:
        parts.append(f"Minimum {req['min_length']} characters")
    if req["require_upper"]:
        parts.append("one uppercase letter")
    if req["require_lower"]:
        parts.append("one lowercase letter")
    if req["require_digit"]:
        parts.append("one number")
    if req["require_special"]:
        parts.append("one special character")
    history_count = int(getattr(runtime_settings or get_system_settings(), "password_history_count", 0) or 0)
    history_note = ""
    if history_count > 0:
        history_note = f" You cannot reuse your last {history_count} password{'s' if history_count != 1 else ''}."
    if not parts:
        return f"{req['label']} policy: no password restrictions." + history_note
    return f"{req['label']} policy: " + ", ".join(parts) + "." + history_note


def build_policy_compliant_temporary_password(seed_text="", runtime_settings=None):
    runtime_settings = runtime_settings or get_system_settings()
    req = password_policy_requirements(runtime_settings)

    cleaned_seed = "".join(ch for ch in (seed_text or "") if ch.isalpha())
    cleaned_seed = cleaned_seed[:8] or "NexaUser"
    cleaned_seed = cleaned_seed[0].upper() + cleaned_seed[1:].lower() if cleaned_seed else "NexaUser"

    password = f"{cleaned_seed}2026"
    if req["require_special"]:
        password += "!"

    if req["require_upper"] and not any(ch.isupper() for ch in password):
        password = "N" + password
    if req["require_lower"] and not any(ch.islower() for ch in password):
        password += "a"
    if req["require_digit"] and not any(ch.isdigit() for ch in password):
        password += "2"
    if req["require_special"] and not any(not ch.isalnum() for ch in password):
        password += "!"

    target_length = max(req["min_length"], 8)
    while len(password) < target_length:
        password += "X9!" if req["require_special"] else "X9"

    return password[: max(len(password), target_length)]


def validate_password_against_policy(password, runtime_settings=None):
    password = password or ""
    req = password_policy_requirements(runtime_settings)
    errors = []

    if len(password) < req["min_length"]:
        errors.append(f"Password must be at least {req['min_length']} characters long.")
    if req["require_upper"] and not any(ch.isupper() for ch in password):
        errors.append("Password must include at least one uppercase letter.")
    if req["require_lower"] and not any(ch.islower() for ch in password):
        errors.append("Password must include at least one lowercase letter.")
    if req["require_digit"] and not any(ch.isdigit() for ch in password):
        errors.append("Password must include at least one number.")
    if req["require_special"] and not any(not ch.isalnum() for ch in password):
        errors.append("Password must include at least one special character.")

    return errors


def validate_password_history(user, password, runtime_settings=None):
    runtime_settings = runtime_settings or get_system_settings()
    history_count = int(getattr(runtime_settings, "password_history_count", 0) or 0)
    if history_count <= 0 or user is None or not getattr(user, "pk", None):
        return []

    if getattr(user, "password", "") and check_password(password, user.password):
        return [f"Password cannot match your current password or your last {history_count} password{'s' if history_count != 1 else ''}."]

    recent_hashes = user.password_history_entries.order_by("-created_at", "-id")[:history_count]
    for entry in recent_hashes:
        if check_password(password, entry.password_hash):
            return [f"Password cannot match your last {history_count} password{'s' if history_count != 1 else ''}."]

    return []


def record_password_history(user, runtime_settings=None):
    runtime_settings = runtime_settings or get_system_settings()
    history_count = int(getattr(runtime_settings, "password_history_count", 0) or 0)
    if not getattr(user, "pk", None) or not getattr(user, "password", ""):
        return
    if history_count <= 0:
        user.password_history_entries.all().delete()
        return

    latest_entry = user.password_history_entries.order_by("-created_at", "-id").first()
    if latest_entry and latest_entry.password_hash == user.password:
        return

    PasswordHistory.objects.create(user=user, password_hash=user.password)
    keep_ids = list(
        user.password_history_entries.order_by("-created_at", "-id").values_list("id", flat=True)[:history_count]
    )
    user.password_history_entries.exclude(id__in=keep_ids).delete()


def mark_password_changed(user, force_change_next_login=False, save=True):
    user.password_changed_at = timezone.now()
    user.must_change_password = force_change_next_login
    if save:
        user.save(update_fields=["password", "password_changed_at", "must_change_password"])
        record_password_history(user)
    return user


def password_has_expired(user, runtime_settings=None):
    runtime_settings = runtime_settings or get_system_settings()
    expiry_days = int(getattr(runtime_settings, "password_expiry_days", 0) or 0)
    if expiry_days <= 0:
        return False

    changed_at = getattr(user, "password_changed_at", None)
    if not changed_at:
        return True

    return (timezone.now() - changed_at).days >= expiry_days


def password_change_required(user, runtime_settings=None):
    runtime_settings = runtime_settings or get_system_settings()
    return bool(getattr(user, "must_change_password", False) or password_has_expired(user, runtime_settings))
