from __future__ import annotations

from datetime import timedelta
from typing import Iterable, Any

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import OperationalError, ProgrammingError, connection, transaction
from django.db.models import Count, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import urlencode
from django.urls import reverse, NoReverseMatch
from django.utils import timezone
from scorecard.functions_view.main_customer_lookup import is_all_branches_selected

from scorecard.functions_view.email import (
    remember_application_base_url,
    reviewer_recipients_for_submitted_item,
    run_email_task_async,
    send_api_schedule_repeated_failure_email,
    send_basel_approved_email,
    send_basel_checker_pending_reminder_email,
    send_basel_returned_email,
    send_basel_submitted_email,
    send_basel_template_approved_email,
    send_basel_template_returned_email,
    send_basel_template_submitted_email,
    send_api_import_failed_email,
    send_api_schedule_failed_email,
    send_ifrs9_approved_email,
    send_ifrs9_checker_pending_reminder_email,
    send_ifrs9_returned_email,
    send_ifrs9_submitted_email,
    send_ifrs9_template_approved_email,
    send_ifrs9_template_returned_email,
    send_ifrs9_template_submitted_email,
    send_main_sync_failed_email,
)
from scorecard.models import (
    ApiImportRun,
    ApiImportSchedule,
    BankBranch,
    CreditEvaluation,
    IFRS9Evaluation,
    ScorecardEmailConfiguration,
    ScorecardNotification,
)


BUSINESS_NOTIFICATION_CATEGORIES = (
    ScorecardNotification.CATEGORY_SCORING,
    ScorecardNotification.CATEGORY_API,
    ScorecardNotification.CATEGORY_SYNC,
)

NOTIFICATION_CHECKER_BACKFILL_TTL_SECONDS = 60
NOTIFICATION_SCHEDULE_BACKFILL_TTL_SECONDS = 300
NOTIFICATION_LIST_PAGE_SIZE_OPTIONS = (20, 50, 100)
NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS = 120
NOTIFICATION_SUMMARY_VERSION_CACHE_KEY = "scorecard:notifications:version"

def _notifications_ready() -> bool:
    try:
        return ScorecardNotification._meta.db_table in connection.introspection.table_names()
    except Exception:
        return False


def _safe_reverse(name: str, args: Iterable[Any] | None = None, kwargs: dict[str, Any] | None = None) -> str:
    try:
        return reverse(name, args=args, kwargs=kwargs)
    except NoReverseMatch:
        return ""


def _email_configuration() -> ScorecardEmailConfiguration | None:
    try:
        return ScorecardEmailConfiguration.objects.filter(pk=1).only(
            "checker_pending_reminder_hours",
            "schedule_failure_repeat_threshold",
            "schedule_failure_repeat_window_hours",
        ).first()
    except (OperationalError, ProgrammingError):
        return None


def _checker_pending_reminder_hours() -> int:
    configuration = _email_configuration()
    return max(1, int(getattr(configuration, "checker_pending_reminder_hours", 24) or 24))


def _schedule_failure_repeat_threshold() -> int:
    configuration = _email_configuration()
    return max(1, int(getattr(configuration, "schedule_failure_repeat_threshold", 3) or 3))


def _schedule_failure_repeat_window_hours() -> int:
    configuration = _email_configuration()
    return max(1, int(getattr(configuration, "schedule_failure_repeat_window_hours", 24) or 24))


def _normalize_users(users: Iterable[Any]) -> list[Any]:
    seen: set[int] = set()
    normalized: list[Any] = []
    for user in users:
        if user is None:
            continue
        if not getattr(user, "is_active", False):
            continue
        user_id = getattr(user, "id", None)
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        normalized.append(user)
    return normalized


def _admin_users() -> list[Any]:
    user_model = get_user_model()
    return list(
        user_model.objects.filter(is_active=True).filter(Q(is_superuser=True) | Q(is_staff=True)).distinct()
    )


def _business_notifications_for_user(user: Any):
    return ScorecardNotification.objects.filter(user=user, category__in=BUSINESS_NOTIFICATION_CATEGORIES)


def _request_branch_scope(user: Any, request: HttpRequest | None) -> tuple[set[str], str, bool]:
    if request is None:
        return set(), "", False

    cached_scope = getattr(request, "_scorecard_notification_branch_scope", None)
    if cached_scope is not None:
        return cached_scope

    accessible_branches = getattr(request, "_scorecard_accessible_branches", None)
    current_branch = getattr(request, "_scorecard_current_branch", None)
    current_branch_id = str(request.session.get("current_branch_id") or "").strip()
    current_branch_code = (request.session.get("current_branch_code") or "").strip()

    try:
        if accessible_branches is None:
            if hasattr(user, "get_accessible_branches"):
                accessible_branch_source = user.get_accessible_branches()
                if hasattr(accessible_branch_source, "only"):
                    accessible_branches = list(accessible_branch_source.only("branch_code", "branch_name"))
                else:
                    accessible_branches = list(accessible_branch_source)
            else:
                accessible_branches = []

        accessible_branch_names = {
            (branch.branch_name or "").strip().lower()
            for branch in accessible_branches
            if (branch.branch_name or "").strip()
        }

        if current_branch is None and current_branch_id:
            branch_lookup = {
                str(branch.id): branch
                for branch in accessible_branches
                if getattr(branch, "id", None)
            }
            current_branch = branch_lookup.get(current_branch_id)
            if current_branch is None and getattr(user, "is_superuser", False):
                current_branch = (
                    BankBranch.objects.only("id", "branch_code", "branch_name")
                    .filter(pk=current_branch_id)
                    .first()
                )
        if current_branch is None and current_branch_code:
            branch_lookup = {
                (branch.branch_code or "").strip(): branch
                for branch in accessible_branches
                if (branch.branch_code or "").strip()
            }
            current_branch = branch_lookup.get(current_branch_code)
            if current_branch is None and getattr(user, "is_superuser", False):
                current_branch = (
                    BankBranch.objects.only("id", "branch_code", "branch_name")
                    .filter(branch_code=current_branch_code)
                    .order_by("branch_name", "id")
                    .first()
                )

        current_branch_name = ""
        if current_branch is not None:
            current_branch_name = (current_branch.branch_name or "").strip().lower()
        if current_branch_name and not getattr(user, "is_superuser", False) and current_branch_name not in accessible_branch_names:
            current_branch_name = ""
        all_branches = bool(is_all_branches_selected(request))
    except (OperationalError, ProgrammingError):
        accessible_branch_names = set()
        current_branch_name = ""
        all_branches = False

    scope = (accessible_branch_names, current_branch_name, all_branches)
    request._scorecard_notification_branch_scope = scope
    return scope


def _user_accessible_branch_names(user: Any, request: HttpRequest | None = None) -> set[str]:
    if request is not None:
        return _request_branch_scope(user, request)[0]
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


def _current_branch_name_for_user(user: Any, request: HttpRequest | None) -> str:
    if request is not None:
        return _request_branch_scope(user, request)[1]
    if request is None:
        return ""
    current_branch_id = str(request.session.get("current_branch_id") or "").strip()
    current_branch_code = (request.session.get("current_branch_code") or "").strip()
    if not current_branch_id and not current_branch_code:
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
            return ""
    except (BankBranch.DoesNotExist, OperationalError, ProgrammingError):
        return ""
    if getattr(user, "is_superuser", False):
        return (branch.branch_name or "").strip().lower()
    if not getattr(user, "has_branch_access", None) or not user.has_branch_access(
        branch_id=getattr(branch, "id", None),
        branch_code=branch.branch_code,
        branch_name=branch.branch_name,
    ):
        return ""
    return (branch.branch_name or "").strip().lower()


def _resolve_notification_branch_name(
    metadata: dict[str, Any] | None = None,
    explicit_branch_name: str = "",
) -> str:
    if explicit_branch_name:
        return explicit_branch_name.strip()
    if not metadata:
        return ""
    return str(metadata.get("branch_name") or "").strip()


def _branch_filtered_notifications_queryset(
    user: Any,
    request: HttpRequest | None = None,
    queryset=None,
):
    notifications = queryset if queryset is not None else _business_notifications_for_user(user)
    accessible_branch_names, current_branch_name, all_branches = _request_branch_scope(user, request)
    branchless_filter = Q(branch_name="") | Q(branch_name__isnull=True)

    if all_branches:
        if not accessible_branch_names:
            return notifications.filter(branchless_filter)
        branch_scope_filter = Q()
        for branch_name in accessible_branch_names:
            branch_scope_filter |= Q(branch_name__iexact=branch_name)
        return notifications.filter(branchless_filter | branch_scope_filter)
    if not current_branch_name:
        return notifications.filter(branchless_filter)
    if not getattr(user, "is_superuser", False) and current_branch_name not in accessible_branch_names:
        return notifications.none()
    return notifications.filter(branchless_filter | Q(branch_name__iexact=current_branch_name))


def _get_visible_notification_or_404(user: Any, notification_id: int, request: HttpRequest | None = None) -> ScorecardNotification:
    return get_object_or_404(
        _branch_filtered_notifications_queryset(
            user,
            request,
            _business_notifications_for_user(user),
        ),
        pk=notification_id,
    )


def _run_cached_backfill(cache_key: str, ttl_seconds: int, callback) -> None:
    if not cache.add(cache_key, True, ttl_seconds):
        return
    try:
        callback()
    except Exception:
        cache.delete(cache_key)
        raise


def _run_notification_backfills(user: Any) -> None:
    if not _notifications_ready():
        return
    try:
        if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
            _run_cached_backfill(
                "scorecard:notifications:api-schedule-failures:v1",
                NOTIFICATION_SCHEDULE_BACKFILL_TTL_SECONDS,
                _backfill_recent_api_schedule_failure_notifications,
            )
            _run_cached_backfill(
                "scorecard:notifications:api-schedule-repeat-failures:v1",
                NOTIFICATION_SCHEDULE_BACKFILL_TTL_SECONDS,
                _backfill_repeated_schedule_failure_reminders,
            )
        _run_cached_backfill(
            f"scorecard:notifications:checker-pending:{timezone.localdate().isoformat()}",
            NOTIFICATION_CHECKER_BACKFILL_TTL_SECONDS,
            _backfill_checker_pending_reminders,
        )
    except (OperationalError, ProgrammingError):
        return


def _notification_module_label(notification: ScorecardNotification) -> str:
    if notification.category == ScorecardNotification.CATEGORY_SCORING:
        score_type = (notification.metadata or {}).get("score_type")
        if score_type == "basel":
            return "Basel II Scorecard"
        if score_type == "ifrs9":
            return "IFRS9 Scorecard"
        if score_type == "basel_template":
            return "Basel II Template Workflow"
        if score_type == "ifrs9_template":
            return "IFRS9 Template Workflow"
        return "Scorecard Workflow"
    if notification.category == ScorecardNotification.CATEGORY_API:
        return "API Import Management"
    if notification.category == ScorecardNotification.CATEGORY_SYNC:
        return "Main Customer Sync"
    return notification.get_category_display()


def _notification_reference(notification: ScorecardNotification) -> str:
    metadata = notification.metadata or {}
    if metadata.get("evaluation_id"):
        score_type = (metadata.get("score_type") or "score").upper()
        return f"{score_type}-{metadata['evaluation_id']}"
    if metadata.get("template_id"):
        score_type = (metadata.get("score_type") or "template").upper()
        return f"{score_type}-{metadata['template_id']}"
    if metadata.get("import_run_id"):
        return f"IMPORT-{metadata['import_run_id']}"
    if metadata.get("sync_run_id"):
        return f"SYNC-{metadata['sync_run_id']}"
    if notification.event_code:
        return notification.event_code.replace("_", "-").upper()
    return "-"


def _notification_status_label(notification: ScorecardNotification) -> str:
    return "Seen" if notification.is_read else "Not Seen"


def _notification_actor_label(notification: ScorecardNotification) -> str:
    if not notification.actor:
        return "-"
    full_name = f"{getattr(notification.actor, 'name', '')} {getattr(notification.actor, 'surname', '')}".strip()
    return full_name or getattr(notification.actor, "username", "-")


def _normalize_notification_page_size(raw_value: Any, default: int = 20) -> int:
    try:
        page_size = int(raw_value or default)
    except (TypeError, ValueError):
        page_size = default
    return page_size if page_size in NOTIFICATION_LIST_PAGE_SIZE_OPTIONS else default


def _build_notification_list_query_string(
    *,
    category: str,
    level: str,
    status: str,
    search_query: str,
    page_size: int,
) -> str:
    params: dict[str, Any] = {}
    if category:
        params["category"] = category
    if level:
        params["level"] = level
    if status:
        params["status"] = status
    if search_query:
        params["q"] = search_query
    if page_size != NOTIFICATION_LIST_PAGE_SIZE_OPTIONS[0]:
        params["page_size"] = page_size
    return urlencode(params)


def _notification_summary_cache_key(user: Any, request: HttpRequest | None, suffix: str) -> str:
    accessible_branch_names, current_branch_name, all_branches = _request_branch_scope(user, request)
    if all_branches:
        scope_key = "all-assigned:" + ("|".join(sorted(accessible_branch_names)) if accessible_branch_names else "none")
    else:
        scope_key = current_branch_name or "branchless"
    try:
        version = int(cache.get(NOTIFICATION_SUMMARY_VERSION_CACHE_KEY, 1))
    except (TypeError, ValueError):
        version = 1
    return f"scorecard:notifications:{version}:{getattr(user, 'id', 0)}:{scope_key}:{suffix}"


def _clear_notification_summary_cache(user: Any, request: HttpRequest | None = None) -> None:
    cache.delete(_notification_summary_cache_key(user, request, "summary"))
    cache.delete(_notification_summary_cache_key(user, request, "categories"))
    try:
        cache.incr(NOTIFICATION_SUMMARY_VERSION_CACHE_KEY)
    except (ValueError, TypeError):
        cache.set(NOTIFICATION_SUMMARY_VERSION_CACHE_KEY, 2, None)


def _get_notification_list_summary(user: Any, request: HttpRequest | None, notifications_qs):
    summary_cache_key = _notification_summary_cache_key(user, request, "summary")
    category_cache_key = _notification_summary_cache_key(user, request, "categories")
    cached_summary = cache.get(summary_cache_key)
    cached_categories = cache.get(category_cache_key)
    if cached_summary is not None and cached_categories is not None:
        return cached_summary, cached_categories

    summary = notifications_qs.aggregate(
        total_notifications=Count("id"),
        unread_total=Count("id", filter=Q(is_read=False)),
    )
    category_totals = list(
        notifications_qs.order_by()
        .values("category")
        .annotate(total=Count("id"))
        .order_by("category")
    )
    cache.set(summary_cache_key, summary, NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS)
    cache.set(category_cache_key, category_totals, NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS)
    return summary, category_totals


def _build_notification_rows(
    notifications: Iterable[ScorecardNotification],
    *,
    start_index: int = 1,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, notification in enumerate(notifications, start=start_index):
        rows.append(
            {
                "row_number": index,
                "id": notification.id,
                "title": notification.title,
                "module_label": _notification_module_label(notification),
                "reference_label": _notification_reference(notification),
                "status_label": _notification_status_label(notification),
                "receipt_date": notification.created_at,
                "detail_url": _safe_reverse("scorecard:notification_detail", kwargs={"notification_id": notification.id}),
                "is_read": notification.is_read,
                "category_value": notification.category,
                "level_value": notification.level,
                "level_display": notification.get_level_display(),
                "category_display": notification.get_category_display(),
            }
        )
    return rows


def create_notification(
    *,
    user: Any,
    category: str,
    level: str,
    event_code: str,
    title: str,
    message: str,
    actor: Any = None,
    action_url: str = "",
    action_label: str = "",
    metadata: dict[str, Any] | None = None,
    branch_name: str = "",
) -> ScorecardNotification | None:
    users = _normalize_users([user])
    if not users or not _notifications_ready():
        return None
    try:
        created_notification = ScorecardNotification.objects.create(
            user=users[0],
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            category=category,
            level=level,
            event_code=event_code,
            title=title,
            message=message,
            action_url=action_url,
            action_label=action_label,
            branch_name=_resolve_notification_branch_name(metadata, branch_name),
            metadata=metadata or {},
        )
        _clear_notification_summary_cache(users[0])
        return created_notification
    except (OperationalError, ProgrammingError):
        return None


def create_notifications_for_users(
    *,
    users: Iterable[Any],
    category: str,
    level: str,
    event_code: str,
    title: str,
    message: str,
    actor: Any = None,
    action_url: str = "",
    action_label: str = "",
    metadata: dict[str, Any] | None = None,
    branch_name: str = "",
) -> list[ScorecardNotification]:
    recipients = _normalize_users(users)
    if not recipients or not _notifications_ready():
        return []
    created_at = timezone.now()
    notifications = [
        ScorecardNotification(
            user=user,
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            category=category,
            level=level,
            event_code=event_code,
            title=title,
            message=message,
            action_url=action_url,
            action_label=action_label,
            branch_name=_resolve_notification_branch_name(metadata, branch_name),
            metadata=metadata or {},
            created_at=created_at,
        )
        for user in recipients
    ]
    try:
        created_notifications = ScorecardNotification.objects.bulk_create(notifications)
        for user in recipients:
            _clear_notification_summary_cache(user)
        return created_notifications
    except (OperationalError, ProgrammingError):
        return []


def notification_context(request: HttpRequest, *, run_backfills: bool = True) -> dict[str, int | bool]:
    unread_count = 0
    has_unread = False
    total_count = 0
    user = getattr(request, "user", None)
    remember_application_base_url(request)
    if not getattr(user, "is_authenticated", False) or not _notifications_ready():
        return {
            "scorecard_notification_unread_count": unread_count,
            "scorecard_notification_has_unread": has_unread,
            "scorecard_notification_total_count": total_count,
            "scorecard_notification_has_any": False,
        }
    if run_backfills:
        _run_notification_backfills(user)
    try:
        notifications = _branch_filtered_notifications_queryset(
            user,
            request,
            _business_notifications_for_user(user),
        )
        summary_counts, _category_totals = _get_notification_list_summary(user, request, notifications)
        unread_count = summary_counts.get("unread_total") or 0
        total_count = summary_counts.get("total_notifications") or 0
    except (OperationalError, ProgrammingError):
        unread_count = 0
        total_count = 0
    has_unread = unread_count > 0
    has_any = total_count > 0
    return {
        "scorecard_notification_unread_count": unread_count,
        "scorecard_notification_has_unread": has_unread,
        "scorecard_notification_total_count": total_count,
        "scorecard_notification_has_any": has_any,
    }


def _scorecard_owner(item: Any):
    return getattr(item, "submitted_by", None) or getattr(item, "maker", None)


def notify_basel_submitted(evaluation: Any) -> None:
    branch_name = getattr(evaluation, "branch_name", "") or ""
    recipients = reviewer_recipients_for_submitted_item(
        evaluation,
        "basel_score",
        branch_name=branch_name,
    )
    action_url = _safe_reverse("scorecard:checker_review", kwargs={"evaluation_id": evaluation.id})
    create_notifications_for_users(
        users=recipients,
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_ACTION,
        event_code="basel_submitted",
        title="Basel score submitted for review",
        message=(
            f"{evaluation.customer_name} ({evaluation.customer_id}) was submitted for checker review "
            f"at {evaluation.branch_name}."
        ),
        actor=evaluation.submitted_by or evaluation.maker,
        action_url=action_url,
        action_label="Review Score",
        metadata={
            "evaluation_id": evaluation.id,
            "score_type": "basel",
            "branch_name": branch_name,
        },
    )
    transaction.on_commit(
        lambda evaluation=evaluation: run_email_task_async(send_basel_submitted_email, evaluation)
    )


def notify_ifrs9_submitted(evaluation: Any) -> None:
    branch_name = getattr(evaluation, "branch_name", "") or ""
    recipients = reviewer_recipients_for_submitted_item(
        evaluation,
        "ifrs9_score",
        branch_name=branch_name,
    )
    action_url = _safe_reverse("scorecard:checker_ifrs9_scores_review", kwargs={"evaluation_id": evaluation.id})
    create_notifications_for_users(
        users=recipients,
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_ACTION,
        event_code="ifrs9_submitted",
        title="IFRS9 score submitted for review",
        message=(
            f"{evaluation.customer_name} ({evaluation.customer_id}) was submitted for IFRS9 checker review "
            f"at {evaluation.branch_name}."
        ),
        actor=evaluation.submitted_by or evaluation.maker,
        action_url=action_url,
        action_label="Review Score",
        metadata={
            "evaluation_id": evaluation.id,
            "score_type": "ifrs9",
            "branch_name": branch_name,
        },
    )
    transaction.on_commit(
        lambda evaluation=evaluation: run_email_task_async(send_ifrs9_submitted_email, evaluation)
    )


def notify_basel_approved(evaluation: Any, approver: Any) -> None:
    action_url = _safe_reverse("scorecard:basel_scores_view_detail", kwargs={"evaluation_id": evaluation.id})
    create_notification(
        user=_scorecard_owner(evaluation),
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_INFO,
        event_code="basel_approved",
        title="Basel score approved",
        message=(
            f"Your Basel score for {evaluation.customer_name} ({evaluation.customer_id}) was approved. "
            f"Final grade: {evaluation.final_grade}."
        ),
        actor=approver,
        action_url=action_url,
        action_label="View Approved Score",
        metadata={
            "evaluation_id": evaluation.id,
            "score_type": "basel",
            "branch_name": getattr(evaluation, "branch_name", "") or "",
        },
    )
    transaction.on_commit(
        lambda evaluation=evaluation, approver=approver: run_email_task_async(
            send_basel_approved_email,
            evaluation,
            approver,
        )
    )


def notify_ifrs9_approved(evaluation: Any, approver: Any) -> None:
    action_url = _safe_reverse("scorecard:ifrs9_scores_view_detail", kwargs={"evaluation_id": evaluation.id})
    create_notification(
        user=_scorecard_owner(evaluation),
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_INFO,
        event_code="ifrs9_approved",
        title="IFRS9 score approved",
        message=(
            f"Your IFRS9 score for {evaluation.customer_name} ({evaluation.customer_id}) was approved. "
            f"Final grade: {evaluation.final_grade}."
        ),
        actor=approver,
        action_url=action_url,
        action_label="View Approved Score",
        metadata={
            "evaluation_id": evaluation.id,
            "score_type": "ifrs9",
            "branch_name": getattr(evaluation, "branch_name", "") or "",
        },
    )
    transaction.on_commit(
        lambda evaluation=evaluation, approver=approver: run_email_task_async(
            send_ifrs9_approved_email,
            evaluation,
            approver,
        )
    )


def notify_basel_returned(evaluation: Any, checker: Any, reason: str) -> None:
    action_url = _safe_reverse("scorecard:basel_scores_edit", kwargs={"evaluation_id": evaluation.id})
    create_notification(
        user=_scorecard_owner(evaluation),
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_ACTION,
        event_code="basel_returned",
        title="Basel score returned for changes",
        message=(
            f"Your Basel score for {evaluation.customer_name} ({evaluation.customer_id}) was returned by the checker. "
            f"Reason: {reason}"
        ),
        actor=checker,
        action_url=action_url,
        action_label="Open Score Form",
        metadata={
            "evaluation_id": evaluation.id,
            "score_type": "basel",
            "branch_name": getattr(evaluation, "branch_name", "") or "",
        },
    )
    send_basel_returned_email(evaluation, checker, reason)


def notify_ifrs9_returned(evaluation: Any, checker: Any, reason: str) -> None:
    action_url = _safe_reverse("scorecard:ifrs9_scores_edit", kwargs={"evaluation_id": evaluation.id})
    create_notification(
        user=_scorecard_owner(evaluation),
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_ACTION,
        event_code="ifrs9_returned",
        title="IFRS9 score returned for changes",
        message=(
            f"Your IFRS9 score for {evaluation.customer_name} ({evaluation.customer_id}) was returned by the checker. "
            f"Reason: {reason}"
        ),
        actor=checker,
        action_url=action_url,
        action_label="Open Score Form",
        metadata={
            "evaluation_id": evaluation.id,
            "score_type": "ifrs9",
            "branch_name": getattr(evaluation, "branch_name", "") or "",
        },
    )
    send_ifrs9_returned_email(evaluation, checker, reason)


def notify_basel_template_submitted(template: Any) -> None:
    recipients = reviewer_recipients_for_submitted_item(template, "basel_template")
    action_url = _safe_reverse("scorecard:template_checker_review", kwargs={"template_id": template.id})
    create_notifications_for_users(
        users=recipients,
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_ACTION,
        event_code="basel_template_submitted",
        title="Basel template submitted for review",
        message=(
            f"Template {template.code} - {template.name} was submitted for checker review."
        ),
        actor=template.submitted_by or template.maker,
        action_url=action_url,
        action_label="Review Template",
        metadata={"template_id": template.id, "score_type": "basel_template"},
    )
    send_basel_template_submitted_email(template)


def notify_ifrs9_template_submitted(template: Any) -> None:
    recipients = reviewer_recipients_for_submitted_item(template, "ifrs9_template")
    action_url = _safe_reverse("scorecard:ifrs9_template_checker_review", kwargs={"template_id": template.id})
    create_notifications_for_users(
        users=recipients,
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_ACTION,
        event_code="ifrs9_template_submitted",
        title="IFRS9 template submitted for review",
        message=(
            f"Template {template.code} - {template.name} was submitted for checker review."
        ),
        actor=template.submitted_by or template.maker,
        action_url=action_url,
        action_label="Review Template",
        metadata={"template_id": template.id, "score_type": "ifrs9_template"},
    )
    send_ifrs9_template_submitted_email(template)


def notify_basel_template_approved(template: Any, approver: Any) -> None:
    action_url = _safe_reverse("scorecard:template_maker_view", kwargs={"template_id": template.id})
    create_notification(
        user=_scorecard_owner(template),
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_INFO,
        event_code="basel_template_approved",
        title="Basel template approved",
        message=(
            f"Template {template.code} - {template.name} was approved and is now the active approved version."
        ),
        actor=approver,
        action_url=action_url,
        action_label="View Template",
        metadata={"template_id": template.id, "score_type": "basel_template"},
    )
    send_basel_template_approved_email(template, approver)


def notify_ifrs9_template_approved(template: Any, approver: Any) -> None:
    action_url = _safe_reverse("scorecard:ifrs9_template_maker_view", kwargs={"template_id": template.id})
    create_notification(
        user=_scorecard_owner(template),
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_INFO,
        event_code="ifrs9_template_approved",
        title="IFRS9 template approved",
        message=(
            f"Template {template.code} - {template.name} was approved and is now the active approved version."
        ),
        actor=approver,
        action_url=action_url,
        action_label="View Template",
        metadata={"template_id": template.id, "score_type": "ifrs9_template"},
    )
    send_ifrs9_template_approved_email(template, approver)


def notify_basel_template_returned(template: Any, checker: Any, reason: str) -> None:
    action_url = _safe_reverse("scorecard:template_maker_view", kwargs={"template_id": template.id})
    create_notification(
        user=_scorecard_owner(template),
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_ACTION,
        event_code="basel_template_returned",
        title="Basel template returned for changes",
        message=(
            f"Template {template.code} - {template.name} was returned by the checker. Reason: {reason}"
        ),
        actor=checker,
        action_url=action_url,
        action_label="Open Template",
        metadata={"template_id": template.id, "score_type": "basel_template"},
    )
    send_basel_template_returned_email(template, checker, reason)


def notify_ifrs9_template_returned(template: Any, checker: Any, reason: str) -> None:
    action_url = _safe_reverse("scorecard:ifrs9_template_maker_view", kwargs={"template_id": template.id})
    create_notification(
        user=_scorecard_owner(template),
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_ACTION,
        event_code="ifrs9_template_returned",
        title="IFRS9 template returned for changes",
        message=(
            f"Template {template.code} - {template.name} was returned by the checker. Reason: {reason}"
        ),
        actor=checker,
        action_url=action_url,
        action_label="Open Template",
        metadata={"template_id": template.id, "score_type": "ifrs9_template"},
    )
    send_ifrs9_template_returned_email(template, checker, reason)


def notify_api_import_result(import_run: Any) -> None:
    if import_run.status not in {"success", "failed", "stopped"}:
        return
    level = (
        ScorecardNotification.LEVEL_CRITICAL
        if import_run.status == "failed"
        else ScorecardNotification.LEVEL_WARNING if import_run.status == "stopped" else ScorecardNotification.LEVEL_INFO
    )
    title = (
        f"API import failed: {import_run.endpoint.name}"
        if import_run.status == "failed"
        else f"API import stopped: {import_run.endpoint.name}" if import_run.status == "stopped" else f"API import completed: {import_run.endpoint.name}"
    )
    message = (
        f"{import_run.endpoint.name} import {import_run.status}. "
        f"Fetched {import_run.fetched}, created {import_run.created}, updated {import_run.updated}, skipped {import_run.skipped}."
    )
    if import_run.failure_message:
        message = f"{message} Reason: {import_run.failure_message}"
    create_notifications_for_users(
        users=_admin_users(),
        category=ScorecardNotification.CATEGORY_API,
        level=level,
        event_code=f"api_import_{import_run.status}",
        title=title,
        message=message,
        actor=import_run.triggered_by,
        action_url=_safe_reverse("scorecard:api_import"),
        action_label="Open Import History",
        metadata={"import_run_id": import_run.id, "endpoint_id": import_run.endpoint_id},
    )
    if import_run.status == "failed":
        send_api_import_failed_email(import_run)


def notify_api_schedule_failure(schedule: Any, error_message: str) -> None:
    schedule_run_at = getattr(schedule, "last_run_at", None)
    create_notifications_for_users(
        users=_admin_users(),
        category=ScorecardNotification.CATEGORY_API,
        level=ScorecardNotification.LEVEL_CRITICAL,
        event_code="api_schedule_failed",
        title=f"Scheduled API import failed: {schedule.name}",
        message=f"{schedule.name} failed during scheduled execution. Reason: {error_message}",
        actor=None,
        action_url=_safe_reverse("scorecard:api_scheduler"),
        action_label="Open Automation",
        metadata={
            "schedule_id": getattr(schedule, "id", None),
            "endpoint_id": getattr(schedule, "endpoint_id", None),
            "schedule_name": getattr(schedule, "name", ""),
            "schedule_last_run_at": schedule_run_at.isoformat() if schedule_run_at else "",
        },
    )
    send_api_schedule_failed_email(schedule, error_message)


def notify_api_schedule_repeated_failure(schedule: Any, failure_count: int, latest_error: str) -> None:
    repeat_window_hours = _schedule_failure_repeat_window_hours()
    create_notifications_for_users(
        users=_admin_users(),
        category=ScorecardNotification.CATEGORY_API,
        level=ScorecardNotification.LEVEL_WARNING,
        event_code="api_schedule_repeated_failure",
        title=f"Repeated schedule failure: {schedule.name}",
        message=(
            f"{schedule.name} has failed {failure_count} times in the last "
            f"{repeat_window_hours} hours. Latest reason: {latest_error}"
        ),
        actor=None,
        action_url=_safe_reverse("scorecard:api_scheduler"),
        action_label="Open Automation",
        metadata={
            "schedule_id": getattr(schedule, "id", None),
            "failure_count": failure_count,
            "schedule_last_run_at": getattr(schedule, "last_run_at", None).isoformat()
            if getattr(schedule, "last_run_at", None)
            else "",
        },
    )
    send_api_schedule_repeated_failure_email(schedule, failure_count, latest_error)


def notify_basel_checker_pending_reminder(evaluation: Any) -> None:
    if not getattr(evaluation, "checker_id", None):
        return
    reminder_hours = _checker_pending_reminder_hours()
    action_url = _safe_reverse("scorecard:checker_review", kwargs={"evaluation_id": evaluation.id})
    create_notification(
        user=evaluation.checker,
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_WARNING,
        event_code="basel_checker_pending_reminder",
        title=f"Pending Basel review: {evaluation.customer_name}",
        message=(
            f"Basel score for {evaluation.customer_name} ({evaluation.customer_id}) has been waiting for review "
            f"for more than {reminder_hours} hours."
        ),
        actor=evaluation.maker,
        action_url=action_url,
        action_label="Review Score",
        metadata={
            "evaluation_id": evaluation.id,
            "score_type": "basel",
            "branch_name": getattr(evaluation, "branch_name", "") or "",
            "reminder_date": timezone.localdate().isoformat(),
        },
    )
    send_basel_checker_pending_reminder_email(evaluation)


def notify_ifrs9_checker_pending_reminder(evaluation: Any) -> None:
    if not getattr(evaluation, "checker_id", None):
        return
    reminder_hours = _checker_pending_reminder_hours()
    action_url = _safe_reverse("scorecard:checker_ifrs9_scores_review", kwargs={"evaluation_id": evaluation.id})
    create_notification(
        user=evaluation.checker,
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_WARNING,
        event_code="ifrs9_checker_pending_reminder",
        title=f"Pending IFRS9 review: {evaluation.customer_name}",
        message=(
            f"IFRS9 score for {evaluation.customer_name} ({evaluation.customer_id}) has been waiting for review "
            f"for more than {reminder_hours} hours."
        ),
        actor=evaluation.maker,
        action_url=action_url,
        action_label="Review Score",
        metadata={
            "evaluation_id": evaluation.id,
            "score_type": "ifrs9",
            "branch_name": getattr(evaluation, "branch_name", "") or "",
            "reminder_date": timezone.localdate().isoformat(),
        },
    )
    send_ifrs9_checker_pending_reminder_email(evaluation)


def _backfill_recent_api_schedule_failure_notifications() -> None:
    if not _notifications_ready():
        return

    cutoff = timezone.now() - timedelta(days=7)
    recent_failed_schedules = list(
        ApiImportSchedule.objects.filter(last_status="failed", last_run_at__gte=cutoff).only(
            "id",
            "name",
            "endpoint_id",
            "last_message",
            "last_run_at",
        )
    )
    if not recent_failed_schedules:
        return

    existing_notifications = ScorecardNotification.objects.filter(
        category=ScorecardNotification.CATEGORY_API,
        event_code="api_schedule_failed",
        created_at__gte=cutoff,
    ).only("metadata")

    notified_schedule_runs: set[tuple[int, str]] = set()
    for notification in existing_notifications:
        metadata = notification.metadata or {}
        schedule_id = metadata.get("schedule_id")
        schedule_last_run_at = metadata.get("schedule_last_run_at") or ""
        if isinstance(schedule_id, int):
            notified_schedule_runs.add((schedule_id, str(schedule_last_run_at)))

    for schedule in recent_failed_schedules:
        schedule_run_key = (
            schedule.id,
            schedule.last_run_at.isoformat() if getattr(schedule, "last_run_at", None) else "",
        )
        if schedule_run_key in notified_schedule_runs:
            continue
        notify_api_schedule_failure(schedule, schedule.last_message or "Scheduled execution failed.")


def _backfill_checker_pending_reminders() -> None:
    if not _notifications_ready():
        return

    cutoff = timezone.now() - timedelta(hours=_checker_pending_reminder_hours())
    reminder_date = timezone.localdate().isoformat()

    basel_pending = CreditEvaluation.objects.filter(status="submitted", checker__isnull=False, created_at__lte=cutoff)
    for evaluation in basel_pending:
        already_sent = ScorecardNotification.objects.filter(
            category=ScorecardNotification.CATEGORY_SCORING,
            event_code="basel_checker_pending_reminder",
            user=evaluation.checker,
            metadata__evaluation_id=evaluation.id,
            metadata__reminder_date=reminder_date,
        ).exists()
        if not already_sent:
            notify_basel_checker_pending_reminder(evaluation)

    ifrs9_pending = IFRS9Evaluation.objects.filter(status="submitted", checker__isnull=False, created_at__lte=cutoff)
    for evaluation in ifrs9_pending:
        already_sent = ScorecardNotification.objects.filter(
            category=ScorecardNotification.CATEGORY_SCORING,
            event_code="ifrs9_checker_pending_reminder",
            user=evaluation.checker,
            metadata__evaluation_id=evaluation.id,
            metadata__reminder_date=reminder_date,
        ).exists()
        if not already_sent:
            notify_ifrs9_checker_pending_reminder(evaluation)


def _backfill_repeated_schedule_failure_reminders() -> None:
    if not _notifications_ready():
        return

    cutoff = timezone.now() - timedelta(hours=_schedule_failure_repeat_window_hours())
    repeat_threshold = _schedule_failure_repeat_threshold()
    failed_runs = (
        ApiImportRun.objects.filter(
            run_source=ApiImportRun.SOURCE_SCHEDULE,
            status=ApiImportRun.STATUS_FAILED,
            completed_at__gte=cutoff,
            schedule__isnull=False,
        )
        .select_related("schedule", "endpoint")
        .order_by("schedule_id", "-completed_at")
    )

    schedule_latest: dict[int, ApiImportRun] = {}
    schedule_counts: dict[int, int] = {}
    for run in failed_runs:
        schedule_id = run.schedule_id
        schedule_counts[schedule_id] = schedule_counts.get(schedule_id, 0) + 1
        schedule_latest.setdefault(schedule_id, run)

    for schedule_id, failure_count in schedule_counts.items():
        if failure_count < repeat_threshold:
            continue
        latest_run = schedule_latest[schedule_id]
        schedule = latest_run.schedule
        if not schedule:
            continue
        reminder_key = latest_run.completed_at.isoformat() if latest_run.completed_at else ""
        already_sent = ScorecardNotification.objects.filter(
            category=ScorecardNotification.CATEGORY_API,
            event_code="api_schedule_repeated_failure",
            metadata__schedule_id=schedule.id,
            metadata__schedule_last_run_at=reminder_key,
        ).exists()
        if already_sent:
            continue
        schedule.last_run_at = latest_run.completed_at
        notify_api_schedule_repeated_failure(schedule, failure_count, latest_run.failure_message or schedule.last_message or "-")


def notify_main_sync_result(sync_run: Any) -> None:
    if sync_run.status not in {"success", "failed"}:
        return
    create_notifications_for_users(
        users=_admin_users(),
        category=ScorecardNotification.CATEGORY_SYNC,
        level=ScorecardNotification.LEVEL_CRITICAL if sync_run.status == "failed" else ScorecardNotification.LEVEL_INFO,
        event_code=f"main_sync_{sync_run.status}",
        title=(
            f"Main customer sync failed: {sync_run.reporting_date:%Y-%m-%d}"
            if sync_run.status == "failed"
            else f"Main customer sync completed: {sync_run.reporting_date:%Y-%m-%d}"
        ),
        message=(
            f"Main customer sync for {sync_run.reporting_date:%Y-%m-%d} {sync_run.status}. "
            f"Rows synced: {sync_run.rows_synced}. "
            f"{sync_run.failure_message or sync_run.detail_message}"
        ),
        actor=sync_run.triggered_by,
        action_url=_safe_reverse("scorecard:api_main_sync"),
        action_label="Open Auto Sync",
        metadata={"sync_run_id": sync_run.id},
    )
    if sync_run.status == "failed":
        send_main_sync_failed_email(sync_run)

def notify_main_sync_customer_quality(reporting_date, incomplete_count: int, actor: Any = None) -> None:
    return None


def notify_manual_fallback_customer_added(customer: Any, actor: Any = None) -> None:
    return None


@login_required
def notification_list_view(request: HttpRequest) -> HttpResponse:
    if not _notifications_ready():
        messages.info(request, "Notifications will appear here after the latest migrations are applied.")
        return render(
            request,
            "notifications/list.html",
            {
                "notifications": [],
                "unread_total": 0,
                "total_notifications": 0,
                "category_totals": [],
                "filters": {"category": "", "level": "", "status": ""},
                "category_choices": [
                    choice for choice in ScorecardNotification.CATEGORY_CHOICES if choice[0] in BUSINESS_NOTIFICATION_CATEGORIES
                ],
                "level_choices": ScorecardNotification.LEVEL_CHOICES,
            },
        )

    _run_notification_backfills(request.user)

    notifications_qs = _branch_filtered_notifications_queryset(
        request.user,
        request,
        _business_notifications_for_user(request.user),
    ).only(
        "id",
        "title",
        "category",
        "level",
        "created_at",
        "is_read",
        "event_code",
        "branch_name",
        "metadata",
    )
    category = (request.GET.get("category") or "").strip()
    level = (request.GET.get("level") or "").strip()
    status = (request.GET.get("status") or "").strip()
    search_query = (request.GET.get("q") or "").strip()
    page_size = _normalize_notification_page_size(request.GET.get("page_size"))

    filtered_notifications = notifications_qs.order_by("-created_at", "-id")
    if category:
        filtered_notifications = filtered_notifications.filter(category=category)
    if level:
        filtered_notifications = filtered_notifications.filter(level=level)
    if status == "unread":
        filtered_notifications = filtered_notifications.filter(is_read=False)
    elif status == "read":
        filtered_notifications = filtered_notifications.filter(is_read=True)
    if search_query:
        filtered_notifications = filtered_notifications.filter(
            Q(title__icontains=search_query)
            | Q(event_code__icontains=search_query)
            | Q(message__icontains=search_query)
        )

    paginator = Paginator(filtered_notifications, page_size)
    page_obj = paginator.get_page(request.GET.get("page") or "1")
    page_start = page_obj.start_index() if paginator.count else 0
    page_end = page_obj.end_index() if paginator.count else 0
    notification_rows = _build_notification_rows(
        page_obj.object_list,
        start_index=page_start or 1,
    )
    summary_totals, category_totals = _get_notification_list_summary(
        request.user,
        request,
        notifications_qs,
    )
    list_query_string = _build_notification_list_query_string(
        category=category,
        level=level,
        status=status,
        search_query=search_query,
        page_size=page_size,
    )
    context = {
        "notifications": notification_rows,
        "unread_total": summary_totals["unread_total"],
        "total_notifications": summary_totals["total_notifications"],
        "page_obj": page_obj,
        "page_size": page_size,
        "page_size_options": NOTIFICATION_LIST_PAGE_SIZE_OPTIONS,
        "page_start": page_start,
        "page_end": page_end,
        "category_totals": category_totals,
        "filters": {"category": category, "level": level, "status": status},
        "search_query": search_query,
        "list_query_string": list_query_string,
        "category_choices": [
            choice for choice in ScorecardNotification.CATEGORY_CHOICES if choice[0] in BUSINESS_NOTIFICATION_CATEGORIES
        ],
        "level_choices": ScorecardNotification.LEVEL_CHOICES,
    }
    return render(request, "notifications/list.html", context)


@login_required
def notification_detail_view(request: HttpRequest, notification_id: int) -> HttpResponse:
    if not _notifications_ready():
        return HttpResponse("Notifications are not available yet.", status=503)
    notification = _get_visible_notification_or_404(request.user, notification_id, request)
    was_unread = not notification.is_read
    if was_unread:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at"])
        _clear_notification_summary_cache(request.user, request)
    context = {
        "notification": notification,
        "module_label": _notification_module_label(notification),
        "reference_label": _notification_reference(notification),
        "status_label": _notification_status_label(notification),
        "actor_label": _notification_actor_label(notification),
        "was_unread": was_unread,
    }
    return render(request, "notifications/_notification_detail_modal.html", context)


@login_required
def notification_mark_read_view(request: HttpRequest, notification_id: int) -> HttpResponse:
    if not _notifications_ready():
        return redirect(_safe_reverse("scorecard:notifications"))
    notification = _get_visible_notification_or_404(request.user, notification_id, request)
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at"])
        _clear_notification_summary_cache(request.user, request)
    next_url = request.GET.get("next") or notification.action_url or _safe_reverse("scorecard:notifications")
    return redirect(next_url)


@login_required
def notification_mark_all_read_view(request: HttpRequest) -> HttpResponse:
    if not _notifications_ready():
        return redirect(_safe_reverse("scorecard:notifications"))
    updated_count = _branch_filtered_notifications_queryset(
        request.user,
        request,
        _business_notifications_for_user(request.user),
    ).filter(is_read=False).update(
            is_read=True,
            read_at=timezone.now(),
        )
    if updated_count:
        _clear_notification_summary_cache(request.user, request)
        messages.success(request, f"{updated_count} notification(s) marked as read.")
    else:
        messages.info(request, "You already have no unread notifications.")
    next_url = request.GET.get("next") or _safe_reverse("scorecard:notifications")
    return redirect(next_url)
