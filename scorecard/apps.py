from importlib import import_module
from pathlib import Path

from scorecard_utils.scorecard.apps import ScorecardConfig as SourceScorecardConfig


def _resolve_package_root():
    shim_dir = Path(__file__).resolve().parent
    candidates = []

    for package_name in ("scorecard", "scorecard_utils.scorecard"):
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


class ScorecardConfig(SourceScorecardConfig):
    path = _resolve_package_root()
