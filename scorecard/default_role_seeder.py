import json
from hashlib import md5

from django.contrib.auth.models import Group, Permission
from django.core.cache import cache

from scorecard.permission_catalog import DEFAULT_ROLE_DEFINITIONS


DEFAULT_ROLE_SEEDER_SIGNATURE_CACHE_KEY = "scorecard:default-role-seeder:signature"


def _current_role_definition_signature():
    payload = [
        {
            "name": definition["name"],
            "legacy_names": sorted(definition.get("legacy_names", [])),
            "permissions": sorted(definition.get("permissions", [])),
        }
        for definition in DEFAULT_ROLE_DEFINITIONS
    ]
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return md5(encoded).hexdigest()


def _seed_default_roles():
    for definition in DEFAULT_ROLE_DEFINITIONS:
        group = Group.objects.filter(name=definition["name"]).first()
        if group is None:
            for legacy_name in definition.get("legacy_names", []):
                group = Group.objects.filter(name=legacy_name).first()
                if group is not None:
                    group.name = definition["name"]
                    group.save(update_fields=["name"])
                    break
        if group is None:
            group = Group.objects.create(name=definition["name"])
        permissions = list(
            Permission.objects.filter(
                content_type__app_label="scorecard",
                codename__in=[item.split(".", 1)[1] for item in definition["permissions"]],
            )
        )
        group.permissions.set(permissions)


def ensure_default_role_seeder_synced(force=False):
    signature = _current_role_definition_signature()
    if not force and cache.get(DEFAULT_ROLE_SEEDER_SIGNATURE_CACHE_KEY) == signature:
        return
    _seed_default_roles()
    cache.set(DEFAULT_ROLE_SEEDER_SIGNATURE_CACHE_KEY, signature, None)


def run_default_role_seeder(sender, **kwargs):
    ensure_default_role_seeder_synced(force=True)
