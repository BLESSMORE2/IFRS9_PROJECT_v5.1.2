from importlib import import_module
from pathlib import Path

from system_core_utils.IFRS9.apps import Ifrs9Config as SourceIfrs9Config


def _resolve_package_root():
    shim_dir = Path(__file__).resolve().parent
    candidates = []

    for package_name in ("IFRS9", "system_core_utils.IFRS9"):
        try:
            package = import_module(package_name)
        except Exception:
            continue

        for package_path in getattr(package, "__path__", []):
            resolved = Path(package_path).resolve()
            if resolved not in candidates:
                candidates.append(resolved)

    for candidate in candidates:
        if candidate == shim_dir:
            continue
        if (candidate / "templates").exists() or (candidate / "static").exists():
            return str(candidate)

    for candidate in candidates:
        if candidate != shim_dir and candidate.exists():
            return str(candidate)

    return str(shim_dir)


class Ifrs9Config(SourceIfrs9Config):
    path = _resolve_package_root()
