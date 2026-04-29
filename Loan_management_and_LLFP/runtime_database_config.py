import json
from pathlib import Path


RUNTIME_DATABASE_CONFIG_FILENAME = "runtime_database_config.json"


def _normalise_vendor(value, supported_vendors, fallback_vendor):
    candidate = (value or "").strip().lower()
    return candidate if candidate in supported_vendors else fallback_vendor


def get_runtime_database_config_path(base_dir):
    return Path(base_dir) / RUNTIME_DATABASE_CONFIG_FILENAME


def load_runtime_database_config(base_dir, fallback_vendor, supported_vendors):
    path = get_runtime_database_config_path(base_dir)
    fallback_vendor = _normalise_vendor(fallback_vendor, supported_vendors, supported_vendors[0])
    default_config = {
        "database_vendor": fallback_vendor,
        "functions_db_backend": fallback_vendor,
        "source": "default",
        "path": path,
    }

    if not path.exists():
        return default_config

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default_config

    database_vendor = _normalise_vendor(
        payload.get("database_vendor"),
        supported_vendors,
        fallback_vendor,
    )
    functions_db_backend = _normalise_vendor(
        payload.get("functions_db_backend"),
        supported_vendors,
        database_vendor,
    )

    return {
        "database_vendor": database_vendor,
        "functions_db_backend": functions_db_backend,
        "source": "file",
        "path": path,
    }


def save_runtime_database_config(base_dir, database_vendor, supported_vendors, functions_db_backend=None):
    path = get_runtime_database_config_path(base_dir)
    database_vendor = _normalise_vendor(database_vendor, supported_vendors, supported_vendors[0])
    functions_db_backend = _normalise_vendor(
        functions_db_backend or database_vendor,
        supported_vendors,
        database_vendor,
    )
    payload = {
        "database_vendor": database_vendor,
        "functions_db_backend": functions_db_backend,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {
        "database_vendor": database_vendor,
        "functions_db_backend": functions_db_backend,
        "source": "file",
        "path": path,
    }
