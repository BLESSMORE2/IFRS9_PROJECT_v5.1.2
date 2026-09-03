from decimal import Decimal
from typing import Dict, List, Tuple
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import models, transaction
from django.db.models import IntegerField, Q
from django.db.models.deletion import ProtectedError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from scorecard.functions_view.audit import log_ifrs9_template_audit
from scorecard.models import (
    IFRS9Attribute,
    IFRS9GradeBand,
    IFRS9Option,
    IFRS9RiskDriver,
    IFRS9Section,
    IFRS9ScoreSheetTemplate,
    IFRS9TemplateVersion,
)


# Available formula variables/columns that can be used in formulas
FORMULA_VARIABLES = [
    ("ALLOCATED_SCORE", "ALLOCATED_SCORE - The score assigned to the selected option"),
    ("ACTUAL_SCORE", "ACTUAL_SCORE - Sum of allocated scores for the driver"),
    ("WEIGHTED_SCORE", "WEIGHTED_SCORE - Calculated weighted percentage"),
    ("Highest_Possible_Score", "Highest_Possible_Score - Maximum possible score for the driver"),
    ("WEIGHT", "WEIGHT - Weight percentage for the driver"),
    ("MAX_SCORE", "MAX_SCORE - Maximum score (same as Highest_Possible_Score)"),
]

# Operators that can be used in formulas
FORMULA_OPERATORS = [
    (" + ", "Addition"),
    (" - ", "Subtraction"),
    (" * ", "Multiplication"),
    (" / ", "Division"),
    (" = ", "Equals"),
    (" != ", "Not Equals"),
    (" > ", "Greater Than"),
    (" < ", "Less Than"),
    (" >= ", "Greater Than or Equal"),
    (" <= ", "Less Than or Equal"),
    ("(", "Open Parenthesis"),
    (")", "Close Parenthesis"),
]

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


def _generate_archived_template_code(original_code: str, template_id: int | None = None) -> str:
    suffix = 1
    while True:
        suffix_text = f"-ARCH{suffix}"
        candidate = f"{original_code[:50 - len(suffix_text)]}{suffix_text}"
        existing = IFRS9ScoreSheetTemplate.objects.filter(code=candidate)
        if template_id is not None:
            existing = existing.exclude(id=template_id)
        if not existing.exists():
            return candidate
        suffix += 1


def _soft_delete_ifrs9_attribute(attribute: IFRS9Attribute) -> None:
    with transaction.atomic():
        IFRS9Option.all_objects.filter(attribute=attribute, is_deleted=False).update(is_deleted=True)
        if not attribute.is_deleted:
            attribute.is_deleted = True
            attribute.save(update_fields=["is_deleted"])


def _soft_delete_ifrs9_driver(driver: IFRS9RiskDriver) -> None:
    with transaction.atomic():
        IFRS9Option.all_objects.filter(attribute__risk_driver=driver, is_deleted=False).update(is_deleted=True)
        IFRS9Attribute.all_objects.filter(risk_driver=driver, is_deleted=False).update(is_deleted=True)
        if not driver.is_deleted:
            driver.is_deleted = True
            driver.save(update_fields=["is_deleted"])


def _soft_delete_ifrs9_section(section: IFRS9Section) -> None:
    with transaction.atomic():
        IFRS9Option.all_objects.filter(attribute__risk_driver__section=section, is_deleted=False).update(is_deleted=True)
        IFRS9Attribute.all_objects.filter(risk_driver__section=section, is_deleted=False).update(is_deleted=True)
        IFRS9RiskDriver.all_objects.filter(section=section, is_deleted=False).update(is_deleted=True)
        if not section.is_deleted:
            section.is_deleted = True
            section.save(update_fields=["is_deleted"])


class FormulaField(forms.CharField):
    """Custom field for formula input with variable selection."""
    
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", forms.TextInput(attrs={
            "style": "width: 100%;",
            "class": "formula-input",
            "list": "formula-variables",
        }))
        super().__init__(*args, **kwargs)


class IFRS9ScoreSheetTemplateForm(forms.ModelForm):
    class Meta:
        model = IFRS9ScoreSheetTemplate
        fields = [
            "code",
            "name",
            "description",
            "is_active",
            "formula_actual_score",
            "formula_weighted_score",
            "formula_proof",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "formula_actual_score": forms.TextInput(attrs={
                "style": "width: 100%;",
                "class": "formula-input",
                "data-formula-type": "actual_score",
            }),
            "formula_weighted_score": forms.TextInput(attrs={
                "style": "width: 100%;",
                "class": "formula-input",
                "data-formula-type": "weighted_score",
            }),
            "formula_proof": forms.TextInput(attrs={
                "style": "width: 100%;",
                "class": "formula-input",
                "data-formula-type": "proof",
            }),
        }
        help_texts = {
            "formula_actual_score": "Formula for ACTUAL_SCORE (default: ALLOCATED_SCORE)",
            "formula_weighted_score": "Formula for WEIGHTED_SCORE (default: ACTUAL_SCORE / Highest_Possible_Score * WEIGHT)",
            "formula_proof": "Formula for PROOF validation (default: IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE)",
        }


def _get_reference_template() -> IFRS9ScoreSheetTemplate | None:
    """
    Get the reference template for IFRS9.
    This is the master template that new templates can clone from.
    """
    try:
        return IFRS9ScoreSheetTemplate.objects.prefetch_related(
            'sections__risk_drivers__attributes__options',
            'grade_bands'
        ).filter(is_active=True).order_by("id").first()
    except IFRS9ScoreSheetTemplate.DoesNotExist:
        return None


def _get_active_template() -> IFRS9ScoreSheetTemplate | None:
    """
    Returns the active IFRS9 score sheet template.
    """
    return IFRS9ScoreSheetTemplate.objects.filter(is_active=True).order_by("id").first()


def _build_configuration(
    template: IFRS9ScoreSheetTemplate,
    use_approved_version: bool = False,
) -> Tuple[List[IFRS9Section], Dict[int, List[IFRS9RiskDriver]], Dict[int, List[IFRS9Attribute]]]:
    """
    Prepares configuration objects for template rendering.
    
    Args:
        template: The template to build configuration from
        use_approved_version: If True, use the approved template version structure instead of current structure
    
    Returns:
        sections: ordered list of sections
        drivers_by_section: mapping section.id -> list of RiskDriver
        attributes_by_driver: mapping driver.id -> list of Attribute
    """
    # If using approved version, build from the approved version snapshot
    if use_approved_version:
        approved_version = template.versions.filter(is_approved=True).order_by('-approved_at', '-version_number').first()
        if approved_version:
            # Build from version snapshot
            sections = []
            drivers_by_section: Dict[int, List[IFRS9RiskDriver]] = {}
            attributes_by_driver: Dict[int, List[IFRS9Attribute]] = {}
            
            for section_version in approved_version.sections.all().order_by('display_order'):
                section = section_version.section
                sections.append(section)
                drivers_by_section[section.id] = []
                
                for driver_version in section_version.risk_drivers.all().order_by('display_order'):
                    driver = driver_version.risk_driver
                    drivers_by_section[section.id].append(driver)
                    attributes_by_driver[driver.id] = []
                    
                    for attr_version in driver_version.attributes.all().order_by('display_order'):
                        attribute = attr_version.attribute
                        attributes_by_driver[driver.id].append(attribute)
            
            return sections, drivers_by_section, attributes_by_driver
        # If no approved version exists, fall back to current structure
    
    # Build from current template structure
    sections = list(template.sections.all().order_by("display_order", "id"))

    drivers_by_section: Dict[int, List[IFRS9RiskDriver]] = {}
    attributes_by_driver: Dict[int, List[IFRS9Attribute]] = {}

    drivers = (
        IFRS9RiskDriver.objects.filter(section__template=template)
        .select_related("section")
        .order_by("display_order", "id")
    )
    attrs = (
        IFRS9Attribute.objects.filter(risk_driver__section__template=template)
        .select_related("risk_driver")
        .prefetch_related("options")
        .order_by("display_order", "id")
    )

    for driver in drivers:
        drivers_by_section.setdefault(driver.section_id, []).append(driver)

    for attr in attrs:
        attributes_by_driver.setdefault(attr.risk_driver_id, []).append(attr)

    return sections, drivers_by_section, attributes_by_driver


def _field_name_for_attribute(attribute: IFRS9Attribute) -> str:
    return f"attr_{attribute.id}"


@login_required
def ifrs9_template_list_view(request: HttpRequest) -> HttpResponse:
    """
    Simple frontend list of IFRS9 score sheet templates and link to create a new one.
    
    Shows all templates (same as Basel).
    When viewing a template detail, it shows the approved version (handled in detail view).
    This ensures that when editing an approved template, it still appears in the list,
    but viewing it shows the approved version, not the pending edits.
    """
    search_query = request.GET.get("q", "").strip()
    page_size = _normalize_list_page_size(request.GET.get("page_size"))

    templates = IFRS9ScoreSheetTemplate.objects.annotate(
        archived_sort=models.Case(
            models.When(status="cancelled", then=models.Value(1)),
            default=models.Value(0),
            output_field=IntegerField(),
        )
    ).only(
        "id",
        "code",
        "name",
        "is_active",
        "status",
        "created_at",
        "updated_at",
        "submitted_at",
        "approved_at",
        "returned_at",
    ).order_by("archived_sort", "-is_active", "name")
    if search_query:
        templates = templates.filter(
            Q(code__icontains=search_query) | Q(name__icontains=search_query)
        )
    paginator = Paginator(templates, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))
    list_query_string = _build_list_query_string(request, excluded_keys={"page"})

    context = {
        "templates": page_obj.object_list,
        "page_obj": page_obj,
        "search_query": search_query,
        "page_size": page_size,
        "list_query_string": list_query_string,
    }
    return render(request, "ifrs9_score_config/ifrs9_template_list.html", context)


def _clone_template_structure(
    source_template: IFRS9ScoreSheetTemplate, target_template: IFRS9ScoreSheetTemplate
) -> Dict[str, int]:
    """
    Clone all sections, risk drivers, attributes, options, and grade bands
    from source_template to target_template.
    
    Returns a dictionary with counts of what was cloned:
    {'sections': X, 'drivers': Y, 'attributes': Z, 'options': W, 'grade_bands': V}
    """
    counts = {
        'sections': 0,
        'drivers': 0,
        'attributes': 0,
        'options': 0,
        'grade_bands': 0
    }
    
    with transaction.atomic():
        # Step 1: Clone grade bands FIRST (independent of sections)
        source_bands_qs = source_template.grade_bands.all().order_by("display_order")
        source_bands = list(source_bands_qs)
        
        for source_band in source_bands:
            IFRS9GradeBand.objects.create(
                template=target_template,
                grade_code=source_band.grade_code,
                description=source_band.description,
                min_percent=source_band.min_percent,
                max_percent=source_band.max_percent,
                display_order=source_band.display_order,
            )
            counts['grade_bands'] += 1
        
        # Step 2: Clone sections and their nested structure
        source_sections_qs = source_template.sections.all().order_by("display_order")
        source_sections = list(source_sections_qs)
        
        for source_section in source_sections:
            # Create new section
            new_section = IFRS9Section.objects.create(
                template=target_template,
                code=source_section.code,
                name=source_section.name,
                display_order=source_section.display_order,
            )
            counts['sections'] += 1

            # Step 3: Clone risk drivers for this section
            source_drivers_qs = source_section.risk_drivers.all().order_by("display_order")
            source_drivers = list(source_drivers_qs)
            
            for source_driver in source_drivers:
                # Create new risk driver
                new_driver = IFRS9RiskDriver.objects.create(
                    section=new_section,
                    code=source_driver.code,
                    name=source_driver.name,
                    weight_percent=source_driver.weight_percent,
                    max_score=source_driver.max_score,
                    display_order=source_driver.display_order,
                )
                counts['drivers'] += 1

                # Step 4: Clone attributes for this driver
                source_attributes_qs = source_driver.attributes.all().order_by("display_order")
                source_attributes = list(source_attributes_qs)
                
                for source_attr in source_attributes:
                    # Create new attribute
                    new_attr = IFRS9Attribute.objects.create(
                        risk_driver=new_driver,
                        code=source_attr.code,
                        label=source_attr.label,
                        help_text=source_attr.help_text,
                        data_type=source_attr.data_type,
                        input_type=source_attr.input_type,
                        group_label=source_attr.group_label,
                        weight_percent=source_attr.weight_percent,
                        is_required=source_attr.is_required,
                        display_order=source_attr.display_order,
                        requires_document=source_attr.requires_document,
                    )
                    counts['attributes'] += 1

                    # Step 5: Clone options for this attribute
                    source_options_qs = source_attr.options.all().order_by("display_order")
                    source_options = list(source_options_qs)
                    
                    for source_option in source_options:
                        IFRS9Option.objects.create(
                            attribute=new_attr,
                            label=source_option.label,
                            value=source_option.value,
                            allocated_score=source_option.allocated_score,
                            display_order=source_option.display_order,
                            is_default=source_option.is_default,
                        )
                        counts['options'] += 1
    
    return counts


@login_required
def ifrs9_template_create_view(request: HttpRequest) -> HttpResponse:
    """
    Frontend form to create a new IFRS9 score sheet template.
    Supports cloning from a reference template when checkbox is checked.
    """

    reference_template = _get_reference_template()

    if request.method == "POST":
        form = IFRS9ScoreSheetTemplateForm(request.POST)
        
        if form.is_valid():
            # Save the new template first - set as draft for maker-checker workflow
            template = form.save(commit=False)
            template.is_active = True
            template.status = 'draft'
            template.maker = request.user
            template.save()
            log_ifrs9_template_audit(
                request.user,
                "create",
                template,
                "IFRS9 template created from the template form.",
            )
            
            # Check if cloning checkbox was checked
            clone_checkbox = request.POST.get("clone_from_reference")
            should_clone = clone_checkbox == "on"
            
            # Try to clone if reference template exists and checkbox was checked
            if should_clone and reference_template:
                try:
                    counts = _clone_template_structure(reference_template, template)
                    messages.success(
                        request,
                        f"✅ Template '{template.code}' created successfully! "
                        f"Cloned {counts['sections']} sections, {counts['drivers']} risk drivers, "
                        f"{counts['attributes']} attributes, {counts['options']} options, "
                        f"and {counts['grade_bands']} grade bands from reference template."
                    )
                except Exception as e:
                    import traceback
                    messages.error(request, f"❌ Error during cloning: {str(e)}")
                    print(f"CLONING ERROR:\n{traceback.format_exc()}")
            
            return redirect("scorecard:ifrs9_template_list")
    else:
        form = IFRS9ScoreSheetTemplateForm()
        # Pre-fill formulas from reference template if it exists
        if reference_template:
            form.fields["formula_actual_score"].initial = reference_template.formula_actual_score
            form.fields["formula_weighted_score"].initial = reference_template.formula_weighted_score
            form.fields["formula_proof"].initial = reference_template.formula_proof

    context = {
        "form": form,
        "formula_variables": FORMULA_VARIABLES,
        "formula_operators": FORMULA_OPERATORS,
        "reference_template": reference_template,
    }

    return render(
        request,
        "ifrs9_score_config/ifrs9_template_create.html",
        context,
    )


@login_required
def ifrs9_template_detail_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    View to see the full configuration of an IFRS9 template including:
    - Template details and formulas
    - Sections
    - Risk Drivers
    - Attributes
    - Options (with allocated scores)
    - Grade bands
    
    IMPORTANT: This view shows ONLY APPROVED template information.
    Used from the main template list - shows what questionnaires will use.
    Does NOT show pending/unapproved changes.
    """
    template = get_object_or_404(
        IFRS9ScoreSheetTemplate.objects.prefetch_related(
            "sections__risk_drivers__attributes__options"
        ),
        id=template_id,
    )

    # CRITICAL: Use approved template version structure for detail view
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=True)
    
    # Get approved template metadata and formulas from the approved version snapshot (if exists)
    approved_version = template.versions.filter(is_approved=True).order_by('-approved_at', '-version_number').first()
    
    # Default to current template values (for legacy templates without versions)
    approved_template_info = {
        'code': template.code,
        'name': template.name,
        'description': template.description,
        'version': template.version or "N/A",
    }
    
    approved_formulas = {
        'formula_actual_score': template.formula_actual_score or "ALLOCATED_SCORE",
        'formula_weighted_score': template.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT",
        'formula_proof': template.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE",
    }
    
    # CRITICAL: Build approved options mapping from version snapshot
    approved_options_by_attribute = {}
    approved_max_scores_by_attribute = {}
    approved_attribute_weights = {}
    approved_driver_weights = {}
    if approved_version:
        # Build mapping: attribute.id -> list of approved options with their allocated scores
        for section_version in approved_version.sections.all():
            for driver_version in section_version.risk_drivers.all():
                approved_driver_weights[driver_version.risk_driver.id] = driver_version.weight_percent
                
                for attr_version in driver_version.attributes.all():
                    approved_attribute_weights[attr_version.attribute.id] = attr_version.weight_percent
                    
                    approved_options_by_attribute[attr_version.attribute.id] = []
                    max_score = None
                    for option_version in attr_version.options.all().order_by('display_order'):
                        allocated_score = option_version.allocated_score
                        approved_options_by_attribute[attr_version.attribute.id].append({
                            'option': option_version.option,
                            'allocated_score': allocated_score,
                            'label': option_version.label,
                            'display_order': option_version.display_order,
                        })
                        if max_score is None or allocated_score > max_score:
                            max_score = allocated_score
                    approved_max_scores_by_attribute[attr_version.attribute.id] = max_score if max_score is not None else 0
        
        # Use approved version values
        if hasattr(approved_version, 'template_code'):
            approved_template_info['code'] = approved_version.template_code or template.code
        if hasattr(approved_version, 'template_name'):
            approved_template_info['name'] = approved_version.template_name or template.name
        if hasattr(approved_version, 'template_description'):
            approved_template_info['description'] = approved_version.template_description or ""
        if hasattr(approved_version, 'template_version'):
            approved_template_info['version'] = approved_version.template_version or "N/A"
        
        if hasattr(approved_version, 'formula_actual_score'):
            approved_formulas['formula_actual_score'] = approved_version.formula_actual_score or "ALLOCATED_SCORE"
        if hasattr(approved_version, 'formula_weighted_score'):
            approved_formulas['formula_weighted_score'] = approved_version.formula_weighted_score or "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT"
        if hasattr(approved_version, 'formula_proof'):
            approved_formulas['formula_proof'] = approved_version.formula_proof or "IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE"
    
    # Get all grade bands for the grading system display
    grade_bands = template.grade_bands.all().order_by("display_order")

    section_totals = {}
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
    workflow_history = list(
        template.workflow_history.select_related("performed_by").all().order_by("-performed_at")
    )[:10]

    context = {
        "template": template,
        "sections": sections,
        "drivers_by_section": drivers_by_section,
        "attributes_by_driver": attributes_by_driver,
        "approved_options_by_attribute": approved_options_by_attribute,
        "approved_max_scores_by_attribute": approved_max_scores_by_attribute,
        "approved_attribute_weights": approved_attribute_weights,
        "approved_driver_weights": approved_driver_weights,
        "grade_bands": grade_bands,
        "approved_formulas": approved_formulas,
        "approved_template_info": approved_template_info,
        "approved_version": approved_version,
        "section_totals": section_totals,
        "overall_totals": overall_totals,
        "workflow_history": workflow_history,
    }

    template_name = "ifrs9_score_config/ifrs9_template_detail.html"
    if getattr(request.resolver_match, "url_name", "") == "checker_my_approvals_ifrs9_template_view":
        template_name = "maker_checker/checker_ifrs9_template_approval_view.html"

    return render(
        request,
        template_name,
        context,
    )


@login_required
def ifrs9_template_edit_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """Edit template details including formulas."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    
    # Check if template can be edited
    if not template.can_be_edited_by(request.user):
        messages.error(request, "This template cannot be edited. It must be in draft, in_progress, returned, or approved status.")
        return redirect("scorecard:ifrs9_template_detail", template_id=template.id)

    if request.method == "POST":
        form = IFRS9ScoreSheetTemplateForm(request.POST, instance=template)
        if form.is_valid():
            _ensure_template_edit_started(template, request.user)
            form.save()
            log_ifrs9_template_audit(
                request.user,
                "update",
                template,
                "IFRS9 template details and formulas updated.",
            )
            messages.success(request, f"Template '{template.code}' updated successfully!")
            return redirect("scorecard:ifrs9_template_list")
    else:
        form = IFRS9ScoreSheetTemplateForm(instance=template)

    return render(
        request,
        "ifrs9_score_config/ifrs9_template_edit.html",
        {"form": form, "template": template},
    )


@login_required
def ifrs9_template_delete_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """Delete a template."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)

    can_manage_template = (
        request.user.is_superuser
        or request.user.has_perm("scorecard.manage_ifrs9_templates")
        or template.maker == request.user
        or template.maker is None
    )
    if not can_manage_template:
        messages.error(request, "You don't have permission to delete this template.")
        return redirect("scorecard:ifrs9_template_detail", template_id=template.id)

    evaluation_count = template.evaluations.count()
    can_delete = evaluation_count == 0

    if request.method == "POST":
        template_code = template.code
        if evaluation_count > 0:
            archived_code = template.code if (template.status == "cancelled" and not template.is_active) else _generate_archived_template_code(template.code, template.id)
            template.is_active = False
            template.status = "cancelled"
            template.code = archived_code
            template.save(update_fields=["is_active", "status", "code", "updated_at"])
            log_ifrs9_template_audit(
                request.user,
                "archive",
                template,
                f"Template archived because {evaluation_count} evaluation(s) are linked to it. Archived code: {archived_code}.",
            )
            messages.success(
                request,
                f"Template '{template_code}' has been archived as '{archived_code}' because it has {evaluation_count} associated evaluation(s). It is now inactive and will not be used for new IFRS9 evaluations.",
            )
        else:
            try:
                log_ifrs9_template_audit(
                    request.user,
                    "delete",
                    template,
                    "IFRS9 template permanently deleted.",
                )
                template.delete()
                messages.success(request, f"Template '{template_code}' permanently deleted successfully!")
            except ProtectedError:
                archived_code = template.code if (template.status == "cancelled" and not template.is_active) else _generate_archived_template_code(template.code, template.id)
                template.is_active = False
                template.status = "cancelled"
                template.code = archived_code
                template.save(update_fields=["is_active", "status", "code", "updated_at"])
                log_ifrs9_template_audit(
                    request.user,
                    "archive",
                    template,
                    f"Template archived because protected version history or template components still reference it. Archived code: {archived_code}.",
                )
                messages.warning(
                    request,
                    f"Template '{template_code}' could not be permanently deleted because version history or template components still reference it. It has been archived as '{archived_code}' instead and will not be used for new IFRS9 evaluations.",
                )
        return redirect("scorecard:ifrs9_template_list")

    return render(
        request,
        "ifrs9_score_config/ifrs9_template_delete.html",
        {
            "template": template,
            "evaluation_count": evaluation_count,
            "can_delete": can_delete,
        },
    )


# ============================================================================
# HELPER FUNCTIONS FOR TEMPLATE EDITING
# ============================================================================

def _check_template_editable(template, user, request):
    """Return whether the current user is allowed to edit this template."""
    if not template.can_be_edited_by(user):
        messages.error(request, f"Template '{template.code}' cannot be edited. It must be in draft, in_progress, returned, or approved status.")
        return False
    return True


def _ensure_template_edit_started(template, user):
    """Start or claim an edit cycle only after the user saves a real change."""
    update_fields = []
    if template.status in ['approved', None, '']:
        if template.status == 'approved' and not template.versions.filter(is_approved=True).exists():
            from scorecard.functions_view.template_maker_checker import _create_ifrs9_template_version
            version_number = (template.versions.order_by('-version_number').first().version_number + 1) if template.versions.exists() else 1
            baseline = _create_ifrs9_template_version(
                template,
                version_number,
                user=getattr(template, 'approved_by', None) or user,
                change_description=f"Approved baseline before builder changes - Version {version_number}",
            )
            baseline.is_approved = True
            baseline.approved_at = template.approved_at
            baseline.approved_by = getattr(template, 'approved_by', None)
            baseline.save(update_fields=['is_approved', 'approved_at', 'approved_by'])
        template.status = 'in_progress'
        update_fields.append('status')
    elif template.status == 'draft':
        template.status = 'in_progress'
        update_fields.append('status')

    # A permitted user who saves a change owns the current edit cycle.
    if template.maker_id != user.id:
        template.maker = user
        update_fields.append('maker')

    # Submission ownership belongs to the previous review cycle and must not
    # keep the newly edited template in the previous maker's queue.
    if template.submitted_by_id is not None:
        template.submitted_by = None
        update_fields.append('submitted_by')
    if template.submitted_at is not None:
        template.submitted_at = None
        update_fields.append('submitted_at')

    if update_fields:
        update_fields.append('updated_at')
        template.save(update_fields=list(dict.fromkeys(update_fields)))

# ============================================================================
# FORMS FOR SECTION/DRIVER/ATTRIBUTE/OPTION MANAGEMENT
# ============================================================================

class IFRS9SectionForm(forms.ModelForm):
    class Meta:
        model = IFRS9Section
        fields = ["name", "display_order"]
        widgets = {
            "name": forms.TextInput(attrs={"style": "width: 100%;"}),
            "display_order": forms.NumberInput(attrs={"style": "width: 100%;"}),
        }
        help_texts = {
            "name": "Section name",
            "display_order": "Order in which this section should appear (lower numbers first)",
        }


class IFRS9RiskDriverForm(forms.ModelForm):
    class Meta:
        model = IFRS9RiskDriver
        fields = ["name", "display_order"]
        widgets = {
            "name": forms.TextInput(attrs={"style": "width: 100%;"}),
            "display_order": forms.NumberInput(attrs={"style": "width: 100%;"}),
        }
        help_texts = {
            "name": "Risk driver name",
            "display_order": "Order in which this risk driver should appear (lower numbers first)",
        }


class IFRS9AttributeForm(forms.ModelForm):
    class Meta:
        model = IFRS9Attribute
        fields = [
            "label",
            "help_text",
            "group_label",
            "input_type",
            "weight_percent",
            "display_order",
            "requires_document",
        ]
        widgets = {
            "label": forms.TextInput(attrs={"style": "width: 100%;"}),
            "help_text": forms.Textarea(attrs={"rows": 3, "style": "width: 100%;"}),
            "group_label": forms.TextInput(attrs={"style": "width: 100%;"}),
            "input_type": forms.Select(attrs={"style": "width: 100%;"}),
            "weight_percent": forms.NumberInput(attrs={"step": "0.01", "style": "width: 100%;"}),
            "display_order": forms.NumberInput(attrs={"style": "width: 100%;"}),
            "requires_document": forms.CheckboxInput(attrs={"style": "width: auto;"}),
        }
        help_texts = {
            "label": "Attribute label/question text",
            "help_text": "Optional help text for this attribute",
            "group_label": "Optional group label",
            "input_type": "Choose radio for single choice, select for dropdown, or checkbox to allow one or many selections.",
            "weight_percent": "Weight as a percentage of the total score",
            "display_order": "Order in which this attribute should appear (lower numbers first)",
            "requires_document": "If enabled, users will be able to upload supporting documents when selecting an option for this attribute.",
        }


class IFRS9OptionForm(forms.ModelForm):
    class Meta:
        model = IFRS9Option
        fields = ["label", "allocated_score", "display_order", "is_default"]
        widgets = {
            "label": forms.TextInput(attrs={"style": "width: 100%;"}),
            "allocated_score": forms.NumberInput(attrs={"step": "0.01", "style": "width: 100%;"}),
            "display_order": forms.NumberInput(attrs={"style": "width: 100%;"}),
            "is_default": forms.CheckboxInput(),
        }
        help_texts = {
            "label": "Option label",
            "allocated_score": "Allocated score for this option",
            "display_order": "Order in which this option should appear (lower numbers first)",
            "is_default": "Whether this option is selected by default",
        }


def _build_ifrs9_template_tree_counts(template: IFRS9ScoreSheetTemplate):
    sections = list(template.sections.only("id", "code", "name", "display_order").order_by("display_order", "id"))
    drivers = list(IFRS9RiskDriver.objects.filter(section__template=template).only("id", "section_id", "code", "name", "display_order", "max_score", "weight_percent").order_by("section__display_order", "display_order", "id"))
    attributes = list(IFRS9Attribute.objects.filter(risk_driver__section__template=template).only("id", "risk_driver_id", "code", "label", "group_label", "help_text", "display_order", "data_type", "input_type", "is_required", "weight_percent", "requires_document").order_by("risk_driver__section__display_order", "risk_driver__display_order", "display_order", "id"))
    options = list(IFRS9Option.objects.filter(attribute__risk_driver__section__template=template, is_deleted=False).only("id", "attribute_id", "label", "value", "allocated_score", "display_order", "is_default").order_by("attribute__risk_driver__section__display_order", "attribute__risk_driver__display_order", "attribute__display_order", "display_order", "id"))

    drivers_by_section = {}
    attributes_by_driver = {}
    options_by_attribute = {}
    for option in options:
        options_by_attribute.setdefault(option.attribute_id, []).append(option)
    for attribute in attributes:
        attribute.sorted_options = options_by_attribute.get(attribute.id, [])
        attributes_by_driver.setdefault(attribute.risk_driver_id, []).append(attribute)
    for driver in drivers:
        driver.sorted_attributes = attributes_by_driver.get(driver.id, [])
        drivers_by_section.setdefault(driver.section_id, []).append(driver)
    for section in sections:
        section.sorted_risk_drivers = drivers_by_section.get(section.id, [])

    return sections, {
        "section_count": len(sections),
        "driver_count": len(drivers),
        "attribute_count": len(attributes),
        "option_count": len(options),
    }


def _build_ifrs9_template_tree(template: IFRS9ScoreSheetTemplate):
    sections, _counts = _build_ifrs9_template_tree_counts(template)
    return sections


def _next_display_order(queryset) -> int:
    last_order = queryset.aggregate(models.Max("display_order"))["display_order__max"]
    return (last_order or 0) + 1


def _number_to_letters(index: int) -> str:
    letters = []
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters.append(chr(65 + remainder))
    return "".join(reversed(letters))


def _next_section_code(template: IFRS9ScoreSheetTemplate) -> str:
    used_codes = set(template.sections.values_list("code", flat=True))
    candidate_index = 1
    while True:
        candidate = _number_to_letters(candidate_index)
        if candidate not in used_codes:
            return candidate
        candidate_index += 1


def _next_risk_driver_code(section: IFRS9Section) -> str:
    used_codes = set(section.risk_drivers.values_list("code", flat=True))
    candidate_index = 1
    while True:
        candidate = str(candidate_index)
        if candidate not in used_codes:
            return candidate
        candidate_index += 1


def _reorder_siblings(queryset, current_id: int, direction: str) -> bool:
    items = list(queryset.order_by("display_order", "id"))
    current_index = next((index for index, item in enumerate(items) if item.id == current_id), None)
    if current_index is None:
        return False

    target_index = current_index - 1 if direction == "up" else current_index + 1
    if target_index < 0 or target_index >= len(items):
        return False

    item = items.pop(current_index)
    items.insert(target_index, item)

    with transaction.atomic():
        for index, sibling in enumerate(items, start=1):
            if sibling.display_order != index:
                sibling.display_order = index
                sibling.save(update_fields=["display_order"])
    return True


def _build_ifrs9_builder_context(template, *, form_state=None):
    form_state = form_state or {}
    sections, tree_counts = _build_ifrs9_template_tree_counts(template)

    driver_forms = {}
    attribute_forms = {}
    option_forms = {}
    section_edit_forms = {}
    driver_edit_forms = {}
    attribute_edit_forms = {}
    option_edit_forms = {}

    for section in sections:
        section_edit_forms[section.id] = form_state.get(
            ("edit_section", section.id),
            IFRS9SectionForm(
                prefix=f"edit-section-{section.id}",
                instance=section,
            ),
        )
        driver_forms[section.id] = form_state.get(
            ("driver", section.id),
            IFRS9RiskDriverForm(
                prefix=f"driver-{section.id}",
                initial={"display_order": _next_display_order(section.risk_drivers.all())},
            ),
        )
        for driver in section.sorted_risk_drivers:
            driver_edit_forms[driver.id] = form_state.get(
                ("edit_driver", driver.id),
                IFRS9RiskDriverForm(
                    prefix=f"edit-driver-{driver.id}",
                    instance=driver,
                ),
            )
            attribute_forms[driver.id] = form_state.get(
                ("attribute", driver.id),
                IFRS9AttributeForm(
                    prefix=f"attribute-{driver.id}",
                    initial={"display_order": _next_display_order(driver.attributes.all())},
                ),
            )
            for attribute in driver.sorted_attributes:
                attribute_edit_forms[attribute.id] = form_state.get(
                    ("edit_attribute", attribute.id),
                    IFRS9AttributeForm(
                        prefix=f"edit-attribute-{attribute.id}",
                        instance=attribute,
                    ),
                )
                option_forms[attribute.id] = form_state.get(
                    ("option", attribute.id),
                    IFRS9OptionForm(
                        prefix=f"option-{attribute.id}",
                        initial={"display_order": _next_display_order(attribute.options.all())},
                    ),
                )
                for option in attribute.sorted_options:
                    option_edit_forms[option.id] = form_state.get(
                        ("edit_option", option.id),
                        IFRS9OptionForm(
                            prefix=f"edit-option-{option.id}",
                            instance=option,
                        ),
                    )

    return {
        "template": template,
        "sections": sections,
        "section_form": form_state.get(
            ("section", None),
            IFRS9SectionForm(
                prefix="section",
                initial={"display_order": _next_display_order(template.sections.all())},
            ),
        ),
        "driver_forms": driver_forms,
        "attribute_forms": attribute_forms,
        "option_forms": option_forms,
        "section_edit_forms": section_edit_forms,
        "driver_edit_forms": driver_edit_forms,
        "attribute_edit_forms": attribute_edit_forms,
        "option_edit_forms": option_edit_forms,
        "section_count": tree_counts["section_count"],
        "driver_count": tree_counts["driver_count"],
        "attribute_count": tree_counts["attribute_count"],
        "option_count": tree_counts["option_count"],
        "active_anchor": form_state.get("active_anchor", ""),
    }


@login_required
def ifrs9_template_builder_view(request: HttpRequest, template_id: int) -> HttpResponse:
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)

    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:ifrs9_template_detail", template_id=template.id)

    form_state = {}

    if request.method == "POST":
        _ensure_template_edit_started(template, request.user)
        action = request.POST.get("builder_action")

        if action in {"move_section_up", "move_section_down"}:
            section = get_object_or_404(IFRS9Section, id=request.POST.get("section_id"), template=template)
            direction = "up" if action.endswith("_up") else "down"
            moved = _reorder_siblings(template.sections.all(), section.id, direction)
            if moved:
                log_ifrs9_template_audit(request.user, "reorder_section", template, f"Moved section '{section.code}' {direction} in the IFRS9 builder.")
                messages.success(request, f"Section '{section.code}' moved {direction}.")
            else:
                messages.info(request, f"Section '{section.code}' is already at the {'top' if direction == 'up' else 'bottom'}.")
            return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#section-{section.id}")
        elif action in {"move_driver_up", "move_driver_down"}:
            driver = get_object_or_404(IFRS9RiskDriver, id=request.POST.get("driver_id"), section__template=template)
            direction = "up" if action.endswith("_up") else "down"
            moved = _reorder_siblings(driver.section.risk_drivers.all(), driver.id, direction)
            if moved:
                log_ifrs9_template_audit(request.user, "reorder_risk_driver", template, f"Moved risk driver '{driver.code}' {direction} in the IFRS9 builder.")
                messages.success(request, f"Risk driver '{driver.code}' moved {direction}.")
            else:
                messages.info(request, f"Risk driver '{driver.code}' is already at the {'top' if direction == 'up' else 'bottom'}.")
            return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#driver-{driver.id}")
        elif action in {"move_attribute_up", "move_attribute_down"}:
            attribute = get_object_or_404(IFRS9Attribute, id=request.POST.get("attribute_id"), risk_driver__section__template=template)
            direction = "up" if action.endswith("_up") else "down"
            moved = _reorder_siblings(attribute.risk_driver.attributes.all(), attribute.id, direction)
            if moved:
                log_ifrs9_template_audit(request.user, "reorder_attribute", template, f"Moved attribute '{attribute.label}' {direction} in the IFRS9 builder.")
                messages.success(request, f"Attribute '{attribute.label}' moved {direction}.")
            else:
                messages.info(request, f"Attribute '{attribute.label}' is already at the {'top' if direction == 'up' else 'bottom'}.")
            return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#attribute-{attribute.id}")
        elif action in {"move_option_up", "move_option_down"}:
            option = get_object_or_404(IFRS9Option, id=request.POST.get("option_id"), attribute__risk_driver__section__template=template)
            direction = "up" if action.endswith("_up") else "down"
            moved = _reorder_siblings(option.attribute.options.all(), option.id, direction)
            if moved:
                log_ifrs9_template_audit(request.user, "reorder_option", template, f"Moved option '{option.label}' {direction} in the IFRS9 builder.")
                messages.success(request, f"Option '{option.label}' moved {direction}.")
            else:
                messages.info(request, f"Option '{option.label}' is already at the {'top' if direction == 'up' else 'bottom'}.")
            return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#attribute-{option.attribute_id}")
        elif action == "add_section":
            form = IFRS9SectionForm(request.POST, prefix="section")
            if form.is_valid():
                section = form.save(commit=False)
                section.template = template
                section.code = _next_section_code(template)
                section.save()
                log_ifrs9_template_audit(request.user, "create_section", template, f"Created section '{section.code}' from the IFRS9 builder.")
                messages.success(request, f"Section '{section.code}' added to the IFRS9 template.")
                return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#add-driver-{section.id}")
            form_state[("section", None)] = form
            form_state["active_anchor"] = "add-section"
        elif action == "update_section":
            section = get_object_or_404(IFRS9Section, id=request.POST.get("section_id"), template=template)
            form = IFRS9SectionForm(request.POST, prefix=f"edit-section-{section.id}", instance=section)
            if form.is_valid():
                form.save()
                log_ifrs9_template_audit(request.user, "update_section", template, f"Updated section '{section.code}' from the IFRS9 builder.")
                messages.success(request, f"Section '{section.code}' updated.")
                return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#section-{section.id}")
            form_state[("edit_section", section.id)] = form
            form_state["active_anchor"] = f"section-{section.id}"
        elif action == "delete_section":
            section = get_object_or_404(IFRS9Section, id=request.POST.get("section_id"), template=template)
            section_code = section.code
            _soft_delete_ifrs9_section(section)
            log_ifrs9_template_audit(request.user, "delete_section", template, f"Deleted section '{section_code}' from the IFRS9 builder.")
            messages.success(request, f"Section '{section_code}' deleted.")
            return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}")
        elif action == "add_driver":
            section = get_object_or_404(IFRS9Section, id=request.POST.get("section_id"), template=template)
            form = IFRS9RiskDriverForm(request.POST, prefix=f"driver-{section.id}")
            if form.is_valid():
                driver = form.save(commit=False)
                driver.section = section
                driver.code = _next_risk_driver_code(section)
                driver.max_score = Decimal("0")
                driver.weight_percent = Decimal("0")
                driver.save()
                log_ifrs9_template_audit(request.user, "create_risk_driver", template, f"Created risk driver '{driver.code}' from the IFRS9 builder.")
                messages.success(request, f"Risk driver '{driver.code}' added under section '{section.code}'.")
                return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#add-attribute-{driver.id}")
            form_state[("driver", section.id)] = form
            form_state["active_anchor"] = f"add-driver-{section.id}"
        elif action == "update_driver":
            driver = get_object_or_404(IFRS9RiskDriver, id=request.POST.get("driver_id"), section__template=template)
            form = IFRS9RiskDriverForm(request.POST, prefix=f"edit-driver-{driver.id}", instance=driver)
            if form.is_valid():
                form.save()
                log_ifrs9_template_audit(request.user, "update_risk_driver", template, f"Updated risk driver '{driver.code}' from the IFRS9 builder.")
                messages.success(request, f"Risk driver '{driver.code}' updated.")
                return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#driver-{driver.id}")
            form_state[("edit_driver", driver.id)] = form
            form_state["active_anchor"] = f"driver-{driver.id}"
        elif action == "delete_driver":
            driver = get_object_or_404(IFRS9RiskDriver, id=request.POST.get("driver_id"), section__template=template)
            driver_code = driver.code
            section_id = driver.section_id
            _soft_delete_ifrs9_driver(driver)
            log_ifrs9_template_audit(request.user, "delete_risk_driver", template, f"Deleted risk driver '{driver_code}' from the IFRS9 builder.")
            messages.success(request, f"Risk driver '{driver_code}' deleted.")
            return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#section-{section_id}")
        elif action == "add_attribute":
            driver = get_object_or_404(IFRS9RiskDriver, id=request.POST.get("driver_id"), section__template=template)
            form = IFRS9AttributeForm(request.POST, prefix=f"attribute-{driver.id}")
            if form.is_valid():
                attribute = form.save(commit=False)
                attribute.risk_driver = driver
                attribute.group_label = (attribute.label or "").strip()
                if not attribute.code:
                    import re

                    words = re.findall(r"\b\w", attribute.label.strip())
                    base_code = "".join(words).upper()[:50] or f"ATTR_{attribute.display_order}"
                    code = base_code
                    counter = 1
                    while IFRS9Attribute.objects.filter(risk_driver=driver, code=code).exists():
                        code = f"{base_code}_{counter}"
                        counter += 1
                    attribute.code = code
                attribute.data_type = "choice"
                attribute.input_type = attribute.input_type or "radio"
                attribute.is_required = True
                attribute.save()
                log_ifrs9_template_audit(request.user, "create_attribute", template, f"Created attribute '{attribute.label}' from the IFRS9 builder.")
                messages.success(request, f"Attribute '{attribute.label}' added under risk driver '{driver.code}'.")
                return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#add-option-{attribute.id}")
            form_state[("attribute", driver.id)] = form
            form_state["active_anchor"] = f"add-attribute-{driver.id}"
        elif action == "update_attribute":
            attribute = get_object_or_404(IFRS9Attribute, id=request.POST.get("attribute_id"), risk_driver__section__template=template)
            form = IFRS9AttributeForm(request.POST, prefix=f"edit-attribute-{attribute.id}", instance=attribute)
            if form.is_valid():
                saved_attribute = form.save(commit=False)
                saved_attribute.group_label = (saved_attribute.label or "").strip()
                saved_attribute.save()
                log_ifrs9_template_audit(request.user, "update_attribute", template, f"Updated attribute '{attribute.label}' from the IFRS9 builder.")
                messages.success(request, f"Attribute '{attribute.label}' updated.")
                return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#attribute-{attribute.id}")
            form_state[("edit_attribute", attribute.id)] = form
            form_state["active_anchor"] = f"attribute-{attribute.id}"
        elif action == "delete_attribute":
            attribute = get_object_or_404(IFRS9Attribute, id=request.POST.get("attribute_id"), risk_driver__section__template=template)
            attr_label = attribute.label
            driver_id = attribute.risk_driver_id
            _soft_delete_ifrs9_attribute(attribute)
            log_ifrs9_template_audit(request.user, "delete_attribute", template, f"Deleted attribute '{attr_label}' from the IFRS9 builder.")
            messages.success(request, f"Attribute '{attr_label}' deleted.")
            return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#driver-{driver_id}")
        elif action == "add_option":
            attribute = get_object_or_404(IFRS9Attribute, id=request.POST.get("attribute_id"), risk_driver__section__template=template)
            form = IFRS9OptionForm(request.POST, prefix=f"option-{attribute.id}")
            if form.is_valid():
                option = form.save(commit=False)
                option.attribute = attribute
                if not option.value:
                    option.value = option.label
                option.save()
                log_ifrs9_template_audit(request.user, "create_option", template, f"Created option '{option.label}' from the IFRS9 builder.")
                messages.success(request, f"Option '{option.label}' added under attribute '{attribute.label}'.")
                return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#add-option-{attribute.id}")
            form_state[("option", attribute.id)] = form
            form_state["active_anchor"] = f"add-option-{attribute.id}"
        elif action == "update_option":
            option = get_object_or_404(IFRS9Option, id=request.POST.get("option_id"), attribute__risk_driver__section__template=template)
            form = IFRS9OptionForm(request.POST, prefix=f"edit-option-{option.id}", instance=option)
            if form.is_valid():
                saved_option = form.save(commit=False)
                if not saved_option.value:
                    saved_option.value = saved_option.label
                saved_option.save()
                log_ifrs9_template_audit(request.user, "update_option", template, f"Updated option '{saved_option.label}' from the IFRS9 builder.")
                messages.success(request, f"Option '{saved_option.label}' updated.")
                return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#attribute-{saved_option.attribute_id}")
            form_state[("edit_option", option.id)] = form
            form_state["active_anchor"] = f"attribute-{option.attribute_id}"
        elif action == "delete_option":
            option = get_object_or_404(IFRS9Option, id=request.POST.get("option_id"), attribute__risk_driver__section__template=template)
            option_label = option.label
            attribute_id = option.attribute_id
            option.is_deleted = True
            option.save(update_fields=["is_deleted"])
            log_ifrs9_template_audit(request.user, "delete_option", template, f"Deleted option '{option_label}' from the IFRS9 builder.")
            messages.success(request, f"Option '{option_label}' deleted.")
            return redirect(f"{reverse('scorecard:ifrs9_template_builder', kwargs={'template_id': template.id})}#attribute-{attribute_id}")

    return render(
        request,
        "ifrs9_score_config/template_builder/ifrs9_template_builder.html",
        _build_ifrs9_builder_context(template, form_state=form_state),
    )


# ============================================================================
# SECTION MANAGEMENT VIEWS
# ============================================================================

@login_required
def ifrs9_section_list_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """List all sections for a template."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    sections = template.sections.all().order_by("display_order")

    # Calculate total weight across all sections
    total_weight = Decimal("0")
    total_max_score = Decimal("0")
    for section in sections:
        total_weight += section.get_total_weight_percent()
        total_max_score += section.get_total_max_score()
    
    weight_diff = total_weight - Decimal("100")

    context = {
        "template": template,
        "sections": sections,
        "total_weight": total_weight,
        "total_max_score": total_max_score,
        "weight_diff": weight_diff,
    }

    return render(request, "ifrs9_score_config/sections_creation/section_list.html", context)


@login_required
def ifrs9_section_create_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """Create a new section for a template."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:ifrs9_template_detail", template_id=template.id)

    if request.method == "POST":
        form = IFRS9SectionForm(request.POST)
        if form.is_valid():
            section = form.save(commit=False)
            section.template = template
            section.code = _next_section_code(template)
            section.save()
            log_ifrs9_template_audit(request.user, "create_section", template, f"Created section '{section.code}'.")
            messages.success(request, f"Section '{section.code}' created successfully!")
            return redirect("scorecard:ifrs9_section_list", template_id=template.id)
    else:
        last_order = template.sections.aggregate(models.Max("display_order"))["display_order__max"]
        form = IFRS9SectionForm(initial={"display_order": (last_order or 0) + 1})

    return render(
        request,
        "ifrs9_score_config/sections_creation/section_edit.html",
        {"form": form, "template": template, "is_new": True},
    )


@login_required
def ifrs9_section_edit_view(request: HttpRequest, template_id: int, section_id: int) -> HttpResponse:
    """Edit an existing section."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:ifrs9_section_list", template_id=template.id)

    if request.method == "POST":
        form = IFRS9SectionForm(request.POST, instance=section)
        if form.is_valid():
            form.save()
            log_ifrs9_template_audit(request.user, "update_section", template, f"Updated section '{section.code}'.")
            messages.success(request, f"Section '{section.code}' updated successfully!")
            return redirect("scorecard:ifrs9_section_list", template_id=template.id)
    else:
        form = IFRS9SectionForm(instance=section)

    return render(
        request,
        "ifrs9_score_config/sections_creation/section_edit.html",
        {"form": form, "template": template, "section": section, "is_new": False},
    )


@login_required
def ifrs9_section_delete_view(request: HttpRequest, template_id: int, section_id: int) -> HttpResponse:
    """Delete a section."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:ifrs9_section_list", template_id=template.id)

    if request.method == "POST":
        section_code = section.code
        _soft_delete_ifrs9_section(section)
        log_ifrs9_template_audit(request.user, "delete_section", template, f"Deleted section '{section_code}'.")
        messages.success(request, f"Section '{section_code}' deleted successfully!")
        return redirect("scorecard:ifrs9_section_list", template_id=template.id)

    return render(
        request,
        "ifrs9_score_config/sections_creation/section_delete.html",
        {"template": template, "section": section},
    )


# ============================================================================
# RISK DRIVER MANAGEMENT VIEWS
# ============================================================================

@login_required
def ifrs9_risk_driver_list_view(request: HttpRequest, template_id: int, section_id: int) -> HttpResponse:
    """List all risk drivers for a section."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    risk_drivers = section.risk_drivers.all().order_by("display_order")

    context = {
        "template": template,
        "section": section,
        "risk_drivers": risk_drivers,
    }

    return render(request, "ifrs9_score_config/sections_creation/risk_driver_list.html", context)


@login_required
def ifrs9_risk_driver_create_view(request: HttpRequest, template_id: int, section_id: int) -> HttpResponse:
    """Create a new risk driver for a section."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:ifrs9_risk_driver_list", template_id=template.id, section_id=section.id)

    if request.method == "POST":
        form = IFRS9RiskDriverForm(request.POST)
        if form.is_valid():
            risk_driver = form.save(commit=False)
            risk_driver.section = section
            risk_driver.code = _next_risk_driver_code(section)
            risk_driver.max_score = Decimal("0")
            risk_driver.weight_percent = Decimal("0")
            risk_driver.save()
            log_ifrs9_template_audit(request.user, "create_risk_driver", template, f"Created risk driver '{risk_driver.code}' in section '{section.code}'.")
            messages.success(request, f"Risk driver '{risk_driver.code}' created successfully!")
            return redirect("scorecard:ifrs9_risk_driver_list", template_id=template.id, section_id=section.id)
    else:
        last_order = section.risk_drivers.aggregate(models.Max("display_order"))["display_order__max"]
        form = IFRS9RiskDriverForm(initial={"display_order": (last_order or 0) + 1})

    return render(
        request,
        "ifrs9_score_config/sections_creation/risk_driver_edit.html",
        {"form": form, "template": template, "section": section, "is_new": True},
    )


@login_required
def ifrs9_risk_driver_edit_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int
) -> HttpResponse:
    """Edit an existing risk driver."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    risk_driver = get_object_or_404(IFRS9RiskDriver, id=driver_id, section=section)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:ifrs9_risk_driver_list", template_id=template.id, section_id=section.id)

    if request.method == "POST":
        form = IFRS9RiskDriverForm(request.POST, instance=risk_driver)
        if form.is_valid():
            form.save()
            log_ifrs9_template_audit(request.user, "update_risk_driver", template, f"Updated risk driver '{risk_driver.code}'.")
            messages.success(request, f"Risk driver '{risk_driver.code}' updated successfully!")
            return redirect("scorecard:ifrs9_risk_driver_list", template_id=template.id, section_id=section.id)
    else:
        form = IFRS9RiskDriverForm(instance=risk_driver)

    return render(
        request,
        "ifrs9_score_config/sections_creation/risk_driver_edit.html",
        {"form": form, "template": template, "section": section, "risk_driver": risk_driver, "is_new": False},
    )


@login_required
def ifrs9_risk_driver_delete_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int
) -> HttpResponse:
    """Delete a risk driver."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    risk_driver = get_object_or_404(IFRS9RiskDriver, id=driver_id, section=section)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:ifrs9_risk_driver_list", template_id=template.id, section_id=section.id)

    if request.method == "POST":
        driver_code = risk_driver.code
        _soft_delete_ifrs9_driver(risk_driver)
        log_ifrs9_template_audit(request.user, "delete_risk_driver", template, f"Deleted risk driver '{driver_code}'.")
        messages.success(request, f"Risk driver '{driver_code}' deleted successfully!")
        return redirect("scorecard:ifrs9_risk_driver_list", template_id=template.id, section_id=section.id)

    return render(
        request,
        "ifrs9_score_config/sections_creation/risk_driver_delete.html",
        {"template": template, "section": section, "risk_driver": risk_driver},
    )


# ============================================================================
# ATTRIBUTE MANAGEMENT VIEWS
# ============================================================================

@login_required
def ifrs9_attribute_list_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int
) -> HttpResponse:
    """List all attributes for a risk driver."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    risk_driver = get_object_or_404(IFRS9RiskDriver, id=driver_id, section=section)
    attributes = risk_driver.attributes.all().order_by("display_order")

    context = {
        "template": template,
        "section": section,
        "risk_driver": risk_driver,
        "attributes": attributes,
    }

    return render(request, "ifrs9_score_config/sections_creation/attribute_list.html", context)


@login_required
def ifrs9_attribute_create_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int
) -> HttpResponse:
    """Create a new attribute for a risk driver."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    risk_driver = get_object_or_404(IFRS9RiskDriver, id=driver_id, section=section)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:ifrs9_attribute_list", template_id=template.id, section_id=section.id, driver_id=driver_id)

    if request.method == "POST":
        form = IFRS9AttributeForm(request.POST)
        if form.is_valid():
            attribute = form.save(commit=False)
            attribute.risk_driver = risk_driver
            
            # Auto-generate code if not provided
            if not attribute.code:
                import re
                label = attribute.label.strip()
                words = re.findall(r'\b\w', label)
                base_code = ''.join(words).upper()[:50]
                if not base_code:
                    base_code = f"ATTR_{attribute.display_order}"
                
                counter = 1
                code = base_code
                while IFRS9Attribute.objects.filter(risk_driver=risk_driver, code=code).exists():
                    code = f"{base_code}_{counter}"
                    counter += 1
                attribute.code = code
            
            attribute.data_type = "choice"
            attribute.input_type = attribute.input_type or "radio"
            attribute.is_required = True
            
            attribute.save()
            log_ifrs9_template_audit(request.user, "create_attribute", template, f"Created attribute '{attribute.label}' in risk driver '{risk_driver.code}'.")
            messages.success(request, f"Attribute '{attribute.label}' created successfully!")
            return redirect(
                "scorecard:ifrs9_attribute_list",
                template_id=template.id,
                section_id=section.id,
                driver_id=risk_driver.id,
            )
    else:
        last_order = risk_driver.attributes.aggregate(models.Max("display_order"))["display_order__max"]
        form = IFRS9AttributeForm(initial={"display_order": (last_order or 0) + 1})

    return render(
        request,
        "ifrs9_score_config/sections_creation/attribute_edit.html",
        {"form": form, "template": template, "section": section, "risk_driver": risk_driver, "is_new": True},
    )


@login_required
def ifrs9_attribute_edit_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int, attribute_id: int
) -> HttpResponse:
    """Edit an existing attribute."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    risk_driver = get_object_or_404(IFRS9RiskDriver, id=driver_id, section=section)
    attribute = get_object_or_404(IFRS9Attribute, id=attribute_id, risk_driver=risk_driver)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:ifrs9_attribute_list", template_id=template.id, section_id=section.id, driver_id=driver_id)

    if request.method == "POST":
        form = IFRS9AttributeForm(request.POST, instance=attribute)
        if form.is_valid():
            form.save()
            log_ifrs9_template_audit(request.user, "update_attribute", template, f"Updated attribute '{attribute.label}'.")
            messages.success(request, f"Attribute '{attribute.label}' updated successfully!")
            return redirect(
                "scorecard:ifrs9_attribute_list",
                template_id=template.id,
                section_id=section.id,
                driver_id=risk_driver.id,
            )
    else:
        form = IFRS9AttributeForm(instance=attribute)

    return render(
        request,
        "ifrs9_score_config/sections_creation/attribute_edit.html",
        {
            "form": form,
            "template": template,
            "section": section,
            "risk_driver": risk_driver,
            "attribute": attribute,
            "is_new": False,
        },
    )


@login_required
def ifrs9_attribute_delete_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int, attribute_id: int
) -> HttpResponse:
    """Delete an attribute."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    risk_driver = get_object_or_404(IFRS9RiskDriver, id=driver_id, section=section)
    attribute = get_object_or_404(IFRS9Attribute, id=attribute_id, risk_driver=risk_driver)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:ifrs9_attribute_list", template_id=template.id, section_id=section.id, driver_id=driver_id)

    if request.method == "POST":
        attr_label = attribute.label
        _soft_delete_ifrs9_attribute(attribute)
        log_ifrs9_template_audit(request.user, "delete_attribute", template, f"Deleted attribute '{attr_label}'.")
        messages.success(request, f"Attribute '{attr_label}' deleted successfully!")
        return redirect(
            "scorecard:ifrs9_attribute_list",
            template_id=template.id,
            section_id=section.id,
            driver_id=risk_driver.id,
        )

    return render(
        request,
        "ifrs9_score_config/sections_creation/attribute_delete.html",
        {"template": template, "section": section, "risk_driver": risk_driver, "attribute": attribute},
    )


# ============================================================================
# OPTION MANAGEMENT VIEWS
# ============================================================================

@login_required
def ifrs9_option_list_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int, attribute_id: int
) -> HttpResponse:
    """List all options for an attribute."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    risk_driver = get_object_or_404(IFRS9RiskDriver, id=driver_id, section=section)
    attribute = get_object_or_404(IFRS9Attribute, id=attribute_id, risk_driver=risk_driver)
    options = attribute.options.all().order_by("display_order")

    context = {
        "template": template,
        "section": section,
        "risk_driver": risk_driver,
        "attribute": attribute,
        "options": options,
    }

    return render(request, "ifrs9_score_config/sections_creation/option_list.html", context)


@login_required
def ifrs9_option_create_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int, attribute_id: int
) -> HttpResponse:
    """Create a new option for an attribute."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    risk_driver = get_object_or_404(IFRS9RiskDriver, id=driver_id, section=section)
    attribute = get_object_or_404(IFRS9Attribute, id=attribute_id, risk_driver=risk_driver)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:ifrs9_option_list", template_id=template.id, section_id=section.id, driver_id=driver_id, attribute_id=attribute_id)

    if request.method == "POST":
        form = IFRS9OptionForm(request.POST)
        if form.is_valid():
            option = form.save(commit=False)
            option.attribute = attribute
            if not option.value:
                option.value = option.label
            option.save()
            log_ifrs9_template_audit(request.user, "create_option", template, f"Created option '{option.label}' for attribute '{attribute.label}'.")
            messages.success(request, f"Option '{option.label}' created successfully!")
            return redirect(
                "scorecard:ifrs9_option_list",
                template_id=template.id,
                section_id=section.id,
                driver_id=risk_driver.id,
                attribute_id=attribute.id,
            )
    else:
        last_order = attribute.options.aggregate(models.Max("display_order"))["display_order__max"]
        form = IFRS9OptionForm(initial={"display_order": (last_order or 0) + 1, "allocated_score": 0})

    return render(
        request,
        "ifrs9_score_config/sections_creation/option_edit.html",
        {
            "form": form,
            "template": template,
            "section": section,
            "risk_driver": risk_driver,
            "attribute": attribute,
            "is_new": True,
        },
    )


@login_required
def ifrs9_option_edit_view(
    request: HttpRequest,
    template_id: int,
    section_id: int,
    driver_id: int,
    attribute_id: int,
    option_id: int,
) -> HttpResponse:
    """Edit an existing option."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    risk_driver = get_object_or_404(IFRS9RiskDriver, id=driver_id, section=section)
    attribute = get_object_or_404(IFRS9Attribute, id=attribute_id, risk_driver=risk_driver)
    option = get_object_or_404(IFRS9Option, id=option_id, attribute=attribute)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:ifrs9_option_list", template_id=template.id, section_id=section.id, driver_id=driver_id, attribute_id=attribute_id)

    if request.method == "POST":
        form = IFRS9OptionForm(request.POST, instance=option)
        if form.is_valid():
            form.save()
            log_ifrs9_template_audit(request.user, "update_option", template, f"Updated option '{option.label}'.")
            messages.success(request, f"Option '{option.label}' updated successfully!")
            return redirect(
                "scorecard:ifrs9_option_list",
                template_id=template.id,
                section_id=section.id,
                driver_id=risk_driver.id,
                attribute_id=attribute.id,
            )
    else:
        form = IFRS9OptionForm(instance=option)

    return render(
        request,
        "ifrs9_score_config/sections_creation/option_edit.html",
        {
            "form": form,
            "template": template,
            "section": section,
            "risk_driver": risk_driver,
            "attribute": attribute,
            "option": option,
            "is_new": False,
        },
    )


@login_required
def ifrs9_option_delete_view(
    request: HttpRequest,
    template_id: int,
    section_id: int,
    driver_id: int,
    attribute_id: int,
    option_id: int,
) -> HttpResponse:
    """Delete an option."""
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    section = get_object_or_404(IFRS9Section, id=section_id, template=template)
    risk_driver = get_object_or_404(IFRS9RiskDriver, id=driver_id, section=section)
    attribute = get_object_or_404(IFRS9Attribute, id=attribute_id, risk_driver=risk_driver)
    option = get_object_or_404(IFRS9Option, id=option_id, attribute=attribute)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:ifrs9_option_list", template_id=template.id, section_id=section.id, driver_id=driver_id, attribute_id=attribute_id)

    if request.method == "POST":
        option_label = option.label
        option.is_deleted = True
        option.save(update_fields=["is_deleted"])
        log_ifrs9_template_audit(request.user, "delete_option", template, f"Deleted option '{option_label}'.")
        messages.success(request, f"Option '{option_label}' deleted successfully!")
        return redirect(
            "scorecard:ifrs9_option_list",
            template_id=template.id,
            section_id=section.id,
            driver_id=risk_driver.id,
            attribute_id=attribute.id,
        )

    return render(
        request,
        "ifrs9_score_config/sections_creation/option_delete.html",
        {
            "template": template,
            "section": section,
            "risk_driver": risk_driver,
            "attribute": attribute,
            "option": option,
        },
    )


class IFRS9GradeBandForm(forms.ModelForm):
    class Meta:
        model = IFRS9GradeBand
        fields = ["grade_code", "description", "min_percent", "max_percent", "display_order"]
        widgets = {
            "grade_code": forms.TextInput(attrs={"style": "width: 100%;"}),
            "description": forms.TextInput(attrs={"style": "width: 100%;"}),
            "min_percent": forms.NumberInput(attrs={"step": "0.01", "style": "width: 100%;"}),
            "max_percent": forms.NumberInput(attrs={"step": "0.01", "style": "width: 100%;"}),
            "display_order": forms.NumberInput(attrs={"style": "width: 100%;"}),
        }
        help_texts = {
            "grade_code": "Grade code (e.g., A1, A2, B1, C, D, E). Saved as superscript style such as A¹, A², B¹.",
            "description": "Description of the grade range (e.g., '85% & Above', '80% - 84%')",
            "min_percent": "Minimum percentage for this grade (inclusive)",
            "max_percent": "Maximum percentage for this grade (inclusive)",
            "display_order": "Order in which this grade should appear (lower numbers first)",
        }


@login_required
def ifrs9_grade_band_list_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    List all grade bands for an IFRS9 template.
    """
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    grade_bands = template.grade_bands.all().order_by("display_order")

    context = {
        "template": template,
        "grade_bands": grade_bands,
    }

    return render(
        request,
        "ifrs9_score_config/grade_band_list.html",
        context,
    )


@login_required
def ifrs9_grade_band_create_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Create a new grade band for an IFRS9 template.
    """
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)

    if request.method == "POST":
        form = IFRS9GradeBandForm(request.POST)
        if form.is_valid():
            grade_band = form.save(commit=False)
            grade_band.template = template
            grade_band.save()
            return redirect("scorecard:ifrs9_grade_band_list", template_id=template.id)
    else:
        # Set default display_order to be after the last one
        last_order = template.grade_bands.aggregate(models.Max("display_order"))["display_order__max"]
        form = IFRS9GradeBandForm(initial={"display_order": (last_order or 0) + 1})

    return render(
        request,
        "ifrs9_score_config/grade_band_edit.html",
        {"form": form, "template": template, "is_new": True},
    )


@login_required
def ifrs9_grade_band_edit_view(request: HttpRequest, template_id: int, band_id: int) -> HttpResponse:
    """
    Edit an existing IFRS9 grade band.
    """
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    grade_band = get_object_or_404(IFRS9GradeBand, id=band_id, template=template)

    if request.method == "POST":
        form = IFRS9GradeBandForm(request.POST, instance=grade_band)
        if form.is_valid():
            form.save()
            return redirect("scorecard:ifrs9_grade_band_list", template_id=template.id)
    else:
        form = IFRS9GradeBandForm(instance=grade_band)

    return render(
        request,
        "ifrs9_score_config/grade_band_edit.html",
        {"form": form, "template": template, "grade_band": grade_band, "is_new": False},
    )


@login_required
def ifrs9_grade_band_delete_view(request: HttpRequest, template_id: int, band_id: int) -> HttpResponse:
    """
    Delete an IFRS9 grade band.
    """
    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id)
    grade_band = get_object_or_404(IFRS9GradeBand, id=band_id, template=template)

    if request.method == "POST":
        grade_band.delete()
        return redirect("scorecard:ifrs9_grade_band_list", template_id=template.id)

    return render(
        request,
        "ifrs9_score_config/grade_band_delete.html",
        {"template": template, "grade_band": grade_band},
    )
