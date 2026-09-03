"""
Maker-Checker Workflow Views for Template Management

This module implements maker-checker workflow for template configuration:
- Maker: creates/edits templates (sections, drivers, attributes, options)
- Checker: reviews and approves/rejects template changes
- Supervisor/Admin: can reassign, override, or unlock

Status workflow:
- Draft / In Progress: maker is still working
- Submitted (Pending Review): maker finished, sent to checker, locked from editing
- Returned (Needs Changes): checker rejected with comments, maker can edit again
- Approved (Completed): checker approved, final/locked
- Cancelled / Voided: stopped/invalid
"""

from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from scorecard.context_processors import (
    bump_checker_pending_counts_version,
    bump_maker_queue_counts_version,
)
from scorecard.models import (
    Attribute,
    BaselScoreSheetTemplate,
    Option,
    RiskDriver,
    Section,
    TemplateVersion,
    TemplateVersionAttribute,
    TemplateVersionOption,
    TemplateVersionRiskDriver,
    TemplateVersionSection,
    TemplateWorkflowHistory,
    IFRS9ScoreSheetTemplate,
    IFRS9Section,
    IFRS9RiskDriver,
    IFRS9Attribute,
    IFRS9Option,
    IFRS9TemplateVersion,
    IFRS9TemplateVersionSection,
    IFRS9TemplateVersionRiskDriver,
    IFRS9TemplateVersionAttribute,
    IFRS9TemplateVersionOption,
    IFRS9TemplateWorkflowHistory,
)
from scorecard.functions_view.credit_scoreshits import _build_configuration
from scorecard.functions_view.ifrs9_score_config import _build_configuration as _build_ifrs9_configuration
from scorecard.functions_view.audit import log_basel_template_audit, log_ifrs9_template_audit
from scorecard.functions_view.notifications import (
    notify_basel_template_approved,
    notify_basel_template_returned,
    notify_basel_template_submitted,
    notify_ifrs9_template_approved,
    notify_ifrs9_template_returned,
    notify_ifrs9_template_submitted,
)
from scorecard.workflow_approval import (
    can_user_self_review_template_submission,
    should_auto_approve_scorecard_workflow,
)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

LIST_PAGE_SIZE_OPTIONS = (20, 50, 100)


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

def _can_auto_approve_basel_template_submission(user) -> bool:
    return should_auto_approve_scorecard_workflow(
        user,
        "basel_templates",
        "scorecard.review_basel_templates",
    )


def _can_auto_approve_ifrs9_template_submission(user) -> bool:
    return should_auto_approve_scorecard_workflow(
        user,
        "ifrs9_templates",
        "scorecard.review_ifrs9_templates",
    )


def _can_user_self_review_basel_templates(user) -> bool:
    return can_user_self_review_template_submission(
        user,
        "basel_templates",
        "scorecard.review_basel_templates",
    )


def _can_user_self_review_ifrs9_templates(user) -> bool:
    return can_user_self_review_template_submission(
        user,
        "ifrs9_templates",
        "scorecard.review_ifrs9_templates",
    )


def _active_template_submitter_id(template):
    return getattr(template, "submitted_by_id", None) or getattr(template, "maker_id", None)


def _template_belongs_to_current_submitter(template, user) -> bool:
    user_id = getattr(user, "id", None)
    return bool(user_id and _active_template_submitter_id(template) == user_id)


def _must_use_different_template_reviewer(template, user, *, basel: bool) -> bool:
    if not _template_belongs_to_current_submitter(template, user):
        return False
    if basel:
        return not _can_user_self_review_basel_templates(user)
    return not _can_user_self_review_ifrs9_templates(user)


def _reject_self_template_review(request, redirect_name: str) -> HttpResponse:
    messages.error(
        request,
        "You cannot review, approve, or return your own submitted template while template auto-approval is disabled. Another checker must handle this template.",
    )
    return redirect(redirect_name)


def _get_basel_template_total_weight(template: BaselScoreSheetTemplate) -> Decimal:
    total_weight = Decimal("0")
    for section in template.sections.all():
        total_weight += section.get_total_weight_percent()
    return total_weight


def _get_ifrs9_template_total_weight(template: IFRS9ScoreSheetTemplate) -> Decimal:
    total_weight = Decimal("0")
    for section in template.sections.all():
        total_weight += section.get_total_weight_percent()
    return total_weight


def _build_template_structure_totals(sections) -> tuple[dict[int, dict[str, Decimal]], dict[str, Decimal]]:
    section_totals: dict[int, dict[str, Decimal]] = {}
    overall_allocated = Decimal("0")
    overall_weight = Decimal("0")

    for section in sections:
        allocated_total = section.get_total_max_score()
        weight_total = section.get_total_weight_percent()
        section_totals[section.id] = {
            "allocated": allocated_total,
            "weight": weight_total,
        }
        overall_allocated += allocated_total
        overall_weight += weight_total

    overall_totals = {
        "allocated": overall_allocated,
        "weight": overall_weight,
    }
    return section_totals, overall_totals


def _finalize_basel_template_auto_approval(
    template: BaselScoreSheetTemplate,
    acting_user,
    *,
    old_status: str,
    comments: str,
) -> None:
    approval_time = timezone.now()
    latest_unapproved_version = (
        template.versions.filter(is_approved=False).order_by("-version_number").first()
    )

    if latest_unapproved_version:
        latest_unapproved_version.is_approved = True
        latest_unapproved_version.approved_at = approval_time
        latest_unapproved_version.approved_by = acting_user
        latest_unapproved_version.save()
        template.versions.filter(is_approved=False).exclude(
            id=latest_unapproved_version.id
        ).delete()

    template.status = "approved"
    template.approved_by = acting_user
    template.approved_at = approval_time
    template.save()

    TemplateWorkflowHistory.objects.create(
        template=template,
        action="approved",
        from_status=old_status,
        to_status="approved",
        performed_by=acting_user,
        comments=comments,
    )


def _finalize_ifrs9_template_auto_approval(
    template: IFRS9ScoreSheetTemplate,
    acting_user,
    *,
    old_status: str,
    comments: str,
) -> None:
    approval_time = timezone.now()
    latest_unapproved_version = (
        template.versions.filter(is_approved=False).order_by("-version_number").first()
    )

    if latest_unapproved_version:
        latest_unapproved_version.is_approved = True
        latest_unapproved_version.approved_at = approval_time
        latest_unapproved_version.approved_by = acting_user
        latest_unapproved_version.save()
        template.versions.filter(is_approved=False).exclude(
            id=latest_unapproved_version.id
        ).delete()

    template.status = "approved"
    template.approved_by = acting_user
    template.approved_at = approval_time
    template.save()

    IFRS9TemplateWorkflowHistory.objects.create(
        template=template,
        action="approved",
        from_status=old_status,
        to_status="approved",
        performed_by=acting_user,
        comments=comments,
    )


def _create_template_version(
    template: BaselScoreSheetTemplate,
    version_number: int,
    user=None,
    change_description: str = ""
) -> TemplateVersion:
    """
    Create a snapshot of the template structure at a specific point in time.
    Stores all sections, risk drivers, attributes, and options.
    """
    with transaction.atomic():
        if user is None:
            user = getattr(template, "submitted_by", None) or getattr(template, "maker", None)

        # Create version record (including template metadata and formula snapshots)
        version = TemplateVersion.objects.create(
            template=template,
            version_number=version_number,
            created_by=user,
            change_description=change_description,
            template_code=template.code or "",
            template_name=template.name or "",
            template_description=template.description or "",
            template_version=template.version or "",
            formula_actual_score=template.formula_actual_score or "",
            formula_weighted_score=template.formula_weighted_score or "",
            formula_proof=template.formula_proof or "",
        )
        
        # Copy all sections
        for section in template.sections.all().order_by('display_order'):
            section_version = TemplateVersionSection.objects.create(
                version=version,
                section=section,
                code=section.code,
                name=section.name,
                display_order=section.display_order,
            )
            
            # Copy all risk drivers for this section
            for driver in section.risk_drivers.all().order_by('display_order'):
                driver_version = TemplateVersionRiskDriver.objects.create(
                    version=version,
                    risk_driver=driver,
                    section_version=section_version,
                    code=driver.code,
                    name=driver.name,
                    weight_percent=driver.weight_percent,
                    max_score=driver.max_score,
                    display_order=driver.display_order,
                )
                
                # Copy all attributes for this driver
                for attribute in driver.attributes.all().order_by('display_order'):
                    attribute_version = TemplateVersionAttribute.objects.create(
                        version=version,
                        attribute=attribute,
                        driver_version=driver_version,
                        code=attribute.code,
                        label=attribute.label,
                        help_text=attribute.help_text,
                        data_type=attribute.data_type,
                        input_type=attribute.input_type,
                        is_required=attribute.is_required,
                        group_label=attribute.group_label,
                        weight_percent=attribute.weight_percent,
                        display_order=attribute.display_order,
                        requires_document=attribute.requires_document,
                    )
                    
                    # Copy all options for this attribute
                    for option in attribute.options.all().order_by('display_order'):
                        TemplateVersionOption.objects.create(
                            version=version,
                            option=option,
                            attribute_version=attribute_version,
                            label=option.label,
                            value=option.value,
                            allocated_score=option.allocated_score,
                            display_order=option.display_order,
                            is_default=option.is_default,
                        )
        
        return version


# ============================================================================
# MAKER VIEWS
# ============================================================================

@login_required
def template_maker_draft_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all draft/in-progress/returned templates for the current maker.
    Shows templates that the maker can still edit.
    """
    templates = BaselScoreSheetTemplate.objects.filter(
        Q(maker=request.user) | Q(submitted_by=request.user),
        status__in=['draft', 'in_progress', 'returned']
    ).select_related('checker', 'submitted_by').only(
        'id',
        'code',
        'name',
        'description',
        'status',
        'updated_at',
        'version',
        'return_reason',
        'checker_id',
        'checker__email',
        'submitted_by_id',
        'submitted_by__email',
    ).order_by('-updated_at')
    
    # Search functionality
    search_query = request.GET.get('search', '').strip()
    if search_query:
        templates = templates.filter(
            Q(code__icontains=search_query) |
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query)
        )
    
    page_obj, page_size, list_query_string = _paginate_list_queryset(request, templates)
    
    context = {
        'templates': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'page_size': page_size,
        'list_query_string': list_query_string,
        'page_title': 'My Template Drafts',
    }
    
    return render(
        request,
        'maker_checker/baselscore_templates/template_maker_draft_list.html',
        context
    )


@login_required
def template_maker_submitted_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all submitted templates for the current maker.
    Shows templates that are pending review or have been reviewed.
    """
    templates = BaselScoreSheetTemplate.objects.filter(
        Q(maker=request.user) | Q(submitted_by=request.user),
        status__in=['submitted', 'approved', 'returned']
    ).select_related('checker', 'submitted_by', 'approved_by', 'returned_by').only(
        'id',
        'code',
        'name',
        'description',
        'status',
        'submitted_at',
        'updated_at',
        'version',
        'checker_id',
        'checker__email',
        'submitted_by_id',
        'submitted_by__email',
        'approved_by_id',
        'returned_by_id',
    ).order_by('-submitted_at', '-updated_at')
    
    # Search functionality
    search_query = request.GET.get('search', '').strip()
    if search_query:
        templates = templates.filter(
            Q(code__icontains=search_query) |
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query)
        )
    
    page_obj, page_size, list_query_string = _paginate_list_queryset(request, templates)
    
    context = {
        'templates': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'page_size': page_size,
        'list_query_string': list_query_string,
        'page_title': 'My Template Submissions',
    }
    
    return render(
        request,
        'maker_checker/baselscore_templates/template_maker_submitted_list.html',
        context
    )


@login_required
def template_maker_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    View a template from the maker drafts list.
    
    IMPORTANT: This view shows CURRENT/PENDING template information.
    Used from maker drafts/submitted lists - shows what's being submitted for approval.
    Shows pending changes that are waiting to be approved.
    
    For viewing approved template (template list), use basel_template_detail_view instead.
    
    This view is read-only for submitted/approved, editable for draft/returned.
    """
    template = get_object_or_404(
        BaselScoreSheetTemplate.objects.select_related('maker', 'checker', 'submitted_by', 'approved_by'),
        id=template_id
    )
    
    # Permission check
    if not template.can_be_edited_by(request.user) and template.status not in ['submitted', 'approved']:
        messages.error(request, "You don't have permission to view this template.")
        return redirect('scorecard:template_maker_draft_list')
    
    # CRITICAL: Use CURRENT template structure (not approved version) for maker view
    # This shows pending changes that are being submitted for approval
    # Unlike basel_template_detail_view which shows only approved information
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=False)
    
    section_totals, overall_totals = _build_template_structure_totals(sections)

    section_totals, overall_totals = _build_template_structure_totals(sections)

    # Get workflow history
    workflow_history = template.workflow_history.all().select_related('performed_by').order_by('-performed_at')[:10]
    
    # Get versions
    versions = template.versions.all().select_related('created_by', 'approved_by').order_by('-version_number')
    latest_approved_version = template.versions.filter(is_approved=True).order_by('-approved_at', '-version_number').first()
    if not latest_approved_version and template.status in ['in_progress', 'returned']:
        # Older approved templates may have been edited before an approved baseline
        # snapshot existed. Use the latest pre-edit snapshot so the submit view can
        # still show Approved vs Your Changes instead of a plain structure table.
        latest_approved_version = template.versions.order_by('-version_number').first()
    
    # Build comparison data to show what changed compared to approved version
    # This helps the maker see what they're submitting for approval
    approved_sections = None
    approved_drivers_by_section = {}
    approved_attributes_by_driver = {}
    approved_options_by_attribute = {}
    
    # Build approved structure from version snapshot for comparison
    if latest_approved_version:
        approved_sections_list = []
        for section_version in latest_approved_version.sections.all().order_by('display_order'):
            approved_sections_list.append(section_version.section)
            approved_drivers_by_section[section_version.section.id] = []
            
            for driver_version in section_version.risk_drivers.all().order_by('display_order'):
                approved_drivers_by_section[section_version.section.id].append(driver_version.risk_driver)
                approved_attributes_by_driver[driver_version.risk_driver.id] = []
                
                for attr_version in driver_version.attributes.all().order_by('display_order'):
                    approved_attributes_by_driver[driver_version.risk_driver.id].append(attr_version.attribute)
                    approved_options_by_attribute[attr_version.attribute.id] = []
                    
                    for option_version in attr_version.options.all().order_by('display_order'):
                        approved_options_by_attribute[attr_version.attribute.id].append(option_version.option)
        
        approved_sections = approved_sections_list
    
    # Build options_by_attribute for current template
    options_by_attribute = {}
    for section in sections:
        for driver in drivers_by_section.get(section.id, []):
            for attribute in attributes_by_driver.get(driver.id, []):
                options_by_attribute[attribute.id] = list(attribute.options.all().order_by('display_order'))

    section_totals, overall_totals = _build_template_structure_totals(sections)
    
    # Build comparison dictionaries for easier template access
    approved_sections_dict = {}
    approved_drivers_dict = {}
    approved_attributes_dict = {}
    approved_options_dict = {}
    
    if latest_approved_version:
        for section_version in latest_approved_version.sections.all():
            approved_sections_dict[section_version.section.id] = {
                'name': section_version.name,
                'code': section_version.code,
            }
            
            for driver_version in section_version.risk_drivers.all():
                approved_drivers_dict[driver_version.risk_driver.id] = {
                    'name': driver_version.name,
                    'code': driver_version.code,
                    'weight_percent': driver_version.weight_percent,
                    'max_score': driver_version.max_score,
                }
                
                for attr_version in driver_version.attributes.all():
                    approved_attributes_dict[attr_version.attribute.id] = {
                        'label': attr_version.label,
                        'code': attr_version.code,
                        'input_type': attr_version.input_type or 'radio',
                        'weight_percent': attr_version.weight_percent,
                    }
                    
                    approved_options_dict[attr_version.attribute.id] = {}
                    for option_version in attr_version.options.all():
                        approved_options_dict[attr_version.attribute.id][option_version.option.id] = {
                            'option': option_version.option,
                            'label': option_version.label,
                            'allocated_score': option_version.allocated_score,
                        }
    
    # Compare formulas
    formula_comparison = {
        'actual_score': {'status': 'same', 'approved': '', 'pending': ''},
        'weighted_score': {'status': 'same', 'approved': '', 'pending': ''},
        'proof': {'status': 'same', 'approved': '', 'pending': ''},
    }
    
    if latest_approved_version:
        approved_actual_score = getattr(latest_approved_version, 'formula_actual_score', None) or template.formula_actual_score or "ALLOCATED_SCORE"
        approved_weighted_score = getattr(latest_approved_version, 'formula_weighted_score', None) or template.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT"
        approved_proof = getattr(latest_approved_version, 'formula_proof', None) or template.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE"
        
        pending_actual_score = template.formula_actual_score or "ALLOCATED_SCORE"
        pending_weighted_score = template.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT"
        pending_proof = template.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE"
        
        formula_comparison['actual_score'] = {
            'status': 'same' if approved_actual_score == pending_actual_score else 'changed',
            'approved': approved_actual_score,
            'pending': pending_actual_score,
        }
        formula_comparison['weighted_score'] = {
            'status': 'same' if approved_weighted_score == pending_weighted_score else 'changed',
            'approved': approved_weighted_score,
            'pending': pending_weighted_score,
        }
        formula_comparison['proof'] = {
            'status': 'same' if approved_proof == pending_proof else 'changed',
            'approved': approved_proof,
            'pending': pending_proof,
        }
    else:
        formula_comparison['actual_score'] = {
            'status': 'new',
            'approved': '',
            'pending': template.formula_actual_score or "ALLOCATED_SCORE",
        }
        formula_comparison['weighted_score'] = {
            'status': 'new',
            'approved': '',
            'pending': template.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT",
        }
        formula_comparison['proof'] = {
            'status': 'new',
            'approved': '',
            'pending': template.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE",
        }
    
    pending_template_statuses = {'draft', 'in_progress', 'returned', 'submitted'}
    is_pending_template_review = template.status in pending_template_statuses
    show_pending_comparison = bool(latest_approved_version and is_pending_template_review)

    back_to_submissions = request.GET.get('from') == 'submitted' or template.status in ['submitted', 'approved']

    context = {
        'template': template,
        'back_to_submissions': back_to_submissions,
        'sections': sections,
        'drivers_by_section': drivers_by_section,
        'attributes_by_driver': attributes_by_driver,
        'options_by_attribute': options_by_attribute,
        'approved_sections': approved_sections,
        'approved_drivers_by_section': approved_drivers_by_section,
        'approved_attributes_by_driver': approved_attributes_by_driver,
        'approved_options_by_attribute': approved_options_by_attribute,
        'approved_sections_dict': approved_sections_dict,
        'approved_drivers_dict': approved_drivers_dict,
        'approved_attributes_dict': approved_attributes_dict,
        'approved_options_dict': approved_options_dict,
        'workflow_history': workflow_history,
        'versions': versions,
        'latest_approved_version': latest_approved_version,
        'show_pending_comparison': show_pending_comparison,
        'is_pending_template_review': is_pending_template_review,
        'formula_comparison': formula_comparison,
        'can_edit': template.can_be_edited_by(request.user),
        'section_totals': section_totals,
        'overall_totals': overall_totals,
    }
    
    return render(
        request,
        'maker_checker/baselscore_templates/template_maker_view.html',
        context
    )


@login_required
def template_submit_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Submit a template for review.
    """
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    
    # Permission check
    if not template.can_be_edited_by(request.user):
        messages.error(request, "You don't have permission to submit this template.")
        return redirect('scorecard:template_maker_draft_list')
    
    if template.status not in ['draft', 'in_progress', 'returned']:
        messages.error(request, "Only draft, in-progress, or returned templates can be submitted.")
        return redirect('scorecard:template_maker_view', template_id=template.id)

    total_weight = _get_basel_template_total_weight(template)
    if total_weight != Decimal("100"):
        messages.error(
            request,
            f"Template '{template.code}' cannot be submitted for review because its total section/demographic weight is {total_weight:.2f}%. It must be exactly 100.00%.",
        )
        return redirect('scorecard:template_maker_view', template_id=template.id)
    
    if request.method == 'POST':
        with transaction.atomic():
            old_status = template.status
            auto_approve = _can_auto_approve_basel_template_submission(request.user)
            
            # Get the highest version number before creating new version
            last_version = template.versions.order_by('-version_number').first()
            if last_version:
                version_number = last_version.version_number + 1
            else:
                version_number = 1
            
            # Delete all unapproved versions before creating a new one
            template.versions.filter(is_approved=False).delete()
            
            # Create template version snapshot
            _create_template_version(
                template=template,
                version_number=version_number,
                user=request.user,
                change_description=request.POST.get('comments', '') or f"Template submitted for review - Version {version_number}"
            )
            
            template.status = 'approved' if auto_approve else 'submitted'
            template.submitted_by = request.user
            template.submitted_at = timezone.now()
            template.save()

            if auto_approve:
                _finalize_basel_template_auto_approval(
                    template,
                    request.user,
                    old_status=old_status,
                    comments="Template auto-approved on submission",
                )
                log_basel_template_audit(
                    request.user,
                    "approve",
                    template,
                    f"Template auto-approved on submission from status {old_status}.",
                )
                notify_basel_template_approved(template, request.user)
                messages.success(
                    request,
                    f"Template '{template.code}' was approved automatically on submit.",
                )
            else:
                TemplateWorkflowHistory.objects.create(
                    template=template,
                    action='submitted',
                    from_status=old_status,
                    to_status='submitted',
                    performed_by=request.user,
                    comments=request.POST.get('comments', '')
                )
                log_basel_template_audit(request.user, "submit_for_review", template, f"Template submitted for review from status {old_status}.")
                notify_basel_template_submitted(template)
                messages.success(request, f"Template '{template.code}' has been submitted for review.")
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            return redirect('scorecard:template_maker_submitted_list')
    
    context = {
        'template': template,
    }
    
    return render(
        request,
        'maker_checker/baselscore_templates/template_submit.html',
        context
    )


@login_required
def template_withdraw_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Withdraw a submitted template back to draft.
    """
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    
    # Permission check
    if (
        template.maker != request.user
        and template.submitted_by != request.user
        and not request.user.is_superuser
    ):
        messages.error(request, "You don't have permission to withdraw this template.")
        return redirect('scorecard:template_maker_submitted_list')
    
    if template.status != 'submitted':
        messages.error(request, "Only submitted templates can be withdrawn.")
        return redirect('scorecard:template_maker_view', template_id=template.id)
    
    if request.method == 'POST':
        with transaction.atomic():
            old_status = template.status
            template.status = 'in_progress'
            template.submitted_at = None
            template.save()
            
            # Create workflow history
            TemplateWorkflowHistory.objects.create(
                template=template,
                action='withdrawn',
                from_status=old_status,
                to_status='in_progress',
                performed_by=request.user,
                comments=request.POST.get('comments', '')
            )
            log_basel_template_audit(request.user, "withdraw_submission", template, f"Template withdrawn back to in_progress from {old_status}.")
            
            messages.success(request, f"Template '{template.code}' has been withdrawn.")
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            return redirect('scorecard:template_maker_draft_list')
    
    context = {
        'template': template,
    }
    
    return render(
        request,
        'maker_checker/baselscore_templates/template_withdraw.html',
        context
    )


# ============================================================================
# CHECKER VIEWS
# ============================================================================

@login_required
def template_checker_pending_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all templates pending review for the current checker.
    """
    templates = BaselScoreSheetTemplate.objects.filter(
        status='submitted'
    ).select_related('maker', 'checker', 'submitted_by').only(
        'id',
        'code',
        'name',
        'description',
        'status',
        'submitted_at',
        'updated_at',
        'version',
        'maker_id',
        'maker__email',
        'checker_id',
        'checker__email',
        'submitted_by_id',
        'submitted_by__email',
    ).order_by('-submitted_at')
    
    # Filter by checker if not superuser
    if not request.user.is_superuser:
        templates = templates.filter(
            Q(checker=request.user) | Q(checker__isnull=True)
        )
    if not _can_user_self_review_basel_templates(request.user):
        templates = templates.exclude(
            Q(submitted_by=request.user) | Q(submitted_by__isnull=True, maker=request.user)
        )
    
    # Search functionality
    search_query = request.GET.get('search', '').strip()
    if search_query:
        templates = templates.filter(
            Q(code__icontains=search_query) |
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(maker__email__icontains=search_query) |
            Q(submitted_by__email__icontains=search_query)
        )
    
    page_obj, page_size, list_query_string = _paginate_list_queryset(request, templates)
    
    context = {
        'templates': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'page_size': page_size,
        'list_query_string': list_query_string,
        'page_title': 'Templates Pending Review',
    }
    
    return render(
        request,
        'maker_checker/baselscore_templates/template_checker_pending_list.html',
        context
    )


@login_required
def template_checker_review_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Review a submitted template (approve or return).
    """
    template = get_object_or_404(
        BaselScoreSheetTemplate.objects.select_related('maker', 'checker', 'submitted_by'),
        id=template_id
    )
    
    # Permission check
    if not template.can_be_reviewed_by(request.user) and template.status != 'approved':
        messages.error(request, "You don't have permission to review this template.")
        return redirect('scorecard:template_checker_pending_list')
    if template.status != 'approved' and _must_use_different_template_reviewer(template, request.user, basel=True):
        return _reject_self_template_review(request, 'scorecard:template_checker_pending_list')
    
    # Build configuration for current template (pending changes)
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template)
    
    # Get workflow history
    workflow_history = template.workflow_history.all().select_related('performed_by').order_by('-performed_at')[:10]
    
    # Get versions
    versions = template.versions.all().select_related('created_by', 'approved_by').order_by('-version_number')
    latest_unapproved_version = template.versions.filter(is_approved=False).order_by('-version_number').first()
    latest_approved_version = template.versions.filter(is_approved=True).order_by('-approved_at', '-version_number').first()
    
    # Build configuration for approved version (if exists) for comparison
    # We'll use the same structure but build from version snapshots
    approved_sections = None
    approved_drivers_by_section = {}
    approved_attributes_by_driver = {}
    approved_options_by_attribute = {}
    
    if latest_approved_version:
        # Build structure from approved version snapshots
        approved_sections_list = []
        
        for section_version in latest_approved_version.sections.all().order_by('display_order'):
            approved_sections_list.append(section_version.section)
            approved_drivers_by_section[section_version.section.id] = []
            
            for driver_version in section_version.risk_drivers.all().order_by('display_order'):
                approved_drivers_by_section[section_version.section.id].append(driver_version.risk_driver)
                approved_attributes_by_driver[driver_version.risk_driver.id] = []
                
                for attr_version in driver_version.attributes.all().order_by('display_order'):
                    approved_attributes_by_driver[driver_version.risk_driver.id].append(attr_version.attribute)
                    approved_options_by_attribute[attr_version.attribute.id] = []
                    
                    for option_version in attr_version.options.all().order_by('display_order'):
                        approved_options_by_attribute[attr_version.attribute.id].append(option_version.option)
        
        approved_sections = approved_sections_list
    
    # Build options_by_attribute for current template
    options_by_attribute = {}
    for section in sections:
        for driver in drivers_by_section.get(section.id, []):
            for attribute in attributes_by_driver.get(driver.id, []):
                options_by_attribute[attribute.id] = list(attribute.options.all().order_by('display_order'))

    section_totals, overall_totals = _build_template_structure_totals(sections)
    
    # Build approved options_by_attribute from version snapshots
    approved_options_by_attribute = {}
    if latest_approved_version:
        for section_version in latest_approved_version.sections.all():
            for driver_version in section_version.risk_drivers.all():
                for attr_version in driver_version.attributes.all():
                    approved_options_by_attribute[attr_version.attribute.id] = []
                    for option_version in attr_version.options.all():
                        approved_options_by_attribute[attr_version.attribute.id].append(option_version.option)
    
    # Build comprehensive comparison dictionaries for easier template access
    # Sections: section_id -> {name, code}
    approved_sections_dict = {}
    # Drivers: driver_id -> {name, code, weight_percent}
    approved_drivers_dict = {}
    # Attributes: attribute_id -> {label, code, weight_percent}
    approved_attributes_dict = {}
    # Options: attribute_id -> {option_id: {label, allocated_score}}
    approved_options_dict = {}
    
    if latest_approved_version:
        for section_version in latest_approved_version.sections.all():
            approved_sections_dict[section_version.section.id] = {
                'name': section_version.name,
                'code': section_version.code,
            }
            
            for driver_version in section_version.risk_drivers.all():
                approved_drivers_dict[driver_version.risk_driver.id] = {
                    'name': driver_version.name,
                    'code': driver_version.code,
                    'weight_percent': driver_version.weight_percent,
                }
                
                for attr_version in driver_version.attributes.all():
                    approved_attributes_dict[attr_version.attribute.id] = {
                        'label': attr_version.label,
                        'code': attr_version.code,
                        'input_type': attr_version.input_type or 'radio',
                        'weight_percent': attr_version.weight_percent,
                    }
                    
                    approved_options_dict[attr_version.attribute.id] = {}
                    for option_version in attr_version.options.all():
                        approved_options_dict[attr_version.attribute.id][option_version.option.id] = {
                            'option': option_version.option,
                            'label': option_version.label,
                            'allocated_score': option_version.allocated_score,
                        }
    
    # Compare calculation formulas between approved and pending versions
    formula_comparison = {
        'actual_score': {'status': 'same', 'approved': '', 'pending': ''},
        'weighted_score': {'status': 'same', 'approved': '', 'pending': ''},
        'proof': {'status': 'same', 'approved': '', 'pending': ''},
    }
    
    if latest_approved_version:
        # Get approved formulas from version snapshot
        # Use getattr to safely access fields that might not exist in DB yet (before migration)
        approved_actual_score = getattr(latest_approved_version, 'formula_actual_score', None) or template.formula_actual_score or "ALLOCATED_SCORE"
        approved_weighted_score = getattr(latest_approved_version, 'formula_weighted_score', None) or template.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT"
        approved_proof = getattr(latest_approved_version, 'formula_proof', None) or template.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE"
        
        # Get pending formulas from current template
        pending_actual_score = template.formula_actual_score or "ALLOCATED_SCORE"
        pending_weighted_score = template.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT"
        pending_proof = template.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE"
        
        # Compare formulas
        formula_comparison['actual_score'] = {
            'status': 'same' if approved_actual_score == pending_actual_score else 'changed',
            'approved': approved_actual_score,
            'pending': pending_actual_score,
        }
        formula_comparison['weighted_score'] = {
            'status': 'same' if approved_weighted_score == pending_weighted_score else 'changed',
            'approved': approved_weighted_score,
            'pending': pending_weighted_score,
        }
        formula_comparison['proof'] = {
            'status': 'same' if approved_proof == pending_proof else 'changed',
            'approved': approved_proof,
            'pending': pending_proof,
        }
    else:
        # No approved version - all formulas are new
        formula_comparison['actual_score'] = {
            'status': 'new',
            'approved': '',
            'pending': template.formula_actual_score or "ALLOCATED_SCORE",
        }
        formula_comparison['weighted_score'] = {
            'status': 'new',
            'approved': '',
            'pending': template.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT",
        }
        formula_comparison['proof'] = {
            'status': 'new',
            'approved': '',
            'pending': template.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE",
        }
    
    pending_template_statuses = {'draft', 'in_progress', 'returned', 'submitted'}
    is_pending_template_review = template.status in pending_template_statuses
    show_pending_comparison = bool(latest_approved_version and is_pending_template_review)

    context = {
        'template': template,
        'sections': sections,
        'drivers_by_section': drivers_by_section,
        'attributes_by_driver': attributes_by_driver,
        'options_by_attribute': options_by_attribute,
        'approved_sections': approved_sections,
        'approved_drivers_by_section': approved_drivers_by_section,
        'approved_attributes_by_driver': approved_attributes_by_driver,
        'approved_options_by_attribute': approved_options_by_attribute,
        'approved_sections_dict': approved_sections_dict,
        'approved_drivers_dict': approved_drivers_dict,
        'approved_attributes_dict': approved_attributes_dict,
        'approved_options_dict': approved_options_dict,
        'workflow_history': workflow_history,
        'versions': versions,
        'latest_unapproved_version': latest_unapproved_version,
        'latest_approved_version': latest_approved_version,
        'formula_comparison': formula_comparison,
        'section_totals': section_totals,
        'overall_totals': overall_totals,
    }
    
    return render(
        request,
        'maker_checker/baselscore_templates/template_checker_review.html',
        context
    )


@login_required
def template_approve_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Approve a submitted template.
    """
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    
    # Permission check
    if not template.can_be_reviewed_by(request.user):
        messages.error(request, "You don't have permission to approve this template.")
        return redirect('scorecard:template_checker_pending_list')
    if _must_use_different_template_reviewer(template, request.user, basel=True):
        return _reject_self_template_review(request, 'scorecard:template_checker_pending_list')
    
    if template.status != 'submitted':
        messages.error(request, "Only submitted templates can be approved.")
        return redirect('scorecard:template_checker_review', template_id=template.id)
    
    if request.method == 'POST':
        with transaction.atomic():
            old_status = template.status
            
            # Get latest unapproved version and mark it as approved
            latest_unapproved_version = template.versions.filter(is_approved=False).order_by('-version_number').first()
            
            if latest_unapproved_version:
                latest_unapproved_version.is_approved = True
                latest_unapproved_version.approved_at = timezone.now()
                latest_unapproved_version.approved_by = request.user
                latest_unapproved_version.save()
                
                # Delete other unapproved versions (keep only approved ones)
                template.versions.filter(is_approved=False).exclude(id=latest_unapproved_version.id).delete()
            
            template.status = 'approved'
            template.approved_by = request.user
            template.approved_at = timezone.now()
            template.save()
            
            # Create workflow history
            TemplateWorkflowHistory.objects.create(
                template=template,
                action='approved',
                from_status=old_status,
                to_status='approved',
                performed_by=request.user,
                comments=request.POST.get('comments', '')
            )
            log_basel_template_audit(request.user, "approve", template, "Template approved in maker checker workflow.")
            notify_basel_template_approved(template, request.user)
            
            messages.success(request, f"Template '{template.code}' has been approved.")
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            return redirect('scorecard:template_checker_pending_list')
    
    context = {
        'template': template,
        'latest_unapproved_version': template.versions.filter(is_approved=False).order_by('-version_number').first(),
    }
    
    return render(
        request,
        'maker_checker/baselscore_templates/template_approve.html',
        context
    )


@login_required
def template_return_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Return a submitted template for changes.
    """
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    
    # Permission check
    if not template.can_be_reviewed_by(request.user):
        messages.error(request, "You don't have permission to return this template.")
        return redirect('scorecard:template_checker_pending_list')
    if _must_use_different_template_reviewer(template, request.user, basel=True):
        return _reject_self_template_review(request, 'scorecard:template_checker_pending_list')
    
    if template.status != 'submitted':
        messages.error(request, "Only submitted templates can be returned.")
        return redirect('scorecard:template_checker_review', template_id=template.id)
    
    if request.method == 'POST':
        return_reason = request.POST.get('return_reason', '').strip()
        if not return_reason:
            messages.error(request, "Please provide a reason for returning the template.")
            return redirect('scorecard:template_return', template_id=template.id)
        
        with transaction.atomic():
            old_status = template.status
            
            # Delete unapproved versions (they will be recreated when maker edits)
            template.versions.filter(is_approved=False).delete()
            
            template.status = 'returned'
            template.returned_by = request.user
            template.returned_at = timezone.now()
            template.return_reason = return_reason
            template.save()
            
            # Create workflow history
            TemplateWorkflowHistory.objects.create(
                template=template,
                action='returned',
                from_status=old_status,
                to_status='returned',
                performed_by=request.user,
                comments=return_reason
            )
            log_basel_template_audit(request.user, "return", template, f"Template returned for changes. Reason: {return_reason}")
            notify_basel_template_returned(template, request.user, return_reason)
            
            messages.success(request, f"Template '{template.code}' has been returned for changes.")
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            return redirect('scorecard:template_checker_pending_list')
    
    context = {
        'template': template,
    }
    
    return render(
        request,
        'maker_checker/baselscore_templates/template_return.html',
        context
    )


# ============================================================================
# IFRS9 HELPER FUNCTIONS
# ============================================================================

def _create_ifrs9_template_version(
    template: IFRS9ScoreSheetTemplate,
    version_number: int,
    user=None,
    change_description: str = ""
) -> IFRS9TemplateVersion:
    """
    Create a snapshot of the IFRS9 template structure at a specific point in time.
    Stores all sections, risk drivers, attributes, and options.
    """
    with transaction.atomic():
        if user is None:
            user = getattr(template, "submitted_by", None) or getattr(template, "maker", None)

        # Create version record (including template metadata and formula snapshots)
        version = IFRS9TemplateVersion.objects.create(
            template=template,
            version_number=version_number,
            created_by=user,
            change_description=change_description,
            template_code=template.code or "",
            template_name=template.name or "",
            template_description=template.description or "",
            template_version=template.version or "",
            formula_actual_score=template.formula_actual_score or "",
            formula_weighted_score=template.formula_weighted_score or "",
            formula_proof=template.formula_proof or "",
        )
        
        # Copy all sections
        for section in template.sections.all().order_by('display_order'):
            section_version = IFRS9TemplateVersionSection.objects.create(
                version=version,
                section=section,
                code=section.code,
                name=section.name,
                display_order=section.display_order,
            )
            
            # Copy all risk drivers for this section
            for driver in section.risk_drivers.all().order_by('display_order'):
                driver_version = IFRS9TemplateVersionRiskDriver.objects.create(
                    version=version,
                    risk_driver=driver,
                    section_version=section_version,
                    code=driver.code,
                    name=driver.name,
                    weight_percent=driver.weight_percent,
                    max_score=driver.max_score,
                    display_order=driver.display_order,
                )
                
                # Copy all attributes for this driver
                for attribute in driver.attributes.all().order_by('display_order'):
                    attribute_version = IFRS9TemplateVersionAttribute.objects.create(
                        version=version,
                        attribute=attribute,
                        driver_version=driver_version,
                        code=attribute.code,
                        label=attribute.label,
                        help_text=attribute.help_text,
                        data_type=attribute.data_type,
                        input_type=attribute.input_type,
                        is_required=attribute.is_required,
                        group_label=attribute.group_label,
                        weight_percent=attribute.weight_percent,
                        display_order=attribute.display_order,
                        requires_document=attribute.requires_document,
                    )
                    
                    # Copy all options for this attribute
                    for option in attribute.options.all().order_by('display_order'):
                        IFRS9TemplateVersionOption.objects.create(
                            version=version,
                            option=option,
                            attribute_version=attribute_version,
                            label=option.label,
                            value=option.value,
                            allocated_score=option.allocated_score,
                            display_order=option.display_order,
                            is_default=option.is_default,
                        )
        
        return version


# ============================================================================
# IFRS9 MAKER VIEWS
# ============================================================================

@login_required
def ifrs9_template_maker_draft_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all draft/in-progress/returned IFRS9 templates for the current maker.
    Shows templates that the maker can still edit.
    """
    templates = IFRS9ScoreSheetTemplate.objects.filter(
        Q(maker=request.user) | Q(submitted_by=request.user),
        status__in=['draft', 'in_progress', 'returned']
    ).select_related('checker', 'submitted_by').only(
        'id',
        'code',
        'name',
        'description',
        'status',
        'updated_at',
        'version',
        'return_reason',
        'checker_id',
        'checker__email',
        'submitted_by_id',
        'submitted_by__email',
    ).order_by('-updated_at')
    
    # Search functionality
    search_query = request.GET.get('search', '').strip()
    if search_query:
        templates = templates.filter(
            Q(code__icontains=search_query) |
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query)
        )
    
    page_obj, page_size, list_query_string = _paginate_list_queryset(request, templates)
    
    context = {
        'templates': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'page_size': page_size,
        'list_query_string': list_query_string,
        'page_title': 'My IFRS9 Template Drafts',
    }
    
    return render(
        request,
        'maker_checker/ifrs9scores_templates/template_maker_draft_list.html',
        context
    )


@login_required
def ifrs9_template_maker_submitted_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all submitted IFRS9 templates for the current maker.
    Shows templates that are pending review or have been reviewed.
    """
    templates = IFRS9ScoreSheetTemplate.objects.filter(
        Q(maker=request.user) | Q(submitted_by=request.user),
        status__in=['submitted', 'approved', 'returned']
    ).select_related('checker', 'submitted_by', 'approved_by', 'returned_by').only(
        'id',
        'code',
        'name',
        'description',
        'status',
        'submitted_at',
        'updated_at',
        'version',
        'checker_id',
        'checker__email',
        'submitted_by_id',
        'submitted_by__email',
        'approved_by_id',
        'returned_by_id',
    ).order_by('-submitted_at', '-updated_at')
    
    # Search functionality
    search_query = request.GET.get('search', '').strip()
    if search_query:
        templates = templates.filter(
            Q(code__icontains=search_query) |
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query)
        )
    
    page_obj, page_size, list_query_string = _paginate_list_queryset(request, templates)
    
    context = {
        'templates': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'page_size': page_size,
        'list_query_string': list_query_string,
        'page_title': 'My IFRS9 Template Submissions',
    }
    
    return render(
        request,
        'maker_checker/ifrs9scores_templates/template_maker_submitted_list.html',
        context
    )


@login_required
def ifrs9_template_maker_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    View an IFRS9 template from the maker drafts list.
    
    IMPORTANT: This view shows CURRENT/PENDING template information.
    Used from maker drafts/submitted lists - shows what's being submitted for approval.
    Shows pending changes that are waiting to be approved.
    
    For viewing approved template (template list), use ifrs9_template_detail_view instead.
    
    This view is read-only for submitted/approved, editable for draft/returned.
    """
    template = get_object_or_404(
        IFRS9ScoreSheetTemplate.objects.select_related('maker', 'checker', 'submitted_by', 'approved_by'),
        id=template_id
    )
    
    # Permission check
    if not template.can_be_edited_by(request.user) and template.status not in ['submitted', 'approved']:
        messages.error(request, "You don't have permission to view this template.")
        return redirect('scorecard:ifrs9_template_maker_draft_list')
    
    # CRITICAL: Use CURRENT template structure (not approved version) for maker view
    # This shows pending changes that are being submitted for approval
    # Unlike ifrs9_template_detail_view which shows only approved information
    sections, drivers_by_section, attributes_by_driver = _build_ifrs9_configuration(template, use_approved_version=False)
    section_totals, overall_totals = _build_template_structure_totals(sections)
    
    # Get workflow history
    workflow_history = template.workflow_history.all().select_related('performed_by').order_by('-performed_at')[:10]
    
    # Get versions
    versions = template.versions.all().select_related('created_by', 'approved_by').order_by('-version_number')
    latest_approved_version = template.versions.filter(is_approved=True).order_by('-approved_at', '-version_number').first()
    if not latest_approved_version and template.status in ['in_progress', 'returned']:
        # Older approved templates may have been edited before an approved baseline
        # snapshot existed. Use the latest pre-edit snapshot so the submit view can
        # still show Approved vs Your Changes instead of a plain structure table.
        latest_approved_version = template.versions.order_by('-version_number').first()
    
    # Build comparison data to show what changed compared to approved version
    # This helps the maker see what they're submitting for approval
    approved_sections = None
    approved_drivers_by_section = {}
    approved_attributes_by_driver = {}
    approved_options_by_attribute = {}
    
    # Build approved structure from version snapshot for comparison
    if latest_approved_version:
        approved_sections_list = []
        for section_version in latest_approved_version.sections.all().order_by('display_order'):
            approved_sections_list.append(section_version.section)
            approved_drivers_by_section[section_version.section.id] = []
            
            for driver_version in section_version.risk_drivers.all().order_by('display_order'):
                approved_drivers_by_section[section_version.section.id].append(driver_version.risk_driver)
                approved_attributes_by_driver[driver_version.risk_driver.id] = []
                
                for attr_version in driver_version.attributes.all().order_by('display_order'):
                    approved_attributes_by_driver[driver_version.risk_driver.id].append(attr_version.attribute)
                    approved_options_by_attribute[attr_version.attribute.id] = []
                    
                    for option_version in attr_version.options.all().order_by('display_order'):
                        approved_options_by_attribute[attr_version.attribute.id].append(option_version.option)
        
        approved_sections = approved_sections_list
    
    # Build options_by_attribute for current template
    options_by_attribute = {}
    for section in sections:
        for driver in drivers_by_section.get(section.id, []):
            for attribute in attributes_by_driver.get(driver.id, []):
                options_by_attribute[attribute.id] = list(attribute.options.all().order_by('display_order'))
    
    # Build comparison dictionaries for easier template access
    approved_sections_dict = {}
    approved_drivers_dict = {}
    approved_attributes_dict = {}
    approved_options_dict = {}
    
    if latest_approved_version:
        for section_version in latest_approved_version.sections.all():
            approved_sections_dict[section_version.section.id] = {
                'name': section_version.name,
                'code': section_version.code,
            }
            
            for driver_version in section_version.risk_drivers.all():
                approved_drivers_dict[driver_version.risk_driver.id] = {
                    'name': driver_version.name,
                    'code': driver_version.code,
                    'weight_percent': driver_version.weight_percent,
                    'max_score': driver_version.max_score,
                }
                
                for attr_version in driver_version.attributes.all():
                    approved_attributes_dict[attr_version.attribute.id] = {
                        'label': attr_version.label,
                        'code': attr_version.code,
                        'input_type': attr_version.input_type or 'radio',
                        'weight_percent': attr_version.weight_percent,
                    }
                    
                    approved_options_dict[attr_version.attribute.id] = {}
                    for option_version in attr_version.options.all():
                        approved_options_dict[attr_version.attribute.id][option_version.option.id] = {
                            'option': option_version.option,
                            'label': option_version.label,
                            'allocated_score': option_version.allocated_score,
                        }
    
    # Compare formulas
    formula_comparison = {
        'actual_score': {'status': 'same', 'approved': '', 'pending': ''},
        'weighted_score': {'status': 'same', 'approved': '', 'pending': ''},
        'proof': {'status': 'same', 'approved': '', 'pending': ''},
    }
    
    if latest_approved_version:
        approved_actual_score = getattr(latest_approved_version, 'formula_actual_score', None) or template.formula_actual_score or "ALLOCATED_SCORE"
        approved_weighted_score = getattr(latest_approved_version, 'formula_weighted_score', None) or template.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT"
        approved_proof = getattr(latest_approved_version, 'formula_proof', None) or template.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE"
        
        pending_actual_score = template.formula_actual_score or "ALLOCATED_SCORE"
        pending_weighted_score = template.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT"
        pending_proof = template.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE"
        
        formula_comparison['actual_score'] = {
            'status': 'same' if approved_actual_score == pending_actual_score else 'changed',
            'approved': approved_actual_score,
            'pending': pending_actual_score,
        }
        formula_comparison['weighted_score'] = {
            'status': 'same' if approved_weighted_score == pending_weighted_score else 'changed',
            'approved': approved_weighted_score,
            'pending': pending_weighted_score,
        }
        formula_comparison['proof'] = {
            'status': 'same' if approved_proof == pending_proof else 'changed',
            'approved': approved_proof,
            'pending': pending_proof,
        }
    else:
        formula_comparison['actual_score'] = {
            'status': 'new',
            'approved': '',
            'pending': template.formula_actual_score or "ALLOCATED_SCORE",
        }
        formula_comparison['weighted_score'] = {
            'status': 'new',
            'approved': '',
            'pending': template.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT",
        }
        formula_comparison['proof'] = {
            'status': 'new',
            'approved': '',
            'pending': template.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE",
        }
    
    pending_template_statuses = {'draft', 'in_progress', 'returned', 'submitted'}
    is_pending_template_review = template.status in pending_template_statuses
    show_pending_comparison = bool(latest_approved_version and is_pending_template_review)

    back_to_submissions = request.GET.get('from') == 'submitted' or template.status in ['submitted', 'approved']

    context = {
        'template': template,
        'back_to_submissions': back_to_submissions,
        'sections': sections,
        'drivers_by_section': drivers_by_section,
        'attributes_by_driver': attributes_by_driver,
        'options_by_attribute': options_by_attribute,
        'approved_sections': approved_sections,
        'approved_drivers_by_section': approved_drivers_by_section,
        'approved_attributes_by_driver': approved_attributes_by_driver,
        'approved_options_by_attribute': approved_options_by_attribute,
        'approved_sections_dict': approved_sections_dict,
        'approved_drivers_dict': approved_drivers_dict,
        'approved_attributes_dict': approved_attributes_dict,
        'approved_options_dict': approved_options_dict,
        'workflow_history': workflow_history,
        'versions': versions,
        'latest_approved_version': latest_approved_version,
        'show_pending_comparison': show_pending_comparison,
        'is_pending_template_review': is_pending_template_review,
        'formula_comparison': formula_comparison,
        'can_edit': template.can_be_edited_by(request.user),
        'section_totals': section_totals,
        'overall_totals': overall_totals,
    }
    
    return render(
        request,
        'maker_checker/ifrs9scores_templates/template_maker_view.html',
        context
    )


@login_required
def ifrs9_template_submit_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Submit an IFRS9 template for review.
    """
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    
    # Permission check
    if not template.can_be_edited_by(request.user):
        messages.error(request, "You don't have permission to submit this template.")
        return redirect('scorecard:ifrs9_template_maker_draft_list')
    
    if template.status not in ['draft', 'in_progress', 'returned']:
        messages.error(request, "Only draft, in-progress, or returned templates can be submitted.")
        return redirect('scorecard:ifrs9_template_maker_view', template_id=template.id)

    total_weight = _get_ifrs9_template_total_weight(template)
    if total_weight != Decimal("100"):
        messages.error(
            request,
            f"Template '{template.code}' cannot be submitted for review because its total section/demographic weight is {total_weight:.2f}%. It must be exactly 100.00%.",
        )
        return redirect('scorecard:ifrs9_template_maker_view', template_id=template.id)
    
    if request.method == 'POST':
        with transaction.atomic():
            old_status = template.status
            auto_approve = _can_auto_approve_ifrs9_template_submission(request.user)
            
            # Get the highest version number before creating new version
            last_version = template.versions.order_by('-version_number').first()
            if last_version:
                version_number = last_version.version_number + 1
            else:
                version_number = 1
            
            # Delete all unapproved versions before creating a new one
            template.versions.filter(is_approved=False).delete()
            
            # Create template version snapshot
            _create_ifrs9_template_version(
                template=template,
                version_number=version_number,
                user=request.user,
                change_description=request.POST.get('comments', '') or f"Template submitted for review - Version {version_number}"
            )
            
            template.status = 'approved' if auto_approve else 'submitted'
            template.submitted_by = request.user
            template.submitted_at = timezone.now()
            template.save()

            if auto_approve:
                _finalize_ifrs9_template_auto_approval(
                    template,
                    request.user,
                    old_status=old_status,
                    comments="Template auto-approved on submission",
                )
                log_ifrs9_template_audit(
                    request.user,
                    "approve",
                    template,
                    f"Template auto-approved on submission from status {old_status}.",
                )
                notify_ifrs9_template_approved(template, request.user)
                messages.success(
                    request,
                    f"Template '{template.code}' was approved automatically on submit.",
                )
            else:
                IFRS9TemplateWorkflowHistory.objects.create(
                    template=template,
                    action='submitted',
                    from_status=old_status,
                    to_status='submitted',
                    performed_by=request.user,
                    comments=request.POST.get('comments', '')
                )
                log_ifrs9_template_audit(request.user, "submit_for_review", template, f"Template submitted for review from status {old_status}.")
                notify_ifrs9_template_submitted(template)
                messages.success(request, f"Template '{template.code}' has been submitted for review.")
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            return redirect('scorecard:ifrs9_template_maker_submitted_list')
    
    context = {
        'template': template,
    }
    
    return render(
        request,
        'maker_checker/ifrs9scores_templates/template_submit.html',
        context
    )


@login_required
def ifrs9_template_withdraw_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Withdraw a submitted IFRS9 template back to draft.
    """
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    
    # Permission check
    if (
        template.maker != request.user
        and template.submitted_by != request.user
        and not request.user.is_superuser
    ):
        messages.error(request, "You don't have permission to withdraw this template.")
        return redirect('scorecard:ifrs9_template_maker_submitted_list')
    
    if template.status != 'submitted':
        messages.error(request, "Only submitted templates can be withdrawn.")
        return redirect('scorecard:ifrs9_template_maker_view', template_id=template.id)
    
    if request.method == 'POST':
        with transaction.atomic():
            old_status = template.status
            template.status = 'in_progress'
            template.submitted_at = None
            template.save()
            
            # Create workflow history
            IFRS9TemplateWorkflowHistory.objects.create(
                template=template,
                action='withdrawn',
                from_status=old_status,
                to_status='in_progress',
                performed_by=request.user,
                comments=request.POST.get('comments', '')
            )
            log_ifrs9_template_audit(request.user, "withdraw_submission", template, f"Template withdrawn back to in_progress from {old_status}.")
            
            messages.success(request, f"Template '{template.code}' has been withdrawn.")
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            return redirect('scorecard:ifrs9_template_maker_draft_list')
    
    context = {
        'template': template,
    }
    
    return render(
        request,
        'maker_checker/ifrs9scores_templates/template_withdraw.html',
        context
    )


# ============================================================================
# IFRS9 CHECKER VIEWS
# ============================================================================

@login_required
def ifrs9_template_checker_pending_list_view(request: HttpRequest) -> HttpResponse:
    """
    List all IFRS9 templates pending review for the current checker.
    """
    templates = IFRS9ScoreSheetTemplate.objects.filter(
        status='submitted'
    ).select_related('maker', 'checker', 'submitted_by').only(
        'id',
        'code',
        'name',
        'description',
        'status',
        'submitted_at',
        'updated_at',
        'version',
        'maker_id',
        'maker__email',
        'checker_id',
        'checker__email',
        'submitted_by_id',
        'submitted_by__email',
    ).order_by('-submitted_at')
    
    # Filter by checker if not superuser
    if not request.user.is_superuser:
        templates = templates.filter(
            Q(checker=request.user) | Q(checker__isnull=True)
        )
    if not _can_user_self_review_ifrs9_templates(request.user):
        templates = templates.exclude(
            Q(submitted_by=request.user) | Q(submitted_by__isnull=True, maker=request.user)
        )
    
    # Search functionality
    search_query = request.GET.get('search', '').strip()
    if search_query:
        templates = templates.filter(
            Q(code__icontains=search_query) |
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(maker__email__icontains=search_query) |
            Q(submitted_by__email__icontains=search_query)
        )
    
    page_obj, page_size, list_query_string = _paginate_list_queryset(request, templates)
    
    context = {
        'templates': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'page_size': page_size,
        'list_query_string': list_query_string,
        'page_title': 'IFRS9 Templates Pending Review',
    }
    
    return render(
        request,
        'maker_checker/ifrs9scores_templates/template_checker_pending_list.html',
        context
    )


@login_required
def ifrs9_template_checker_review_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Review a submitted IFRS9 template (approve or return).
    """
    template = get_object_or_404(
        IFRS9ScoreSheetTemplate.objects.select_related('maker', 'checker', 'submitted_by'),
        id=template_id
    )
    
    # Permission check
    if not template.can_be_reviewed_by(request.user) and template.status != 'approved':
        messages.error(request, "You don't have permission to review this template.")
        return redirect('scorecard:ifrs9_template_checker_pending_list')
    if template.status != 'approved' and _must_use_different_template_reviewer(template, request.user, basel=False):
        return _reject_self_template_review(request, 'scorecard:ifrs9_template_checker_pending_list')
    
    # Build configuration for current template (pending changes)
    sections, drivers_by_section, attributes_by_driver = _build_ifrs9_configuration(template)
    section_totals, overall_totals = _build_template_structure_totals(sections)
    
    # Get workflow history
    workflow_history = template.workflow_history.all().select_related('performed_by').order_by('-performed_at')[:10]
    
    # Get versions
    versions = template.versions.all().select_related('created_by', 'approved_by').order_by('-version_number')
    latest_unapproved_version = template.versions.filter(is_approved=False).order_by('-version_number').first()
    latest_approved_version = template.versions.filter(is_approved=True).order_by('-approved_at', '-version_number').first()
    
    # Build configuration for approved version (if exists) for comparison
    # We'll use the same structure but build from version snapshots
    approved_sections = None
    approved_drivers_by_section = {}
    approved_attributes_by_driver = {}
    approved_options_by_attribute = {}
    
    if latest_approved_version:
        # Build structure from approved version snapshots
        approved_sections_list = []
        
        for section_version in latest_approved_version.sections.all().order_by('display_order'):
            approved_sections_list.append(section_version.section)
            approved_drivers_by_section[section_version.section.id] = []
            
            for driver_version in section_version.risk_drivers.all().order_by('display_order'):
                approved_drivers_by_section[section_version.section.id].append(driver_version.risk_driver)
                approved_attributes_by_driver[driver_version.risk_driver.id] = []
                
                for attr_version in driver_version.attributes.all().order_by('display_order'):
                    approved_attributes_by_driver[driver_version.risk_driver.id].append(attr_version.attribute)
                    approved_options_by_attribute[attr_version.attribute.id] = []
                    
                    for option_version in attr_version.options.all().order_by('display_order'):
                        approved_options_by_attribute[attr_version.attribute.id].append(option_version.option)
        
        approved_sections = approved_sections_list
    
    # Build options_by_attribute for current template
    options_by_attribute = {}
    for section in sections:
        for driver in drivers_by_section.get(section.id, []):
            for attribute in attributes_by_driver.get(driver.id, []):
                options_by_attribute[attribute.id] = list(attribute.options.all().order_by('display_order'))
    
    # Build approved options_by_attribute from version snapshots
    approved_options_by_attribute = {}
    if latest_approved_version:
        for section_version in latest_approved_version.sections.all():
            for driver_version in section_version.risk_drivers.all():
                for attr_version in driver_version.attributes.all():
                    approved_options_by_attribute[attr_version.attribute.id] = []
                    for option_version in attr_version.options.all():
                        approved_options_by_attribute[attr_version.attribute.id].append(option_version.option)
    
    # Build comprehensive comparison dictionaries for easier template access
    # Sections: section_id -> {name, code}
    approved_sections_dict = {}
    # Drivers: driver_id -> {name, code, weight_percent, max_score}
    approved_drivers_dict = {}
    # Attributes: attribute_id -> {label, code, weight_percent}
    approved_attributes_dict = {}
    # Options: attribute_id -> {option_id: {label, allocated_score}}
    approved_options_dict = {}
    
    if latest_approved_version:
        for section_version in latest_approved_version.sections.all():
            approved_sections_dict[section_version.section.id] = {
                'name': section_version.name,
                'code': section_version.code,
            }
            
            for driver_version in section_version.risk_drivers.all():
                approved_drivers_dict[driver_version.risk_driver.id] = {
                    'name': driver_version.name,
                    'code': driver_version.code,
                    'weight_percent': driver_version.weight_percent,
                    'max_score': driver_version.max_score,
                }
                
                for attr_version in driver_version.attributes.all():
                    approved_attributes_dict[attr_version.attribute.id] = {
                        'label': attr_version.label,
                        'code': attr_version.code,
                        'input_type': attr_version.input_type or 'radio',
                        'weight_percent': attr_version.weight_percent,
                    }
                    
                    approved_options_dict[attr_version.attribute.id] = {}
                    for option_version in attr_version.options.all():
                        approved_options_dict[attr_version.attribute.id][option_version.option.id] = {
                            'option': option_version.option,
                            'label': option_version.label,
                            'allocated_score': option_version.allocated_score,
                        }
    
    # Compare calculation formulas between approved and pending versions
    formula_comparison = {
        'actual_score': {'status': 'same', 'approved': '', 'pending': ''},
        'weighted_score': {'status': 'same', 'approved': '', 'pending': ''},
        'proof': {'status': 'same', 'approved': '', 'pending': ''},
    }
    
    if latest_approved_version:
        # Get approved formulas from version snapshot
        # Use getattr to safely access fields that might not exist in DB yet (before migration)
        approved_actual_score = getattr(latest_approved_version, 'formula_actual_score', None) or template.formula_actual_score or "ALLOCATED_SCORE"
        approved_weighted_score = getattr(latest_approved_version, 'formula_weighted_score', None) or template.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT"
        approved_proof = getattr(latest_approved_version, 'formula_proof', None) or template.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE"
        
        # Get pending formulas from current template
        pending_actual_score = template.formula_actual_score or "ALLOCATED_SCORE"
        pending_weighted_score = template.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT"
        pending_proof = template.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE"
        
        # Compare formulas
        formula_comparison['actual_score'] = {
            'status': 'same' if approved_actual_score == pending_actual_score else 'changed',
            'approved': approved_actual_score,
            'pending': pending_actual_score,
        }
        formula_comparison['weighted_score'] = {
            'status': 'same' if approved_weighted_score == pending_weighted_score else 'changed',
            'approved': approved_weighted_score,
            'pending': pending_weighted_score,
        }
        formula_comparison['proof'] = {
            'status': 'same' if approved_proof == pending_proof else 'changed',
            'approved': approved_proof,
            'pending': pending_proof,
        }
    else:
        # No approved version - all formulas are new
        formula_comparison['actual_score'] = {
            'status': 'new',
            'approved': '',
            'pending': template.formula_actual_score or "ALLOCATED_SCORE",
        }
        formula_comparison['weighted_score'] = {
            'status': 'new',
            'approved': '',
            'pending': template.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT",
        }
        formula_comparison['proof'] = {
            'status': 'new',
            'approved': '',
            'pending': template.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE",
        }
    
    pending_template_statuses = {'draft', 'in_progress', 'returned', 'submitted'}
    is_pending_template_review = template.status in pending_template_statuses
    show_pending_comparison = bool(latest_approved_version and is_pending_template_review)

    context = {
        'template': template,
        'sections': sections,
        'drivers_by_section': drivers_by_section,
        'attributes_by_driver': attributes_by_driver,
        'options_by_attribute': options_by_attribute,
        'approved_sections': approved_sections,
        'approved_drivers_by_section': approved_drivers_by_section,
        'approved_attributes_by_driver': approved_attributes_by_driver,
        'approved_options_by_attribute': approved_options_by_attribute,
        'approved_sections_dict': approved_sections_dict,
        'approved_drivers_dict': approved_drivers_dict,
        'approved_attributes_dict': approved_attributes_dict,
        'approved_options_dict': approved_options_dict,
        'workflow_history': workflow_history,
        'versions': versions,
        'latest_unapproved_version': latest_unapproved_version,
        'latest_approved_version': latest_approved_version,
        'formula_comparison': formula_comparison,
        'section_totals': section_totals,
        'overall_totals': overall_totals,
    }
    
    return render(
        request,
        'maker_checker/ifrs9scores_templates/template_checker_review.html',
        context
    )


@login_required
def ifrs9_template_approve_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Approve a submitted IFRS9 template.
    """
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    
    # Permission check
    if not template.can_be_reviewed_by(request.user):
        messages.error(request, "You don't have permission to approve this template.")
        return redirect('scorecard:ifrs9_template_checker_pending_list')
    if _must_use_different_template_reviewer(template, request.user, basel=False):
        return _reject_self_template_review(request, 'scorecard:ifrs9_template_checker_pending_list')
    
    if template.status != 'submitted':
        messages.error(request, "Only submitted templates can be approved.")
        return redirect('scorecard:ifrs9_template_checker_review', template_id=template.id)
    
    if request.method == 'POST':
        with transaction.atomic():
            old_status = template.status
            
            # Get latest unapproved version and mark it as approved
            latest_unapproved_version = template.versions.filter(is_approved=False).order_by('-version_number').first()
            
            if latest_unapproved_version:
                latest_unapproved_version.is_approved = True
                latest_unapproved_version.approved_at = timezone.now()
                latest_unapproved_version.approved_by = request.user
                latest_unapproved_version.save()
                
                # Delete other unapproved versions (keep only approved ones)
                template.versions.filter(is_approved=False).exclude(id=latest_unapproved_version.id).delete()
            
            template.status = 'approved'
            template.approved_by = request.user
            template.approved_at = timezone.now()
            template.save()
            
            # Create workflow history
            IFRS9TemplateWorkflowHistory.objects.create(
                template=template,
                action='approved',
                from_status=old_status,
                to_status='approved',
                performed_by=request.user,
                comments=request.POST.get('comments', '')
            )
            log_ifrs9_template_audit(request.user, "approve", template, "Template approved in maker checker workflow.")
            notify_ifrs9_template_approved(template, request.user)
            
            messages.success(request, f"Template '{template.code}' has been approved.")
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            return redirect('scorecard:ifrs9_template_checker_pending_list')
    
    context = {
        'template': template,
        'latest_unapproved_version': template.versions.filter(is_approved=False).order_by('-version_number').first(),
    }
    
    return render(
        request,
        'maker_checker/ifrs9scores_templates/template_approve.html',
        context
    )


@login_required
def ifrs9_template_return_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Return a submitted IFRS9 template for changes.
    """
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    
    # Permission check
    if not template.can_be_reviewed_by(request.user):
        messages.error(request, "You don't have permission to return this template.")
        return redirect('scorecard:ifrs9_template_checker_pending_list')
    if _must_use_different_template_reviewer(template, request.user, basel=False):
        return _reject_self_template_review(request, 'scorecard:ifrs9_template_checker_pending_list')
    
    if template.status != 'submitted':
        messages.error(request, "Only submitted templates can be returned.")
        return redirect('scorecard:ifrs9_template_checker_review', template_id=template.id)
    
    if request.method == 'POST':
        return_reason = request.POST.get('return_reason', '').strip()
        if not return_reason:
            messages.error(request, "Please provide a reason for returning the template.")
            return redirect('scorecard:ifrs9_template_return', template_id=template.id)
        
        with transaction.atomic():
            old_status = template.status
            
            # Delete unapproved versions (they will be recreated when maker edits)
            template.versions.filter(is_approved=False).delete()
            
            template.status = 'returned'
            template.returned_by = request.user
            template.returned_at = timezone.now()
            template.return_reason = return_reason
            template.save()
            
            # Create workflow history
            IFRS9TemplateWorkflowHistory.objects.create(
                template=template,
                action='returned',
                from_status=old_status,
                to_status='returned',
                performed_by=request.user,
                comments=return_reason
            )
            log_ifrs9_template_audit(request.user, "return", template, f"Template returned for changes. Reason: {return_reason}")
            notify_ifrs9_template_returned(template, request.user, return_reason)
            
            messages.success(request, f"Template '{template.code}' has been returned for changes.")
            bump_checker_pending_counts_version()
            bump_maker_queue_counts_version()
            return redirect('scorecard:ifrs9_template_checker_pending_list')
    
    context = {
        'template': template,
    }
    
    return render(
        request,
        'maker_checker/ifrs9scores_templates/template_return.html',
        context
    )
