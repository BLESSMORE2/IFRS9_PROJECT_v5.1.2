from .settings import *  # noqa


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "scorecard_test.sqlite3",
    }
}

MIGRATION_MODULES = {
    "IFRS9": None,
    "Users": None,
    "scorecard": None,
    "admin": None,
    "auth": None,
    "contenttypes": None,
    "sessions": None,
    "axes": None,
}

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

AXES_ENABLED = False
