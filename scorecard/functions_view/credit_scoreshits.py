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

from scorecard.functions_view.audit import log_basel_template_audit
from scorecard.functions_view.basel_scores_form import _build_configuration_from_version
from scorecard.models import (
    Attribute,
    AttributeResponse,
    BaselScoreSheetTemplate,
    CreditEvaluation,
    GradeBand,
    Option,
    RiskDriver,
    RiskDriverScore,
    Section,
    SectionScore,
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
        existing = BaselScoreSheetTemplate.objects.filter(code=candidate)
        if template_id is not None:
            existing = existing.exclude(id=template_id)
        if not existing.exists():
            return candidate
        suffix += 1


class FormulaField(forms.CharField):
    """Custom field for formula input with variable selection."""
    
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", forms.TextInput(attrs={
            "style": "width: 100%;",
            "class": "formula-input",
            "list": "formula-variables",
        }))
        super().__init__(*args, **kwargs)


class BaselScoreSheetTemplateForm(forms.ModelForm):
    class Meta:
        model = BaselScoreSheetTemplate
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


def _get_reference_template() -> BaselScoreSheetTemplate | None:
    """
    Get the reference template (AFCSC1-17 - Farmers Credit Scoresheet).
    This is the master template that new templates can clone from.
    """
    try:
        return BaselScoreSheetTemplate.objects.prefetch_related(
            'sections__risk_drivers__attributes__options',
            'grade_bands'
        ).get(code="AFCSC1-17")
    except BaselScoreSheetTemplate.DoesNotExist:
        return None


def _get_active_template() -> BaselScoreSheetTemplate | None:
    """
    Returns the active Basel score sheet template for Farmers Credit Scoresheet.
    For now we simply take the first active template. Later we can filter by code.
    """

    return BaselScoreSheetTemplate.objects.filter(is_active=True).order_by("id").first()


def _build_configuration(
    template: BaselScoreSheetTemplate,
    use_approved_version: bool = False,
) -> Tuple[List[Section], Dict[int, List[RiskDriver]], Dict[int, List[Attribute]]]:
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
            return _build_configuration_from_version(approved_version)
        # If no approved version exists, fall back to current structure
    
    # Build from current template structure
    sections = list(template.sections.all().order_by("display_order", "id"))

    drivers_by_section: Dict[int, List[RiskDriver]] = {}
    attributes_by_driver: Dict[int, List[Attribute]] = {}

    drivers = (
        RiskDriver.objects.filter(section__template=template)
        .select_related("section")
        .order_by("display_order", "id")
    )
    attrs = (
        Attribute.objects.filter(risk_driver__section__template=template)
        .select_related("risk_driver")
        .prefetch_related("options")
        .order_by("display_order", "id")
    )

    for driver in drivers:
        drivers_by_section.setdefault(driver.section_id, []).append(driver)

    for attr in attrs:
        attributes_by_driver.setdefault(attr.risk_driver_id, []).append(attr)

    return sections, drivers_by_section, attributes_by_driver


def _field_name_for_attribute(attribute: Attribute) -> str:
    return f"attr_{attribute.id}"


@login_required
def basel_template_list_view(request: HttpRequest) -> HttpResponse:
    """
    Simple frontend list of Basel score sheet templates and link to create a new one.
    """

    search_query = request.GET.get("q", "").strip()
    page_size = _normalize_list_page_size(request.GET.get("page_size"))

    templates = BaselScoreSheetTemplate.objects.annotate(
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
    return render(request, "credit_scoreshifts/basel_template_list.html", context)


def _clone_template_structure(
    source_template: BaselScoreSheetTemplate, target_template: BaselScoreSheetTemplate
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
    
    print(f"\n{'='*60}")
    print(f"CLONING: {source_template.code} -> {target_template.code}")
    print(f"{'='*60}")
    
    with transaction.atomic():
        # Step 1: Clone grade bands FIRST (independent of sections)
        print(f"\n[STEP 1] Cloning Grade Bands...")
        source_bands_qs = source_template.grade_bands.all().order_by("display_order")
        source_bands = list(source_bands_qs)
        print(f"  Found {len(source_bands)} grade bands")
        
        for source_band in source_bands:
            GradeBand.objects.create(
                template=target_template,
                grade_code=source_band.grade_code,
                description=source_band.description,
                min_percent=source_band.min_percent,
                max_percent=source_band.max_percent,
                display_order=source_band.display_order,
            )
            counts['grade_bands'] += 1
            print(f"    ✓ Cloned: {source_band.grade_code} ({source_band.min_percent}% - {source_band.max_percent}%)")
        
        print(f"  [RESULT] Cloned {counts['grade_bands']} grade bands\n")

        # Step 2: Clone sections and their nested structure
        print(f"[STEP 2] Cloning Sections...")
        source_sections_qs = source_template.sections.all().order_by("display_order")
        source_sections = list(source_sections_qs)
        print(f"  Found {len(source_sections)} sections")
        
        for source_section in source_sections:
            # Create new section
            new_section = Section.objects.create(
                template=target_template,
                code=source_section.code,
                name=source_section.name,
                display_order=source_section.display_order,
            )
            counts['sections'] += 1
            print(f"  ✓ Cloned section: {source_section.code} - {source_section.name}")

            # Step 3: Clone risk drivers for this section
            source_drivers_qs = source_section.risk_drivers.all().order_by("display_order")
            source_drivers = list(source_drivers_qs)
            print(f"    Found {len(source_drivers)} risk drivers")
            
            for source_driver in source_drivers:
                # Create new risk driver
                new_driver = RiskDriver.objects.create(
                    section=new_section,
                    code=source_driver.code,
                    name=source_driver.name,
                    weight_percent=source_driver.weight_percent,
                    max_score=source_driver.max_score,
                    display_order=source_driver.display_order,
                )
                counts['drivers'] += 1
                print(f"      ✓ Cloned driver: {source_driver.code} - {source_driver.name}")

                # Step 4: Clone attributes for this driver
                source_attributes_qs = source_driver.attributes.all().order_by("display_order")
                source_attributes = list(source_attributes_qs)
                
                for source_attr in source_attributes:
                    # Create new attribute - copy ALL fields including help_text and weight_percent
                    new_attr = Attribute.objects.create(
                        risk_driver=new_driver,
                        code=source_attr.code,
                        label=source_attr.label,
                        help_text=source_attr.help_text,  # Include help_text
                        data_type=source_attr.data_type,
                        input_type=source_attr.input_type,
                        group_label=source_attr.group_label,
                        weight_percent=source_attr.weight_percent,  # Include weight_percent
                        is_required=source_attr.is_required,
                        display_order=source_attr.display_order,
                    )
                    counts['attributes'] += 1

                    # Step 5: Clone options for this attribute - copy ALL fields
                    source_options_qs = source_attr.options.all().order_by("display_order")
                    source_options = list(source_options_qs)
                    
                    for source_option in source_options:
                        # Copy ALL Option fields: label, value, allocated_score, display_order, is_default
                        Option.objects.create(
                            attribute=new_attr,
                            label=source_option.label,
                            value=source_option.value,  # Include value field (stores POST value)
                            allocated_score=source_option.allocated_score,  # The actual score data
                            display_order=source_option.display_order,
                            is_default=source_option.is_default,  # Include is_default flag
                        )
                        counts['options'] += 1
        
        print(f"\n[RESULT] Cloned {counts['sections']} sections, {counts['drivers']} drivers")
        print(f"         {counts['attributes']} attributes, {counts['options']} options")
        print(f"{'='*60}\n")
    
    return counts


@login_required
def basel_template_create_new_view(request: HttpRequest) -> HttpResponse:
    """
    Create a new Basel score sheet template with cloning support from AFCSC1-17.
    This is the dedicated view for creating new templates from the reference template.
    """
    # Get the reference template (AFCSC1-17 - Farmers Credit Scoresheet)
    reference_template = _get_reference_template()

    if request.method == "POST":
        form = BaselScoreSheetTemplateForm(request.POST)
        
        if form.is_valid():
            # Save the new template first - set as draft for maker-checker workflow
            template = form.save(commit=False)
            template.is_active = True
            template.status = 'draft'
            template.maker = request.user
            template.save()
            log_basel_template_audit(
                request.user,
                "create",
                template,
                "Basel template created from the dedicated new-template flow.",
            )

            clone_checkbox = request.POST.get("clone_from_reference")
            should_clone = clone_checkbox == "on"
            
            print(f"\n{'='*70}")
            print("TEMPLATE CREATE - OPTIONAL CLONING")
            print(f"{'='*70}")
            print(f"New template created: {template.code} (ID: {template.id})")
            print(f"Reference template exists: {reference_template is not None}")
            print(f"Clone requested: {should_clone}")
            print(f"{'='*70}\n")
            
            if reference_template and should_clone:
                print(f"✓ Proceeding with automatic cloning from reference template...")
                
                try:
                    # Re-fetch reference template with ALL relationships loaded
                    ref_template = BaselScoreSheetTemplate.objects.prefetch_related(
                        'sections',
                        'sections__risk_drivers',
                        'sections__risk_drivers__attributes',
                        'sections__risk_drivers__attributes__options',
                        'grade_bands'
                    ).get(code="AFCSC1-17")
                    
                    print(f"✓ Reference template found: {ref_template.code} (ID: {ref_template.id})")
                    
                    # Count what we're about to clone
                    ref_section_count = ref_template.sections.count()
                    ref_driver_count = RiskDriver.objects.filter(section__template=ref_template).count()
                    ref_attr_count = Attribute.objects.filter(risk_driver__section__template=ref_template).count()
                    ref_option_count = Option.objects.filter(attribute__risk_driver__section__template=ref_template).count()
                    ref_band_count = ref_template.grade_bands.count()
                    
                    print(f"  Reference has: {ref_section_count} sections, {ref_driver_count} drivers")
                    print(f"                  {ref_attr_count} attributes, {ref_option_count} options")
                    print(f"                  {ref_band_count} grade bands")
                    
                    if ref_section_count == 0:
                        messages.warning(
                            request,
                            "⚠️ Reference template (AFCSC1-17) exists but has no sections to clone. "
                            "Please run: python manage.py seed_farmers_scoresheet"
                        )
                        print("WARNING: Reference template has no sections!")
                    else:
                        print(f"\n🚀 Starting clone operation from {ref_template.code} to {template.code}...")
                        
                        # Perform the cloning operation
                        counts = _clone_template_structure(ref_template, template)
                        
                        print(f"✓ Clone function returned: {counts}")
                        
                        # Force refresh from database to get latest data
                        template.refresh_from_db()
                        
                        # Verify what was actually created in database
                        cloned_section_count = template.sections.count()
                        cloned_driver_count = RiskDriver.objects.filter(section__template=template).count()
                        cloned_attr_count = Attribute.objects.filter(risk_driver__section__template=template).count()
                        cloned_option_count = Option.objects.filter(attribute__risk_driver__section__template=template).count()
                        cloned_band_count = template.grade_bands.count()
                        
                        print(f"\n📊 Verification after clone:")
                        print(f"  Sections: {cloned_section_count} (expected {ref_section_count})")
                        print(f"  Drivers: {cloned_driver_count} (expected {ref_driver_count})")
                        print(f"  Attributes: {cloned_attr_count} (expected {ref_attr_count})")
                        print(f"  Options: {cloned_option_count} (expected {ref_option_count})")
                        print(f"  Grade Bands: {cloned_band_count} (expected {ref_band_count})")
                        
                        # Check if cloning was successful
                        if cloned_section_count == ref_section_count and cloned_band_count == ref_band_count:
                            messages.success(
                                request,
                                f"✅ Template '{template.code}' created successfully! "
                                f"Cloned {counts['sections']} sections, {counts['drivers']} risk drivers, "
                                f"{counts['attributes']} attributes, {counts['options']} options, "
                                f"and {counts['grade_bands']} grade bands from {ref_template.code}."
                            )
                            print(f"✅ SUCCESS: Cloning completed successfully!")
                        else:
                            messages.error(
                                request,
                                f"❌ Cloning incomplete! Expected {ref_section_count} sections and {ref_band_count} grade bands, "
                                f"but got {cloned_section_count} sections and {cloned_band_count} grade bands. "
                                f"Please check the console logs for details."
                            )
                            print(f"❌ ERROR: Cloning verification failed!")
                            
                except BaselScoreSheetTemplate.DoesNotExist:
                    messages.error(
                        request,
                        "❌ Reference template (AFCSC1-17) not found. Please ensure it exists and is seeded."
                    )
                    print("ERROR: Reference template DoesNotExist exception!")
                except Exception as e:
                    import traceback
                    error_trace = traceback.format_exc()
                    messages.error(request, f"❌ Error during cloning: {str(e)}")
                    print(f"❌ CLONING EXCEPTION:\n{error_trace}")
            elif reference_template and not should_clone:
                messages.success(
                    request,
                    f"Template '{template.code}' created successfully without cloning the reference template."
                )
            else:
                messages.warning(
                    request,
                    "⚠️ Reference template (AFCSC1-17) not found. Template created without cloning. "
                    "Please ensure the reference template exists and is seeded: python manage.py seed_farmers_scoresheet"
                )
                print("WARNING: Reference template not found - template created without cloning")
            
            # New templates are always created as active - no need to deactivate others
            
            return redirect("scorecard:basel_template_list")
    else:
        # GET request - show form
        form = BaselScoreSheetTemplateForm()
        
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
        "credit_scoreshifts/basel_scoreshits_template.html",
        context,
    )


@login_required
def basel_template_create_view(request: HttpRequest) -> HttpResponse:
    """
    Frontend form to create a new Basel score sheet template (BaselScoreSheetTemplate).
    Supports cloning from the reference template (AFCSC1-17) when checkbox is checked.
    """

    reference_template = _get_reference_template()

    if request.method == "POST":
        form = BaselScoreSheetTemplateForm(request.POST)
        
        if form.is_valid():
            # Save the new template first - set as draft for maker-checker workflow
            template = form.save(commit=False)
            template.is_active = True
            template.status = 'draft'
            template.maker = request.user
            template.save()
            log_basel_template_audit(
                request.user,
                "create",
                template,
                "Basel template created from the standard template form.",
            )
            
            # Check if cloning checkbox was checked
            # Checkbox sends "on" when checked, or is missing when unchecked
            clone_checkbox = request.POST.get("clone_from_reference")
            should_clone = clone_checkbox == "on"
            
            # Always try to clone if reference template exists and checkbox was checked
            if should_clone:
                try:
                    # Get reference template fresh from database with all relationships
                    ref_template = BaselScoreSheetTemplate.objects.select_related().prefetch_related(
                        'sections',
                        'sections__risk_drivers',
                        'sections__risk_drivers__attributes',
                        'sections__risk_drivers__attributes__options',
                        'grade_bands'
                    ).get(code="AFCSC1-17")
                    
                    # Check if reference has data
                    section_count_before = ref_template.sections.count()
                    
                    if section_count_before > 0:
                        # Perform cloning
                        counts = _clone_template_structure(ref_template, template)
                        
                        # Verify what was created
                        section_count_after = template.sections.count()
                        driver_count_after = RiskDriver.objects.filter(section__template=template).count()
                        band_count_after = template.grade_bands.count()
                        
                        if section_count_after == section_count_before:
                            messages.success(
                                request,
                                f"✅ Template created successfully! Cloned {counts['sections']} sections, "
                                f"{counts['drivers']} risk drivers, {counts['attributes']} attributes, "
                                f"{counts['options']} options, and {counts['grade_bands']} grade bands from {ref_template.code}."
                            )
                        else:
                            messages.warning(
                                request,
                                f"Template created, but cloning may be incomplete. "
                                f"Expected {section_count_before} sections, got {section_count_after}."
                            )
                    else:
                        messages.warning(
                            request,
                            "Reference template (AFCSC1-17) has no sections to clone. "
                            "Please run: python manage.py seed_farmers_scoresheet"
                        )
                        
                except BaselScoreSheetTemplate.DoesNotExist:
                    messages.error(
                        request,
                        "Reference template (AFCSC1-17) not found. Cannot clone."
                    )
                except Exception as e:
                    import traceback
                    error_msg = f"Error during cloning: {str(e)}"
                    messages.error(request, error_msg)
                    print(f"CLONING ERROR:\n{traceback.format_exc()}")
            
            # New templates are always created as active - no need to deactivate others
            
            return redirect("scorecard:basel_template_list")
    else:
        form = BaselScoreSheetTemplateForm()
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
        "credit_scoreshifts/basel_template_create.html",
        context,
    )


@login_required
def basel_template_detail_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    View to see the full configuration of a Basel template including:
    - Template details and formulas
    - Sections
    - Risk Drivers
    - Attributes
    - Options (with allocated scores)
    - Grade bands
    
    IMPORTANT: This view shows ONLY APPROVED template information.
    Used from the main template list - shows what questionnaires will use.
    Does NOT show pending/unapproved changes.
    
    For viewing pending changes (maker drafts), use template_maker_view instead.
    """
    template = get_object_or_404(
        BaselScoreSheetTemplate.objects.prefetch_related(
            "sections__risk_drivers__attributes__options"
        ),
        id=template_id,
    )

    # CRITICAL: Use approved template version structure for detail view
    # This ensures users see only approved changes, not pending unapproved changes
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=True)
    
    # Get approved template metadata and formulas from the approved version snapshot (if exists)
    # This ensures template info shown is from approved version, not pending changes
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
    # This ensures allocated scores shown are from approved version, not pending changes
    approved_options_by_attribute = {}
    approved_max_scores_by_attribute = {}
    approved_attribute_weights = {}  # attribute.id -> approved weight_percent
    approved_driver_weights = {}  # driver.id -> approved weight_percent
    if approved_version:
        # Build mapping: attribute.id -> list of approved options with their allocated scores
        for section_version in approved_version.sections.all():
            for driver_version in section_version.risk_drivers.all():
                # Store approved driver weight
                approved_driver_weights[driver_version.risk_driver.id] = driver_version.weight_percent
                
                for attr_version in driver_version.attributes.all():
                    # Store approved attribute weight
                    approved_attribute_weights[attr_version.attribute.id] = attr_version.weight_percent
                    
                    approved_options_by_attribute[attr_version.attribute.id] = []
                    max_score = None
                    for option_version in attr_version.options.all().order_by('display_order'):
                        # Create a dict-like object to store approved option data
                        # We'll use the Option object but override allocated_score when displaying
                        allocated_score = option_version.allocated_score
                        approved_options_by_attribute[attr_version.attribute.id].append({
                            'option': option_version.option,
                            'allocated_score': allocated_score,  # Use approved score from snapshot
                            'label': option_version.label,
                            'display_order': option_version.display_order,
                        })
                        # Track max score
                        if max_score is None or allocated_score > max_score:
                            max_score = allocated_score
                    approved_max_scores_by_attribute[attr_version.attribute.id] = max_score if max_score is not None else 0
        
        # CRITICAL: Always use approved version values when it exists, even if fields are empty
        # This ensures we show approved state, not pending changes
        # Use getattr to safely access fields that might not exist in DB yet (before migration)
        if hasattr(approved_version, 'template_code'):
            # Use approved code even if empty (empty means it was empty when approved)
            approved_template_info['code'] = approved_version.template_code or template.code
        if hasattr(approved_version, 'template_name'):
            # Use approved name even if empty
            approved_template_info['name'] = approved_version.template_name or template.name
        if hasattr(approved_version, 'template_description'):
            # Use approved description (can be empty)
            approved_template_info['description'] = approved_version.template_description or ""
        if hasattr(approved_version, 'template_version'):
            # Use approved version string
            approved_template_info['version'] = approved_version.template_version or "N/A"
        
        # CRITICAL: Always use approved formulas when approved version exists
        # This ensures formulas shown are from approved state, not pending changes
        if hasattr(approved_version, 'formula_actual_score'):
            # Use approved formula even if empty (will fall back to default in template)
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
        "approved_options_by_attribute": approved_options_by_attribute,  # Pass approved options mapping
        "approved_max_scores_by_attribute": approved_max_scores_by_attribute,  # Pass max scores for approved options
        "approved_attribute_weights": approved_attribute_weights,  # Pass approved attribute weights
        "approved_driver_weights": approved_driver_weights,  # Pass approved driver weights
        "grade_bands": grade_bands,
        "approved_formulas": approved_formulas,
        "approved_template_info": approved_template_info,
        "approved_version": approved_version,  # Pass approved_version for template logic
        "section_totals": section_totals,
        "overall_totals": overall_totals,
        "workflow_history": workflow_history,
    }

    template_name = "credit_scoreshifts/basel_template_detail.html"
    if getattr(request.resolver_match, "url_name", "") == "checker_my_approvals_basel_template_view":
        template_name = "maker_checker/checker_basel_template_approval_view.html"

    return render(
        request,
        template_name,
        context,
    )


def _ensure_basel_template_edit_started(template, user):
    """Start or claim a Basel edit cycle only after the user saves a change."""
    update_fields = []
    if template.status in ['approved', None, '']:
        if template.status == 'approved' and not template.versions.filter(is_approved=True).exists():
            from scorecard.functions_view.template_maker_checker import _create_template_version
            version_number = (template.versions.order_by('-version_number').first().version_number + 1) if template.versions.exists() else 1
            baseline = _create_template_version(
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

    # Clear ownership from the previous review cycle so the draft appears in
    # the current editor's maker queue.
    if template.submitted_by_id is not None:
        template.submitted_by = None
        update_fields.append('submitted_by')
    if template.submitted_at is not None:
        template.submitted_at = None
        update_fields.append('submitted_at')

    if update_fields:
        update_fields.append('updated_at')
        template.save(update_fields=list(dict.fromkeys(update_fields)))

@login_required
def basel_template_edit_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """Edit template details including formulas."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    
    # Check if template can be edited
    if not template.can_be_edited_by(request.user):
        messages.error(request, "This template cannot be edited. It must be in draft, in_progress, returned, or approved status.")
        return redirect("scorecard:basel_template_detail", template_id=template.id)

    if request.method == "POST":
        form = BaselScoreSheetTemplateForm(request.POST, instance=template)
        if form.is_valid():
            _ensure_basel_template_edit_started(template, request.user)
            form.save()
            log_basel_template_audit(
                request.user,
                "update",
                template,
                "Basel template details and formulas updated.",
            )
            messages.success(request, f"Template '{template.code}' updated successfully!")
            return redirect("scorecard:basel_template_list")
    else:
        form = BaselScoreSheetTemplateForm(instance=template)

    return render(
        request,
        "credit_scoreshifts/basel_template_edit.html",
        {"form": form, "template": template},
    )


@login_required
def basel_template_delete_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """Delete a template."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    
    # Deletion permission is broader than edit permission: allow maker/superuser to
    # permanently delete templates even when status is approved/cancelled.
    can_manage_template = (
        request.user.is_superuser
        or request.user.has_perm("scorecard.manage_basel_templates")
        or template.maker == request.user
        or template.maker is None
    )
    if not can_manage_template:
        messages.error(request, "You don't have permission to delete this template.")
        return redirect("scorecard:basel_template_detail", template_id=template.id)
    
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
            log_basel_template_audit(
                request.user,
                "archive",
                template,
                f"Template archived because {evaluation_count} evaluation(s) are linked to it. Archived code: {archived_code}.",
            )
            messages.success(
                request,
                f"Template '{template_code}' has been archived as '{archived_code}' because it has {evaluation_count} associated evaluation(s). It is now inactive and will not be used for new evaluations.",
            )
        else:
            try:
                log_basel_template_audit(
                    request.user,
                    "delete",
                    template,
                    "Basel template permanently deleted.",
                )
                template.delete()
                messages.success(request, f"Template '{template_code}' permanently deleted successfully!")
            except ProtectedError:
                archived_code = template.code if (template.status == "cancelled" and not template.is_active) else _generate_archived_template_code(template.code, template.id)
                template.is_active = False
                template.status = "cancelled"
                template.code = archived_code
                template.save(update_fields=["is_active", "status", "code", "updated_at"])
                log_basel_template_audit(
                    request.user,
                    "archive",
                    template,
                    f"Template archived because protected version history or template components still reference it. Archived code: {archived_code}.",
                )
                messages.warning(
                    request,
                    f"Template '{template_code}' could not be permanently deleted because version history or template components still reference it. It has been archived as '{archived_code}' instead and will not be used for new evaluations.",
                )
        return redirect("scorecard:basel_template_list")
    
    return render(
        request,
        "credit_scoreshifts/basel_template_delete.html",
        {
            "template": template,
            "evaluation_count": evaluation_count,
            "can_delete": can_delete,
        },
    )


class OptionEditForm(forms.ModelForm):
    class Meta:
        model = Option
        fields = ["allocated_score"]
        widgets = {
            "allocated_score": forms.NumberInput(
                attrs={"step": "0.01", "style": "width: 100px;"}
            ),
        }


@login_required
def option_edit_view(request: HttpRequest, template_id: int, option_id: int) -> HttpResponse:
    """
    Edit the allocated score for an option.
    """
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    option = get_object_or_404(Option, id=option_id)

    if request.method == "POST":
        form = OptionEditForm(request.POST, instance=option)
        if form.is_valid():
            form.save()
            log_basel_template_audit(
                request.user,
                "update_option_score",
                template,
                f"Updated allocated score for option '{option.label}'.",
            )
            return redirect("scorecard:basel_template_list")
    else:
        form = OptionEditForm(instance=option)

    return render(
        request,
        "credit_scoreshifts/option_edit.html",
        {"form": form, "template": template, "option": option},
    )


class GradeBandForm(forms.ModelForm):
    class Meta:
        model = GradeBand
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
def grade_band_list_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    List all grade bands for a template.
    """
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    grade_bands = template.grade_bands.all().order_by("display_order")

    context = {
        "template": template,
        "grade_bands": grade_bands,
    }

    return render(
        request,
        "credit_scoreshifts/grade_band_list.html",
        context,
    )


@login_required
def grade_band_create_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Create a new grade band for a template.
    """
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)

    if request.method == "POST":
        form = GradeBandForm(request.POST)
        if form.is_valid():
            grade_band = form.save(commit=False)
            grade_band.template = template
            grade_band.save()
            log_basel_template_audit(
                request.user,
                "create_grade_band",
                template,
                f"Created grade band '{grade_band.grade_code}'.",
            )
            return redirect("scorecard:grade_band_list", template_id=template.id)
    else:
        # Set default display_order to be after the last one
        last_order = template.grade_bands.aggregate(models.Max("display_order"))["display_order__max"]
        form = GradeBandForm(initial={"display_order": (last_order or 0) + 1})

    return render(
        request,
        "credit_scoreshifts/grade_band_edit.html",
        {"form": form, "template": template, "is_new": True},
    )


@login_required
def grade_band_edit_view(request: HttpRequest, template_id: int, band_id: int) -> HttpResponse:
    """
    Edit an existing grade band.
    """
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    grade_band = get_object_or_404(GradeBand, id=band_id, template=template)

    if request.method == "POST":
        form = GradeBandForm(request.POST, instance=grade_band)
        if form.is_valid():
            form.save()
            log_basel_template_audit(
                request.user,
                "update_grade_band",
                template,
                f"Updated grade band '{grade_band.grade_code}'.",
            )
            return redirect("scorecard:grade_band_list", template_id=template.id)
    else:
        form = GradeBandForm(instance=grade_band)

    return render(
        request,
        "credit_scoreshifts/grade_band_edit.html",
        {"form": form, "template": template, "grade_band": grade_band, "is_new": False},
    )


@login_required
def grade_band_delete_view(request: HttpRequest, template_id: int, band_id: int) -> HttpResponse:
    """
    Delete a grade band.
    """
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    grade_band = get_object_or_404(GradeBand, id=band_id, template=template)

    if request.method == "POST":
        log_basel_template_audit(
            request.user,
            "delete_grade_band",
            template,
            f"Deleted grade band '{grade_band.grade_code}'.",
        )
        grade_band.delete()
        return redirect("scorecard:grade_band_list", template_id=template.id)

    return render(
        request,
        "credit_scoreshifts/grade_band_delete.html",
        {"template": template, "grade_band": grade_band},
    )


@login_required
def farmers_credit_scoresheet_view(request: HttpRequest) -> HttpResponse:
    """
    Single generic view that:
    - Renders the Basel II Farmers Credit Scoresheet based on configuration
    - Accepts branch & customer details and attribute responses
    - Calculates raw and weighted scores plus final grade
    - Persists the full audit trail
    """

    template = _get_active_template()
    if template is None:
        # No Basel template configured yet – show a friendly message instead of 404.
        return render(
            request,
            "credit_scoreshifts/farmers_score_form.html",
            {
                "template": None,
                "sections": [],
                "drivers_by_section": {},
                "attributes_by_driver": {},
                "evaluation": None,
                "driver_scores": None,
                "section_scores": None,
                "attribute_responses": None,
                "grade_band": None,
                "grade_bands": [],
                "missing_template": True,
            },
        )

    sections, drivers_by_section, attributes_by_driver = _build_configuration(template)

    if request.method == "POST":
        branch_name = request.POST.get("branch_name", "").strip()
        customer_name = request.POST.get("customer_name", "").strip()
        customer_id = request.POST.get("customer_id", "").strip()

        missing_meta = not (branch_name and customer_name and customer_id)

        missing_attributes = []
        attribute_values: Dict[int, str] = {}

        # Validate attribute responses
        for driver_id, attrs in attributes_by_driver.items():
            for attribute in attrs:
                field_name = _field_name_for_attribute(attribute)
                value = request.POST.get(field_name)

                if attribute.is_required and (value is None or value == ""):
                    missing_attributes.append(attribute)
                else:
                    attribute_values[attribute.id] = value or ""

        if missing_meta or missing_attributes:
            # Get all grade bands for the grading system display
            grade_bands = template.grade_bands.all().order_by("display_order")

            context = {
                "template": template,
                "sections": sections,
                "drivers_by_section": drivers_by_section,
                "attributes_by_driver": attributes_by_driver,
                "grade_bands": grade_bands,
                "errors": {
                    "missing_meta": missing_meta,
                    "missing_attributes": missing_attributes,
                },
            }
            return render(
                request,
                "credit_scoreshifts/farmers_score_form.html",
                context,
            )

        with transaction.atomic():
            evaluation = CreditEvaluation.objects.create(
                template=template,
                branch_name=branch_name,
                customer_name=customer_name,
                customer_id=customer_id,
            )

            # Calculate scores
            driver_raw_scores: Dict[int, Decimal] = {}
            section_raw_scores: Dict[int, Decimal] = {}

            for driver_id, attrs in attributes_by_driver.items():
                driver_total = Decimal("0")
                for attribute in attrs:
                    raw_value = attribute_values.get(attribute.id, "")
                    selected_option = None
                    allocated_score = Decimal("0")

                    if attribute.data_type == "choice":
                        if raw_value:
                            selected_option = attribute.options.filter(
                                id=int(raw_value)
                            ).first()
                        if selected_option is not None:
                            allocated_score = selected_option.allocated_score
                            stored_raw_value = selected_option.value or str(
                                selected_option.id
                            )
                        else:
                            stored_raw_value = ""
                    else:
                        # For now, treat non-choice attributes as decimal scores
                        stored_raw_value = raw_value
                        try:
                            allocated_score = Decimal(raw_value or "0")
                        except Exception:
                            allocated_score = Decimal("0")

                    AttributeResponse.objects.create(
                        evaluation=evaluation,
                        attribute=attribute,
                        option=selected_option,
                        raw_value=stored_raw_value,
                        allocated_score=allocated_score,
                    )

                    driver_total += allocated_score

                driver_raw_scores[driver_id] = driver_total

            total_raw_score = Decimal("0")
            total_weighted_percent = Decimal("0")

            # Persist driver and section scores
            # Formulas (same for all sections):
            # ACTUAL_SCORE = ALLOCATED_SCORE (sum of allocated scores for the driver)
            # WEIGHTED_SCORE = ACTUAL_SCORE / Highest Possible Score * WEIGHT
            # PROOF = IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE <> ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE
            for section in sections:
                section_total = Decimal("0")
                for driver in drivers_by_section.get(section.id, []):
                    # ACTUAL_SCORE = ALLOCATED_SCORE (sum of allocated scores)
                    actual_score = driver_raw_scores.get(driver.id, Decimal("0"))
                    
                    # WEIGHTED_SCORE = ACTUAL_SCORE / Highest Possible Score * WEIGHT
                    max_score = driver.get_max_score()
                    if max_score > 0:
                        weighted_percent = (actual_score / max_score) * driver.get_total_weight_percent()
                    else:
                        weighted_percent = Decimal("0")

                    # PROOF validation according to Excel formula:
                    # PROOF = IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE <> ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE
                    allocated_score = max_score
                    
                    if actual_score == Decimal("0") or actual_score is None:
                        # If ACTUAL_SCORE is empty, return '' + ALLOCATED_SCORE = ALLOCATED_SCORE
                        proof = str(allocated_score)
                    elif actual_score != allocated_score:
                        # If ACTUAL_SCORE != ALLOCATED_SCORE, return 'ERROR' + ALLOCATED_SCORE
                        proof = f"ERROR{allocated_score}"
                    else:
                        # If ACTUAL_SCORE == ALLOCATED_SCORE, return '' + ALLOCATED_SCORE = ALLOCATED_SCORE
                        proof = str(allocated_score)

                    RiskDriverScore.objects.create(
                        evaluation=evaluation,
                        risk_driver=driver,
                        raw_score=actual_score,
                        weighted_percent=weighted_percent,
                        proof=proof,
                    )

                    total_raw_score += actual_score
                    section_total += weighted_percent
                    total_weighted_percent += weighted_percent

                SectionScore.objects.create(
                    evaluation=evaluation,
                    section=section,
                    raw_score=section_total,
                    weighted_percent=section_total,
                )

            # Determine final grade
            grade_band: GradeBand | None = (
                template.grade_bands.filter(
                    min_percent__lte=total_weighted_percent,
                    max_percent__gte=total_weighted_percent,
                )
                .order_by("display_order")
                .first()
            )

            evaluation.total_raw_score = total_raw_score
            evaluation.total_weighted_percent = total_weighted_percent
            evaluation.final_grade = grade_band.grade_code if grade_band else ""
            evaluation.save(update_fields=["total_raw_score", "total_weighted_percent", "final_grade"])

        return redirect(
            reverse("scorecard:farmers_credit_scoresheet") + f"?eval_id={evaluation.id}"
        )

    # GET: either blank form or show results if eval_id is present
    eval_id = request.GET.get("eval_id")
    evaluation = None
    driver_scores = None
    section_scores = None
    attribute_responses = None
    grade_band = None

    if eval_id:
        evaluation = get_object_or_404(
            CreditEvaluation.objects.select_related("template"), id=eval_id
        )
        driver_scores = (
            evaluation.driver_scores.select_related("risk_driver")
            .all()
            .order_by("risk_driver__section__display_order", "risk_driver__display_order")
        )
        section_scores = (
            evaluation.section_scores.select_related("section")
            .all()
            .order_by("section__display_order")
        )
        attribute_responses = (
            evaluation.attribute_responses.select_related("attribute", "option")
            .all()
            .order_by(
                "attribute__risk_driver__section__display_order",
                "attribute__risk_driver__display_order",
                "attribute__display_order",
            )
        )
        if evaluation.total_weighted_percent is not None:
            grade_band = (
                template.grade_bands.filter(
                    min_percent__lte=evaluation.total_weighted_percent,
                    max_percent__gte=evaluation.total_weighted_percent,
                )
                .order_by("display_order")
                .first()
            )

    # Get all grade bands for the grading system display
    grade_bands = template.grade_bands.all().order_by("display_order")

    context = {
        "template": template,
        "sections": sections,
        "drivers_by_section": drivers_by_section,
        "attributes_by_driver": attributes_by_driver,
        "evaluation": evaluation,
        "driver_scores": driver_scores,
        "section_scores": section_scores,
        "attribute_responses": attribute_responses,
        "grade_band": grade_band,
        "grade_bands": grade_bands,
    }

    return render(request, "credit_scoreshifts/farmers_score_form.html", context)
