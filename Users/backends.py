from django.contrib.auth.backends import ModelBackend
from django.utils import timezone

from .models import CustomUser


def resolve_login_user(identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return None

    exact_match = CustomUser.objects.filter(email__iexact=identifier).order_by("id").first()
    if exact_match:
        return exact_match

    alias_matches = CustomUser.objects.filter(email__istartswith=f"{identifier}@").order_by("id")
    normalized_identifier = identifier.lower()
    for user in alias_matches:
        local_part = (user.email or "").split("@", 1)[0].lower()
        if local_part == normalized_identifier:
            return user

    return None


class CaseInsensitiveEmailOrAliasBackend(ModelBackend):
    def user_can_authenticate(self, user):
        if getattr(user, "inactivity_locked_at", None):
            return False
        if getattr(user, "permanently_locked", False):
            return False
        lockout_until = getattr(user, "lockout_until", None)
        if lockout_until and lockout_until > timezone.now():
            return False
        return super().user_can_authenticate(user)

    def authenticate(self, request, username=None, password=None, email=None, **kwargs):
        identifier = email or username or kwargs.get(CustomUser.USERNAME_FIELD)
        if not identifier or password is None:
            return None

        user = kwargs.get("resolved_user") or resolve_login_user(identifier)
        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
