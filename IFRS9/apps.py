from importlib import import_module
from pathlib import Path

from system_core_utils.IFRS9.apps import Ifrs9Config as SourceIfrs9Config


def _resolve_package_root():
    package = import_module("system_core_utils.IFRS9")
    package_paths = list(getattr(package, "__path__", []))
    if package_paths:
        return str(Path(package_paths[0]).resolve())
    package_file = getattr(package, "__file__", None)
    if package_file:
        return str(Path(package_file).resolve().parent)
    return str(Path(__file__).resolve().parent)


class Ifrs9Config(SourceIfrs9Config):
    path = _resolve_package_root()
