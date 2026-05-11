import importlib
import os
from pkgutil import extend_path
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_SOURCE_DIRS = [
    _PACKAGE_DIR.parent.parent / 'system_scorecard_v5.1.2' / 'scorecard_utils' / 'scorecard',
]
_USE_SOURCE_FALLBACK = (
    os.getenv("NEXA9_USE_SOURCE_PACKAGE_FALLBACK", "1").strip().lower()
    not in {"0", "false", "no"}
)

__path__ = extend_path(__path__, __name__)
if str(_PACKAGE_DIR) not in __path__:
    __path__.append(str(_PACKAGE_DIR))

try:
    _installed_package = importlib.import_module("scorecard_utils.scorecard")
except Exception:
    _installed_package = None

if _installed_package is not None:
    for installed_path in getattr(_installed_package, "__path__", []):
        if installed_path not in __path__:
            __path__.append(installed_path)
elif _USE_SOURCE_FALLBACK:
    for candidate in _SOURCE_DIRS:
        candidate_str = str(candidate)
        if candidate.exists() and candidate_str not in __path__:
            __path__.append(candidate_str)
