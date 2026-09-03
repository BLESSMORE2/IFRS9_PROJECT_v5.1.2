from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
from datetime import timedelta
from pathlib import Path
import smtplib
from typing import Any, Iterable
from urllib.parse import urljoin

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.paginator import Paginator
from django.db import DatabaseError, OperationalError, ProgrammingError, close_old_connections, connection
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template import Context, Template
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from cryptography.fernet import Fernet, InvalidToken

from scorecard.functions_view.audit import log_email_audit
from scorecard.functions_view.customers import build_without_score_customer_snapshot_for_branch_scope
from scorecard.models import (
    ApiImportRun,
    BankBranch,
    CreditEvaluation,
    IFRS9Evaluation,
    ScorecardEmailConfiguration,
    ScorecardEmailSendLog,
    ScorecardEmailTemplate,
)
from scorecard.workflow_approval import (
    should_limit_submission_notifications_to_single_branch_reviewers,
    should_limit_without_score_notifications_to_single_branch_makers,
    get_without_score_list_customer_sources,
)


logger = logging.getLogger(__name__)
_email_configuration_schema_checked = False


EMAIL_WORKSPACE_CACHE_TTL_SECONDS = 120
EMAIL_DELIVERY_CACHE_TTL_SECONDS = 60
TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates" / "email"
ENCRYPTED_VALUE_PREFIX = "enc::"
SCORECARD_FIXED_SENDER_EMAIL_SETTING = "SCORECARD_FIXED_SENDER_EMAIL"
SCORECARD_FIXED_SENDER_PASSWORD_SETTING = "SCORECARD_FIXED_SENDER_PASSWORD"
BRANCH_SCOPED_EMAIL_EVENTS = {
    "basel_submitted",
    "ifrs9_submitted",
    "basel_approved",
    "ifrs9_approved",
    "basel_returned",
    "ifrs9_returned",
    "basel_checker_pending_reminder",
    "ifrs9_checker_pending_reminder",
    "without_score_summary",
}

EMAIL_EVENT_DEFINITIONS: dict[str, dict[str, str]] = {
    "api_import_failed": {
        "name": "API Import Failed",
        "category": ScorecardEmailTemplate.CATEGORY_FAILURE,
        "description": "Sent to administrators when an API import fails.",
        "subject_file": "api_import_failed_subject.txt",
        "text_file": "api_import_failed_body.txt",
        "html_file": "api_import_failed_body.html",
    },
    "api_schedule_failed": {
        "name": "Scheduled API Import Failed",
        "category": ScorecardEmailTemplate.CATEGORY_FAILURE,
        "description": "Sent to administrators when a scheduled API import fails.",
        "subject_file": "api_schedule_failed_subject.txt",
        "text_file": "api_schedule_failed_body.txt",
        "html_file": "api_schedule_failed_body.html",
    },
    "main_sync_failed": {
        "name": "Main Customer Sync Failed",
        "category": ScorecardEmailTemplate.CATEGORY_FAILURE,
        "description": "Sent to administrators when the main customer sync fails.",
        "subject_file": "main_sync_failed_subject.txt",
        "text_file": "main_sync_failed_body.txt",
        "html_file": "main_sync_failed_body.html",
    },
    "api_schedule_repeated_failure": {
        "name": "Scheduled API Repeated Failure Reminder",
        "category": ScorecardEmailTemplate.CATEGORY_FAILURE,
        "description": "Sent to administrators when the same schedule keeps failing repeatedly.",
        "subject_file": "api_schedule_repeated_failure_subject.txt",
        "text_file": "api_schedule_repeated_failure_body.txt",
        "html_file": "api_schedule_repeated_failure_body.html",
    },
    "basel_submitted": {
        "name": "Basel Score Submitted",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent to the assigned checker when a Basel score is submitted for review.",
        "subject_file": "score_submitted_subject.txt",
        "text_file": "score_submitted_body.txt",
        "html_file": "score_submitted_body.html",
    },
    "ifrs9_submitted": {
        "name": "IFRS9 Score Submitted",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent to the assigned checker when an IFRS9 score is submitted for review.",
        "subject_file": "score_submitted_subject.txt",
        "text_file": "score_submitted_body.txt",
        "html_file": "score_submitted_body.html",
    },
    "basel_approved": {
        "name": "Basel Score Approved",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent to the maker when a Basel score is approved.",
        "subject_file": "score_approved_subject.txt",
        "text_file": "score_approved_body.txt",
        "html_file": "score_approved_body.html",
    },
    "ifrs9_approved": {
        "name": "IFRS9 Score Approved",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent to the maker when an IFRS9 score is approved.",
        "subject_file": "score_approved_subject.txt",
        "text_file": "score_approved_body.txt",
        "html_file": "score_approved_body.html",
    },
    "basel_returned": {
        "name": "Basel Score Returned",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent to the maker when a Basel score is returned for changes.",
        "subject_file": "score_returned_subject.txt",
        "text_file": "score_returned_body.txt",
        "html_file": "score_returned_body.html",
    },
    "ifrs9_returned": {
        "name": "IFRS9 Score Returned",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent to the maker when an IFRS9 score is returned for changes.",
        "subject_file": "score_returned_subject.txt",
        "text_file": "score_returned_body.txt",
        "html_file": "score_returned_body.html",
    },
    "basel_template_submitted": {
        "name": "Basel Template Submitted",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent to the assigned checker when a Basel template is submitted for review.",
        "subject_file": "template_submitted_subject.txt",
        "text_file": "template_submitted_body.txt",
        "html_file": "template_submitted_body.html",
    },
    "ifrs9_template_submitted": {
        "name": "IFRS9 Template Submitted",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent to the assigned checker when an IFRS9 template is submitted for review.",
        "subject_file": "template_submitted_subject.txt",
        "text_file": "template_submitted_body.txt",
        "html_file": "template_submitted_body.html",
    },
    "basel_template_approved": {
        "name": "Basel Template Approved",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent to the maker when a Basel template is approved.",
        "subject_file": "template_approved_subject.txt",
        "text_file": "template_approved_body.txt",
        "html_file": "template_approved_body.html",
    },
    "ifrs9_template_approved": {
        "name": "IFRS9 Template Approved",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent to the maker when an IFRS9 template is approved.",
        "subject_file": "template_approved_subject.txt",
        "text_file": "template_approved_body.txt",
        "html_file": "template_approved_body.html",
    },
    "basel_template_returned": {
        "name": "Basel Template Returned",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent to the maker when a Basel template is returned for changes.",
        "subject_file": "template_returned_subject.txt",
        "text_file": "template_returned_body.txt",
        "html_file": "template_returned_body.html",
    },
    "ifrs9_template_returned": {
        "name": "IFRS9 Template Returned",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent to the maker when an IFRS9 template is returned for changes.",
        "subject_file": "template_returned_subject.txt",
        "text_file": "template_returned_body.txt",
        "html_file": "template_returned_body.html",
    },
    "basel_checker_pending_reminder": {
        "name": "Basel Pending Checker Reminder",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent when a Basel score has been waiting for checker review too long.",
        "subject_file": "checker_pending_reminder_subject.txt",
        "text_file": "checker_pending_reminder_body.txt",
        "html_file": "checker_pending_reminder_body.html",
    },
    "ifrs9_checker_pending_reminder": {
        "name": "IFRS9 Pending Checker Reminder",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Sent when an IFRS9 score has been waiting for checker review too long.",
        "subject_file": "checker_pending_reminder_subject.txt",
        "text_file": "checker_pending_reminder_body.txt",
        "html_file": "checker_pending_reminder_body.html",
    },
    "without_score_summary": {
        "name": "Customers Without Scores Summary",
        "category": ScorecardEmailTemplate.CATEGORY_WORKFLOW,
        "description": "Scheduled list of customers missing Basel II and IFRS9 scores for each branch.",
        "subject_file": "without_score_summary_subject.txt",
        "text_file": "without_score_summary_body.txt",
        "html_file": "without_score_summary_body.html",
    },
}


class ScorecardEmailConfigurationForm(forms.ModelForm):
    application_base_url = forms.URLField(
        assume_scheme="https",
        required=False,
        max_length=500,
        widget=forms.URLInput(attrs={"placeholder": "http://192.168.x.x:8000"}),
    )

    class Meta:
        model = ScorecardEmailConfiguration
        fields = [
            "name",
            "is_enabled",
            "checker_pending_reminder_hours",
            "without_score_summary_frequency",
            "without_score_summary_hour",
            "without_score_summary_weekday",
            "without_score_summary_month_day",
            "schedule_failure_repeat_threshold",
            "schedule_failure_repeat_window_hours",
            "application_base_url",
            "from_email_override",
            "reply_to_email",
            "footer_text",
        ]
        widgets = {
            "checker_pending_reminder_hours": forms.NumberInput(attrs={"min": 1}),
            "without_score_summary_frequency": forms.Select(),
            "without_score_summary_hour": forms.NumberInput(attrs={"min": 0, "max": 23}),
            "without_score_summary_weekday": forms.NumberInput(attrs={"min": 0, "max": 6}),
            "without_score_summary_month_day": forms.NumberInput(attrs={"min": 1, "max": 28}),
            "schedule_failure_repeat_threshold": forms.NumberInput(attrs={"min": 1}),
            "schedule_failure_repeat_window_hours": forms.NumberInput(attrs={"min": 1}),
            "footer_text": forms.Textarea(attrs={"rows": 4}),
        }

    def clean(self):
        cleaned_data = super().clean()
        sender_email, _ = _resolved_system_sender(self.instance)
        cleaned_data["from_email_override"] = sender_email
        cleaned_data["application_base_url"] = (cleaned_data.get("application_base_url") or "").strip()
        cleaned_data["checker_pending_reminder_hours"] = max(1, int(cleaned_data.get("checker_pending_reminder_hours") or 24))
        cleaned_data["without_score_summary_hour"] = min(
            23,
            max(0, int(cleaned_data.get("without_score_summary_hour") or 8)),
        )
        cleaned_data["without_score_summary_weekday"] = min(
            6,
            max(0, int(cleaned_data.get("without_score_summary_weekday") or 0)),
        )
        cleaned_data["without_score_summary_month_day"] = min(
            28,
            max(1, int(cleaned_data.get("without_score_summary_month_day") or 1)),
        )
        cleaned_data["schedule_failure_repeat_threshold"] = max(1, int(cleaned_data.get("schedule_failure_repeat_threshold") or 3))
        cleaned_data["schedule_failure_repeat_window_hours"] = max(1, int(cleaned_data.get("schedule_failure_repeat_window_hours") or 24))
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        sender_email, _ = _resolved_system_sender(instance)
        if not sender_email:
            if commit:
                instance.save()
            return instance
        provider_setup = _infer_provider_setup(sender_email)
        smtp_host = provider_setup["host"]
        smtp_port = provider_setup["port"]
        smtp_use_tls = provider_setup["use_tls"]
        smtp_use_ssl = provider_setup["use_ssl"]
        smtp_username = sender_email

        instance.from_email_override = sender_email
        instance.smtp_host = smtp_host
        instance.smtp_port = smtp_port
        instance.smtp_username = smtp_username
        instance.smtp_use_tls = smtp_use_tls
        instance.smtp_use_ssl = smtp_use_ssl
        instance.smtp_password = ""
        if commit:
            instance.save()
        return instance


class ScorecardEmailTemplateForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ("event_code", "name", "category", "description"):
            self.fields[field_name].disabled = True
            self.fields[field_name].required = False

    class Meta:
        model = ScorecardEmailTemplate
        fields = [
            "event_code",
            "name",
            "category",
            "description",
            "is_enabled",
            "subject_template",
            "text_body_template",
            "html_body_template",
        ]
        widgets = {
            "event_code": forms.TextInput(attrs={"readonly": "readonly"}),
            "name": forms.TextInput(attrs={"readonly": "readonly"}),
            "category": forms.Select(attrs={"disabled": "disabled"}),
            "description": forms.Textarea(attrs={"rows": 3, "readonly": "readonly"}),
            "subject_template": forms.Textarea(attrs={"rows": 2}),
            "text_body_template": forms.Textarea(attrs={"rows": 12}),
            "html_body_template": forms.Textarea(attrs={"rows": 12}),
        }

    def clean_event_code(self):
        return self.instance.event_code or self.cleaned_data.get("event_code")

    def clean_name(self):
        return self.instance.name or self.cleaned_data.get("name")

    def clean_category(self):
        return self.instance.category or self.cleaned_data.get("category")

    def clean_description(self):
        return self.instance.description or self.cleaned_data.get("description")


def _setting_or_env(name: str) -> str:
    return str(getattr(settings, name, "") or os.environ.get(name, "") or "").strip()


def _table_exists(model) -> bool:
    try:
        return model._meta.db_table in connection.introspection.table_names()
    except Exception:
        return False


def _email_models_ready() -> bool:
    return _table_exists(ScorecardEmailConfiguration) and _table_exists(ScorecardEmailTemplate)


def _ensure_email_configuration_schema() -> None:
    global _email_configuration_schema_checked

    if _email_configuration_schema_checked or not _table_exists(ScorecardEmailConfiguration):
        return

    model = ScorecardEmailConfiguration
    table_name = model._meta.db_table
    field_names = [
        "without_score_summary_frequency",
        "without_score_summary_hour",
        "without_score_summary_weekday",
        "without_score_summary_month_day",
        "without_score_summary_last_sent_at",
    ]
    try:
        with connection.cursor() as cursor:
            existing_columns = {
                column.name.lower()
                for column in connection.introspection.get_table_description(cursor, table_name)
            }
            missing_fields = [
                model._meta.get_field(field_name)
                for field_name in field_names
                if model._meta.get_field(field_name).column.lower() not in existing_columns
            ]
            if not missing_fields:
                _email_configuration_schema_checked = True
                return

        with connection.schema_editor() as schema_editor:
            for field in missing_fields:
                schema_editor.add_field(model, field)
        _email_configuration_schema_checked = True
    except DatabaseError:
        _email_configuration_schema_checked = False
        logger.exception("Unable to repair scorecard email configuration schema.")
        raise


def _email_audit_ready() -> bool:
    return _table_exists(ScorecardEmailSendLog)


def _application_base_url() -> str:
    configuration = None
    if _email_models_ready():
        try:
            configuration = ScorecardEmailConfiguration.objects.filter(pk=1).only("application_base_url").first()
        except (OperationalError, ProgrammingError):
            configuration = None
    configured_from_db = (getattr(configuration, "application_base_url", "") or "").strip()
    if configured_from_db:
        return configured_from_db.rstrip("/")
    configured = (getattr(settings, "SCORECARD_PUBLIC_BASE_URL", "") or "").strip()
    if configured:
        return configured.rstrip("/")
    return "http://127.0.0.1:8000"


def remember_application_base_url(request: HttpRequest | None) -> None:
    if request is None or not _email_models_ready():
        return
    try:
        detected = request.build_absolute_uri("/").rstrip("/")
    except Exception:
        return
    if not detected:
        return
    try:
        configuration, _ = _ensure_email_defaults()
        if configuration is None:
            return
        if (configuration.application_base_url or "").rstrip("/") == detected:
            return
        configuration.application_base_url = detected
        configuration.save(update_fields=["application_base_url", "updated_at"])
    except (OperationalError, ProgrammingError):
        return


def _relative_reverse(name: str, kwargs: dict[str, Any] | None = None) -> str:
    """Build same-origin URLs for links used inside the web application."""
    try:
        return reverse(name, kwargs=kwargs)
    except NoReverseMatch:
        return ""


def _safe_reverse(name: str, kwargs: dict[str, Any] | None = None) -> str:
    """Build absolute URLs for links sent outside the application, such as email."""
    path = _relative_reverse(name, kwargs=kwargs)
    if not path:
        return ""
    return urljoin(_application_base_url().rstrip("/") + "/", path.lstrip("/"))


def _display_name(user: Any) -> str:
    if not user:
        return ""
    full_name = " ".join(
        part
        for part in [
            getattr(user, "first_name", ""),
            getattr(user, "last_name", ""),
            getattr(user, "name", ""),
            getattr(user, "surname", ""),
        ]
        if part
    ).strip()
    if full_name:
        return full_name
    for attr in ("username", "email"):
        value = getattr(user, attr, "")
        if value:
            return value
    return ""


def _infer_provider_setup(email_address: str) -> dict[str, Any]:
    domain = email_address.split("@", 1)[1].strip().lower() if "@" in email_address else ""
    provider = "Other"
    host = domain
    port = 465
    use_tls = False
    use_ssl = True

    if domain in {"gmail.com", "googlemail.com"}:
        provider = "Google"
        host = "smtp.gmail.com"
        port = 587
        use_tls = True
        use_ssl = False
    elif domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        provider = "Outlook"
        host = "smtp.office365.com"
        port = 587
        use_tls = True
        use_ssl = False
    elif domain in {"yahoo.com", "ymail.com", "rocketmail.com"}:
        provider = "Yahoo"
        host = "smtp.mail.yahoo.com"
        port = 587
        use_tls = True
        use_ssl = False
    elif domain:
        provider = "Domain Email"

    return {
        "provider": provider,
        "host": host,
        "port": port,
        "use_tls": use_tls,
        "use_ssl": use_ssl,
        "security": "SSL" if use_ssl else "TLS" if use_tls else "-",
    }


def _email_crypto() -> Fernet:
    secret = (getattr(settings, "SECRET_KEY", "") or "scorecard-email-fallback").encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret + b":scorecard-email").digest())
    return Fernet(key)


def _is_encrypted_secret(value: str | None) -> bool:
    return bool(value and value.startswith(ENCRYPTED_VALUE_PREFIX))


def encrypt_email_secret(value: str | None) -> str:
    if not value:
        return ""
    if _is_encrypted_secret(value):
        return value
    token = _email_crypto().encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_VALUE_PREFIX}{token}"


def decrypt_email_secret(value: str | None) -> str:
    if not value:
        return ""
    if not _is_encrypted_secret(value):
        return value
    token = value[len(ENCRYPTED_VALUE_PREFIX):]
    return _email_crypto().decrypt(token.encode("utf-8")).decode("utf-8")


def _configured_sender_email_from_settings() -> str:
    return _setting_or_env(SCORECARD_FIXED_SENDER_EMAIL_SETTING)


def _configured_sender_password_from_settings() -> str:
    return _setting_or_env(SCORECARD_FIXED_SENDER_PASSWORD_SETTING) or _setting_or_env("EMAIL_HOST_PASSWORD")


def _resolved_system_sender(
    configuration: ScorecardEmailConfiguration | None = None,
) -> tuple[str, str]:
    sender_email = _configured_sender_email_from_settings()
    sender_password = _configured_sender_password_from_settings()

    if configuration is not None:
        if not sender_email:
            sender_email = (
                configuration.from_email_override
                or configuration.smtp_username
                or ""
            ).strip()

    return sender_email, sender_password


def _provider_sign_in_guidance(email_address: str) -> str:
    domain = email_address.split("@", 1)[1].strip().lower() if "@" in email_address else ""
    if domain in {"gmail.com", "googlemail.com"}:
        return "Google usually requires an App Password for SMTP. Sign in to the Gmail account, enable 2-Step Verification, then create a 16-character App Password and use that here instead of the normal mailbox password."
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        return "Microsoft mailboxes often need SMTP AUTH enabled. If normal sign-in is blocked, use an app password or enable authenticated SMTP on the mailbox."
    if domain in {"yahoo.com", "ymail.com", "rocketmail.com"}:
        return "Yahoo usually requires an App Password for SMTP. Generate the app password in the Yahoo account security settings and use it here."
    if domain:
        return "This domain mailbox may require the exact hosted-mail password, or the provider may use a different outgoing server/security policy than the auto-detected one."
    return "Confirm the email address and password, then try again."


def _configured_from_email(configuration: ScorecardEmailConfiguration | None = None) -> str:
    if configuration and configuration.from_email_override:
        return configuration.from_email_override
    configured_sender = _configured_sender_email_from_settings()
    if configured_sender:
        return configured_sender
    return (
        getattr(settings, "DEFAULT_FROM_EMAIL", "")
        or getattr(settings, "SERVER_EMAIL", "")
        or "noreply@scorecard.local"
    )


def _apply_fixed_sender_configuration(
    configuration: ScorecardEmailConfiguration | None,
) -> ScorecardEmailConfiguration | None:
    if not configuration:
        return configuration
    sender_email, sender_password = _resolved_system_sender(configuration)
    if not sender_email:
        return configuration
    provider_setup = _infer_provider_setup(sender_email)
    changed_fields: list[str] = []

    field_updates = {
        "from_email_override": sender_email,
        "smtp_host": provider_setup["host"],
        "smtp_port": provider_setup["port"],
        "smtp_username": sender_email,
        "smtp_password": "",
        "smtp_use_tls": provider_setup["use_tls"],
        "smtp_use_ssl": provider_setup["use_ssl"],
    }
    for field_name, field_value in field_updates.items():
        if getattr(configuration, field_name) != field_value:
            setattr(configuration, field_name, field_value)
            changed_fields.append(field_name)

    if changed_fields and configuration.pk:
        changed_fields.append("updated_at")
        configuration.save(update_fields=changed_fields)
    return configuration


def _smtp_config_ready(configuration: ScorecardEmailConfiguration | None) -> bool:
    if not configuration:
        return False
    return bool(configuration.smtp_host and configuration.smtp_port and _configured_from_email(configuration))


def _settings_transport_ready() -> bool:
    backend = getattr(settings, "EMAIL_BACKEND", "") or ""
    if not backend or backend == "django.core.mail.backends.dummy.EmailBackend":
        return False
    if backend == "django.core.mail.backends.smtp.EmailBackend":
        return bool(getattr(settings, "EMAIL_HOST", ""))
    return True


def _is_transport_ready(configuration: ScorecardEmailConfiguration | None = None) -> bool:
    return _smtp_config_ready(configuration) or _settings_transport_ready()


def _recipient_emails(users: Iterable[Any]) -> list[str]:
    emails: list[str] = []
    seen: set[str] = set()
    for user in users:
        if not user or not getattr(user, "is_active", False):
            continue
        email = (getattr(user, "email", "") or "").strip()
        if not email:
            continue
        normalized = email.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        emails.append(email)
    return emails


def _user_accessible_branch_names(user: Any) -> set[str]:
    if not user:
        return set()
    if getattr(user, "is_superuser", False):
        return set()
    if not hasattr(user, "get_accessible_branches"):
        return set()
    try:
        return {
            (branch.branch_name or "").strip().lower()
            for branch in user.get_accessible_branches().only("branch_name")
            if (branch.branch_name or "").strip()
        }
    except Exception:
        return set()


def _user_explicit_branch_names(user: Any) -> set[str]:
    if not user:
        return set()
    try:
        return {
            (entry.branch.branch_name or "").strip().lower()
            for entry in user.scorecard_branch_access_entries.select_related("branch").all()
            if entry.branch and (entry.branch.branch_name or "").strip()
        }
    except Exception:
        return set()


def _user_effective_branch_scope_names(user: Any) -> set[str]:
    explicit_branch_names = _user_explicit_branch_names(user)
    if explicit_branch_names:
        return explicit_branch_names
    if not hasattr(user, "get_accessible_branches"):
        return set()
    try:
        return {
            (branch.branch_name or "").strip().lower()
            for branch in user.get_accessible_branches().only("branch_name")
            if (branch.branch_name or "").strip()
        }
    except Exception:
        return set()


def _user_has_single_matching_branch_scope(user: Any, branch_name: str) -> bool:
    cleaned_branch_name = (branch_name or "").strip().lower()
    if not cleaned_branch_name:
        return False
    branch_names = _user_effective_branch_scope_names(user)
    return len(branch_names) == 1 and cleaned_branch_name in branch_names


def _user_can_receive_branch_email(user: Any, branch_name: str) -> bool:
    if not user or not getattr(user, "is_active", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    cleaned_branch_name = (branch_name or "").strip().lower()
    if not cleaned_branch_name:
        return False
    return cleaned_branch_name in _user_accessible_branch_names(user)


def _current_branch_name_for_request(request: HttpRequest | None) -> str:
    if request is None:
        return ""
    cached_branch_name = getattr(request, "_scorecard_email_current_branch_name", None)
    if cached_branch_name is not None:
        return cached_branch_name

    current_branch_id = str(request.session.get("current_branch_id") or "").strip()
    current_branch_code = (request.session.get("current_branch_code") or "").strip()
    if not current_branch_id and not current_branch_code:
        request._scorecard_email_current_branch_name = ""
        return ""
    try:
        if current_branch_id:
            branch = BankBranch.objects.only("id", "branch_code", "branch_name").get(pk=current_branch_id)
        else:
            branch = (
                BankBranch.objects.only("id", "branch_code", "branch_name")
                .filter(branch_code=current_branch_code)
                .order_by("branch_name", "id")
                .first()
            )
        if branch is None:
            request._scorecard_email_current_branch_name = ""
            return ""
    except (BankBranch.DoesNotExist, OperationalError, ProgrammingError):
        request._scorecard_email_current_branch_name = ""
        return ""
    if getattr(request.user, "is_superuser", False):
        branch_name = (branch.branch_name or "").strip().lower()
        request._scorecard_email_current_branch_name = branch_name
        return branch_name
    if not getattr(request.user, "has_branch_access", None) or not request.user.has_branch_access(
        branch_id=getattr(branch, "id", None),
        branch_code=branch.branch_code,
        branch_name=branch.branch_name,
    ):
        request._scorecard_email_current_branch_name = ""
        return ""
    branch_name = (branch.branch_name or "").strip().lower()
    request._scorecard_email_current_branch_name = branch_name
    return branch_name


def _email_log_branch_name(send_log: ScorecardEmailSendLog) -> str:
    metadata = send_log.metadata or {}
    return (metadata.get("branch_name") or "").strip().lower()


def _email_log_is_branch_scoped(send_log: ScorecardEmailSendLog) -> bool:
    return (send_log.event_code or "").strip() in BRANCH_SCOPED_EMAIL_EVENTS


def _email_log_visible_for_branch_name(send_log: ScorecardEmailSendLog, current_branch_name: str) -> bool:
    if not _email_log_is_branch_scoped(send_log):
        return True
    if not current_branch_name:
        return False
    return _email_log_branch_name(send_log) == current_branch_name


def _email_log_visible_in_current_branch(send_log: ScorecardEmailSendLog, request: HttpRequest | None) -> bool:
    return _email_log_visible_for_branch_name(send_log, _current_branch_name_for_request(request))


def _admin_users_with_email() -> list[Any]:
    user_model = get_user_model()
    return list(
        user_model.objects.filter(is_active=True)
        .filter(is_superuser=True)
        .exclude(email__isnull=True)
        .exclude(email__exact="")
        .distinct()
    )


SUBMITTED_REVIEW_PERMISSION_CODES = {
    "basel_score": ("scorecard.review_basel_scores",),
    "ifrs9_score": ("scorecard.review_ifrs9_scores",),
    "basel_template": ("scorecard.review_basel_templates",),
    "ifrs9_template": ("scorecard.review_ifrs9_templates",),
}

WITHOUT_SCORE_MAKER_PERMISSION_CODES = {
    "basel": ("scorecard.manage_basel_scores",),
    "ifrs9": ("scorecard.manage_ifrs9_scores",),
}

WEEKDAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _user_identity_key(user: Any) -> tuple[str, str]:
    user_id = getattr(user, "id", None)
    if user_id:
        return ("id", str(user_id))
    email = (getattr(user, "email", "") or "").strip().lower()
    return ("email", email)


def _active_users_with_email() -> list[Any]:
    user_model = get_user_model()
    try:
        return list(
            user_model.objects.filter(is_active=True)
            .exclude(email__isnull=True)
            .exclude(email__exact="")
            .distinct()
        )
    except (OperationalError, ProgrammingError):
        return []


def _users_with_any_permissions(permission_codes: Iterable[str]) -> list[Any]:
    permission_codes = tuple(permission_codes)
    users: list[Any] = []
    for user in _active_users_with_email():
        has_perm = getattr(user, "has_perm", lambda perm: False)
        if getattr(user, "is_superuser", False) or any(has_perm(permission) for permission in permission_codes):
            users.append(user)
    return users


def reviewer_recipients_for_submitted_item(
    item: Any,
    workflow_key: str,
    *,
    branch_name: str = "",
) -> list[Any]:
    permission_codes = SUBMITTED_REVIEW_PERMISSION_CODES.get(workflow_key, ())
    candidates: list[Any] = []
    checker = getattr(item, "checker", None)
    if checker is not None:
        candidates.append(checker)
    candidates.extend(_users_with_any_permissions(permission_codes))

    submitter = getattr(item, "submitted_by", None) or getattr(item, "maker", None)
    excluded_keys = {_user_identity_key(submitter)} if submitter is not None else set()
    limit_to_single_branch_reviewers = (
        workflow_key in {"basel_score", "ifrs9_score"}
        and bool(branch_name)
        and should_limit_submission_notifications_to_single_branch_reviewers()
    )
    recipients: list[Any] = []
    seen_keys: set[tuple[str, str]] = set()
    for user in candidates:
        if not user or not getattr(user, "is_active", False):
            continue
        if branch_name and not _user_can_receive_branch_email(user, branch_name):
            continue
        if limit_to_single_branch_reviewers and not _user_has_single_matching_branch_scope(user, branch_name):
            continue
        key = _user_identity_key(user)
        if not key[1] or key in excluded_keys or key in seen_keys:
            continue
        seen_keys.add(key)
        recipients.append(user)
    return recipients


def _score_maker_recipients_for_branch(
    score_type: str,
    branch_name: str,
    *,
    limit_to_single_branch: bool,
) -> list[Any]:
    permission_codes = WITHOUT_SCORE_MAKER_PERMISSION_CODES.get(score_type, ())
    recipients: list[Any] = []
    seen_keys: set[tuple[str, str]] = set()
    for user in _users_with_any_permissions(permission_codes):
        if branch_name and not _user_can_receive_branch_email(user, branch_name):
            continue
        if limit_to_single_branch and branch_name and not _user_has_single_matching_branch_scope(user, branch_name):
            continue
        key = _user_identity_key(user)
        if not key[1] or key in seen_keys:
            continue
        seen_keys.add(key)
        recipients.append(user)
    return recipients


def _load_template_text(file_name: str) -> str:
    path = TEMPLATE_ROOT / file_name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _ensure_email_defaults() -> tuple[ScorecardEmailConfiguration | None, dict[str, ScorecardEmailTemplate]]:
    if not _email_models_ready():
        return None, {}
    try:
        _ensure_email_configuration_schema()
        sender_email, sender_password = _resolved_system_sender()
        provider_setup = _infer_provider_setup(sender_email) if sender_email else {
            "host": "",
            "port": 587,
            "use_tls": True,
            "use_ssl": False,
        }
        configuration, _ = ScorecardEmailConfiguration.objects.get_or_create(
            pk=1,
            defaults={
                "name": "Primary Scorecard Email Configuration",
                "is_enabled": True,
                "send_workflow_emails": True,
                "send_failure_emails": True,
                "checker_pending_reminder_hours": 24,
                "without_score_summary_frequency": ScorecardEmailConfiguration.WITHOUT_SCORE_EMAIL_DAILY,
                "without_score_summary_hour": 8,
                "without_score_summary_weekday": 0,
                "without_score_summary_month_day": 1,
                "schedule_failure_repeat_threshold": 3,
                "schedule_failure_repeat_window_hours": 24,
                "application_base_url": _application_base_url(),
                "smtp_host": provider_setup["host"],
                "smtp_port": provider_setup["port"],
                "smtp_username": sender_email,
                "smtp_password": "",
                "smtp_use_tls": provider_setup["use_tls"],
                "smtp_use_ssl": provider_setup["use_ssl"],
                "from_email_override": sender_email,
                "footer_text": "This email was sent from the scorecard system because the event needs attention or action.",
            },
        )
        configuration = _apply_fixed_sender_configuration(configuration)

        template_queryset = ScorecardEmailTemplate.objects.only(
            "id",
            "event_code",
            "name",
            "category",
            "description",
            "is_enabled",
            "subject_template",
            "text_body_template",
            "html_body_template",
            "updated_at",
        )
        template_map: dict[str, ScorecardEmailTemplate] = {
            template.event_code: template for template in template_queryset
        }
        missing_templates: list[ScorecardEmailTemplate] = []
        for event_code, definition in EMAIL_EVENT_DEFINITIONS.items():
            if event_code in template_map:
                continue
            missing_templates.append(
                ScorecardEmailTemplate(
                    event_code=event_code,
                    name=definition["name"],
                    category=definition["category"],
                    description=definition["description"],
                    is_enabled=True,
                    subject_template=_load_template_text(definition["subject_file"]),
                    text_body_template=_load_template_text(definition["text_file"]),
                    html_body_template=_load_template_text(definition["html_file"]),
                )
            )
        if missing_templates:
            ScorecardEmailTemplate.objects.bulk_create(missing_templates)
            template_map = {
                template.event_code: template
                for template in ScorecardEmailTemplate.objects.only(
                    "id",
                    "event_code",
                    "name",
                    "category",
                    "description",
                    "is_enabled",
                    "subject_template",
                    "text_body_template",
                    "html_body_template",
                    "updated_at",
                )
            }
        return configuration, template_map
    except (OperationalError, ProgrammingError):
        return None, {}


def _render_template_string(template_string: str, context: dict[str, Any]) -> str:
    return Template(template_string).render(Context(context))


def _get_event_template(event_code: str) -> ScorecardEmailTemplate | None:
    _, template_map = _ensure_email_defaults()
    return template_map.get(event_code)


def _get_email_connection(
    configuration: ScorecardEmailConfiguration | None,
    *,
    fail_silently: bool = True,
):
    if configuration and _smtp_config_ready(configuration):
        smtp_password = _configured_sender_password_from_settings()
        return get_connection(
            backend="django.core.mail.backends.smtp.EmailBackend",
            host=configuration.smtp_host,
            port=configuration.smtp_port,
            username=configuration.smtp_username or "",
            password=smtp_password,
            use_tls=configuration.smtp_use_tls,
            use_ssl=configuration.smtp_use_ssl,
            fail_silently=fail_silently,
        )
    return get_connection(fail_silently=fail_silently)


def _email_event_allowed(event_code: str) -> tuple[bool, ScorecardEmailConfiguration | None, ScorecardEmailTemplate | None]:
    configuration, _ = _ensure_email_defaults()
    if not _is_transport_ready(configuration):
        return False, configuration, None
    if not configuration or not configuration.is_enabled:
        return False, configuration, None
    template = _get_event_template(event_code)
    if not template or not template.is_enabled:
        return False, configuration, template
    return True, configuration, template


def _context_related_reference(context: dict[str, Any]) -> str:
    evaluation = context.get("evaluation")
    if evaluation is not None:
        template_code = getattr(getattr(evaluation, "template", None), "code", "")
        return f"{template_code or 'EVAL'}:{getattr(evaluation, 'customer_id', '-')}"
    template = context.get("template")
    if template is not None:
        return f"{getattr(template, 'code', '-')}"
    import_run = context.get("import_run")
    if import_run is not None:
        return f"IMPORT-{getattr(import_run, 'id', '-')}"
    sync_run = context.get("sync_run")
    if sync_run is not None:
        reporting_date = getattr(sync_run, "reporting_date", None)
        return f"SYNC-{reporting_date or getattr(sync_run, 'id', '-')}"
    if context.get("without_score_summary"):
        return f"WITHOUT-SCORES:{context.get('branch_name') or context.get('branch_code') or '-'}"
    schedule_name = context.get("schedule_name")
    if schedule_name:
        return str(schedule_name)
    return ""



def _email_thread_domain(configuration: ScorecardEmailConfiguration | None = None) -> str:
    sender = _configured_from_email(configuration) if configuration else getattr(settings, "DEFAULT_FROM_EMAIL", "")
    if "@" in sender:
        domain = sender.rsplit("@", 1)[1].strip().lower()
    else:
        domain = "scorecard.local"
    safe = []
    for char in domain:
        safe.append(char if char.isalnum() or char in ".-" else "-")
    return "".join(safe).strip(".-") or "scorecard.local"


def _email_thread_safe(value: Any) -> str:
    raw = str(value or "").strip().lower()
    safe = []
    for char in raw:
        safe.append(char if char.isalnum() else "-")
    cleaned = "-".join(part for part in "".join(safe).split("-") if part)
    return cleaned[:80] or "unknown"


def _email_thread_month_key() -> str:
    return timezone.localtime(timezone.now()).strftime("%Y-%m")


def _monthly_thread_key(base_key: str) -> str:
    return f"{base_key}-{_email_thread_month_key()}"


def _thread_key_from_context(event_code: str, context: dict[str, Any]) -> str:
    thread_key_override = context.get("thread_key_override")
    if thread_key_override:
        return str(thread_key_override)

    category_thread_keys = {
        "basel_submitted": "scorecard-basel-score-submitted",
        "ifrs9_submitted": "scorecard-ifrs9-score-submitted",
        "basel_approved": "scorecard-basel-score-approved",
        "ifrs9_approved": "scorecard-ifrs9-score-approved",
        "basel_returned": "scorecard-basel-score-returned",
        "ifrs9_returned": "scorecard-ifrs9-score-returned",
        "basel_checker_pending_reminder": "scorecard-basel-score-pending-reminder",
        "ifrs9_checker_pending_reminder": "scorecard-ifrs9-score-pending-reminder",
        "basel_template_submitted": "scorecard-basel-template-submitted",
        "ifrs9_template_submitted": "scorecard-ifrs9-template-submitted",
        "basel_template_approved": "scorecard-basel-template-approved",
        "ifrs9_template_approved": "scorecard-ifrs9-template-approved",
        "basel_template_returned": "scorecard-basel-template-returned",
        "ifrs9_template_returned": "scorecard-ifrs9-template-returned",
        "without_score_summary": "scorecard-without-score-summary",
    }
    if event_code in category_thread_keys:
        return _monthly_thread_key(category_thread_keys[event_code])

    import_run = context.get("import_run")
    if import_run is not None:
        endpoint = getattr(import_run, "endpoint", None)
        endpoint_id = getattr(endpoint, "id", None)
        if endpoint_id:
            return _monthly_thread_key(f"scorecard-api-import-{endpoint_id}")
        return _monthly_thread_key(f"scorecard-api-import-run-{getattr(import_run, 'id', 'unknown')}")

    schedule = context.get("schedule")
    if schedule is not None:
        return _monthly_thread_key(f"scorecard-api-schedule-{getattr(schedule, 'id', _email_thread_safe(getattr(schedule, 'name', 'unknown')))}")

    schedule_name = context.get("schedule_name")
    if schedule_name:
        return _monthly_thread_key(f"scorecard-api-schedule-{_email_thread_safe(schedule_name)}")

    sync_run = context.get("sync_run")
    if sync_run is not None:
        reporting_date = getattr(sync_run, "reporting_date", None)
        if reporting_date:
            return _monthly_thread_key(f"scorecard-main-sync-{_email_thread_safe(reporting_date)}")
        return _monthly_thread_key(f"scorecard-main-sync-{getattr(sync_run, 'id', 'unknown')}")

    recipient_email = context.get("recipient_email")
    if event_code == "configuration_test" and recipient_email:
        return _monthly_thread_key(f"scorecard-email-test-{_email_thread_safe(recipient_email)}")

    return _monthly_thread_key(f"scorecard-{_email_thread_safe(event_code)}")



def _category_thread_subject(event_code: str, default_subject: str) -> str:
    category_subjects = {
        "basel_submitted": "Basel score submitted for review",
        "ifrs9_submitted": "IFRS9 score submitted for review",
        "basel_approved": "Basel score approved",
        "ifrs9_approved": "IFRS9 score approved",
        "basel_returned": "Basel score returned for changes",
        "ifrs9_returned": "IFRS9 score returned for changes",
        "basel_checker_pending_reminder": "Basel score pending checker review",
        "ifrs9_checker_pending_reminder": "IFRS9 score pending checker review",
        "basel_template_submitted": "Basel template submitted for review",
        "ifrs9_template_submitted": "IFRS9 template submitted for review",
        "basel_template_approved": "Basel template approved",
        "ifrs9_template_approved": "IFRS9 template approved",
        "basel_template_returned": "Basel template returned for changes",
        "ifrs9_template_returned": "IFRS9 template returned for changes",
        "without_score_summary": "Customers without Basel II/IFRS9 scores",
    }
    return category_subjects.get(event_code, default_subject)

def _previous_thread_metadata(thread_key: str, recipient_email: str) -> dict[str, str]:
    if not thread_key or not recipient_email or not _email_audit_ready():
        return {}
    try:
        previous = (
            ScorecardEmailSendLog.objects.filter(
                status=ScorecardEmailSendLog.STATUS_SUCCESS,
                recipient_email__iexact=recipient_email,
                metadata__thread_key=thread_key,
            )
            .exclude(metadata__message_id="")
            .order_by("-created_at", "-id")
            .first()
        )
    except (OperationalError, ProgrammingError):
        return {}
    return previous.metadata if previous and isinstance(previous.metadata, dict) else {}


def _build_thread_headers(
    *,
    event_code: str,
    context: dict[str, Any],
    configuration: ScorecardEmailConfiguration | None = None,
    recipient_email: str = "",
    retry_attempt: int | None = None,
) -> tuple[dict[str, str], str]:
    thread_key = _email_thread_safe(_thread_key_from_context(event_code, context))
    domain = _email_thread_domain(configuration)
    recipient_hash = hashlib.sha1((recipient_email or "recipient").encode("utf-8")).hexdigest()[:10]
    timestamp = timezone.now().strftime("%Y%m%d%H%M%S%f")
    retry_suffix = f".retry{retry_attempt}" if retry_attempt else ""
    message_id = f"<{thread_key}.{timestamp}.{recipient_hash}{retry_suffix}@{domain}>"
    previous_metadata = _previous_thread_metadata(thread_key, recipient_email)
    previous_message_id = (previous_metadata.get("message_id") or "").strip()
    previous_references = (previous_metadata.get("references") or "").strip()
    headers = {
        "Message-ID": message_id,
        "Thread-Topic": thread_key,
        "X-Scorecard-Thread": thread_key,
    }
    if previous_message_id:
        headers["In-Reply-To"] = previous_message_id
        headers["References"] = f"{previous_references} {previous_message_id}".strip()
    else:
        headers["References"] = message_id
    return headers, thread_key


def _create_email_send_log(
    *,
    configuration: ScorecardEmailConfiguration | None,
    template: ScorecardEmailTemplate | None,
    event_code: str,
    recipient_email: str,
    subject: str,
    text_body: str,
    html_body: str,
    status: str,
    error_message: str = "",
    related_reference: str = "",
    metadata: dict[str, Any] | None = None,
    retried_from: ScorecardEmailSendLog | None = None,
) -> None:
    if not _email_audit_ready():
        return
    try:
        ScorecardEmailSendLog.objects.create(
            configuration=configuration,
            event_code=event_code,
            event_name=getattr(template, "name", "") or event_code.replace("_", " ").title(),
            event_category=getattr(template, "category", ""),
            recipient_email=recipient_email,
            subject=subject[:255],
            text_body=text_body,
            html_body=html_body,
            status=status,
            error_message=error_message,
            related_reference=related_reference,
            metadata=metadata or {},
            retried_from=retried_from,
            sent_at=timezone.now() if status == ScorecardEmailSendLog.STATUS_SUCCESS else None,
        )
    except (OperationalError, ProgrammingError):
        return


def _send_event_email(
    *,
    event_code: str,
    to_emails: list[str],
    context: dict[str, Any],
) -> int:
    allowed, configuration, template = _email_event_allowed(event_code)
    if not allowed or not to_emails or not template:
        return 0

    render_context = dict(context)
    render_context["footer_text"] = configuration.footer_text if configuration else ""

    rendered_subject = _render_template_string(template.subject_template, render_context).strip().replace("\n", " ")
    subject = _category_thread_subject(event_code, rendered_subject)
    text_body = _render_template_string(template.text_body_template, render_context)
    html_source = template.html_body_template or "<html><body><pre>{{ body_text }}</pre><p>{{ footer_text }}</p></body></html>"
    if not template.html_body_template:
        render_context["body_text"] = text_body
    html_body = _render_template_string(html_source, render_context)
    related_reference = _context_related_reference(context)
    connection = _get_email_connection(configuration, fail_silently=False)
    sent_total = 0
    for recipient_email in to_emails:
        error_message = ""
        status = ScorecardEmailSendLog.STATUS_FAILED
        thread_headers: dict[str, str] = {}
        thread_key = ""
        try:
            thread_headers, thread_key = _build_thread_headers(
                event_code=event_code,
                context=context,
                configuration=configuration,
                recipient_email=recipient_email,
            )
            message = EmailMultiAlternatives(
                subject=subject,
                body=text_body,
                from_email=_configured_from_email(configuration),
                to=[recipient_email],
                reply_to=[configuration.reply_to_email] if configuration and configuration.reply_to_email else None,
                connection=connection,
                headers=thread_headers,
            )
            message.attach_alternative(html_body, "text/html")
            sent_count = message.send(fail_silently=False)
            if sent_count > 0:
                sent_total += sent_count
                status = ScorecardEmailSendLog.STATUS_SUCCESS
            else:
                error_message = "Email backend reported no delivery."
        except Exception as exc:
            error_message = str(exc) or exc.__class__.__name__
        _create_email_send_log(
            configuration=configuration,
            template=template,
            event_code=event_code,
            recipient_email=recipient_email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            status=status,
            error_message=error_message,
            related_reference=related_reference,
            metadata={
                "action_url": context.get("action_url", ""),
                "branch_name": (context.get("branch_name", "") or "").strip(),
                "thread_key": thread_key,
                "message_id": thread_headers.get("Message-ID", ""),
                "references": thread_headers.get("References", ""),
                "rendered_subject": rendered_subject,
            },
        )
    return sent_total


def _send_single_user_email(*, event_code: str, user: Any, context: dict[str, Any]) -> int:
    return _send_event_email(event_code=event_code, to_emails=_recipient_emails([user]), context=context)


def run_email_task_async(email_callable, *args, **kwargs) -> None:
    def _runner():
        close_old_connections()
        try:
            email_callable(*args, **kwargs)
        except Exception:
            logger.exception("Scorecard async email task failed")
        finally:
            close_old_connections()

    threading.Thread(target=_runner, daemon=True).start()


def _send_branch_scoped_single_user_email(*, event_code: str, user: Any, branch_name: str, context: dict[str, Any]) -> int:
    if not _user_can_receive_branch_email(user, branch_name):
        return 0
    return _send_single_user_email(event_code=event_code, user=user, context=context)


def send_configuration_test_email(
    configuration: ScorecardEmailConfiguration,
    recipient_email: str,
    thread_metadata: dict[str, str] | None = None,
) -> int:
    if not recipient_email:
        return 0
    if not _is_transport_ready(configuration):
        return 0
    context = {
        "recipient_email": recipient_email,
        "from_email": _configured_from_email(configuration),
        "footer_text": configuration.footer_text or "",
    }
    subject = "Scorecard email configuration test"
    text_body = (
        "Hello,\n\n"
        "This is a scorecard email configuration test.\n\n"
        f"From Email: {context['from_email']}\n"
        f"Recipient: {recipient_email}\n\n"
        f"{context['footer_text']}".strip()
    )
    html_body = (
        "<html><body style=\"font-family:Segoe UI, Arial, sans-serif; color:#17324d; line-height:1.5;\">"
        "<h2>Scorecard Email Configuration Test</h2>"
        "<p>This is a scorecard email configuration test.</p>"
        f"<p><strong>From Email:</strong> {context['from_email']}<br>"
        f"<strong>Recipient:</strong> {recipient_email}</p>"
        f"<p style=\"color:#5f7388;\">{context['footer_text']}</p>"
        "</body></html>"
    )
    thread_headers, thread_key = _build_thread_headers(
        event_code="configuration_test",
        context=context,
        configuration=configuration,
        recipient_email=recipient_email,
    )
    if thread_metadata is not None:
        thread_metadata.update(
            {
                "thread_key": thread_key,
                "message_id": thread_headers.get("Message-ID", ""),
                "references": thread_headers.get("References", ""),
                "rendered_subject": subject,
            }
        )
    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=context["from_email"],
        to=[recipient_email],
        reply_to=[configuration.reply_to_email] if configuration.reply_to_email else None,
        connection=_get_email_connection(configuration, fail_silently=False),
        headers=thread_headers,
    )
    message.attach_alternative(html_body, "text/html")
    return message.send(fail_silently=False)


def send_configuration_test_email_result(
    configuration: ScorecardEmailConfiguration,
    recipient_email: str,
) -> tuple[bool, str, dict[str, str]]:
    if not recipient_email:
        return False, "A test recipient email is required.", {}
    if not _is_transport_ready(configuration):
        return False, "Email transport is not ready yet. Configure the sender email and password in secure runtime settings first.", {}

    thread_metadata: dict[str, str] = {}
    try:
        sent_count = send_configuration_test_email(configuration, recipient_email, thread_metadata=thread_metadata)
    except smtplib.SMTPAuthenticationError as exc:
        message = str(exc) or exc.__class__.__name__
        sender_email = configuration.from_email_override or configuration.smtp_username or ""
        guidance = _provider_sign_in_guidance(sender_email)
        return False, f"Email sign-in failed. {guidance} Server response: {message}", thread_metadata
    except smtplib.SMTPException as exc:
        message = str(exc) or exc.__class__.__name__
        sender_email = configuration.from_email_override or configuration.smtp_username or ""
        guidance = _provider_sign_in_guidance(sender_email)
        return False, f"Test email could not be sent. {guidance} Server response: {message}", thread_metadata
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        sender_email = configuration.from_email_override or configuration.smtp_username or ""
        guidance = _provider_sign_in_guidance(sender_email)
        return False, f"Test email could not be sent. {guidance} Technical detail: {message}", thread_metadata

    if sent_count:
        return True, f"Test email was sent to '{recipient_email}'.", thread_metadata
    return False, "The test email could not be sent. Please confirm the email address and password.", thread_metadata


def _without_score_source_label(source_settings: dict[str, bool]) -> str:
    labels: list[str] = []
    if source_settings.get("include_loans"):
        labels.append("Loans")
    if source_settings.get("include_overdrafts"):
        labels.append("Overdrafts")
    return ", ".join(labels) if labels else "No enabled source"


def _without_score_summary_schedule_label(configuration: ScorecardEmailConfiguration) -> str:
    frequency = configuration.get_without_score_summary_frequency_display()
    hour = int(getattr(configuration, "without_score_summary_hour", 8) or 8)
    time_label = f"{hour:02d}:00"
    if configuration.without_score_summary_frequency == ScorecardEmailConfiguration.WITHOUT_SCORE_EMAIL_WEEKLY:
        weekday = int(getattr(configuration, "without_score_summary_weekday", 0) or 0)
        weekday_label = WEEKDAY_LABELS[weekday] if 0 <= weekday < len(WEEKDAY_LABELS) else "Monday"
        return f"{frequency}, every {weekday_label} at {time_label}"
    if configuration.without_score_summary_frequency == ScorecardEmailConfiguration.WITHOUT_SCORE_EMAIL_MONTHLY:
        month_day = int(getattr(configuration, "without_score_summary_month_day", 1) or 1)
        return f"{frequency}, day {month_day} at {time_label}"
    return f"{frequency} at {time_label}"


def _without_score_summary_due(configuration: ScorecardEmailConfiguration, now) -> bool:
    now_local = timezone.localtime(now)
    scheduled_hour = min(23, max(0, int(getattr(configuration, "without_score_summary_hour", 8) or 8)))
    scheduled_time = now_local.replace(hour=scheduled_hour, minute=0, second=0, microsecond=0)
    if now_local < scheduled_time:
        return False

    frequency = configuration.without_score_summary_frequency
    if frequency == ScorecardEmailConfiguration.WITHOUT_SCORE_EMAIL_WEEKLY:
        weekday = min(6, max(0, int(getattr(configuration, "without_score_summary_weekday", 0) or 0)))
        if now_local.weekday() != weekday:
            return False
    elif frequency == ScorecardEmailConfiguration.WITHOUT_SCORE_EMAIL_MONTHLY:
        month_day = min(28, max(1, int(getattr(configuration, "without_score_summary_month_day", 1) or 1)))
        if now_local.day != month_day:
            return False

    last_sent_at = getattr(configuration, "without_score_summary_last_sent_at", None)
    if not last_sent_at:
        return True
    last_local = timezone.localtime(last_sent_at)
    if frequency == ScorecardEmailConfiguration.WITHOUT_SCORE_EMAIL_WEEKLY:
        return last_local.isocalendar()[:2] != now_local.isocalendar()[:2]
    if frequency == ScorecardEmailConfiguration.WITHOUT_SCORE_EMAIL_MONTHLY:
        return (last_local.year, last_local.month) != (now_local.year, now_local.month)
    return last_local.date() < now_local.date()


def _without_score_summary_branch_context(
    *,
    branch: BankBranch,
    snapshot: dict[str, Any],
    source_settings: dict[str, bool],
    generated_at,
    schedule_label: str,
) -> dict[str, Any]:
    basel_rows = list(snapshot.get("basel_rows") or [])
    ifrs9_rows = list(snapshot.get("ifrs9_rows") or [])
    branch_name = (getattr(branch, "branch_name", "") or "").strip()
    return {
        "without_score_summary": True,
        "branch": branch,
        "branch_name": branch_name,
        "branch_code": (getattr(branch, "branch_code", "") or "").strip(),
        "bank_name": (getattr(branch, "bank_name", "") or "").strip(),
        "reporting_date": snapshot.get("reporting_date"),
        "source_label": _without_score_source_label(source_settings),
        "schedule_label": schedule_label,
        "generated_at": timezone.localtime(generated_at),
        "basel_rows": basel_rows,
        "ifrs9_rows": ifrs9_rows,
        "basel_count": len(basel_rows),
        "ifrs9_count": len(ifrs9_rows),
        "total_missing_count": len(basel_rows) + len(ifrs9_rows),
        "total_stage_rows": int(snapshot.get("total_stage_rows") or 0),
        "action_url": _safe_reverse("scorecard:customer_list"),
    }


def send_due_without_score_summary_emails(now=None) -> dict[str, Any]:
    configuration, _ = _ensure_email_defaults()
    if not configuration:
        return {"performed": False, "reason": "email_configuration_unavailable"}
    if not configuration.is_enabled or not configuration.send_workflow_emails:
        return {"performed": False, "reason": "email_disabled"}

    allowed, _, template = _email_event_allowed("without_score_summary")
    if not allowed or not template:
        return {"performed": False, "reason": "without_score_summary_email_disabled"}

    now = now or timezone.now()
    if not _without_score_summary_due(configuration, now):
        return {"performed": False, "reason": "not_due"}

    source_settings = get_without_score_list_customer_sources()
    schedule_label = _without_score_summary_schedule_label(configuration)
    limit_to_single_branch_makers = should_limit_without_score_notifications_to_single_branch_makers()
    branch_results: list[dict[str, Any]] = []
    sent_total = 0

    if not source_settings.get("include_loans") and not source_settings.get("include_overdrafts"):
        configuration.without_score_summary_last_sent_at = now
        configuration.save(update_fields=["without_score_summary_last_sent_at", "updated_at"])
        return {
            "performed": True,
            "reason": "no_enabled_sources",
            "sent": 0,
            "branches": 0,
            "source_settings": source_settings,
        }

    branches = list(BankBranch.objects.all().order_by("bank_name", "branch_name", "branch_code"))
    for branch in branches:
        branch_name = (branch.branch_name or "").strip()
        if not branch_name:
            continue
        snapshot = build_without_score_customer_snapshot_for_branch_scope([branch], source_settings)
        basel_rows = list(snapshot.get("basel_rows") or [])
        ifrs9_rows = list(snapshot.get("ifrs9_rows") or [])
        if not basel_rows and not ifrs9_rows:
            continue

        recipients: list[Any] = []
        if basel_rows:
            recipients.extend(
                _score_maker_recipients_for_branch(
                    "basel",
                    branch_name,
                    limit_to_single_branch=limit_to_single_branch_makers,
                )
            )
        if ifrs9_rows:
            recipients.extend(
                _score_maker_recipients_for_branch(
                    "ifrs9",
                    branch_name,
                    limit_to_single_branch=limit_to_single_branch_makers,
                )
            )
        recipient_emails = _recipient_emails(recipients)
        if not recipient_emails:
            branch_results.append(
                {
                    "branch": branch_name,
                    "basel_missing": len(basel_rows),
                    "ifrs9_missing": len(ifrs9_rows),
                    "sent": 0,
                    "reason": "no_recipients",
                }
            )
            continue

        context = _without_score_summary_branch_context(
            branch=branch,
            snapshot=snapshot,
            source_settings=source_settings,
            generated_at=now,
            schedule_label=schedule_label,
        )
        sent = _send_event_email(
            event_code="without_score_summary",
            to_emails=recipient_emails,
            context=context,
        )
        sent_total += sent
        branch_results.append(
            {
                "branch": branch_name,
                "basel_missing": len(basel_rows),
                "ifrs9_missing": len(ifrs9_rows),
                "recipients": len(recipient_emails),
                "sent": sent,
            }
        )

    configuration.without_score_summary_last_sent_at = now
    configuration.save(update_fields=["without_score_summary_last_sent_at", "updated_at"])
    return {
        "performed": True,
        "reason": "sent" if sent_total else "no_recipients_or_no_rows",
        "sent": sent_total,
        "branches": len(branch_results),
        "source_settings": source_settings,
        "single_branch_only": limit_to_single_branch_makers,
        "branch_results": branch_results,
    }


def retry_email_send_log(send_log: ScorecardEmailSendLog) -> tuple[bool, str]:
    configuration, _ = _ensure_email_defaults()
    if not _is_transport_ready(configuration):
        return False, "Email transport is not ready yet."
    try:
        thread_key = (send_log.metadata or {}).get("thread_key", "")
        thread_context = {
            "recipient_email": send_log.recipient_email,
            "schedule_name": (send_log.metadata or {}).get("schedule_name", ""),
            "thread_key_override": thread_key,
        }
        thread_headers, retry_thread_key = _build_thread_headers(
            event_code=send_log.event_code,
            context=thread_context,
            configuration=configuration,
            recipient_email=send_log.recipient_email,
            retry_attempt=send_log.retry_attempts.count() + 1,
        )
        if thread_key:
            thread_headers["X-Scorecard-Thread"] = _email_thread_safe(thread_key)
            retry_thread_key = _email_thread_safe(thread_key)
        message = EmailMultiAlternatives(
            subject=send_log.subject,
            body=send_log.text_body,
            from_email=_configured_from_email(configuration),
            to=[send_log.recipient_email],
            reply_to=[configuration.reply_to_email] if configuration and configuration.reply_to_email else None,
            connection=_get_email_connection(configuration, fail_silently=False),
            headers=thread_headers,
        )
        if send_log.html_body:
            message.attach_alternative(send_log.html_body, "text/html")
        sent_count = message.send(fail_silently=False)
        status = ScorecardEmailSendLog.STATUS_SUCCESS if sent_count > 0 else ScorecardEmailSendLog.STATUS_FAILED
        error_message = "" if sent_count > 0 else "Email backend reported no delivery."
    except Exception as exc:
        sent_count = 0
        status = ScorecardEmailSendLog.STATUS_FAILED
        error_message = str(exc) or exc.__class__.__name__

    _create_email_send_log(
        configuration=configuration,
        template=_get_event_template(send_log.event_code),
        event_code=send_log.event_code,
        recipient_email=send_log.recipient_email,
        subject=send_log.subject,
        text_body=send_log.text_body,
        html_body=send_log.html_body,
        status=status,
        error_message=error_message,
        related_reference=send_log.related_reference,
        metadata={
            **(send_log.metadata or {}),
            "thread_key": retry_thread_key if "retry_thread_key" in locals() else (send_log.metadata or {}).get("thread_key", ""),
            "message_id": thread_headers.get("Message-ID", "") if "thread_headers" in locals() else "",
            "references": thread_headers.get("References", "") if "thread_headers" in locals() else "",
        },
        retried_from=send_log,
    )
    if sent_count > 0:
        return True, f"Email retry sent successfully to {send_log.recipient_email}."
    return False, f"Email retry failed for {send_log.recipient_email}. {error_message}".strip()


def retry_recent_failed_email_sends(limit: int = 10) -> tuple[int, int]:
    if not _email_audit_ready():
        return 0, 0
    failed_logs = list(
        ScorecardEmailSendLog.objects.filter(status=ScorecardEmailSendLog.STATUS_FAILED, retried_from__isnull=True)
        .order_by("-created_at", "-id")[:limit]
    )
    success_count = 0
    for send_log in failed_logs:
        success, _ = retry_email_send_log(send_log)
        if success:
            success_count += 1
    return len(failed_logs), success_count


def _score_action_context(*, evaluation: Any, actor: Any, action_url: str, score_label: str) -> dict[str, Any]:
    return {
        "evaluation": evaluation,
        "actor_name": _display_name(actor) or "System",
        "score_label": score_label,
        "customer_name": getattr(evaluation, "customer_name", "-"),
        "customer_code": getattr(evaluation, "customer_id", "-"),
        "branch_name": getattr(evaluation, "branch_name", "-"),
        "grade": getattr(evaluation, "final_grade", "") or "-",
        "action_url": action_url,
    }


def _template_action_context(
    *,
    template: Any,
    actor: Any,
    action_url: str,
    workflow_label: str,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "template": template,
        "actor_name": _display_name(actor) or "System",
        "workflow_label": workflow_label,
        "template_code": getattr(template, "code", "-"),
        "template_name": getattr(template, "name", "-"),
        "reason": reason,
        "action_url": action_url,
    }


def _workflow_owner(item: Any) -> Any:
    """Return the user currently responsible for a maker-checker item."""
    return getattr(item, "submitted_by", None) or getattr(item, "maker", None)


def send_api_import_failed_email(import_run: Any) -> int:
    if getattr(import_run, "status", "") != "failed":
        return 0
    endpoint = getattr(import_run, "endpoint", None)
    context = {
        "import_run": import_run,
        "endpoint_name": getattr(endpoint, "name", "Unknown endpoint"),
        "endpoint_code": getattr(endpoint, "code", "-"),
        "target_table": getattr(endpoint, "get_target_table_display", lambda: "-")(),
        "triggered_by_name": _display_name(getattr(import_run, "triggered_by", None)) or "System",
        "action_url": _safe_reverse("scorecard:api_import"),
    }
    return _send_event_email(
        event_code="api_import_failed",
        to_emails=_recipient_emails(_admin_users_with_email()),
        context=context,
    )


def send_api_schedule_failed_email(schedule: Any, error_message: str) -> int:
    context = {
        "schedule": schedule,
        "schedule_name": getattr(schedule, "name", "Unknown schedule"),
        "endpoint_name": getattr(getattr(schedule, "endpoint", None), "name", None)
        or getattr(getattr(schedule, "endpoint", None), "code", None)
        or "-",
        "frequency_label": getattr(schedule, "get_frequency_display", lambda: "-")(),
        "last_run_at": getattr(schedule, "last_run_at", None),
        "next_run_at": getattr(schedule, "next_run_at", None),
        "failure_reason": error_message or getattr(schedule, "last_message", "") or "No failure details were captured.",
        "action_url": _safe_reverse("scorecard:api_scheduler"),
    }
    return _send_event_email(
        event_code="api_schedule_failed",
        to_emails=_recipient_emails(_admin_users_with_email()),
        context=context,
    )


def send_main_sync_failed_email(sync_run: Any) -> int:
    if getattr(sync_run, "status", "") != "failed":
        return 0
    context = {
        "sync_run": sync_run,
        "triggered_by_name": _display_name(getattr(sync_run, "triggered_by", None)) or "System",
        "action_url": _safe_reverse("scorecard:api_main_sync"),
    }
    return _send_event_email(
        event_code="main_sync_failed",
        to_emails=_recipient_emails(_admin_users_with_email()),
        context=context,
    )


def send_api_schedule_repeated_failure_email(schedule: Any, failure_count: int, latest_error: str) -> int:
    context = {
        "schedule": schedule,
        "schedule_name": getattr(schedule, "name", "Unknown schedule"),
        "endpoint_name": getattr(getattr(schedule, "endpoint", None), "name", None)
        or getattr(getattr(schedule, "endpoint", None), "code", None)
        or "-",
        "frequency_label": getattr(schedule, "get_frequency_display", lambda: "-")(),
        "failure_count": failure_count,
        "latest_error": latest_error,
        "last_run_at": getattr(schedule, "last_run_at", None),
        "action_url": _safe_reverse("scorecard:api_scheduler"),
    }
    return _send_event_email(
        event_code="api_schedule_repeated_failure",
        to_emails=_recipient_emails(_admin_users_with_email()),
        context=context,
    )


def send_basel_checker_pending_reminder_email(evaluation: Any) -> int:
    recipient = getattr(evaluation, "checker", None)
    if not recipient:
        return 0
    return _send_branch_scoped_single_user_email(
        event_code="basel_checker_pending_reminder",
        user=recipient,
        branch_name=getattr(evaluation, "branch_name", ""),
        context=_score_action_context(
            evaluation=evaluation,
            actor=_workflow_owner(evaluation),
            action_url=_safe_reverse("scorecard:checker_review", kwargs={"evaluation_id": evaluation.id}),
            score_label="Basel score",
        ) | {"pending_since": getattr(evaluation, "submitted_at", None) or getattr(evaluation, "updated_at", None)},
    )


def send_ifrs9_checker_pending_reminder_email(evaluation: Any) -> int:
    recipient = getattr(evaluation, "checker", None)
    if not recipient:
        return 0
    return _send_branch_scoped_single_user_email(
        event_code="ifrs9_checker_pending_reminder",
        user=recipient,
        branch_name=getattr(evaluation, "branch_name", ""),
        context=_score_action_context(
            evaluation=evaluation,
            actor=_workflow_owner(evaluation),
            action_url=_safe_reverse("scorecard:checker_ifrs9_scores_review", kwargs={"evaluation_id": evaluation.id}),
            score_label="IFRS9 score",
        ) | {"pending_since": getattr(evaluation, "submitted_at", None) or getattr(evaluation, "updated_at", None)},
    )


def send_basel_submitted_email(evaluation: Any) -> int:
    branch_name = getattr(evaluation, "branch_name", "")
    recipients = reviewer_recipients_for_submitted_item(
        evaluation,
        "basel_score",
        branch_name=branch_name,
    )
    return _send_event_email(
        event_code="basel_submitted",
        to_emails=_recipient_emails(recipients),
        context=_score_action_context(
            evaluation=evaluation,
            actor=getattr(evaluation, "submitted_by", None) or getattr(evaluation, "maker", None),
            action_url=_safe_reverse("scorecard:checker_review", kwargs={"evaluation_id": evaluation.id}),
            score_label="Basel score",
        ),
    )


def send_ifrs9_submitted_email(evaluation: Any) -> int:
    branch_name = getattr(evaluation, "branch_name", "")
    recipients = reviewer_recipients_for_submitted_item(
        evaluation,
        "ifrs9_score",
        branch_name=branch_name,
    )
    return _send_event_email(
        event_code="ifrs9_submitted",
        to_emails=_recipient_emails(recipients),
        context=_score_action_context(
            evaluation=evaluation,
            actor=getattr(evaluation, "submitted_by", None) or getattr(evaluation, "maker", None),
            action_url=_safe_reverse("scorecard:checker_ifrs9_scores_review", kwargs={"evaluation_id": evaluation.id}),
            score_label="IFRS9 score",
        ),
    )


def send_basel_approved_email(evaluation: Any, approver: Any) -> int:
    return _send_branch_scoped_single_user_email(
        event_code="basel_approved",
        user=_workflow_owner(evaluation),
        branch_name=getattr(evaluation, "branch_name", ""),
        context=_score_action_context(
            evaluation=evaluation,
            actor=approver,
            action_url=_safe_reverse("scorecard:basel_scores_view_detail", kwargs={"evaluation_id": evaluation.id}),
            score_label="Basel score",
        ),
    )


def send_ifrs9_approved_email(evaluation: Any, approver: Any) -> int:
    return _send_branch_scoped_single_user_email(
        event_code="ifrs9_approved",
        user=_workflow_owner(evaluation),
        branch_name=getattr(evaluation, "branch_name", ""),
        context=_score_action_context(
            evaluation=evaluation,
            actor=approver,
            action_url=_safe_reverse("scorecard:ifrs9_scores_view_detail", kwargs={"evaluation_id": evaluation.id}),
            score_label="IFRS9 score",
        ),
    )


def send_basel_returned_email(evaluation: Any, checker: Any, reason: str) -> int:
    context = _score_action_context(
        evaluation=evaluation,
        actor=checker,
        action_url=_safe_reverse("scorecard:basel_scores_edit", kwargs={"evaluation_id": evaluation.id}),
        score_label="Basel score",
    )
    context["reason"] = reason
    return _send_branch_scoped_single_user_email(
        event_code="basel_returned",
        user=_workflow_owner(evaluation),
        branch_name=getattr(evaluation, "branch_name", ""),
        context=context,
    )


def send_ifrs9_returned_email(evaluation: Any, checker: Any, reason: str) -> int:
    context = _score_action_context(
        evaluation=evaluation,
        actor=checker,
        action_url=_safe_reverse("scorecard:ifrs9_scores_edit", kwargs={"evaluation_id": evaluation.id}),
        score_label="IFRS9 score",
    )
    context["reason"] = reason
    return _send_branch_scoped_single_user_email(
        event_code="ifrs9_returned",
        user=_workflow_owner(evaluation),
        branch_name=getattr(evaluation, "branch_name", ""),
        context=context,
    )


def send_basel_template_submitted_email(template: Any) -> int:
    recipients = reviewer_recipients_for_submitted_item(template, "basel_template")
    return _send_event_email(
        event_code="basel_template_submitted",
        to_emails=_recipient_emails(recipients),
        context=_template_action_context(
            template=template,
            actor=getattr(template, "submitted_by", None) or getattr(template, "maker", None),
            action_url=_safe_reverse("scorecard:template_checker_review", kwargs={"template_id": template.id}),
            workflow_label="Basel template",
        ),
    )


def send_ifrs9_template_submitted_email(template: Any) -> int:
    recipients = reviewer_recipients_for_submitted_item(template, "ifrs9_template")
    return _send_event_email(
        event_code="ifrs9_template_submitted",
        to_emails=_recipient_emails(recipients),
        context=_template_action_context(
            template=template,
            actor=getattr(template, "submitted_by", None) or getattr(template, "maker", None),
            action_url=_safe_reverse("scorecard:ifrs9_template_checker_review", kwargs={"template_id": template.id}),
            workflow_label="IFRS9 template",
        ),
    )


def send_basel_template_approved_email(template: Any, approver: Any) -> int:
    return _send_single_user_email(
        event_code="basel_template_approved",
        user=_workflow_owner(template),
        context=_template_action_context(
            template=template,
            actor=approver,
            action_url=_safe_reverse("scorecard:template_maker_view", kwargs={"template_id": template.id}),
            workflow_label="Basel template",
        ),
    )


def send_ifrs9_template_approved_email(template: Any, approver: Any) -> int:
    return _send_single_user_email(
        event_code="ifrs9_template_approved",
        user=_workflow_owner(template),
        context=_template_action_context(
            template=template,
            actor=approver,
            action_url=_safe_reverse("scorecard:ifrs9_template_maker_view", kwargs={"template_id": template.id}),
            workflow_label="IFRS9 template",
        ),
    )


def send_basel_template_returned_email(template: Any, checker: Any, reason: str) -> int:
    return _send_single_user_email(
        event_code="basel_template_returned",
        user=_workflow_owner(template),
        context=_template_action_context(
            template=template,
            actor=checker,
            action_url=_safe_reverse("scorecard:template_maker_view", kwargs={"template_id": template.id}),
            workflow_label="Basel template",
            reason=reason,
        ),
    )


def send_ifrs9_template_returned_email(template: Any, checker: Any, reason: str) -> int:
    return _send_single_user_email(
        event_code="ifrs9_template_returned",
        user=_workflow_owner(template),
        context=_template_action_context(
            template=template,
            actor=checker,
            action_url=_safe_reverse("scorecard:ifrs9_template_maker_view", kwargs={"template_id": template.id}),
            workflow_label="IFRS9 template",
            reason=reason,
        ),
    )


def _email_navigation_items() -> list[dict[str, str]]:
    return [
        {
            "key": "configuration",
            "label": "Email Configuration",
            "description": "Manage the built-in system mailbox, delivery switches, reminder timing, footer text, and testing.",
            "url": _relative_reverse("scorecard:email_configuration"),
        },
        {
            "key": "delivery",
            "label": "Email Delivery",
            "description": "Review email delivery activity, retry failed sends, and monitor reminder status.",
            "url": _relative_reverse("scorecard:email_delivery"),
        },
        {
            "key": "templates",
            "label": "Email Templates",
            "description": "Update the workflow and failure email wording in separate email template sections.",
            "url": _relative_reverse("scorecard:email_templates"),
        },
    ]


def _email_workspace_cache_key(suffix: str) -> str:
    return f"scorecard:email_workspace:{suffix}"


def _get_email_workspace_summary(*, configuration: ScorecardEmailConfiguration | None, template_records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cache_key = _email_workspace_cache_key("summary")
    cached_summary = cache.get(cache_key)
    if cached_summary is None:
        audit_ready = _email_audit_ready()
        failed_send_count = 0
        success_send_count = 0
        total_send_count = 0
        if audit_ready:
            try:
                total_send_count = ScorecardEmailSendLog.objects.count()
                failed_send_count = ScorecardEmailSendLog.objects.filter(status=ScorecardEmailSendLog.STATUS_FAILED).count()
                success_send_count = ScorecardEmailSendLog.objects.filter(status=ScorecardEmailSendLog.STATUS_SUCCESS).count()
            except (OperationalError, ProgrammingError):
                audit_ready = False
        if template_records is None:
            template_enabled_count = 0
            template_total_count = 0
            if _email_models_ready():
                try:
                    template_total_count = ScorecardEmailTemplate.objects.count()
                    template_enabled_count = ScorecardEmailTemplate.objects.filter(is_enabled=True).count()
                except (OperationalError, ProgrammingError):
                    template_total_count = 0
                    template_enabled_count = 0
        else:
            template_total_count = len(template_records)
            template_enabled_count = sum(1 for record in template_records if record["is_enabled"])
        cached_summary = {
            "audit_ready": audit_ready,
            "failed_send_count": failed_send_count,
            "success_send_count": success_send_count,
            "total_send_count": total_send_count,
            "covered_area_total_count": template_total_count,
            "covered_area_enabled_count": template_enabled_count,
        }
        cache.set(cache_key, cached_summary, EMAIL_WORKSPACE_CACHE_TTL_SECONDS)
    elif template_records is not None:
        cached_summary = {
            **cached_summary,
            "covered_area_total_count": len(template_records),
            "covered_area_enabled_count": sum(1 for record in template_records if record["is_enabled"]),
        }
    return {
        **cached_summary,
        "transport_ready": _is_transport_ready(configuration),
    }


def _clear_email_workspace_caches() -> None:
    cache.delete(_email_workspace_cache_key("summary"))


def _email_page_context(
    *,
    configuration: ScorecardEmailConfiguration | None,
    current_page: str,
    extra_context: dict[str, Any] | None = None,
    template_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    context = {
        "email_navigation": _email_navigation_items(),
        "email_current_page": current_page,
        "email_configuration": configuration,
        "password_is_saved": bool(_configured_sender_password_from_settings()),
        "email_summary": _get_email_workspace_summary(configuration=configuration, template_records=template_records),
    }
    if extra_context:
        context.update(extra_context)
    return context


def _paginate_email_logs(
    queryset,
    *,
    page_number: str | None,
    per_page_value: str | None,
    default_per_page: int,
):
    per_page = _normalize_email_log_per_page(per_page_value, default_per_page)
    paginator = Paginator(queryset, per_page)
    page_obj = paginator.get_page(page_number or 1)
    return paginator, page_obj, per_page


def _normalize_email_log_per_page(per_page_value: str | None, default_per_page: int) -> int:
    allowed_per_page = {10, 20, 50, 100}
    try:
        per_page = int(per_page_value or default_per_page)
    except (TypeError, ValueError):
        per_page = default_per_page
    if per_page not in allowed_per_page:
        per_page = default_per_page
    return per_page


def _paginate_branch_visible_email_logs(
    queryset,
    *,
    request: HttpRequest | None,
    page_number: str | None,
    per_page_value: str | None,
    default_per_page: int,
):
    per_page = _normalize_email_log_per_page(per_page_value, default_per_page)
    try:
        requested_page = int(page_number or 1)
    except (TypeError, ValueError):
        requested_page = 1
    if requested_page < 1:
        requested_page = 1

    slice_start = (requested_page - 1) * per_page
    slice_end = slice_start + per_page
    current_branch_name = _current_branch_name_for_request(request)

    visible_total = 0
    visible_logs = []
    for send_log in queryset.iterator(chunk_size=200):
        if not _email_log_visible_for_branch_name(send_log, current_branch_name):
            continue
        if slice_start <= visible_total < slice_end:
            visible_logs.append(send_log)
        visible_total += 1

    paginator = Paginator(range(visible_total), per_page)
    page_obj = paginator.get_page(requested_page)
    return paginator, page_obj, per_page, visible_logs


def _email_delivery_context_data(request: HttpRequest | None = None) -> dict[str, Any]:
    recent_send_logs: list[dict[str, Any]] = []
    failed_send_logs: list[dict[str, Any]] = []
    reminder_status = {
        "pending_basel_reviews": 0,
        "pending_ifrs9_reviews": 0,
        "repeated_failed_schedules": 0,
        "checker_threshold_hours": 24,
        "schedule_threshold_count": 3,
        "schedule_window_hours": 24,
    }
    configuration, _ = _ensure_email_defaults()
    reminder_status["checker_threshold_hours"] = getattr(configuration, "checker_pending_reminder_hours", 24) if configuration else 24
    reminder_status["schedule_threshold_count"] = getattr(configuration, "schedule_failure_repeat_threshold", 3) if configuration else 3
    reminder_status["schedule_window_hours"] = getattr(configuration, "schedule_failure_repeat_window_hours", 24) if configuration else 24
    activity_paginator = None
    activity_page = None
    failed_paginator = None
    failed_page = None
    activity_search = (request.GET.get("activity_search") if request else "") or ""
    activity_status = (request.GET.get("activity_status") if request else "") or ""
    failed_search = (request.GET.get("failed_search") if request else "") or ""

    if _email_audit_ready():
        try:
            recent_qs = ScorecardEmailSendLog.objects.only(
                "id",
                "event_code",
                "event_name",
                "recipient_email",
                "status",
                "error_message",
                "related_reference",
                "metadata",
                "created_at",
                "retried_from_id",
            ).order_by("-created_at", "-id")
            failed_qs = ScorecardEmailSendLog.objects.filter(
                status=ScorecardEmailSendLog.STATUS_FAILED
            ).only(
                "id",
                "event_code",
                "event_name",
                "recipient_email",
                "status",
                "error_message",
                "related_reference",
                "metadata",
                "created_at",
                "retried_from_id",
            ).order_by("-created_at", "-id")
            if activity_search:
                recent_qs = recent_qs.filter(
                    Q(event_name__icontains=activity_search)
                    | Q(event_code__icontains=activity_search)
                    | Q(recipient_email__icontains=activity_search)
                    | Q(related_reference__icontains=activity_search)
                    | Q(error_message__icontains=activity_search)
                )
            if activity_status in {ScorecardEmailSendLog.STATUS_SUCCESS, ScorecardEmailSendLog.STATUS_FAILED}:
                recent_qs = recent_qs.filter(status=activity_status)
            if failed_search:
                failed_qs = failed_qs.filter(
                    Q(event_name__icontains=failed_search)
                    | Q(event_code__icontains=failed_search)
                    | Q(recipient_email__icontains=failed_search)
                    | Q(related_reference__icontains=failed_search)
                    | Q(error_message__icontains=failed_search)
                )
            activity_paginator, activity_page, activity_per_page, visible_recent_logs = _paginate_branch_visible_email_logs(
                recent_qs,
                request=request,
                page_number=request.GET.get("activity_page") if request else None,
                per_page_value=request.GET.get("activity_per_page") if request else None,
                default_per_page=20,
            )
            failed_paginator, failed_page, failed_per_page, visible_failed_logs = _paginate_branch_visible_email_logs(
                failed_qs,
                request=request,
                page_number=request.GET.get("failed_page") if request else None,
                per_page_value=request.GET.get("failed_per_page") if request else None,
                default_per_page=10,
            )
            recent_send_logs = [
                {
                    "id": log.id,
                    "event_name": log.event_name or log.event_code.replace("_", " ").title(),
                    "recipient_email": log.recipient_email,
                    "related_reference": log.related_reference or "-",
                    "status": log.status,
                    "status_label": "Success" if log.status == ScorecardEmailSendLog.STATUS_SUCCESS else "Failed",
                    "error_message": log.error_message or "-",
                    "created_at": log.created_at,
                    "is_retry": bool(log.retried_from_id),
                }
                for log in visible_recent_logs
            ]
            failed_send_logs = [
                {
                    "id": log.id,
                    "event_name": log.event_name or log.event_code.replace("_", " ").title(),
                    "recipient_email": log.recipient_email,
                    "related_reference": log.related_reference or "-",
                    "error_message": log.error_message or "No error details captured.",
                    "created_at": log.created_at,
                }
                for log in visible_failed_logs
            ]
        except (OperationalError, ProgrammingError):
            recent_send_logs = []
            failed_send_logs = []
    try:
        checker_cutoff = timezone.now() - timedelta(hours=reminder_status["checker_threshold_hours"])
        schedule_cutoff = timezone.now() - timedelta(hours=reminder_status["schedule_window_hours"])
        reminder_status["pending_basel_reviews"] = CreditEvaluation.objects.filter(
            status="submitted",
            checker__isnull=False,
            created_at__lte=checker_cutoff,
        ).count()
        reminder_status["pending_ifrs9_reviews"] = IFRS9Evaluation.objects.filter(
            status="submitted",
            checker__isnull=False,
            created_at__lte=checker_cutoff,
        ).count()
        repeated_schedule_rows = (
            ApiImportRun.objects.filter(
                run_source=ApiImportRun.SOURCE_SCHEDULE,
                status=ApiImportRun.STATUS_FAILED,
                completed_at__gte=schedule_cutoff,
                schedule__isnull=False,
            )
            .values("schedule_id")
            .annotate(failure_count=Count("id"))
            .filter(failure_count__gte=reminder_status["schedule_threshold_count"])
        )
        reminder_status["repeated_failed_schedules"] = repeated_schedule_rows.count()
    except (OperationalError, ProgrammingError):
        pass
    return {
        "recent_send_logs": recent_send_logs,
        "failed_send_logs": failed_send_logs,
        "reminder_status": reminder_status,
        "activity_page_obj": activity_page,
        "activity_paginator": activity_paginator,
        "activity_per_page": activity_page.paginator.per_page if activity_page else 20,
        "activity_search": activity_search,
        "activity_status": activity_status,
        "failed_page_obj": failed_page,
        "failed_paginator": failed_paginator,
        "failed_per_page": failed_page.paginator.per_page if failed_page else 10,
        "failed_search": failed_search,
    }


def _email_template_records(template_map: dict[str, ScorecardEmailTemplate]) -> list[dict[str, Any]]:
    template_records: list[dict[str, Any]] = []
    for event_code, definition in EMAIL_EVENT_DEFINITIONS.items():
        template = template_map.get(event_code)
        if not template:
            continue
        template_records.append(
            {
                "id": template.id,
                "event_code": template.event_code,
                "name": template.name,
                "category_key": template.category,
                "category_label": template.get_category_display(),
                "description": template.description or definition["description"],
                "is_enabled": template.is_enabled,
                "status_label": "Enabled" if template.is_enabled else "Disabled",
                "subject_template": template.subject_template,
                "text_body_template": template.text_body_template,
                "html_body_template": template.html_body_template,
                "edit_url": f"{_relative_reverse('scorecard:email_templates')}?edit={template.event_code}",
                "toggle_field_name": f"event_enabled__{template.event_code}",
            }
        )
    return template_records


def _email_template_sections(template_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "key": "workflow",
            "title": "Workflow Areas",
            "subtitle": "Enable or disable notifications for score submissions, approvals, returns, template workflow, and checker reminders.",
            "records": [record for record in template_records if record["category_key"] == ScorecardEmailTemplate.CATEGORY_WORKFLOW],
        },
        {
            "key": "failure",
            "title": "Failure Areas",
            "subtitle": "Enable or disable operational alerts for imports, schedules, sync failures, and repeated schedule failures.",
            "records": [record for record in template_records if record["category_key"] == ScorecardEmailTemplate.CATEGORY_FAILURE],
        },
    ]


@login_required
def email_configuration_view(request: HttpRequest) -> HttpResponse:
    can_view = request.user.is_superuser or request.user.has_perm("scorecard.view_scorecard_email") or request.user.has_perm("scorecard.manage_scorecard_email")
    can_manage = request.user.is_superuser or request.user.has_perm("scorecard.manage_scorecard_email")
    if not can_view:
        messages.error(request, "You do not have permission to view scorecard email configuration.")
        return redirect("scorecard:scorecard_dashboard")

    if not _email_models_ready():
        messages.error(request, "Email configuration will be available after the latest migrations are applied.")
        return redirect("scorecard:scorecard_dashboard")

    configuration_exists = ScorecardEmailConfiguration.objects.filter(pk=1).exists()
    remember_application_base_url(request)
    configuration, template_map = _ensure_email_defaults()
    edit_mode = can_manage and (not configuration_exists or request.GET.get("edit") == "1")
    template_records = _email_template_records(template_map)
    template_sections = _email_template_sections(template_records)

    if request.method == "POST":
        if not can_manage:
            messages.error(request, "You do not have permission to change scorecard email configuration.")
            return redirect("scorecard:email_configuration")
        config_form = ScorecardEmailConfigurationForm(request.POST, instance=configuration)
        if "save_configuration" in request.POST:
            edit_mode = True
            if config_form.is_valid():
                configuration_record = config_form.save(commit=False)
                configuration_record.updated_by = request.user
                configuration_record.save()
                changed_templates = 0
                for record in template_records:
                    template_instance = template_map.get(record["event_code"])
                    if not template_instance:
                        continue
                    should_enable = request.POST.get(record["toggle_field_name"]) == "1"
                    if template_instance.is_enabled == should_enable:
                        continue
                    template_instance.is_enabled = should_enable
                    template_instance.updated_by = request.user
                    template_instance.save(update_fields=["is_enabled", "updated_by", "updated_at"])
                    changed_templates += 1
                _clear_email_workspace_caches()
                if changed_templates:
                    log_email_audit(
                        request.user,
                        "update_configuration",
                        details=f"Email configuration updated. Covered areas changed: {changed_templates}.",
                        object_id=configuration_record.pk,
                    )
                    messages.success(
                        request,
                        f"Email configuration was updated successfully. {changed_templates} covered area(s) were updated.",
                    )
                else:
                    log_email_audit(
                        request.user,
                        "update_configuration",
                        details="Email configuration updated with no covered-area toggle changes.",
                        object_id=configuration_record.pk,
                    )
                    messages.success(request, "Email configuration was updated successfully.")
                return redirect("scorecard:email_configuration")
            messages.error(request, "Please correct the email configuration before saving.")
        elif "send_test_email" in request.POST:
            test_recipient = (request.POST.get("test_recipient") or "").strip()
            configuration_record = configuration
            if configuration_record is None:
                messages.error(request, "Email configuration is not available yet. Please save the email settings first.")
                return redirect("scorecard:email_configuration")

            recipient_email = (
                test_recipient
                or configuration_record.reply_to_email
                or configuration_record.from_email_override
                or configuration_record.smtp_username
            )
            success, result_message, thread_metadata = send_configuration_test_email_result(configuration_record, recipient_email)
            log_email_audit(
                request.user,
                "send_test_email",
                details=f"Test email requested for recipient {recipient_email or '-'}; result: {result_message}",
                object_id=configuration_record.pk,
            )
            _create_email_send_log(
                configuration=configuration_record,
                template=None,
                event_code="configuration_test",
                recipient_email=recipient_email or "",
                subject="Scorecard email configuration test",
                text_body=result_message,
                html_body="",
                status=ScorecardEmailSendLog.STATUS_SUCCESS if success else ScorecardEmailSendLog.STATUS_FAILED,
                error_message="" if success else result_message,
                related_reference="CONFIGURATION-TEST",
                metadata={
                    "action_url": _safe_reverse("scorecard:email_configuration"),
                    "thread_key": thread_metadata.get(
                        "thread_key",
                        _email_thread_safe(_thread_key_from_context("configuration_test", {"recipient_email": recipient_email})),
                    ),
                    "message_id": thread_metadata.get("message_id", ""),
                    "references": thread_metadata.get("references", ""),
                    "rendered_subject": thread_metadata.get("rendered_subject", "Scorecard email configuration test"),
                    "test_recipient": recipient_email or "",
                },
            )
            request.session["scorecard_email_test_flash"] = {
                "level": "success" if success else "error",
                "message": result_message,
            }
            if success:
                messages.success(request, result_message)
            else:
                messages.error(request, result_message)
            return redirect("scorecard:email_configuration")
        elif "retry_email_log" in request.POST:
            send_log_id = request.POST.get("send_log_id")
            send_log = get_object_or_404(ScorecardEmailSendLog, pk=send_log_id)
            success, result_message = retry_email_send_log(send_log)
            log_email_audit(
                request.user,
                "retry_delivery",
                details=f"Retried email log {send_log.pk}; result: {result_message}",
                object_id=send_log.pk,
            )
            if success:
                messages.success(request, result_message)
            else:
                messages.error(request, result_message)
            return redirect("scorecard:email_configuration")
        elif "retry_failed_emails" in request.POST:
            retried_total, success_total = retry_recent_failed_email_sends(limit=20)
            log_email_audit(
                request.user,
                "retry_failed_deliveries",
                details=f"Retried recent failed emails. Attempted: {retried_total}; Successful: {success_total}.",
            )
            if not retried_total:
                messages.info(request, "There are no failed email sends waiting for retry.")
            elif success_total == retried_total:
                messages.success(request, f"Retried {success_total} failed email(s) successfully.")
            else:
                messages.warning(request, f"Retried {retried_total} failed email(s). Successful: {success_total}.")
            return redirect("scorecard:email_configuration")
        else:
            config_form = ScorecardEmailConfigurationForm(instance=configuration)
    else:
        config_form = ScorecardEmailConfigurationForm(instance=configuration)

    if configuration_exists and not edit_mode:
        for field_name, field in config_form.fields.items():
            if field_name == "name":
                continue
            field.disabled = True

    context = _email_page_context(
        configuration=configuration,
        current_page="configuration",
        template_records=template_records,
        extra_context={
            "config_form": config_form,
            "default_test_recipient": "",
            "email_test_flash": request.session.pop("scorecard_email_test_flash", None),
            "event_template_sections": template_sections,
            "event_template_enabled_count": sum(1 for record in template_records if record["is_enabled"]),
            "event_template_total_count": len(template_records),
            "email_configuration_exists": configuration_exists,
            "email_configuration_edit_mode": edit_mode,
            "email_can_manage": can_manage,
        },
    )
    return render(request, "email/configuration.html", context)


@login_required
def email_delivery_view(request: HttpRequest) -> HttpResponse:
    can_view = request.user.is_superuser or request.user.has_perm("scorecard.view_scorecard_email") or request.user.has_perm("scorecard.manage_scorecard_email")
    can_manage = request.user.is_superuser or request.user.has_perm("scorecard.manage_scorecard_email")
    if not can_view:
        messages.error(request, "You do not have permission to view email delivery activity.")
        return redirect("scorecard:scorecard_dashboard")

    if not _email_models_ready():
        messages.error(request, "Email delivery activity will be available after the latest migrations are applied.")
        return redirect("scorecard:scorecard_dashboard")

    remember_application_base_url(request)
    configuration, _ = _ensure_email_defaults()

    if request.method == "POST":
        if not can_manage:
            messages.error(request, "You do not have permission to retry scorecard email activity.")
            return redirect("scorecard:email_delivery")
        if "retry_email_log" in request.POST:
            send_log_id = request.POST.get("send_log_id")
            send_log = get_object_or_404(ScorecardEmailSendLog, pk=send_log_id)
            if not _email_log_visible_in_current_branch(send_log, request):
                messages.error(request, "You can only retry email delivery records for the branch you are currently viewing.")
                return redirect("scorecard:email_delivery")
            success, result_message = retry_email_send_log(send_log)
            log_email_audit(
                request.user,
                "retry_delivery",
                details=f"Retried email log {send_log.pk}; result: {result_message}",
                object_id=send_log.pk,
            )
            if success:
                messages.success(request, result_message)
            else:
                messages.error(request, result_message)
            return redirect("scorecard:email_delivery")
        if "retry_failed_emails" in request.POST:
            retried_total, success_total = retry_recent_failed_email_sends(limit=20)
            log_email_audit(
                request.user,
                "retry_failed_deliveries",
                details=f"Retried recent failed emails. Attempted: {retried_total}; Successful: {success_total}.",
            )
            if not retried_total:
                messages.info(request, "There are no failed email sends waiting for retry.")
            elif success_total == retried_total:
                messages.success(request, f"Retried {success_total} failed email(s) successfully.")
            else:
                messages.warning(request, f"Retried {retried_total} failed email(s). Successful: {success_total}.")
            return redirect("scorecard:email_delivery")

    delivery_context = _email_delivery_context_data(request)
    delivery_context["email_can_manage"] = can_manage

    context = _email_page_context(
        configuration=configuration,
        current_page="delivery",
        extra_context=delivery_context,
    )
    return render(request, "email/delivery.html", context)


@login_required
def email_templates_view(request: HttpRequest) -> HttpResponse:
    can_view = request.user.is_superuser or request.user.has_perm("scorecard.view_scorecard_email") or request.user.has_perm("scorecard.manage_scorecard_email")
    can_manage = request.user.is_superuser or request.user.has_perm("scorecard.manage_scorecard_email")
    if not can_view:
        messages.error(request, "You do not have permission to view scorecard email templates.")
        return redirect("scorecard:scorecard_dashboard")

    if not _email_models_ready():
        messages.error(request, "Email templates will be available after the latest migrations are applied.")
        return redirect("scorecard:scorecard_dashboard")

    remember_application_base_url(request)
    configuration, template_map = _ensure_email_defaults()
    editing_template = None
    edit_event_code = (request.GET.get("edit") or "").strip()
    if edit_event_code and can_manage:
        editing_template = get_object_or_404(ScorecardEmailTemplate, event_code=edit_event_code)

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if is_ajax and edit_event_code and not can_manage:
        from django.http import JsonResponse
        return JsonResponse({"ok": False, "message": "You do not have permission to edit scorecard email templates."}, status=403)

    if request.method == "POST":
        if not can_manage:
            messages.error(request, "You do not have permission to change scorecard email templates.")
            return redirect("scorecard:email_templates")
        if "cancel_template" in request.POST:
            return redirect("scorecard:email_templates")

        if "save_template" in request.POST:
            template_id = request.POST.get("template_id")
            template_instance = get_object_or_404(ScorecardEmailTemplate, pk=template_id)
            template_form = ScorecardEmailTemplateForm(request.POST, instance=template_instance)
            if template_form.is_valid():
                saved_template = template_form.save(commit=False)
                saved_template.updated_by = request.user
                saved_template.save()
                _clear_email_workspace_caches()
                log_email_audit(
                    request.user,
                    "update_template",
                    details=f"Email template '{saved_template.name}' ({saved_template.event_code}) updated.",
                    object_id=saved_template.pk,
                )
                if is_ajax:
                    from django.http import JsonResponse
                    return JsonResponse({"ok": True, "message": f"Email template '{saved_template.name}' was updated successfully."})
                messages.success(request, f"Email template '{saved_template.name}' was updated successfully.")
                return redirect("scorecard:email_templates")
            editing_template = template_instance
            if is_ajax:
                from django.http import JsonResponse
                html = render_to_string(
                    "email/_template_edit_modal.html",
                    {
                        "template_form": template_form,
                        "editing_template": editing_template,
                    },
                    request=request,
                )
                return JsonResponse({"ok": False, "html": html}, status=400)
            messages.error(request, "Please correct the email template before saving.")
        else:
            template_form = ScorecardEmailTemplateForm(instance=editing_template) if editing_template else None
    else:
        template_form = ScorecardEmailTemplateForm(instance=editing_template) if editing_template else None

    if is_ajax and editing_template:
        html = render_to_string(
            "email/_template_edit_modal.html",
            {
                "template_form": template_form,
                "editing_template": editing_template,
            },
            request=request,
        )
        return HttpResponse(html)

    template_records = _email_template_records(template_map)
    template_sections = [
        {
            "key": "workflow",
            "title": "Workflow Templates",
            "subtitle": "These emails cover submits, approvals, returns, template workflow, and checker reminders.",
            "records": [record for record in template_records if record["category_key"] == ScorecardEmailTemplate.CATEGORY_WORKFLOW],
        },
        {
            "key": "failure",
            "title": "Failure Templates",
            "subtitle": "These emails cover operational failures that need attention, like imports, schedules, and sync issues.",
            "records": [record for record in template_records if record["category_key"] == ScorecardEmailTemplate.CATEGORY_FAILURE],
        },
    ]

    context = _email_page_context(
        configuration=configuration,
        current_page="templates",
        template_records=template_records,
        extra_context={
            "template_form": template_form,
            "editing_template": editing_template,
            "template_records": template_records,
            "template_sections": template_sections,
            "email_can_manage": can_manage,
            "email_summary": {
                "total_templates": len(template_records),
                "enabled_templates": sum(1 for record in template_records if record["is_enabled"]),
                "workflow_templates": sum(1 for record in template_records if record["category_label"] == "Workflow"),
                "failure_templates": sum(1 for record in template_records if record["category_label"] == "Failure"),
                "transport_ready": _is_transport_ready(configuration),
                "workflow_enabled": bool(configuration and configuration.send_workflow_emails),
            },
        },
    )
    return render(request, "email/templates.html", context)
