from django.core.cache import cache
from django.db import DatabaseError
from django.utils import timezone

from .models import ApplicationVersion


DEFAULT_APPLICATION_NAME = "Brain Nexus Platform"
DEFAULT_APPLICATION_VERSION = "5.1.2"
DEFAULT_APPLICATION_RELEASE_TYPE = ApplicationVersion.RELEASE_TYPE_PATCH
DEFAULT_APPLICATION_PATCH_REFERENCE = "5.1.2"
DEFAULT_APPLICATION_BUILD_NUMBER = ""
DEFAULT_APPLICATION_RELEASE_NOTES = "Brain Nexus Platform module launcher release."
DEFAULT_APPLICATION_PATCHES = ""
VERSION_CACHE_KEY = "users.current_application_version"
VERSION_CACHE_SECONDS = 300


def get_configured_application_name():
    return DEFAULT_APPLICATION_NAME


def get_configured_application_version():
    return DEFAULT_APPLICATION_VERSION


def _configured_release_defaults():
    return {
        "release_type": DEFAULT_APPLICATION_RELEASE_TYPE,
        "patch_reference": DEFAULT_APPLICATION_PATCH_REFERENCE,
        "build_number": DEFAULT_APPLICATION_BUILD_NUMBER,
        "status": ApplicationVersion.STATUS_DEPLOYED,
        "is_current": True,
        "deployed_at": timezone.now(),
        "release_notes": DEFAULT_APPLICATION_RELEASE_NOTES,
        "patches_applied": DEFAULT_APPLICATION_PATCHES,
    }


def ensure_configured_application_version():
    """
    Register the configured platform version on first page load.

    This stays out of AppConfig.ready() so Django does not query the database
    during startup or migrations.
    """
    application_name = get_configured_application_name()
    version_number = get_configured_application_version()
    defaults = _configured_release_defaults()

    version_row, created = ApplicationVersion.objects.get_or_create(
        application_name=application_name,
        version_number=version_number,
        defaults=defaults,
    )

    changed_fields = []
    if not version_row.is_current:
        version_row.is_current = True
        changed_fields.append("is_current")
    if version_row.status != ApplicationVersion.STATUS_DEPLOYED:
        version_row.status = ApplicationVersion.STATUS_DEPLOYED
        changed_fields.append("status")
    if not version_row.deployed_at:
        version_row.deployed_at = timezone.now()
        changed_fields.append("deployed_at")
    if changed_fields:
        changed_fields.append("updated_at")
        version_row.save(update_fields=changed_fields)

    if created:
        cache.delete(VERSION_CACHE_KEY)
    return version_row


def get_current_application_version():
    """
    Return the current launcher/platform version.

    If the ApplicationVersion table has not been migrated yet, return the
    configured fallback so the launcher never breaks.
    """
    cached_version = cache.get(VERSION_CACHE_KEY)
    if cached_version:
        return cached_version

    fallback_version = get_configured_application_version()
    try:
        configured_row = ensure_configured_application_version()
        current_row = (
            ApplicationVersion.objects.filter(
                application_name=get_configured_application_name(),
                is_current=True,
                status=ApplicationVersion.STATUS_DEPLOYED,
            )
            .order_by("-deployed_at", "-id")
            .first()
        )
        version_number = (current_row or configured_row).version_number
    except DatabaseError:
        version_number = fallback_version
    except Exception:
        version_number = fallback_version

    cache.set(VERSION_CACHE_KEY, version_number, VERSION_CACHE_SECONDS)
    return version_number
