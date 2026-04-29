"""
Django settings for Loan_management_and_LLFP project.
"""

from datetime import timedelta
from pathlib import Path
import os

from Loan_management_and_LLFP.package_runtime import (
    get_ifrs9_package_status,
    get_scorecard_package_status,
)
from Loan_management_and_LLFP.runtime_database_config import load_runtime_database_config

BASE_DIR = Path(__file__).resolve().parent.parent

IFRS9_PACKAGE_STATUS = get_ifrs9_package_status()
IFRS9_PACKAGE_AVAILABLE = IFRS9_PACKAGE_STATUS["usable"]
SCORECARD_PACKAGE_STATUS = get_scorecard_package_status()
SCORECARD_PACKAGE_AVAILABLE = SCORECARD_PACKAGE_STATUS["usable"]

if IFRS9_PACKAGE_AVAILABLE:
    print(f"[INFO] {IFRS9_PACKAGE_STATUS['message']}")
else:
    print(f"[WARNING] {IFRS9_PACKAGE_STATUS['message']}")

if SCORECARD_PACKAGE_AVAILABLE:
    print(f"[INFO] {SCORECARD_PACKAGE_STATUS['message']}")
else:
    print(f"[WARNING] {SCORECARD_PACKAGE_STATUS['message']}")

SECRET_KEY = 'django-insecure-jn+)3jddi9#0j%!5voeznr+t383172xh#^^fem_rhv5vgy@-so'
DEBUG = True
ALLOWED_HOSTS = []

INSTALLED_APPS = [
    'jazzmin',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'Users',
    'crispy_forms',
    'crispy_bootstrap4',
    'django.contrib.humanize',
    'axes',
    'rest_framework',
    'drf_yasg',
]

if IFRS9_PACKAGE_AVAILABLE:
    INSTALLED_APPS.append('IFRS9.apps.Ifrs9Config')
if SCORECARD_PACKAGE_AVAILABLE:
    INSTALLED_APPS.append('scorecard.apps.ScorecardConfig')

JAZZMIN_SETTINGS = {
    "site_title": "Nexa Compliance Admin",
    "site_header": "Nexa Compliance Administration",
    "site_brand": "Nexa Compliance Admin",
    "site_logo": "images/login/bns_logo_trimmed.png",
    "login_logo": "images/login/bns_logo_trimmed.png",
    "site_icon": "images/login/bns_logo_trimmed.png",
    "welcome_sign": "Welcome to the Brain Nexus administration workspace",
    "copyright": "Brain Nexus Solution",
    "topmenu_links": [
        {"name": "Main Site", "url": "modules_home", "new_window": False},
    ],
}

AXES_FAILURE_LIMIT = 3
AXES_COOLOFF_TIME = 1
AXES_LOCKOUT_TEMPLATE = 'axes/lockout.html'

CRISPY_TEMPLATE_PACK = 'bootstrap4'
AUTH_USER_MODEL = 'Users.CustomUser'
LOGIN_URL = '/login/'
SESSION_COOKIE_AGE = 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_SAVE_EVERY_REQUEST = True

AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'Users.backends.CaseInsensitiveEmailOrAliasBackend',
    'django.contrib.auth.backends.ModelBackend',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'Users.middleware.RuntimeSessionControlMiddleware',
    'axes.middleware.AxesMiddleware',
    'Loan_management_and_LLFP.middleware.Ifrs9AvailabilityMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'Loan_management_and_LLFP.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

if IFRS9_PACKAGE_AVAILABLE:
    TEMPLATES[0]['OPTIONS']['context_processors'].append('IFRS9.context_processors.app_version')

WSGI_APPLICATION = 'Loan_management_and_LLFP.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'mssql',
        'NAME': 'NEXA',
        'USER': 'nexa_user',
        'PASSWORD': '1234',
        'HOST': '127.0.0.1',
        'PORT': '1433',
        'OPTIONS': {
            'driver': 'ODBC Driver 17 for SQL Server',
            'extra_params': 'Encrypt=no;TrustServerCertificate=yes',
        },
    }
}


def _infer_database_vendor(engine_name):
    engine_name = (engine_name or '').lower()

    if 'oracle' in engine_name:
        return 'oracle'
    if 'sql_server' in engine_name or 'mssql' in engine_name:
        return 'mssql'
    if 'postgresql' in engine_name or 'postgis' in engine_name:
        return 'postgresql'

    return 'postgresql'


SUPPORTED_DATABASE_VENDORS = ('oracle', 'mssql', 'postgresql')

_runtime_database_config = load_runtime_database_config(
    BASE_DIR,
    _infer_database_vendor(DATABASES['default']['ENGINE']),
    SUPPORTED_DATABASE_VENDORS,
)

DATABASE_VENDOR = os.getenv(
    'IFRS9_DATABASE_VENDOR',
    _runtime_database_config['database_vendor'],
).lower()
FUNCTIONS_DB_BACKEND = os.getenv(
    'IFRS9_FUNCTIONS_DB_BACKEND',
    _runtime_database_config['functions_db_backend'],
).lower()
DATABASE_RUNTIME_CONFIG_PATH = str(_runtime_database_config['path'])
DATABASE_RUNTIME_CONFIG_SOURCE = (
    'environment'
    if os.getenv('IFRS9_DATABASE_VENDOR') or os.getenv('IFRS9_FUNCTIONS_DB_BACKEND')
    else _runtime_database_config['source']
)

if DATABASE_VENDOR not in SUPPORTED_DATABASE_VENDORS:
    raise ValueError(
        f"Unsupported DATABASE_VENDOR '{DATABASE_VENDOR}'. "
        f"Expected one of: {', '.join(SUPPORTED_DATABASE_VENDORS)}."
    )

if FUNCTIONS_DB_BACKEND not in SUPPORTED_DATABASE_VENDORS:
    raise ValueError(
        f"Unsupported FUNCTIONS_DB_BACKEND '{FUNCTIONS_DB_BACKEND}'. "
        f"Expected one of: {', '.join(SUPPORTED_DATABASE_VENDORS)}."
    )

if DATABASE_VENDOR == 'postgresql':
    MIGRATION_MODULES = {
        'Users': 'Users.migrations_pg',
    }
    if IFRS9_PACKAGE_AVAILABLE:
        MIGRATION_MODULES['IFRS9'] = 'IFRS9.migrations_pg'
elif DATABASE_VENDOR == 'oracle':
    MIGRATION_MODULES = {
        'Users': 'Users.migrations',
    }
    if IFRS9_PACKAGE_AVAILABLE:
        MIGRATION_MODULES['IFRS9'] = 'IFRS9.migrations'
elif DATABASE_VENDOR == 'mssql':
    MIGRATION_MODULES = {
        'Users': 'Users.migrations_mssql',
    }
    if IFRS9_PACKAGE_AVAILABLE:
        MIGRATION_MODULES['IFRS9'] = 'IFRS9.migrations_mssql'

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Harare'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
_static_dir = BASE_DIR / 'static'
STATICFILES_DIRS = [_static_dir] if _static_dir.exists() else []
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
DATA_UPLOAD_MAX_NUMBER_FIELDS = 100000

REST_FRAMEWORK = {
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.BasicAuthentication',
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 25,
    'DEFAULT_FILTER_BACKENDS': [
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'ROTATE_REFRESH_TOKENS': False,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': False,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'VERIFYING_KEY': None,
    'AUDIENCE': None,
    'ISSUER': None,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'AUTH_HEADER_NAME': 'HTTP_AUTHORIZATION',
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),
    'TOKEN_TYPE_CLAIM': 'token_type',
    'JTI_CLAIM': 'jti',
}

CSRF_FAILURE_VIEW = 'Loan_management_and_LLFP.error_handlers.csrf_failure'

SWAGGER_SETTINGS = {
    'SECURITY_DEFINITIONS': {
        'Basic': {
            'type': 'basic'
        },
        'Bearer': {
            'type': 'apiKey',
            'name': 'Authorization',
            'in': 'header',
            'description': 'JWT Token: Enter "Bearer <your_token>"'
        }
    },
    'USE_SESSION_AUTH': True,
    'PERSIST_AUTH': True,
    'DEFAULT_MODEL_RENDERING': 'example',
    'OPERATIONS_SORTER': 'alpha',
    'TAGS_SORTER': 'alpha',
    'DOC_EXPANSION': 'none',
    'DEFAULT_GENERATOR_CLASS': 'drf_yasg.generators.OpenAPISchemaGenerator',
    'DEFAULT_INFO': 'Loan_management_and_LLFP.urls.schema_view.info',
    'DEFAULT_API_URL': 'http://127.0.0.1:7000/api/',
    'VALIDATOR_URL': None,
    'DISPLAY_OPERATION_ID': False,
    'DEEP_LINKING': True,
    'DEFAULT_PAGINATOR_INSPECTORS': [
        'drf_yasg.inspectors.CoreAPICompatInspector',
    ],
    'SUPPORTED_SUBMIT_METHODS': [
        'get',
        'post',
        'put',
        'patch',
        'delete',
    ],
    'TAGS': [
        {'name': 'Core Banking Integration', 'description': 'Endpoints for connecting to core banking systems'},
        {'name': 'Loan Portfolio Analysis', 'description': 'API for analyzing loans from core banking data'},
        {'name': 'Risk Assessment', 'description': 'Real-time risk analysis endpoints'},
        {'name': 'Webhooks', 'description': 'External event notifications for banking system events'},
        {'name': 'Reporting', 'description': 'IFRS 9 compliance reporting APIs'},
        {'name': 'Audit Logs', 'description': 'Access and manage API integration audit logs'},
    ],
}

SCORECARD_FIXED_SENDER_EMAIL = 'nexascorecard@bns.co.zw'
SCORECARD_FIXED_SENDER_PASSWORD = 'brainnexussolutions'
