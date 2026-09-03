"""
Maker-Checker Workflow Views for Questionnaire Score Sheets

This module implements a solid maker-checker design pattern with:
- Maker: captures/edits the score sheet
- Checker: reviews and approves/rejects
- Supervisor/Admin: can reassign, override, or unlock

Status workflow:
- Draft / In Progress: maker is still working
- Submitted (Pending Review): maker finished, sent to checker, locked from editing
- Returned (Needs Changes): checker rejected with comments, maker can edit again
- Approved (Completed): checker approved, final/locked
- Cancelled / Voided: stopped/invalid
"""

from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from scorecard.functions_view.notifications import (
    notify_basel_approved,
    notify_basel_returned,
    notify_ifrs9_approved,
    notify_ifrs9_returned,
)
from scorecard.functions_view.audit import log_basel_score_audit, log_ifrs9_score_audit, log_scorecard_audit
from scorecard.context_processors import (
    bump_checker_pending_counts_version,
    bump_maker_queue_counts_version,
)
from scorecard.workflow_approval import can_user_self_review_score_submission
from scorecard.functions_view.main_customer_lookup import get_request_branch_names
from scorecard.models import (
    Attribute,
    AttributeResponse,
    BaselScoreSheetTemplate,
    CreditEvaluation,
    EvaluationWorkflowHistory,
    IFRS9Attribute,
    IFRS9AttributeResponse,
    IFRS9Evaluation,
    IFRS9EvaluationWorkflowHistory,
    IFRS9ScoreSheetTemplate,
)


LIST_PAGE_SIZE_OPTIONS = (20, 50, 100)


def _grade_from_template_score(template, score):
    """Return the grade code for a score using the template grade bands."""
    if template is None or score in (None, ""):
        return ""
    try:
        score_decimal = Decimal(str(score))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    grade_band = template.grade_bands.filter(
        min_percent__lte=score_decimal,
        max_percent__gte=score_decimal,
    ).order_by("display_order").first()
    return grade_band.grade_code if grade_band else ""


def _version_grade_or_calculated(template, version):
    if not version:
        return ""
    saved_grade = (getattr(version, "final_grade", "") or "").strip()
    if saved_grade:
        return saved_grade
    return _grade_from_template_score(template, getattr(version, "total_weighted_percent", None))


def _normalize_list_page_size(raw_value, default: int = 20) -> int:
    try:
        page_size = int(raw_value or default)
    except (TypeError, ValueError):
        page_size = default
    return page_size if page_size in LIST_PAGE_SIZE_OPTIONS else default


def _build_list_query_string(request: HttpRequest, excluded_keys=None) -> str:
    excluded = set(excluded_keys or [])
    query_params = []
    for key in request.GET:
        if key in excluded:
            continue
        for value in request.GET.getlist(key):
            if value:
                query_params.append((key, value))
    return urlencode(query_params)


def _paginate_list_queryset(request: HttpRequest, queryset, *, page_param: str = "page"):
    page_size = _normalize_list_page_size(request.GET.get("page_size"))
    paginator = Paginator(queryset, page_size)
    page_obj = paginator.get_page(request.GET.get(page_param))
    list_query_string = _build_list_query_string(request, excluded_keys={page_param})
    return page_obj, page_size, list_query_string


def _get_active_autofill_metadata(
    evaluation,
    *,
    preferred_version=None,
    fallback_version=None,
):
    if preferred_version and getattr(preferred_version, "autofill_metadata", None):
        return preferred_version.autofill_metadata
    if fallback_version and getattr(fallback_version, "autofill_metadata", None):
        return fallback_version.autofill_metadata
    return getattr(evaluation, "autofill_metadata", {}) or {}


def _get_request_scope_branch_names(request):
    return get_request_branch_names(request)


def _can_user_self_review_basel_scores(user) -> bool:
    return can_user_self_review_score_submission(user, "basel_scores", "scorecard.review_basel_scores")


def _can_user_self_review_ifrs9_scores(user) -> bool:
    return can_user_self_review_score_submission(user, "ifrs9_scores", "scorecard.review_ifrs9_scores")


def _active_submission_user_id(evaluation):
    return getattr(evaluation, "submitted_by_id", None) or getattr(evaluation, "maker_id", None)


def _must_use_different_score_reviewer(evaluation, user, *, basel: bool) -> bool:
    active_submitter_id = _active_submission_user_id(evaluation)
    user_id = getattr(user, "id", None)
    if not active_submitter_id or not user_id or active_submitter_id != user_id:
        return False
    if basel:
        return not _can_user_self_review_basel_scores(user)
    return not _can_user_self_review_ifrs9_scores(user)


def _reject_self_score_review(request, redirect_name: str) -> HttpResponse:
    messages.error(
        request,
        "You cannot review, approve, or return your own submitted score while score auto-approval is disabled. Another checker must handle this score.",
    )
    return redirect(redirect_name)


def _cached_attribute_responses(evaluation):
    return list(evaluation.attribute_responses.all())


def _cached_versions(evaluation):
    return sorted(
        list(evaluation.versions.all()),
        key=lambda version: getattr(version, "version_number", 0) or 0,
        reverse=True,
    )


def _latest_cached_version(evaluation, *, approved: bool):
    for version in _cached_versions(evaluation):
        if bool(getattr(version, "is_approved", False)) == approved:
            return version
    return None


def _attach_completion_percentages(
    evaluations,
    *,
    attribute_model,
    response_model,
    template_lookup: str,
):
    rows = list(evaluations)
    if not rows:
        return rows

    template_ids = {row.template_id for row in rows if getattr(row, "template_id", None)}
    required_counts = {}
    if template_ids:
        required_counts = dict(
            attribute_model.objects.filter(
                **{f"{template_lookup}__in": template_ids},
                is_required=True,
            )
            .values(template_lookup)
            .annotate(total_required=Count("id"))
            .values_list(template_lookup, "total_required")
        )

    evaluation_ids = [row.id for row in rows]
    answered_counts = dict(
        response_model.objects.filter(
            evaluation_id__in=evaluation_ids,
            attribute__is_required=True,
        )
        .exclude(raw_value="")
        .exclude(raw_value__isnull=True)
        .values("evaluation_id")
        .annotate(answered_required=Count("id"))
        .values_list("evaluation_id", "answered_required")
    )

    for row in rows:
        if not getattr(row, "template_id", None):
            row.completion_percentage_cached = 100
            continue
        total_required = required_counts.get(row.template_id, 0)
        if total_required <= 0:
            row.completion_percentage_cached = 100
            continue
        answered_required = answered_counts.get(row.id, 0)
        row.completion_percentage_cached = int((answered_required / total_required) * 100)

    return rows


# ============================================================================
# MAKER VIEWS
# ============================================================================

@login_required
def maker_draft_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all draft/in-progress/returned evaluations for the current maker.
    Shows evaluations that the maker can still edit.
    """
    evaluations = CreditEvaluation.objects.filter(
        Q(maker=request.user) | Q(submitted_by=request.user),
        status__in=['draft', 'in_progress', 'returned']
    ).select_related('template', 'checker', 'submitted_by').only(
        'id',
        'customer_name',
        'customer_id',
        'branch_name',
        'status',
        'updated_at',
        'version',
        'return_reason',
        'template_id',
        'template__code',
        'checker__email',
        'submitted_by_id',
    ).order_by('-updated_at')
    
    # Filter by branch if user has branch restrictions
    branch_names = _get_request_scope_branch_names(request)
    if branch_names:
        evaluations = evaluations.filter(branch_name__in=branch_names)
    
    # Search functionality
    search_query = request.GET.get('search', '').strip()
    if search_query:
        evaluations = evaluations.filter(
            Q(customer_name__icontains=search_query) |
            Q(customer_id__icontains=search_query) |
            Q(branch_name__icontains=search_query)
        )
    
    page_obj, page_size, list_query_string = _paginate_list_queryset(request, evaluations)
    _attach_completion_percentages(
        page_obj.object_list,
        attribute_model=Attribute,
        response_model=AttributeResponse,
        template_lookup='risk_driver__section__template_id',
    )

    context = {
        'evaluations': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'page_size': page_size,
        'list_query_string': list_query_string,
        'page_title': 'My Drafts',
    }
    
    return render(
        request,
        'maker_checker/maker_draft_list.html',
        context
    )


@login_required
def maker_submitted_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all submitted evaluations for the current maker.
    Shows evaluations that are pending review or have been reviewed.
    Also includes 'completed' status for backward compatibility (old submissions without maker-checker).
    """
    # Show records where this user is either the original maker or the latest submitter.
    evaluations = CreditEvaluation.objects.filter(
        Q(maker=request.user) | Q(submitted_by=request.user) | Q(maker__isnull=True),
        status__in=['submitted', 'approved', 'returned', 'completed']
    ).select_related('template', 'checker', 'submitted_by', 'approved_by', 'returned_by').only(
        'id',
        'customer_name',
        'customer_id',
        'branch_name',
        'status',
        'submitted_at',
        'created_at',
        'updated_at',
        'version',
        'template_id',
        'template__code',
        'checker_id',
        'checker__email',
        'submitted_by_id',
        'submitted_by__email',
        'approved_by_id',
        'returned_by_id',
    ).order_by('-submitted_at', '-created_at', '-updated_at')
    
    # Filter by branch if user has branch restrictions
    branch_names = _get_request_scope_branch_names(request)
    if branch_names:
        evaluations = evaluations.filter(branch_name__in=branch_names)
    
    # Search functionality
    search_query = request.GET.get('search', '').strip()
    if search_query:
        evaluations = evaluations.filter(
            Q(customer_name__icontains=search_query) |
            Q(customer_id__icontains=search_query) |
            Q(branch_name__icontains=search_query)
        )
    
    # Filter by status
    status_filter = request.GET.get('status', '')
    if status_filter:
        evaluations = evaluations.filter(status=status_filter)
    
    page_obj, page_size, list_query_string = _paginate_list_queryset(request, evaluations)

    context = {
        'evaluations': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'status_filter': status_filter,
        'page_size': page_size,
        'list_query_string': list_query_string,
        'page_title': 'My Submissions',
    }
    
    return render(
        request,
        'maker_checker/maker_submitted_list.html',
        context
    )


@login_required
def submit_for_review_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    Submit an evaluation for review by the checker.
    Changes status from draft/in_progress/returned to submitted.
    """
    evaluation = get_object_or_404(
        CreditEvaluation.objects.select_related('template', 'maker', 'checker'),
        id=evaluation_id
    )
    
    # Permission check
    if not evaluation.can_be_edited_by(request.user):
        messages.error(request, "You don't have permission to submit this evaluation.")
        return redirect('scorecard:maker_draft_list')
    
    if request.method == 'POST':
        with transaction.atomic():
            # Update status
            old_status = evaluation.status
            evaluation.status = 'submitted'
            evaluation.submitted_by = request.user
            evaluation.submitted_at = timezone.now()
            
            # Increment version if resubmitting
            if old_status == 'returned':
                evaluation.version += 1
                evaluation.resubmission_count += 1
            
            evaluation.save()
            
            # Create workflow history entry
            EvaluationWorkflowHistory.objects.create(
                evaluation=evaluation,
                action='submitted' if old_status != 'returned' else 'submitted',
                from_status=old_status,
                to_status='submitted',
                performed_by=request.user,
                comments=request.POST.get('comments', '').strip()
            )
            log_basel_score_audit(
                request.user,
                "submit_for_review",
                evaluation,
                f"Submitted from maker checker workflow. Previous status: {old_status}.",
            )
            
            messages.success(
                request,
                f"Evaluation submitted for review successfully! "
                f"Version {evaluation.version} is now pending checker approval."
            )
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            
            return redirect('scorecard:maker_submitted_list')
    
    # GET request - show confirmation page
    context = {
        'evaluation': evaluation,
        'completion_percentage': evaluation.get_completion_percentage(),
    }
    
    return render(
        request,
        'maker_checker/submit_for_review.html',
        context
    )


@login_required
def withdraw_submission_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    Withdraw a submitted evaluation (only if checker hasn't acted yet).
    Changes status from submitted back to in_progress.
    """
    evaluation = get_object_or_404(CreditEvaluation, id=evaluation_id)
    
    # Permission check
    if (
        evaluation.maker != request.user
        and evaluation.submitted_by != request.user
        and not request.user.is_superuser
    ):
        messages.error(request, "You don't have permission to withdraw this evaluation.")
        return redirect('scorecard:maker_submitted_list')
    
    if evaluation.status != 'submitted':
        messages.error(request, "Only submitted evaluations can be withdrawn.")
        return redirect('scorecard:maker_submitted_list')
    
    if request.method == 'POST':
        with transaction.atomic():
            old_status = evaluation.status
            
            # CRITICAL: When withdrawing, restore approved scores if they exist
            # The current total_weighted_percent and final_grade contain unapproved submitted scores
            # We should restore the approved scores (if any) or clear them if this was a first submission
            if evaluation.approved_weighted_percent is not None:
                # Restore approved scores - these are the only scores that should be in the main fields
                evaluation.total_weighted_percent = evaluation.approved_weighted_percent
                evaluation.final_grade = evaluation.approved_grade
                # Also restore raw score if available from approved version
                # (We don't store approved_raw_score separately, so we'll keep current or clear it)
            else:
                # First submission - no approved scores yet, so clear the unapproved scores
                # These scores are not approved, so they shouldn't be in the main fields
                evaluation.total_weighted_percent = None
                evaluation.final_grade = ""
                evaluation.total_raw_score = None
            
            evaluation.status = 'in_progress'
            evaluation.save()
            
            # Create workflow history entry
            EvaluationWorkflowHistory.objects.create(
                evaluation=evaluation,
                action='withdrawn',
                from_status=old_status,
                to_status='in_progress',
                performed_by=request.user,
                comments=request.POST.get('comments', '').strip() or 'Withdrawn by submitter'
            )
            log_basel_score_audit(
                request.user,
                "withdraw_submission",
                evaluation,
                f"Submission withdrawn back to in_progress from {old_status}.",
            )
            
            messages.success(request, "Submission withdrawn successfully. You can continue editing.")
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            return redirect('scorecard:maker_draft_list')
    
    # GET request - show confirmation page
    context = {
        'evaluation': evaluation,
    }
    
    return render(
        request,
        'maker_checker/withdraw_submission.html',
        context
    )


@login_required
def maker_questionnaire_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    View to display a questionnaire for the maker.
    Shows CURRENT/PENDING information that needs verification.
    Different from questionnaire_view_detail which shows approved/final information.
    
    For submitted questionnaires: Shows the NEWLY SUBMITTED scores (pending approval)
    For returned/draft questionnaires: Shows the current state
    """
    evaluation = get_object_or_404(
        CreditEvaluation.objects.select_related("template", "maker", "checker").prefetch_related(
            "section_scores__section",
            "driver_scores__risk_driver__section",
            "attribute_responses__attribute__risk_driver",
            "attribute_responses__option",
            "attribute_responses__attribute__options",
            "attribute_responses__documents__uploaded_by",
            "versions__attribute_responses__attribute",
            "versions__attribute_responses__option",
            "versions__driver_scores__risk_driver",
            "versions__section_scores__section",
        ),
        id=evaluation_id,
    )
    
    # Permission check - original maker, latest submitter, or admin can view.
    if (
        evaluation.maker != request.user
        and evaluation.submitted_by != request.user
        and not request.user.is_superuser
    ):
        messages.error(request, "You don't have permission to view this questionnaire.")
        return redirect('scorecard:maker_draft_list')
    
    template = evaluation.template
    from scorecard.functions_view.basel_scores_form import _build_configuration
    # Use approved template version structure to ensure consistency
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=True)

    # For submitted questionnaires, get data from the latest unapproved version
    # For approved questionnaires, get data from the approved version
    # This ensures we show the correct scores/options based on status
    from scorecard.models import EvaluationVersion
    latest_unapproved_version = None
    approved_version = None
    
    if evaluation.status == 'submitted':
        latest_unapproved_version = _latest_cached_version(evaluation, approved=False)
    elif evaluation.status == 'approved':
        approved_version = _latest_cached_version(evaluation, approved=True)
    
    if approved_version:
        # Use the approved version's data to show approved options/scores
        # Get current evaluation's attribute responses for documents (documents are linked to evaluation, not version)
        current_responses = {}
        for response in _cached_attribute_responses(evaluation):
            current_responses[response.attribute_id] = response
        
        # Get attribute responses from approved version, but include documents from current evaluation
        attribute_responses = {}
        for version_response in approved_version.attribute_responses.all():
            # Get documents from current evaluation if available
            current_response = current_responses.get(version_response.attribute_id)
            # Create a dict similar to evaluation's attribute_responses structure
            attribute_responses[version_response.attribute_id] = {
                'option_id': version_response.option_id,
                'option': version_response.option,
                'allocated_score': version_response.allocated_score,
                'raw_value': version_response.raw_value,
                'documents': current_response.documents.all() if current_response else [],
            }
        
        # Get driver scores from approved version
        driver_scores = {}
        for version_score in approved_version.driver_scores.all():
            driver_scores[version_score.risk_driver_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        # Get section scores from approved version
        section_scores = {}
        for version_score in approved_version.section_scores.all():
            section_scores[version_score.section_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
    elif latest_unapproved_version:
        # Use the latest unapproved version's data to show newly submitted options/scores
        # Get current evaluation's attribute responses for documents (documents are linked to evaluation, not version)
        current_responses = {}
        for response in _cached_attribute_responses(evaluation):
            current_responses[response.attribute_id] = response
        
        # Get attribute responses from version, but include documents from current evaluation
        attribute_responses = {}
        for version_response in latest_unapproved_version.attribute_responses.all():
            # Get documents from current evaluation if available
            current_response = current_responses.get(version_response.attribute_id)
            # Create a dict similar to evaluation's attribute_responses structure
            attribute_responses[version_response.attribute_id] = {
                'option_id': version_response.option_id,
                'option': version_response.option,
                'allocated_score': version_response.allocated_score,
                'raw_value': version_response.raw_value,
                'documents': current_response.documents.all() if current_response else [],
            }
        
        # Get driver scores from version
        driver_scores = {}
        for version_score in latest_unapproved_version.driver_scores.all():
            driver_scores[version_score.risk_driver_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        # Get section scores from version
        section_scores = {}
        for version_score in latest_unapproved_version.section_scores.all():
            section_scores[version_score.section_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
    else:
        # Use current evaluation data (for returned/draft/in_progress)
        # Get attribute responses as a dictionary for easy lookup
        # Prefetch documents to avoid N+1 queries
        attribute_responses = {}
        for response in _cached_attribute_responses(evaluation):
            attribute_responses[response.attribute_id] = response

        # Get driver scores as a dictionary
        driver_scores = {}
        for score in evaluation.driver_scores.all():
            driver_scores[score.risk_driver_id] = score

        # Get section scores as a dictionary
        section_scores = {}
        for score in evaluation.section_scores.all():
            section_scores[score.section_id] = score

    grade_bands = template.grade_bands.all().order_by("display_order")
    
    # Get workflow history
    workflow_history = list(evaluation.workflow_history.all())[:10]
    
    # Get all versions for this evaluation
    all_versions = _cached_versions(evaluation)
    
    # Get the latest version to show who last modified the current version
    latest_version = all_versions[0] if all_versions else None
    
    # Get all history records (all previous values)
    from scorecard.models import EvaluationHistory
    history_records = evaluation.history_records.all().order_by("-recorded_at")
    
    # For maker view, we show CURRENT/PENDING information:
    # - For submitted: Show newly submitted scores (pending approval) prominently
    # - For returned/draft: Show current state
    # - Show pending vs approved scores when applicable
    
    context = {
        "evaluation": evaluation,
        "template": template,
        "sections": sections,
        "drivers_by_section": drivers_by_section,
        "attributes_by_driver": attributes_by_driver,
        "attribute_responses": attribute_responses,
        "driver_scores": driver_scores,
        "section_scores": section_scores,
        "grade_bands": grade_bands,
        "workflow_history": workflow_history,
        "all_versions": all_versions,
        "latest_version": latest_version,
        "history_records": history_records,
        "is_maker_view": True,  # Flag to indicate this is maker view
        "active_autofill_metadata": _get_active_autofill_metadata(
            evaluation,
            preferred_version=approved_version,
            fallback_version=latest_unapproved_version,
        ),
    }

    return render(
        request,
        "maker_checker/maker_questionnaire_view.html",
        context,
    )


# Alias for Basel scores URL naming
maker_basel_scores_view = maker_questionnaire_view



def _can_view_any_checker_approvals(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return any(
        user.has_perm(permission_code)
        for permission_code in (
            "scorecard.review_basel_scores",
            "scorecard.review_ifrs9_scores",
            "scorecard.review_basel_templates",
            "scorecard.review_ifrs9_templates",
        )
    )


def _append_checker_approval_rows(approvals, rows):
    approvals.extend(rows)
    return approvals


def checker_my_approvals_view(request: HttpRequest) -> HttpResponse:
    if not _can_view_any_checker_approvals(request.user):
        raise PermissionDenied("You do not have permission to access My Approvals.")

    user = request.user
    search_query = request.GET.get("search", "").strip()
    type_filter = request.GET.get("type", "all").strip() or "all"
    branch_filter = request.GET.get("branch", "").strip()
    date_from_raw = request.GET.get("date_from", "").strip()
    date_to_raw = request.GET.get("date_to", "").strip()
    date_from = parse_date(date_from_raw) if date_from_raw else None
    date_to = parse_date(date_to_raw) if date_to_raw else None
    branch_names = _get_request_scope_branch_names(request)
    approvals = []

    can_basel_scores = user.is_superuser or user.has_perm("scorecard.review_basel_scores")
    can_ifrs9_scores = user.is_superuser or user.has_perm("scorecard.review_ifrs9_scores")
    can_basel_templates = user.is_superuser or user.has_perm("scorecard.review_basel_templates")
    can_ifrs9_templates = user.is_superuser or user.has_perm("scorecard.review_ifrs9_templates")

    branch_filter_options = set()
    if can_basel_scores:
        branch_qs = CreditEvaluation.objects.filter(status="approved", approved_by=user).exclude(branch_name="")
        if branch_names:
            branch_qs = branch_qs.filter(branch_name__in=branch_names)
        branch_filter_options.update(branch_qs.values_list("branch_name", flat=True).distinct())
    if can_ifrs9_scores:
        branch_qs = IFRS9Evaluation.objects.filter(status="approved", approved_by=user).exclude(branch_name="")
        if branch_names:
            branch_qs = branch_qs.filter(branch_name__in=branch_names)
        branch_filter_options.update(branch_qs.values_list("branch_name", flat=True).distinct())

    include_basel_scores = type_filter in {"all", "basel_score"}
    include_ifrs9_scores = type_filter in {"all", "ifrs9_score"}
    include_basel_templates = type_filter in {"all", "basel_template"} and not branch_filter
    include_ifrs9_templates = type_filter in {"all", "ifrs9_template"} and not branch_filter

    if can_basel_scores and include_basel_scores:
        basel_scores = CreditEvaluation.objects.filter(
            status="approved",
            approved_by=user,
        ).select_related("template", "maker").defer("autofill_metadata").only(
            "id",
            "customer_name",
            "customer_id",
            "branch_name",
            "approved_at",
            "submitted_at",
            "created_at",
            "version",
            "template_id",
            "template__code",
            "maker_id",
            "maker__email",
            "submitted_by_id",
            "submitted_by__email",
        )
        if branch_names:
            basel_scores = basel_scores.filter(branch_name__in=branch_names)
        if branch_filter:
            basel_scores = basel_scores.filter(branch_name=branch_filter)
        if date_from:
            basel_scores = basel_scores.filter(approved_at__date__gte=date_from)
        if date_to:
            basel_scores = basel_scores.filter(approved_at__date__lte=date_to)
        if search_query:
            basel_scores = basel_scores.filter(
                Q(customer_name__icontains=search_query)
                | Q(customer_id__icontains=search_query)
                | Q(branch_name__icontains=search_query)
                | Q(template__code__icontains=search_query)
                | Q(maker__email__icontains=search_query)
            )
        _append_checker_approval_rows(approvals, [
            {
                "item_type": "Basel Score",
                "title": evaluation.customer_name or "Unnamed customer",
                "identifier": evaluation.customer_id or "-",
                "branch_name": evaluation.branch_name or "-",
                "details": f"Template {evaluation.template.code}" if getattr(evaluation, "template", None) else "-",
                "submitted_by": getattr(getattr(evaluation, "submitted_by", None), "email", "")
                or getattr(getattr(evaluation, "maker", None), "email", "")
                or "Unknown",
                "approved_at": evaluation.approved_at,
                "sort_at": evaluation.approved_at or evaluation.submitted_at or evaluation.created_at,
                "view_url": reverse("scorecard:checker_my_approvals_basel_score_view", kwargs={"evaluation_id": evaluation.id}),
            }
            for evaluation in basel_scores
        ])

    if can_ifrs9_scores and include_ifrs9_scores:
        ifrs9_scores = IFRS9Evaluation.objects.filter(
            status="approved",
            approved_by=user,
        ).select_related("template", "maker").defer("autofill_metadata").only(
            "id",
            "customer_name",
            "customer_id",
            "branch_name",
            "approved_at",
            "submitted_at",
            "created_at",
            "version",
            "template_id",
            "template__code",
            "maker_id",
            "maker__email",
            "submitted_by_id",
            "submitted_by__email",
        )
        if branch_names:
            ifrs9_scores = ifrs9_scores.filter(branch_name__in=branch_names)
        if branch_filter:
            ifrs9_scores = ifrs9_scores.filter(branch_name=branch_filter)
        if date_from:
            ifrs9_scores = ifrs9_scores.filter(approved_at__date__gte=date_from)
        if date_to:
            ifrs9_scores = ifrs9_scores.filter(approved_at__date__lte=date_to)
        if search_query:
            ifrs9_scores = ifrs9_scores.filter(
                Q(customer_name__icontains=search_query)
                | Q(customer_id__icontains=search_query)
                | Q(branch_name__icontains=search_query)
                | Q(template__code__icontains=search_query)
                | Q(maker__email__icontains=search_query)
            )
        _append_checker_approval_rows(approvals, [
            {
                "item_type": "IFRS9 Score",
                "title": evaluation.customer_name or "Unnamed customer",
                "identifier": evaluation.customer_id or "-",
                "branch_name": evaluation.branch_name or "-",
                "details": f"Template {evaluation.template.code}" if getattr(evaluation, "template", None) else "-",
                "submitted_by": getattr(getattr(evaluation, "submitted_by", None), "email", "")
                or getattr(getattr(evaluation, "maker", None), "email", "")
                or "Unknown",
                "approved_at": evaluation.approved_at,
                "sort_at": evaluation.approved_at or evaluation.submitted_at or evaluation.created_at,
                "view_url": reverse("scorecard:checker_my_approvals_ifrs9_score_view", kwargs={"evaluation_id": evaluation.id}),
            }
            for evaluation in ifrs9_scores
        ])

    if can_basel_templates and include_basel_templates:
        basel_templates = BaselScoreSheetTemplate.objects.filter(
            status="approved",
            approved_by=user,
        ).select_related("maker", "submitted_by").only(
            "id",
            "code",
            "name",
            "version",
            "approved_at",
            "submitted_at",
            "updated_at",
            "maker_id",
            "maker__email",
            "submitted_by_id",
            "submitted_by__email",
        )
        if date_from:
            basel_templates = basel_templates.filter(approved_at__date__gte=date_from)
        if date_to:
            basel_templates = basel_templates.filter(approved_at__date__lte=date_to)
        if search_query:
            basel_templates = basel_templates.filter(
                Q(code__icontains=search_query)
                | Q(name__icontains=search_query)
                | Q(maker__email__icontains=search_query)
                | Q(submitted_by__email__icontains=search_query)
            )
        _append_checker_approval_rows(approvals, [
            {
                "item_type": "Basel Template",
                "title": template.name or template.code or "Unnamed template",
                "identifier": template.code or "-",
                "branch_name": "-",
                "details": f"Version {template.version}" if template.version is not None else "-",
                "submitted_by": getattr(getattr(template, "submitted_by", None), "email", "")
                or getattr(getattr(template, "maker", None), "email", "")
                or "Unknown",
                "approved_at": template.approved_at,
                "sort_at": template.approved_at or template.submitted_at or template.updated_at,
                "view_url": reverse("scorecard:checker_my_approvals_basel_template_view", kwargs={"template_id": template.id}),
            }
            for template in basel_templates
        ])

    if can_ifrs9_templates and include_ifrs9_templates:
        ifrs9_templates = IFRS9ScoreSheetTemplate.objects.filter(
            status="approved",
            approved_by=user,
        ).select_related("maker", "submitted_by").only(
            "id",
            "code",
            "name",
            "version",
            "approved_at",
            "submitted_at",
            "updated_at",
            "maker_id",
            "maker__email",
            "submitted_by_id",
            "submitted_by__email",
        )
        if date_from:
            ifrs9_templates = ifrs9_templates.filter(approved_at__date__gte=date_from)
        if date_to:
            ifrs9_templates = ifrs9_templates.filter(approved_at__date__lte=date_to)
        if search_query:
            ifrs9_templates = ifrs9_templates.filter(
                Q(code__icontains=search_query)
                | Q(name__icontains=search_query)
                | Q(maker__email__icontains=search_query)
                | Q(submitted_by__email__icontains=search_query)
            )
        _append_checker_approval_rows(approvals, [
            {
                "item_type": "IFRS9 Template",
                "title": template.name or template.code or "Unnamed template",
                "identifier": template.code or "-",
                "branch_name": "-",
                "details": f"Version {template.version}" if template.version is not None else "-",
                "submitted_by": getattr(getattr(template, "submitted_by", None), "email", "")
                or getattr(getattr(template, "maker", None), "email", "")
                or "Unknown",
                "approved_at": template.approved_at,
                "sort_at": template.approved_at or template.submitted_at or template.updated_at,
                "view_url": reverse("scorecard:checker_my_approvals_ifrs9_template_view", kwargs={"template_id": template.id}),
            }
            for template in ifrs9_templates
        ])

    approvals.sort(key=lambda item: item.get("sort_at") or timezone.now(), reverse=True)

    log_scorecard_audit(
        user,
        "ScorecardCheckerApprovals",
        "view_my_approvals",
        object_id=user.pk,
        change_description=(
            "Opened checker My Approvals register; "
            f"Type: {type_filter}; Branch: {branch_filter or 'All'}; "
            f"Date from: {date_from_raw or 'Any'}; Date to: {date_to_raw or 'Any'}; "
            f"Search: {search_query or '-'}; Results: {len(approvals)}."
        ),
    )

    page_size = _normalize_list_page_size(request.GET.get("page_size"))
    paginator = Paginator(approvals, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))
    list_query_string = _build_list_query_string(request, excluded_keys={"page"})

    context = {
        "approvals": page_obj,
        "page_obj": page_obj,
        "search_query": search_query,
        "type_filter": type_filter,
        "branch_filter": branch_filter,
        "date_from": date_from_raw,
        "date_to": date_to_raw,
        "branch_filter_options": sorted(item for item in branch_filter_options if item),
        "page_size": page_size,
        "list_query_string": list_query_string,
        "page_title": "My Approvals",
    }

    return render(
        request,
        "maker_checker/checker_my_approvals.html",
        context,
    )


# ============================================================================
# CHECKER VIEWS
# ============================================================================

@login_required
def checker_pending_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all evaluations pending review for the current checker.
    Shows evaluations assigned to the checker that are in 'submitted' status.
    Also includes 'completed' status evaluations without a checker assigned (for backward compatibility).
    """
    # Show evaluations that are submitted OR completed without a checker (for backward compatibility)
    evaluations = CreditEvaluation.objects.filter(
        Q(status='submitted') | Q(status='completed', checker__isnull=True, approved_by__isnull=True)  # Include completed without checker/approval
    ).select_related('template', 'maker', 'submitted_by').defer('autofill_metadata').only(
        'id',
        'customer_name',
        'customer_id',
        'branch_name',
        'status',
        'submitted_at',
        'created_at',
        'template_id',
        'template__code',
        'template__name',
        'maker_id',
        'maker__email',
        'submitted_by_id',
        'submitted_by__email',
    ).order_by('submitted_at', 'created_at')
    
    # Filter by checker assignment (or show all if superuser)
    if not request.user.is_superuser:
        # Show evaluations assigned to this checker OR unassigned submitted/completed ones
        evaluations = evaluations.filter(
            Q(checker=request.user) | Q(checker__isnull=True)
        )
    
    branch_names = _get_request_scope_branch_names(request)
    if branch_names:
        evaluations = evaluations.filter(branch_name__in=branch_names)

    if not _can_user_self_review_basel_scores(request.user):
        evaluations = evaluations.exclude(
            Q(submitted_by=request.user) | Q(submitted_by__isnull=True, maker=request.user)
        )
    
    # Search functionality
    search_query = request.GET.get('search', '').strip()
    if search_query:
        evaluations = evaluations.filter(
            Q(customer_name__icontains=search_query) |
            Q(customer_id__icontains=search_query) |
            Q(branch_name__icontains=search_query) |
            Q(maker__email__icontains=search_query) |
            Q(submitted_by__email__icontains=search_query)
        )
    
    page_obj, page_size, list_query_string = _paginate_list_queryset(request, evaluations)

    context = {
        'evaluations': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'page_size': page_size,
        'list_query_string': list_query_string,
        'page_title': 'Pending Review',
    }
    
    return render(
        request,
        'maker_checker/checker_pending_list.html',
        context
    )


@login_required
def checker_review_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    Review an evaluation - checker can approve or return for changes.
    Shows read-only view of the evaluation with approve/reject actions.
    """
    evaluation = get_object_or_404(
        CreditEvaluation.objects.select_related(
            'template', 'maker', 'checker', 'submitted_by'
        ).prefetch_related(
            'attribute_responses__option',
            'attribute_responses__documents',
            'attribute_responses__attribute__risk_driver',
            'attribute_responses__attribute__options',
            'driver_scores__risk_driver__section',
            'section_scores__section',
            'workflow_history__performed_by',
            'versions__attribute_responses__attribute',
            'versions__attribute_responses__option',
            'versions__driver_scores__risk_driver',
            'versions__section_scores__section',
        ),
        id=evaluation_id
    )
    
    # Permission check - allow viewing if user can review OR if it's approved (for viewing after approval)
    if not evaluation.can_be_reviewed_by(request.user) and evaluation.status != 'approved':
        messages.error(request, "You don't have permission to review this evaluation.")
        return redirect('scorecard:checker_pending_list')

    if evaluation.status == 'submitted' and _must_use_different_score_reviewer(evaluation, request.user, basel=True):
        return _reject_self_score_review(request, 'scorecard:checker_pending_list')
    
    # Allow viewing submitted or approved evaluations
    if evaluation.status not in ['submitted', 'approved']:
        messages.error(request, "This evaluation is not available for review.")
        return redirect('scorecard:checker_pending_list')
    
    template = evaluation.template
    from scorecard.functions_view.basel_scores_form import _build_configuration
    # Use approved template version structure to ensure consistency
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=True)
    
    # Get workflow history
    workflow_history = list(evaluation.workflow_history.all())[:10]  # Last 10 entries
    
    # Get all versions for this evaluation
    all_versions = _cached_versions(evaluation)
    latest_version = all_versions[0] if all_versions else None
    
    # For approved questionnaires, get data from approved version
    # For submitted questionnaires, get data from latest unapproved version
    from scorecard.models import EvaluationVersion
    approved_version = None
    latest_unapproved_version = None
    
    # Initialize pending score variables
    pending_weighted_percent = None
    pending_grade = None
    score_difference = None
    
    if evaluation.status == 'approved':
        approved_version = _latest_cached_version(evaluation, approved=True)
    
    if evaluation.status == 'submitted':
        latest_unapproved_version = _latest_cached_version(evaluation, approved=False)
        # Get pending score from latest unapproved version
        if latest_unapproved_version:
            pending_weighted_percent = latest_unapproved_version.total_weighted_percent
            pending_grade = _version_grade_or_calculated(template, latest_unapproved_version)
            # Calculate score difference if approved score exists
            if evaluation.approved_weighted_percent is not None:
                from decimal import Decimal
                score_difference = float(pending_weighted_percent) - float(evaluation.approved_weighted_percent)
    
    # Get the latest approved version for comparison (if exists)
    latest_approved_version = None
    if evaluation.approved_weighted_percent is not None:
        latest_approved_version = _latest_cached_version(evaluation, approved=True)
    
    # Prepare comparison data: approved vs pending
    approved_attribute_responses = {}
    approved_driver_scores = {}
    approved_section_scores = {}

    if latest_approved_version:
        approved_current_responses = {}
        for response in _cached_attribute_responses(evaluation):
            approved_current_responses[response.attribute_id] = response
        # Get approved version data for comparison
        for version_response in latest_approved_version.attribute_responses.all():
            current_response = approved_current_responses.get(version_response.attribute_id)
            approved_attribute_responses[version_response.attribute_id] = {
                'option_id': version_response.option_id,
                'option': version_response.option,
                'allocated_score': version_response.allocated_score,
                'raw_value': version_response.raw_value,
                'documents': current_response.documents.all() if current_response else [],
            }
        
        for version_score in latest_approved_version.driver_scores.all():
            approved_driver_scores[version_score.risk_driver_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        for version_score in latest_approved_version.section_scores.all():
            approved_section_scores[version_score.section_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
    
    # Get attribute responses, driver scores, section scores
    if approved_version:
        # Use approved version data
        attribute_responses = {}
        current_responses = {}
        for response in _cached_attribute_responses(evaluation):
            current_responses[response.attribute_id] = response
        for version_response in approved_version.attribute_responses.all():
            current_response = current_responses.get(version_response.attribute_id)
            attribute_responses[version_response.attribute_id] = {
                'option_id': version_response.option_id,
                'option': version_response.option,
                'allocated_score': version_response.allocated_score,
                'raw_value': version_response.raw_value,
                'documents': current_response.documents.all() if current_response else [],
            }
        
        driver_scores = {}
        for version_score in approved_version.driver_scores.all():
            driver_scores[version_score.risk_driver_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        section_scores = {}
        for version_score in approved_version.section_scores.all():
            section_scores[version_score.section_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        display_weighted_percent = approved_version.total_weighted_percent
        has_pending_submission = False
    elif latest_unapproved_version:
        # Use latest unapproved version data (for submitted status)
        attribute_responses = {}
        current_responses = {}
        for response in _cached_attribute_responses(evaluation):
            current_responses[response.attribute_id] = response
        for version_response in latest_unapproved_version.attribute_responses.all():
            current_response = current_responses.get(version_response.attribute_id)
            attribute_responses[version_response.attribute_id] = {
                'option_id': version_response.option_id,
                'option': version_response.option,
                'allocated_score': version_response.allocated_score,
                'raw_value': version_response.raw_value,
                'documents': current_response.documents.all() if current_response else [],
            }
        
        driver_scores = {}
        for version_score in latest_unapproved_version.driver_scores.all():
            driver_scores[version_score.risk_driver_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        section_scores = {}
        for version_score in latest_unapproved_version.section_scores.all():
            section_scores[version_score.section_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        display_weighted_percent = latest_unapproved_version.total_weighted_percent
        has_pending_submission = True
    else:
        # Use current evaluation data
        attribute_responses = {}
        for response in _cached_attribute_responses(evaluation):
            attribute_responses[response.attribute_id] = response
        
        driver_scores = {}
        for score in evaluation.driver_scores.all():
            driver_scores[score.risk_driver_id] = score
        
        section_scores = {}
        for score in evaluation.section_scores.all():
            section_scores[score.section_id] = score
        
        display_weighted_percent = evaluation.total_weighted_percent
        has_pending_submission = False
    
    grade_bands = template.grade_bands.all().order_by("display_order")
    
    context = {
        'evaluation': evaluation,
        'template': template,
        'sections': sections,
        'drivers_by_section': drivers_by_section,
        'attributes_by_driver': attributes_by_driver,
        'attribute_responses': attribute_responses,
        'driver_scores': driver_scores,
        'section_scores': section_scores,
        'grade_bands': grade_bands,
        'workflow_history': workflow_history,
        'all_versions': all_versions,
        'latest_version': latest_version,
        'approved_version': approved_version,
        'display_weighted_percent': display_weighted_percent,
        'has_pending_submission': has_pending_submission,
        'pending_weighted_percent': pending_weighted_percent,
        'pending_grade': pending_grade,
        'score_difference': score_difference,
        'latest_unapproved_version': latest_unapproved_version,
        'latest_approved_version': latest_approved_version,
        'approved_attribute_responses': approved_attribute_responses,
        'approved_driver_scores': approved_driver_scores,
        'approved_section_scores': approved_section_scores,
        'active_autofill_metadata': _get_active_autofill_metadata(
            evaluation,
            preferred_version=approved_version,
            fallback_version=latest_unapproved_version,
        ),
    }
    
    return render(
        request,
        'maker_checker/checker_review.html',
        context
    )


@login_required
def approve_evaluation_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    Approve an evaluation - checker approves and locks it.
    Changes status from submitted to approved.
    """
    evaluation = get_object_or_404(CreditEvaluation, id=evaluation_id)
    
    # Permission check
    if not evaluation.can_be_reviewed_by(request.user):
        messages.error(request, "You don't have permission to approve this evaluation.")
        return redirect('scorecard:checker_pending_list')

    if _must_use_different_score_reviewer(evaluation, request.user, basel=True):
        return _reject_self_score_review(request, 'scorecard:checker_pending_list')
    
    if evaluation.status != 'submitted':
        messages.error(request, "This evaluation is not pending review.")
        return redirect('scorecard:checker_pending_list')
    
    if request.method == 'POST':
        with transaction.atomic():
            old_status = evaluation.status
            
            # Get the NEW scores from the latest UNAPPROVED version (which contains the submitted scores)
            # CRITICAL: We must get the unapproved version, not just any latest version
            # The latest version overall might be an old approved version (e.g., Version 10 with 115.49%)
            # We need the latest unapproved version (e.g., Version 17 with 93.49%)
            from scorecard.models import EvaluationVersion
            latest_unapproved_version = _latest_cached_version(evaluation, approved=False)
            
            if latest_unapproved_version:
                # Update approved scores from the unapproved version (which contains the NEWLY SUBMITTED scores)
                # The version snapshot contains the NEW calculated scores from the submission
                # This ensures we approve the correct new scores (93.49%), not the old approved scores (115.49%)
                evaluation.approved_weighted_percent = latest_unapproved_version.total_weighted_percent
                evaluation.approved_grade = latest_unapproved_version.final_grade
                # Also update the main score fields - these should only contain approved scores
                evaluation.total_weighted_percent = latest_unapproved_version.total_weighted_percent
                evaluation.final_grade = latest_unapproved_version.final_grade
                evaluation.total_raw_score = latest_unapproved_version.total_raw_score
                
                # Mark the latest unapproved version as approved
                latest_unapproved_version.is_approved = True
                latest_unapproved_version.approved_at = timezone.now()
                latest_unapproved_version.approved_by = request.user
                latest_unapproved_version.save()
                
                # Keep older unapproved/returned versions for a complete customer version history.
            else:
                # Fallback: use current evaluation scores (shouldn't happen, but safety check)
                # This might happen if version creation failed - use the current scores
                evaluation.approved_weighted_percent = evaluation.total_weighted_percent
                evaluation.approved_grade = evaluation.final_grade
            
            evaluation.status = 'approved'
            evaluation.approved_by = request.user
            evaluation.approved_at = timezone.now()
            evaluation.save()
            
            # Create workflow history entry
            EvaluationWorkflowHistory.objects.create(
                evaluation=evaluation,
                action='approved',
                from_status=old_status,
                to_status='approved',
                performed_by=request.user,
                comments=request.POST.get('comments', '').strip()
            )
            log_basel_score_audit(
                request.user,
                "approve",
                evaluation,
                f"Approved in maker checker workflow with score {evaluation.total_weighted_percent:.2f}% and grade {evaluation.final_grade or '-'}.",
            )
            
            messages.success(
                request,
                f"Evaluation approved successfully! "
                f"Version {evaluation.version} is now final and locked. "
                f"Score: {evaluation.total_weighted_percent:.2f}%, Grade: {evaluation.final_grade}"
            )
            notify_basel_approved(evaluation, request.user)
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            
            return redirect('scorecard:checker_pending_list')
    
    # GET request - show confirmation page
    # Get the pending score from the latest unapproved version for display
    from scorecard.models import EvaluationVersion
    latest_unapproved_version = _latest_cached_version(evaluation, approved=False)
    
    # Calculate pending score and grade for display
    pending_weighted_percent = None
    pending_grade = None
    if latest_unapproved_version:
        pending_weighted_percent = latest_unapproved_version.total_weighted_percent
        pending_grade = latest_unapproved_version.final_grade
    
    context = {
        'evaluation': evaluation,
        'pending_weighted_percent': pending_weighted_percent,
        'pending_grade': pending_grade,
        'latest_unapproved_version': latest_unapproved_version,
    }
    
    return render(
        request,
        'maker_checker/approve_evaluation.html',
        context
    )


@login_required
def return_evaluation_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    Return an evaluation for changes - checker rejects a submitted evaluation with comments.
    Only works for evaluations with status 'submitted' (under review).
    Changes status from submitted to returned.
    Approved evaluations cannot be returned - they are final.
    """
    evaluation = get_object_or_404(CreditEvaluation, id=evaluation_id)
    
    # Permission check
    if not evaluation.can_be_reviewed_by(request.user):
        messages.error(request, "You don't have permission to return this evaluation.")
        return redirect('scorecard:checker_pending_list')

    if _must_use_different_score_reviewer(evaluation, request.user, basel=True):
        return _reject_self_score_review(request, 'scorecard:checker_pending_list')
    
    if evaluation.status != 'submitted':
        messages.error(request, "This evaluation is not pending review.")
        return redirect('scorecard:checker_pending_list')
    
    if request.method == 'POST':
        return_reason = request.POST.get('return_reason', '').strip()
        
        if not return_reason:
            messages.error(request, "Please provide a reason for returning this evaluation.")
            context = {'evaluation': evaluation}
            return render(request, 'maker_checker/return_evaluation.html', context)
        
        with transaction.atomic():
            old_status = evaluation.status
            
            # IMPORTANT: When returning a submitted evaluation (under review):
            # 1. The submitted scores are rejected, so we keep approved scores as "current" (if they exist)
            # 2. Status changes to 'returned' so maker can edit and resubmit
            # 3. When maker edits and resubmits, new scores will be in total_weighted_percent/final_grade
            # 4. The approved scores remain as the "last approved" state until new approval
            
            evaluation.status = 'returned'
            evaluation.returned_by = request.user
            evaluation.returned_at = timezone.now()
            evaluation.return_reason = return_reason
            
            # Do NOT clear approved scores - they remain as the "last approved" state
            # This ensures that when returned, we show the approved scores (not the rejected submission)
            # If no approved scores exist yet (first submission returned), approved scores remain None
            
            evaluation.save()
            
            # Create workflow history entry
            EvaluationWorkflowHistory.objects.create(
                evaluation=evaluation,
                action='returned',
                from_status=old_status,
                to_status='returned',
                performed_by=request.user,
                comments=return_reason
            )
            log_basel_score_audit(
                request.user,
                "return",
                evaluation,
                f"Returned for changes from submitted state. Reason: {return_reason}",
            )
            
            messages.success(
                request,
                f"Evaluation returned for changes. "
                f"The submitter will be notified and can make corrections."
            )
            notify_basel_returned(evaluation, request.user, return_reason)
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            
            return redirect('scorecard:checker_pending_list')
    
    # GET request - show return form
    context = {
        'evaluation': evaluation,
    }
    
    return render(
        request,
        'maker_checker/return_evaluation.html',
        context
    )


# ============================================================================
# ADMIN VIEWS (Optional)
# ============================================================================

@login_required
def admin_reopen_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    Admin-only: Reopen an approved evaluation for editing.
    Changes status from approved to returned or in_progress.
    """
    if not request.user.is_superuser:
        messages.error(request, "Only administrators can reopen evaluations.")
        return redirect('scorecard:maker_draft_list')
    
    evaluation = get_object_or_404(CreditEvaluation, id=evaluation_id)
    
    if evaluation.status != 'approved':
        messages.error(request, "Only approved evaluations can be reopened.")
        return redirect('scorecard:maker_draft_list')
    
    if request.method == 'POST':
        reopen_reason = request.POST.get('reopen_reason', '').strip()
        new_status = request.POST.get('new_status', 'returned')  # returned or in_progress
        
        if not reopen_reason:
            messages.error(request, "Please provide a reason for reopening.")
            context = {'evaluation': evaluation}
            return render(request, 'maker_checker/admin_reopen.html', context)
        
        with transaction.atomic():
            old_status = evaluation.status
            evaluation.status = new_status
            evaluation.save()
            
            # Create workflow history entry
            EvaluationWorkflowHistory.objects.create(
                evaluation=evaluation,
                action='reopened',
                from_status=old_status,
                to_status=new_status,
                performed_by=request.user,
                comments=reopen_reason
            )
            log_basel_score_audit(
                request.user,
                "reopen",
                evaluation,
                f"Reopened by administrator from {old_status} to {new_status}. Reason: {reopen_reason}",
            )
            
            messages.success(
                request,
                f"Evaluation reopened successfully. "
                f"It is now editable again."
            )
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            
            return redirect('scorecard:maker_draft_list')
    
    # GET request - show reopen form
    context = {
        'evaluation': evaluation,
    }
    
    return render(
        request,
        'maker_checker/admin_reopen.html',
        context
    )


# ============================================================================
# IFRS9 MAKER VIEWS
# ============================================================================

@login_required
def maker_ifrs9_scores_draft_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all draft/in-progress/returned IFRS9 evaluations for the current maker.
    Shows evaluations that the maker can still edit.
    """
    evaluations = IFRS9Evaluation.objects.filter(
        Q(maker=request.user) | Q(submitted_by=request.user),
        status__in=['draft', 'in_progress', 'returned']
    ).select_related('template', 'checker', 'submitted_by').only(
        'id',
        'customer_name',
        'customer_id',
        'branch_name',
        'status',
        'updated_at',
        'version',
        'return_reason',
        'template_id',
        'template__code',
        'checker__email',
        'submitted_by_id',
    ).order_by('-updated_at')
    
    # Filter by branch if user has branch restrictions
    branch_names = _get_request_scope_branch_names(request)
    if branch_names:
        evaluations = evaluations.filter(branch_name__in=branch_names)
    
    # Search functionality
    search_query = request.GET.get('search', '').strip()
    if search_query:
        evaluations = evaluations.filter(
            Q(customer_name__icontains=search_query) |
            Q(customer_id__icontains=search_query) |
            Q(branch_name__icontains=search_query)
        )
    
    page_obj, page_size, list_query_string = _paginate_list_queryset(request, evaluations)
    _attach_completion_percentages(
        page_obj.object_list,
        attribute_model=IFRS9Attribute,
        response_model=IFRS9AttributeResponse,
        template_lookup='risk_driver__section__template_id',
    )

    context = {
        'evaluations': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'page_size': page_size,
        'list_query_string': list_query_string,
        'page_title': 'My IFRS9 Score Form Drafts',
    }
    
    return render(
        request,
        'maker_checker/ifrs9_scores_form/maker_ifrs9_scores_draft_list.html',
        context
    )


@login_required
def maker_ifrs9_scores_submitted_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all submitted IFRS9 evaluations for the current maker.
    Shows evaluations that are pending review or have been reviewed.
    """
    # Show records where this user is either the original maker or the latest submitter.
    evaluations = IFRS9Evaluation.objects.filter(
        Q(maker=request.user) | Q(submitted_by=request.user) | Q(maker__isnull=True),
        status__in=['submitted', 'approved', 'returned', 'completed']
    ).select_related('template', 'checker', 'submitted_by', 'approved_by', 'returned_by').only(
        'id',
        'customer_name',
        'customer_id',
        'branch_name',
        'status',
        'submitted_at',
        'created_at',
        'updated_at',
        'version',
        'template_id',
        'template__code',
        'checker_id',
        'checker__email',
        'submitted_by_id',
        'submitted_by__email',
        'approved_by_id',
        'returned_by_id',
    ).order_by('-submitted_at', '-created_at', '-updated_at')
    
    # Filter by branch if user has branch restrictions
    branch_names = _get_request_scope_branch_names(request)
    if branch_names:
        evaluations = evaluations.filter(branch_name__in=branch_names)
    
    # Search functionality
    search_query = request.GET.get('search', '').strip()
    if search_query:
        evaluations = evaluations.filter(
            Q(customer_name__icontains=search_query) |
            Q(customer_id__icontains=search_query) |
            Q(branch_name__icontains=search_query)
        )
    
    # Filter by status
    status_filter = request.GET.get('status', '')
    if status_filter:
        evaluations = evaluations.filter(status=status_filter)
    
    page_obj, page_size, list_query_string = _paginate_list_queryset(request, evaluations)

    context = {
        'evaluations': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'status_filter': status_filter,
        'page_size': page_size,
        'list_query_string': list_query_string,
        'page_title': 'My IFRS9 Score Form Submissions',
    }
    
    return render(
        request,
        'maker_checker/ifrs9_scores_form/maker_ifrs9_scores_submitted_list.html',
        context
    )


@login_required
def submit_ifrs9_scores_for_review_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    Submit an IFRS9 evaluation for review by the checker.
    Changes status from draft/in_progress/returned to submitted.
    """
    evaluation = get_object_or_404(
        IFRS9Evaluation.objects.select_related('template', 'maker', 'checker'),
        id=evaluation_id
    )
    
    # Permission check
    if not evaluation.can_be_edited_by(request.user):
        messages.error(request, "You don't have permission to submit this evaluation.")
        return redirect('scorecard:maker_ifrs9_scores_draft_list')
    
    if request.method == 'POST':
        with transaction.atomic():
            # Update status
            old_status = evaluation.status
            evaluation.status = 'submitted'
            evaluation.submitted_by = request.user
            evaluation.submitted_at = timezone.now()
            
            # Increment version if resubmitting
            if old_status == 'returned':
                evaluation.version += 1
                evaluation.resubmission_count += 1
            
            evaluation.save()
            
            # Create workflow history entry
            IFRS9EvaluationWorkflowHistory.objects.create(
                evaluation=evaluation,
                action='submitted',
                from_status=old_status,
                to_status='submitted',
                performed_by=request.user,
                comments=request.POST.get('comments', '').strip()
            )
            log_ifrs9_score_audit(
                request.user,
                "submit_for_review",
                evaluation,
                f"Submitted from maker checker workflow. Previous status: {old_status}.",
            )
            
            messages.success(
                request,
                f"IFRS9 Score Form submitted for review successfully! "
                f"Version {evaluation.version} is now pending checker approval."
            )
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            
            return redirect('scorecard:maker_ifrs9_scores_submitted_list')
    
    # GET request - show confirmation page
    context = {
        'evaluation': evaluation,
        'completion_percentage': evaluation.get_completion_percentage(),
    }
    
    return render(
        request,
        'maker_checker/ifrs9_scores_form/submit_ifrs9_scores_for_review.html',
        context
    )


@login_required
def withdraw_ifrs9_scores_submission_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    Withdraw a submitted IFRS9 evaluation (only if checker hasn't acted yet).
    Changes status from submitted back to in_progress.
    """
    evaluation = get_object_or_404(IFRS9Evaluation, id=evaluation_id)
    
    # Permission check
    if (
        evaluation.maker != request.user
        and evaluation.submitted_by != request.user
        and not request.user.is_superuser
    ):
        messages.error(request, "You don't have permission to withdraw this evaluation.")
        return redirect('scorecard:maker_ifrs9_scores_submitted_list')
    
    if evaluation.status != 'submitted':
        messages.error(request, "Only submitted evaluations can be withdrawn.")
        return redirect('scorecard:maker_ifrs9_scores_submitted_list')
    
    if request.method == 'POST':
        with transaction.atomic():
            old_status = evaluation.status
            
            # When withdrawing, restore approved scores if they exist
            if evaluation.approved_weighted_percent is not None:
                evaluation.total_weighted_percent = evaluation.approved_weighted_percent
                evaluation.final_grade = ""
            else:
                evaluation.total_weighted_percent = None
                evaluation.final_grade = ""
                evaluation.total_raw_score = None
            
            evaluation.status = 'in_progress'
            evaluation.save()
            
            # Create workflow history entry
            IFRS9EvaluationWorkflowHistory.objects.create(
                evaluation=evaluation,
                action='withdrawn',
                from_status=old_status,
                to_status='in_progress',
                performed_by=request.user,
                comments=request.POST.get('comments', '').strip() or 'Withdrawn by submitter'
            )
            log_ifrs9_score_audit(
                request.user,
                "withdraw_submission",
                evaluation,
                f"Submission withdrawn back to in_progress from {old_status}.",
            )
            
            messages.success(request, "Submission withdrawn successfully. You can continue editing.")
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            return redirect('scorecard:maker_ifrs9_scores_draft_list')
    
    # GET request - show confirmation page
    context = {
        'evaluation': evaluation,
    }
    
    return render(
        request,
        'maker_checker/ifrs9_scores_form/withdraw_ifrs9_scores_submission.html',
        context
    )


@login_required
def maker_ifrs9_scores_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    View to display an IFRS9 score form for the maker.
    Shows CURRENT/PENDING information that needs verification.
    """
    evaluation = get_object_or_404(
        IFRS9Evaluation.objects.select_related("template", "maker", "checker").prefetch_related(
            "section_scores__section",
            "driver_scores__risk_driver__section",
            "attribute_responses__attribute__risk_driver",
            "attribute_responses__option",
            "attribute_responses__attribute__options",
            "attribute_responses__documents__uploaded_by",
            "versions__attribute_responses__attribute",
            "versions__attribute_responses__option",
            "versions__driver_scores__risk_driver",
            "versions__section_scores__section",
        ),
        id=evaluation_id,
    )
    
    # Permission check - original maker, latest submitter, or admin can view.
    if (
        evaluation.maker != request.user
        and evaluation.submitted_by != request.user
        and not request.user.is_superuser
    ):
        messages.error(request, "You don't have permission to view this IFRS9 score form.")
        return redirect('scorecard:maker_ifrs9_scores_draft_list')
    
    template = evaluation.template
    from scorecard.functions_view.ifrs9_score_config import _build_configuration
    # Use approved template version structure to ensure consistency
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=True)

    # For submitted evaluations, get data from the latest unapproved version
    # For approved evaluations, get data from the approved version
    from scorecard.models import IFRS9EvaluationVersion
    latest_unapproved_version = None
    approved_version = None
    
    if evaluation.status == 'submitted':
        latest_unapproved_version = _latest_cached_version(evaluation, approved=False)
    elif evaluation.status == 'approved':
        approved_version = _latest_cached_version(evaluation, approved=True)
    
    if approved_version:
        # Use the approved version's data
        current_responses = {}
        for response in _cached_attribute_responses(evaluation):
            current_responses[response.attribute_id] = response
        
        attribute_responses = {}
        for version_response in approved_version.attribute_responses.all():
            current_response = current_responses.get(version_response.attribute_id)
            attribute_responses[version_response.attribute_id] = {
                'option_id': version_response.option_id,
                'option': version_response.option,
                'allocated_score': version_response.allocated_score,
                'raw_value': version_response.raw_value,
                'documents': current_response.documents.all() if current_response else [],
            }
        
        driver_scores = {}
        for version_score in approved_version.driver_scores.all():
            driver_scores[version_score.risk_driver_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        section_scores = {}
        for version_score in approved_version.section_scores.all():
            section_scores[version_score.section_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
    elif latest_unapproved_version:
        # Use the latest unapproved version's data
        current_responses = {}
        for response in _cached_attribute_responses(evaluation):
            current_responses[response.attribute_id] = response
        
        attribute_responses = {}
        for version_response in latest_unapproved_version.attribute_responses.all():
            current_response = current_responses.get(version_response.attribute_id)
            attribute_responses[version_response.attribute_id] = {
                'option_id': version_response.option_id,
                'option': version_response.option,
                'allocated_score': version_response.allocated_score,
                'raw_value': version_response.raw_value,
                'documents': current_response.documents.all() if current_response else [],
            }
        
        driver_scores = {}
        for version_score in latest_unapproved_version.driver_scores.all():
            driver_scores[version_score.risk_driver_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        section_scores = {}
        for version_score in latest_unapproved_version.section_scores.all():
            section_scores[version_score.section_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
    else:
        # Use current evaluation data
        attribute_responses = {}
        for response in _cached_attribute_responses(evaluation):
            attribute_responses[response.attribute_id] = response

        driver_scores = {}
        for score in evaluation.driver_scores.all():
            driver_scores[score.risk_driver_id] = score

        section_scores = {}
        for score in evaluation.section_scores.all():
            section_scores[score.section_id] = score
    
    # Get workflow history
    workflow_history = list(evaluation.workflow_history.all())[:10]
    
    # Get all versions for this evaluation
    all_versions = _cached_versions(evaluation)
    
    # Get the latest version
    latest_version = all_versions[0] if all_versions else None
    
    # Get all history records
    from scorecard.models import IFRS9EvaluationHistory
    history_records = evaluation.history_records.all().order_by("-recorded_at")
    
    context = {
        "evaluation": evaluation,
        "template": template,
        "sections": sections,
        "drivers_by_section": drivers_by_section,
        "attributes_by_driver": attributes_by_driver,
        "attribute_responses": attribute_responses,
        "driver_scores": driver_scores,
        "section_scores": section_scores,
        "workflow_history": workflow_history,
        "all_versions": all_versions,
        "latest_version": latest_version,
        "history_records": history_records,
        "is_maker_view": True,
        "active_autofill_metadata": _get_active_autofill_metadata(
            evaluation,
            preferred_version=approved_version,
            fallback_version=latest_unapproved_version,
        ),
    }

    return render(
        request,
        "maker_checker/ifrs9_scores_form/maker_ifrs9_scores_view.html",
        context,
    )


# ============================================================================
# IFRS9 CHECKER VIEWS
# ============================================================================

@login_required
def checker_ifrs9_scores_pending_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all IFRS9 evaluations pending review for the current checker.
    Shows evaluations assigned to the checker that are in 'submitted' status.
    """
    evaluations = IFRS9Evaluation.objects.filter(
        Q(status='submitted') | Q(status='completed', checker__isnull=True, approved_by__isnull=True)
    ).select_related('template', 'maker', 'submitted_by').defer('autofill_metadata').only(
        'id',
        'customer_name',
        'customer_id',
        'branch_name',
        'status',
        'submitted_at',
        'created_at',
        'template_id',
        'template__code',
        'template__name',
        'maker_id',
        'maker__email',
        'submitted_by_id',
        'submitted_by__email',
    ).order_by('submitted_at', 'created_at')
    
    # Filter by checker assignment
    if not request.user.is_superuser:
        evaluations = evaluations.filter(
            Q(checker=request.user) | Q(checker__isnull=True)
        )
    
    branch_names = _get_request_scope_branch_names(request)
    if branch_names:
        evaluations = evaluations.filter(branch_name__in=branch_names)

    if not _can_user_self_review_ifrs9_scores(request.user):
        evaluations = evaluations.exclude(
            Q(submitted_by=request.user) | Q(submitted_by__isnull=True, maker=request.user)
        )
    
    # Search functionality
    search_query = request.GET.get('search', '').strip()
    if search_query:
        evaluations = evaluations.filter(
            Q(customer_name__icontains=search_query) |
            Q(customer_id__icontains=search_query) |
            Q(branch_name__icontains=search_query) |
            Q(maker__email__icontains=search_query) |
            Q(submitted_by__email__icontains=search_query)
        )
    
    page_obj, page_size, list_query_string = _paginate_list_queryset(request, evaluations)

    context = {
        'evaluations': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'page_size': page_size,
        'list_query_string': list_query_string,
        'page_title': 'IFRS9 Score Forms Pending Review',
    }
    
    return render(
        request,
        'maker_checker/ifrs9_scores_form/checker_ifrs9_scores_pending_list.html',
        context
    )


@login_required
def checker_ifrs9_scores_review_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    Review an IFRS9 evaluation - checker can approve or return for changes.
    Shows read-only view of the evaluation with approve/reject actions.
    """
    evaluation = get_object_or_404(
        IFRS9Evaluation.objects.select_related(
            'template', 'maker', 'checker', 'submitted_by'
        ).prefetch_related(
            'attribute_responses__option',
            'attribute_responses__documents',
            'attribute_responses__attribute__risk_driver',
            'attribute_responses__attribute__options',
            'driver_scores__risk_driver__section',
            'section_scores__section',
            'workflow_history__performed_by',
            'versions__attribute_responses__attribute',
            'versions__attribute_responses__option',
            'versions__driver_scores__risk_driver',
            'versions__section_scores__section',
        ),
        id=evaluation_id
    )
    
    # Permission check
    if not evaluation.can_be_reviewed_by(request.user) and evaluation.status != 'approved':
        messages.error(request, "You don't have permission to review this evaluation.")
        return redirect('scorecard:checker_ifrs9_scores_pending_list')

    if evaluation.status == 'submitted' and _must_use_different_score_reviewer(evaluation, request.user, basel=False):
        return _reject_self_score_review(request, 'scorecard:checker_ifrs9_scores_pending_list')
    
    if evaluation.status not in ['submitted', 'approved']:
        messages.error(request, "This evaluation is not available for review.")
        return redirect('scorecard:checker_ifrs9_scores_pending_list')
    
    template = evaluation.template
    from scorecard.functions_view.ifrs9_score_config import _build_configuration
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=True)
    
    # Get workflow history
    workflow_history = list(evaluation.workflow_history.all())[:10]
    
    # Get all versions
    all_versions = _cached_versions(evaluation)
    latest_version = all_versions[0] if all_versions else None
    
    # Get version data
    from scorecard.models import IFRS9EvaluationVersion
    approved_version = None
    latest_unapproved_version = None
    
    pending_weighted_percent = None
    pending_grade = None
    score_difference = None
    
    if evaluation.status == 'approved':
        approved_version = _latest_cached_version(evaluation, approved=True)
    
    if evaluation.status == 'submitted':
        latest_unapproved_version = _latest_cached_version(evaluation, approved=False)
        if latest_unapproved_version:
            pending_weighted_percent = latest_unapproved_version.total_weighted_percent
            pending_grade = _version_grade_or_calculated(template, latest_unapproved_version)
            if evaluation.approved_weighted_percent is not None:
                from decimal import Decimal
                score_difference = float(pending_weighted_percent) - float(evaluation.approved_weighted_percent)
    
    latest_approved_version = None
    if evaluation.approved_weighted_percent is not None:
        latest_approved_version = _latest_cached_version(evaluation, approved=True)
    
    # Prepare comparison data
    approved_attribute_responses = {}
    approved_driver_scores = {}
    approved_section_scores = {}

    if latest_approved_version:
        approved_current_responses = {}
        for response in _cached_attribute_responses(evaluation):
            approved_current_responses[response.attribute_id] = response
        for version_response in latest_approved_version.attribute_responses.all():
            current_response = approved_current_responses.get(version_response.attribute_id)
            approved_attribute_responses[version_response.attribute_id] = {
                'option_id': version_response.option_id,
                'option': version_response.option,
                'allocated_score': version_response.allocated_score,
                'raw_value': version_response.raw_value,
                'documents': current_response.documents.all() if current_response else [],
            }
        
        for version_score in latest_approved_version.driver_scores.all():
            approved_driver_scores[version_score.risk_driver_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        for version_score in latest_approved_version.section_scores.all():
            approved_section_scores[version_score.section_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
    
    # Get attribute responses, driver scores, section scores
    if approved_version:
        attribute_responses = {}
        current_responses = {}
        for response in _cached_attribute_responses(evaluation):
            current_responses[response.attribute_id] = response
        for version_response in approved_version.attribute_responses.all():
            current_response = current_responses.get(version_response.attribute_id)
            attribute_responses[version_response.attribute_id] = {
                'option_id': version_response.option_id,
                'option': version_response.option,
                'allocated_score': version_response.allocated_score,
                'raw_value': version_response.raw_value,
                'documents': current_response.documents.all() if current_response else [],
            }
        
        driver_scores = {}
        for version_score in approved_version.driver_scores.all():
            driver_scores[version_score.risk_driver_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        section_scores = {}
        for version_score in approved_version.section_scores.all():
            section_scores[version_score.section_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        display_weighted_percent = approved_version.total_weighted_percent
        has_pending_submission = False
    elif latest_unapproved_version:
        attribute_responses = {}
        current_responses = {}
        for response in _cached_attribute_responses(evaluation):
            current_responses[response.attribute_id] = response
        for version_response in latest_unapproved_version.attribute_responses.all():
            current_response = current_responses.get(version_response.attribute_id)
            attribute_responses[version_response.attribute_id] = {
                'option_id': version_response.option_id,
                'option': version_response.option,
                'allocated_score': version_response.allocated_score,
                'raw_value': version_response.raw_value,
                'documents': current_response.documents.all() if current_response else [],
            }
        
        driver_scores = {}
        for version_score in latest_unapproved_version.driver_scores.all():
            driver_scores[version_score.risk_driver_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        section_scores = {}
        for version_score in latest_unapproved_version.section_scores.all():
            section_scores[version_score.section_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        display_weighted_percent = latest_unapproved_version.total_weighted_percent
        has_pending_submission = True
    else:
        attribute_responses = {}
        for response in _cached_attribute_responses(evaluation):
            attribute_responses[response.attribute_id] = response
        
        driver_scores = {}
        for score in evaluation.driver_scores.all():
            driver_scores[score.risk_driver_id] = score
        
        section_scores = {}
        for score in evaluation.section_scores.all():
            section_scores[score.section_id] = score
        
        display_weighted_percent = evaluation.total_weighted_percent
        has_pending_submission = False
    
    context = {
        'evaluation': evaluation,
        'template': template,
        'sections': sections,
        'drivers_by_section': drivers_by_section,
        'attributes_by_driver': attributes_by_driver,
        'attribute_responses': attribute_responses,
        'driver_scores': driver_scores,
        'section_scores': section_scores,
        'workflow_history': workflow_history,
        'all_versions': all_versions,
        'latest_version': latest_version,
        'approved_version': approved_version,
        'display_weighted_percent': display_weighted_percent,
        'has_pending_submission': has_pending_submission,
        'pending_weighted_percent': pending_weighted_percent,
        'pending_grade': pending_grade,
        'score_difference': score_difference,
        'latest_unapproved_version': latest_unapproved_version,
        'latest_approved_version': latest_approved_version,
        'approved_attribute_responses': approved_attribute_responses,
        'approved_driver_scores': approved_driver_scores,
        'approved_section_scores': approved_section_scores,
        'active_autofill_metadata': _get_active_autofill_metadata(
            evaluation,
            preferred_version=approved_version,
            fallback_version=latest_unapproved_version,
        ),
    }
    
    return render(
        request,
        'maker_checker/ifrs9_scores_form/checker_ifrs9_scores_review.html',
        context
    )


@login_required
def approve_ifrs9_scores_evaluation_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    Approve an IFRS9 evaluation - checker approves and locks it.
    Changes status from submitted to approved.
    """
    evaluation = get_object_or_404(IFRS9Evaluation, id=evaluation_id)
    
    # Permission check
    if not evaluation.can_be_reviewed_by(request.user):
        messages.error(request, "You don't have permission to approve this evaluation.")
        return redirect('scorecard:checker_ifrs9_scores_pending_list')

    if _must_use_different_score_reviewer(evaluation, request.user, basel=False):
        return _reject_self_score_review(request, 'scorecard:checker_ifrs9_scores_pending_list')
    
    if evaluation.status != 'submitted':
        messages.error(request, "This evaluation is not pending review.")
        return redirect('scorecard:checker_ifrs9_scores_pending_list')
    
    if request.method == 'POST':
        with transaction.atomic():
            old_status = evaluation.status
            
            from scorecard.models import IFRS9EvaluationVersion
            latest_unapproved_version = _latest_cached_version(evaluation, approved=False)
            
            if latest_unapproved_version:
                evaluation.approved_weighted_percent = latest_unapproved_version.total_weighted_percent
                evaluation.approved_grade = ""
                evaluation.total_weighted_percent = latest_unapproved_version.total_weighted_percent
                evaluation.final_grade = ""
                evaluation.total_raw_score = latest_unapproved_version.total_raw_score
                
                latest_unapproved_version.is_approved = True
                latest_unapproved_version.approved_at = timezone.now()
                latest_unapproved_version.approved_by = request.user
                latest_unapproved_version.save()
                
                # Keep older unapproved/returned versions for a complete customer version history.
            else:
                evaluation.approved_weighted_percent = evaluation.total_weighted_percent
                evaluation.approved_grade = ""
            
            evaluation.status = 'approved'
            evaluation.approved_by = request.user
            evaluation.approved_at = timezone.now()
            evaluation.save()
            
            # Create workflow history entry
            IFRS9EvaluationWorkflowHistory.objects.create(
                evaluation=evaluation,
                action='approved',
                from_status=old_status,
                to_status='approved',
                performed_by=request.user,
                comments=request.POST.get('comments', '').strip()
            )
            log_ifrs9_score_audit(
                request.user,
                "approve",
                evaluation,
                f"Approved in maker checker workflow with score {evaluation.total_weighted_percent:.2f}%.",
            )
            
            messages.success(
                request,
                f"IFRS9 Score Form approved successfully! "
                f"Version {evaluation.version} is now final and locked. "
                f"Score: {evaluation.total_weighted_percent:.2f}%"
            )
            notify_ifrs9_approved(evaluation, request.user)
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            
            return redirect('scorecard:checker_ifrs9_scores_pending_list')
    
    # GET request - show confirmation page
    from scorecard.models import IFRS9EvaluationVersion
    latest_unapproved_version = _latest_cached_version(evaluation, approved=False)
    
    pending_weighted_percent = None
    if latest_unapproved_version:
        pending_weighted_percent = latest_unapproved_version.total_weighted_percent
    
    context = {
        'evaluation': evaluation,
        'pending_weighted_percent': pending_weighted_percent,
        'latest_unapproved_version': latest_unapproved_version,
    }
    
    return render(
        request,
        'maker_checker/ifrs9_scores_form/approve_ifrs9_scores_evaluation.html',
        context
    )


@login_required
def return_ifrs9_scores_evaluation_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    Return an IFRS9 evaluation for changes - checker rejects a submitted evaluation with comments.
    Changes status from submitted to returned.
    """
    evaluation = get_object_or_404(IFRS9Evaluation, id=evaluation_id)
    
    # Permission check
    if not evaluation.can_be_reviewed_by(request.user):
        messages.error(request, "You don't have permission to return this evaluation.")
        return redirect('scorecard:checker_ifrs9_scores_pending_list')

    if _must_use_different_score_reviewer(evaluation, request.user, basel=False):
        return _reject_self_score_review(request, 'scorecard:checker_ifrs9_scores_pending_list')
    
    if evaluation.status != 'submitted':
        messages.error(request, "This evaluation is not pending review.")
        return redirect('scorecard:checker_ifrs9_scores_pending_list')
    
    if request.method == 'POST':
        return_reason = request.POST.get('return_reason', '').strip()
        
        if not return_reason:
            messages.error(request, "Please provide a reason for returning this evaluation.")
            context = {'evaluation': evaluation}
            return render(request, 'maker_checker/ifrs9_scores_form/return_ifrs9_scores_evaluation.html', context)
        
        with transaction.atomic():
            old_status = evaluation.status
            
            evaluation.status = 'returned'
            evaluation.returned_by = request.user
            evaluation.returned_at = timezone.now()
            evaluation.return_reason = return_reason
            
            evaluation.save()
            
            # Create workflow history entry
            IFRS9EvaluationWorkflowHistory.objects.create(
                evaluation=evaluation,
                action='returned',
                from_status=old_status,
                to_status='returned',
                performed_by=request.user,
                comments=return_reason
            )
            log_ifrs9_score_audit(
                request.user,
                "return",
                evaluation,
                f"Returned for changes from submitted state. Reason: {return_reason}",
            )
            
            messages.success(
                request,
                f"IFRS9 Score Form returned for changes. "
                f"The submitter will be notified and can make corrections."
            )
            notify_ifrs9_returned(evaluation, request.user, return_reason)
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            
            return redirect('scorecard:checker_ifrs9_scores_pending_list')
    
    # GET request - show return form
    context = {
        'evaluation': evaluation,
    }
    
    return render(
        request,
        'maker_checker/ifrs9_scores_form/return_ifrs9_scores_evaluation.html',
        context
    )


# ============================================================================
# IFRS9 ADMIN VIEWS
# ============================================================================

@login_required
def admin_reopen_ifrs9_scores_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    Admin-only: Reopen an approved IFRS9 evaluation for editing.
    Changes status from approved to returned or in_progress.
    """
    if not request.user.is_superuser:
        messages.error(request, "Only administrators can reopen evaluations.")
        return redirect('scorecard:maker_ifrs9_scores_draft_list')
    
    evaluation = get_object_or_404(IFRS9Evaluation, id=evaluation_id)
    
    if evaluation.status != 'approved':
        messages.error(request, "Only approved evaluations can be reopened.")
        return redirect('scorecard:maker_ifrs9_scores_draft_list')
    
    if request.method == 'POST':
        reopen_reason = request.POST.get('reopen_reason', '').strip()
        new_status = request.POST.get('new_status', 'returned')
        
        if not reopen_reason:
            messages.error(request, "Please provide a reason for reopening.")
            context = {'evaluation': evaluation}
            return render(request, 'maker_checker/ifrs9_scores_form/admin_reopen_ifrs9_scores.html', context)
        
        with transaction.atomic():
            old_status = evaluation.status
            evaluation.status = new_status
            evaluation.save()
            
            # Create workflow history entry
            IFRS9EvaluationWorkflowHistory.objects.create(
                evaluation=evaluation,
                action='reopened',
                from_status=old_status,
                to_status=new_status,
                performed_by=request.user,
                comments=reopen_reason
            )
            log_ifrs9_score_audit(
                request.user,
                "reopen",
                evaluation,
                f"Reopened by administrator from {old_status} to {new_status}. Reason: {reopen_reason}",
            )
            
            messages.success(
                request,
                f"IFRS9 Score Form reopened successfully. "
                f"It is now editable again."
            )
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            
            return redirect('scorecard:maker_ifrs9_scores_draft_list')
    
    # GET request - show reopen form
    context = {
        'evaluation': evaluation,
    }
    
    return render(
        request,
        'maker_checker/ifrs9_scores_form/admin_reopen_ifrs9_scores.html',
        context
    )
