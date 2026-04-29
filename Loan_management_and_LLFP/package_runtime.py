import importlib
import importlib.util
import sys
from datetime import datetime
from pathlib import Path


IFRS9_PACKAGE_ALIAS = "IFRS9"
IFRS9_PACKAGE_SOURCE = "system_core_utils.IFRS9"
SCORECARD_PACKAGE_ALIAS = "scorecard"
SCORECARD_PACKAGE_SOURCE = "scorecard_utils.scorecard"
DEFAULT_SUBSCRIPTION_MESSAGE = (
    "Your IFRS9 subscription is due or expired. Please contact the product owner."
)
DEFAULT_SCORECARD_SUBSCRIPTION_MESSAGE = (
    "Your scorecard subscription is due or expired. Please contact the product owner."
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_SOURCE_ROOTS = {
    IFRS9_PACKAGE_SOURCE: [PROJECT_ROOT.parent / "system_nexa9_v5.1.2"],
    SCORECARD_PACKAGE_SOURCE: [PROJECT_ROOT.parent / "system_scorecard_v5.1.2"],
}
PACKAGE_EXPIRY_FILENAMES = {
    IFRS9_PACKAGE_ALIAS: ".nexa_expiry_date",
    SCORECARD_PACKAGE_ALIAS: ".scorecard_expiry_date",
}
PACKAGE_WARNING_THRESHOLD_DAYS = 31


def _prepend_candidate_roots(source_name):
    for candidate_root in PACKAGE_SOURCE_ROOTS.get(source_name, []):
        if candidate_root.exists():
            candidate_str = str(candidate_root)
            if candidate_str not in sys.path:
                sys.path.insert(0, candidate_str)


def _find_spec(name):
    try:
        return importlib.util.find_spec(name)
    except (ImportError, AttributeError, ValueError):
        return None


def _bootstrap_alias(alias, source):
    importlib.invalidate_caches()
    _prepend_candidate_roots(source)

    if alias in sys.modules:
        return True

    local_spec = _find_spec(alias)
    if local_spec is not None:
        try:
            importlib.import_module(alias)
            return True
        except Exception:
            pass

    source_spec = _find_spec(source)
    if source_spec is None:
        return False

    module = importlib.import_module(source)
    sys.modules.setdefault(alias, module)
    return True


def bootstrap_ifrs9_alias():
    return _bootstrap_alias(IFRS9_PACKAGE_ALIAS, IFRS9_PACKAGE_SOURCE)


def bootstrap_scorecard_alias():
    return _bootstrap_alias(SCORECARD_PACKAGE_ALIAS, SCORECARD_PACKAGE_SOURCE)


def _iter_module_paths(module_name):
    module = sys.modules.get(module_name)
    if module is None:
        return []

    discovered_paths = []
    for raw_path in getattr(module, "__path__", []):
        path = Path(raw_path).resolve()
        if path not in discovered_paths:
            discovered_paths.append(path)
    return discovered_paths


def _read_expiry_from_file(alias):
    expiry_filename = PACKAGE_EXPIRY_FILENAMES.get(alias)
    if not expiry_filename:
        return None

    candidate_paths = []
    for module_name in (alias, IFRS9_PACKAGE_SOURCE if alias == IFRS9_PACKAGE_ALIAS else SCORECARD_PACKAGE_SOURCE):
        for path in _iter_module_paths(module_name):
            if path not in candidate_paths:
                candidate_paths.append(path)

    for package_dir in candidate_paths:
        expiry_file = package_dir / expiry_filename
        if not expiry_file.exists():
            continue

        try:
            expiry_text = expiry_file.read_text(encoding="utf-8").strip()
            return datetime.strptime(expiry_text, "%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError):
            continue

    return None


def _build_file_based_status(*, alias, default_message, expired_message_template, valid_message_template):
    expiry_date = _read_expiry_from_file(alias)
    if expiry_date is None:
        return {
            "installed": True,
            "usable": False,
            "expired": True,
            "message": default_message,
            "expiry_date": None,
        }

    now = datetime.now()
    if expiry_date <= now:
        return {
            "installed": True,
            "usable": False,
            "expired": True,
            "message": expired_message_template.format(
                expiry_label=expiry_date.strftime("%Y-%m-%d %H:%M:%S")
            ),
            "expiry_date": expiry_date,
        }

    remaining_days = max(1, (expiry_date - now).days + 1)
    if remaining_days <= PACKAGE_WARNING_THRESHOLD_DAYS:
        message = (
            f"{valid_message_template.format(expiry_label=expiry_date.strftime('%Y-%m-%d %H:%M:%S'))} "
            f"({remaining_days} day(s) remaining)."
        )
    else:
        message = valid_message_template.format(expiry_label=expiry_date.strftime("%Y-%m-%d %H:%M:%S"))

    return {
        "installed": True,
        "usable": True,
        "expired": False,
        "message": message,
        "expiry_date": expiry_date,
    }


def _get_package_status(*, alias, source, default_message, expired_message_template, valid_message_template):
    if not _bootstrap_alias(alias, source):
        return {
            "installed": False,
            "usable": False,
            "expired": True,
            "message": default_message,
            "expiry_date": None,
        }

    try:
        expiry_module = importlib.import_module(f"{alias}.check_package_expiry")
    except ModuleNotFoundError:
        return _build_file_based_status(
            alias=alias,
            default_message=default_message,
            expired_message_template=expired_message_template,
            valid_message_template=valid_message_template,
        )
    except Exception:
        return _build_file_based_status(
            alias=alias,
            default_message=default_message,
            expired_message_template=expired_message_template,
            valid_message_template=valid_message_template,
        )

    try:
        expiry_date = None
        if hasattr(expiry_module, "get_expiry_time"):
            _, expiry_date = expiry_module.get_expiry_time()

        status_message = expiry_module.check_expiry()
        expiry_label = expiry_date.strftime("%Y-%m-%d %H:%M:%S") if expiry_date else "unknown"

        if status_message and "expired" in status_message.lower():
            return {
                "installed": True,
                "usable": False,
                "expired": True,
                "message": expired_message_template.format(expiry_label=expiry_label),
                "expiry_date": expiry_date,
            }

        message = status_message or valid_message_template.format(expiry_label=expiry_label)
        return {
            "installed": True,
            "usable": True,
            "expired": False,
            "message": message,
            "expiry_date": expiry_date,
        }
    except Exception:
        return _build_file_based_status(
            alias=alias,
            default_message=default_message,
            expired_message_template=expired_message_template,
            valid_message_template=valid_message_template,
        )


def get_ifrs9_package_status():
    return _get_package_status(
        alias=IFRS9_PACKAGE_ALIAS,
        source=IFRS9_PACKAGE_SOURCE,
        default_message=DEFAULT_SUBSCRIPTION_MESSAGE,
        expired_message_template="Your IFRS9 license expired on {expiry_label}. Please contact support.",
        valid_message_template="Your IFRS9 license is valid until {expiry_label}.",
    )


def get_scorecard_package_status():
    return _get_package_status(
        alias=SCORECARD_PACKAGE_ALIAS,
        source=SCORECARD_PACKAGE_SOURCE,
        default_message=DEFAULT_SCORECARD_SUBSCRIPTION_MESSAGE,
        expired_message_template="Your scorecard license expired on {expiry_label}. Please contact support.",
        valid_message_template="Your scorecard license is valid until {expiry_label}.",
    )


def ifrs9_package_available():
    return get_ifrs9_package_status()["usable"]


def scorecard_package_available():
    return get_scorecard_package_status()["usable"]
