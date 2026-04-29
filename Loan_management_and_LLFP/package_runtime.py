import importlib
import importlib.util
import sys
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
        return {
            "installed": False,
            "usable": False,
            "expired": True,
            "message": default_message,
            "expiry_date": None,
        }
    except Exception:
        return {
            "installed": True,
            "usable": False,
            "expired": True,
            "message": default_message,
            "expiry_date": None,
        }

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
        return {
            "installed": True,
            "usable": False,
            "expired": True,
            "message": default_message,
            "expiry_date": None,
        }


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
