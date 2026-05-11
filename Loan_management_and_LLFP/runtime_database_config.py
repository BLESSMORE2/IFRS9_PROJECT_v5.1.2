import json
from pathlib import Path


RUNTIME_DATABASE_CONFIG_FILENAME = "runtime_database_config.json"


DEFAULT_DR_DATABASE_CONFIG = {
    "enabled": False,
    "engine": "mssql",
    "name": "",
    "user": "",
    "password": "",
    "host": "",
    "port": "1433",
    "driver": "ODBC Driver 17 for SQL Server",
    "extra_params": "Encrypt=no;TrustServerCertificate=yes",
    "backup_method": "sql_server_native",
    "backup_frequency": "daily",
    "backup_window": "22:00",
    "table_scope": "full_database",
}


def _normalise_vendor(value, supported_vendors, fallback_vendor):
    candidate = (value or "").strip().lower()
    return candidate if candidate in supported_vendors else fallback_vendor


def _normalise_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _normalise_dr_database_config(payload):
    source = payload if isinstance(payload, dict) else {}
    config = DEFAULT_DR_DATABASE_CONFIG.copy()
    for key in config:
        if key in source:
            config[key] = source.get(key)
    config["enabled"] = _normalise_bool(config.get("enabled"))
    for key in (
        "engine",
        "name",
        "user",
        "password",
        "host",
        "port",
        "driver",
        "extra_params",
        "backup_method",
        "backup_frequency",
        "backup_window",
        "table_scope",
    ):
        config[key] = str(config.get(key) or "").strip()
    if not config["engine"]:
        config["engine"] = DEFAULT_DR_DATABASE_CONFIG["engine"]
    if not config["port"]:
        config["port"] = DEFAULT_DR_DATABASE_CONFIG["port"]
    if not config["driver"]:
        config["driver"] = DEFAULT_DR_DATABASE_CONFIG["driver"]
    if not config["extra_params"]:
        config["extra_params"] = DEFAULT_DR_DATABASE_CONFIG["extra_params"]
    if not config["backup_window"]:
        config["backup_window"] = DEFAULT_DR_DATABASE_CONFIG["backup_window"]
    return config


def get_runtime_database_config_path(base_dir):
    return Path(base_dir) / RUNTIME_DATABASE_CONFIG_FILENAME


def load_runtime_database_config(base_dir, fallback_vendor, supported_vendors):
    path = get_runtime_database_config_path(base_dir)
    fallback_vendor = _normalise_vendor(fallback_vendor, supported_vendors, supported_vendors[0])
    default_config = {
        "database_vendor": fallback_vendor,
        "functions_db_backend": fallback_vendor,
        "dr_database": DEFAULT_DR_DATABASE_CONFIG.copy(),
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
        "dr_database": _normalise_dr_database_config(payload.get("dr_database")),
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
    existing = load_runtime_database_config(base_dir, database_vendor, supported_vendors)
    payload = {
        "database_vendor": database_vendor,
        "functions_db_backend": functions_db_backend,
        "dr_database": existing.get("dr_database", DEFAULT_DR_DATABASE_CONFIG.copy()),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {
        "database_vendor": database_vendor,
        "functions_db_backend": functions_db_backend,
        "dr_database": payload["dr_database"],
        "source": "file",
        "path": path,
    }


def save_runtime_dr_database_config(base_dir, dr_database_config, fallback_vendor, supported_vendors):
    existing = load_runtime_database_config(base_dir, fallback_vendor, supported_vendors)
    payload = {
        "database_vendor": existing["database_vendor"],
        "functions_db_backend": existing["functions_db_backend"],
        "dr_database": _normalise_dr_database_config(dr_database_config),
    }
    path = get_runtime_database_config_path(base_dir)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {
        "database_vendor": payload["database_vendor"],
        "functions_db_backend": payload["functions_db_backend"],
        "dr_database": payload["dr_database"],
        "source": "file",
        "path": path,
    }
