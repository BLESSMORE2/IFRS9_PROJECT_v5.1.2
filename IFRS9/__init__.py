import importlib
from pkgutil import extend_path
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_SOURCE_DIRS = [
    _PACKAGE_DIR.parent.parent / 'system_nexa9_v5.1.2' / 'system_core_utils' / 'IFRS9',
]

__path__ = extend_path(__path__, __name__)
if str(_PACKAGE_DIR) not in __path__:
    __path__.append(str(_PACKAGE_DIR))
for candidate in _SOURCE_DIRS:
    candidate_str = str(candidate)
    if candidate.exists() and candidate_str not in __path__:
        __path__.append(candidate_str)

try:
    _installed_package = importlib.import_module("system_core_utils.IFRS9")
except Exception:
    _installed_package = None

if _installed_package is not None:
    for installed_path in getattr(_installed_package, "__path__", []):
        if installed_path not in __path__:
            __path__.append(installed_path)
