from django.http import JsonResponse

from scorecard.context_processors import (
    _build_scorecard_route_access,
    _empty_checker_pending_counts,
    _empty_customer_queue_counts,
    _empty_maker_queue_counts,
    _get_checker_pending_counts,
    _get_customer_queue_counts,
    _get_maker_queue_counts,
    _resolve_scorecard_branch_context,
)
from scorecard.functions_view.notifications import notification_context


def badge_counts_view(request):
    branch_context = _resolve_scorecard_branch_context(request)
    refresh_customers = request.GET.get("customers") == "1"
    _route_access, nav_visibility = _build_scorecard_route_access(request)
    checker = (
        _get_checker_pending_counts(request, branch_context)
        if nav_visibility.get("checker")
        else _empty_checker_pending_counts()
    )
    maker = (
        _get_maker_queue_counts(request, branch_context)
        if nav_visibility.get("maker")
        else _empty_maker_queue_counts()
    )
    customers = (
        _get_customer_queue_counts(request, refresh_if_stale=refresh_customers)
        if nav_visibility.get("customers")
        else _empty_customer_queue_counts()
    )
    notifications = (
        notification_context(request, run_backfills=False)
        if nav_visibility.get("notifications")
        else {}
    )
    return JsonResponse(
        {
            "checker": checker,
            "maker": maker,
            "customers": customers,
            "notifications": {
                "unread": int(notifications.get("scorecard_notification_unread_count", 0) or 0),
            },
        }
    )
