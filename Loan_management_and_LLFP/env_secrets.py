from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


ENV_MASTER_KEY_NAME = "NEXA_ENV_MASTER_KEY"
ENCRYPTED_PREFIX = "ENC:"
SENSITIVE_ENV_KEYS = {
    "DB_USER",
    "DB_PASSWORD",
    "SCORECARD_FIXED_SENDER_EMAIL",
    "SCORECARD_FIXED_SENDER_PASSWORD",
}


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if value and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_persistent_windows_env(name: str) -> str:
    if os.name != "nt":
        return ""

    try:
        import winreg
    except ImportError:
        return ""

    for hive, subkey in (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(hive, subkey) as key_handle:
                value, _ = winreg.QueryValueEx(key_handle, name)
                if value:
                    return str(value).strip()
        except OSError:
            continue

    return ""


def _build_fernet() -> Fernet | None:
    raw_key = os.getenv(ENV_MASTER_KEY_NAME, "").strip()
    if not raw_key:
        raw_key = _read_persistent_windows_env(ENV_MASTER_KEY_NAME)
        if raw_key:
            os.environ.setdefault(ENV_MASTER_KEY_NAME, raw_key)
    if not raw_key:
        return None
    try:
        return Fernet(raw_key.encode("utf-8"))
    except Exception as exc:  # pragma: no cover - defensive configuration path
        raise RuntimeError(
            f"{ENV_MASTER_KEY_NAME} is not a valid Fernet key."
        ) from exc


def _decrypt_value(key: str, value: str, fernet: Fernet | None) -> str:
    if not value.startswith(ENCRYPTED_PREFIX):
        return value
    if fernet is None:
        raise RuntimeError(
            f"{key} is encrypted in .env, but {ENV_MASTER_KEY_NAME} is not set."
        )
    token = value[len(ENCRYPTED_PREFIX):]
    try:
        return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            f"{key} could not be decrypted with the current {ENV_MASTER_KEY_NAME}."
        ) from exc


def _encrypt_value(value: str, fernet: Fernet) -> str:
    token = fernet.encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_PREFIX}{token}"


def load_root_env_file(base_dir: Path) -> None:
    env_path = base_dir / ".env"
    if not env_path.exists():
        return

    fernet = _build_fernet()
    original_lines = env_path.read_text(encoding="utf-8").splitlines()
    updated_lines: list[str] = []
    env_changed = False

    for raw_line in original_lines:
        stripped_line = raw_line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in raw_line:
            updated_lines.append(raw_line)
            continue

        key, value = raw_line.split("=", 1)
        normalized_key = key.strip()
        normalized_value = _strip_wrapping_quotes(value)

        loaded_value = normalized_value
        stored_value = normalized_value

        if normalized_key in SENSITIVE_ENV_KEYS:
            loaded_value = _decrypt_value(normalized_key, normalized_value, fernet)
            if (
                fernet is not None
                and loaded_value
                and not normalized_value.startswith(ENCRYPTED_PREFIX)
            ):
                stored_value = _encrypt_value(loaded_value, fernet)
                env_changed = True

        updated_lines.append(f"{normalized_key}={stored_value}")
        os.environ.setdefault(normalized_key, loaded_value)

    if env_changed:
        env_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
