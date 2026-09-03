from decimal import Decimal
from typing import Dict, List

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models, transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from scorecard.functions_view.audit import log_basel_template_audit
from scorecard.functions_view.credit_scoreshits import (
    GradeBandForm,
    _ensure_basel_template_edit_started,
)
from scorecard.models import (
    Attribute,
    BaselScoreSheetTemplate,
    GradeBand,
    Option,
    RiskDriver,
    Section,
)


def _check_template_editable(template, user, request):
    """Return whether the current user is allowed to edit this template."""
    if not template.can_be_edited_by(user):
        from django.contrib import messages
        messages.error(request, f"Template '{template.code}' cannot be edited. It must be in draft, in_progress, returned, or approved status.")
        return False
    return True


def _soft_delete_basel_attribute(attribute: Attribute) -> None:
    with transaction.atomic():
        Option.all_objects.filter(attribute=attribute, is_deleted=False).update(is_deleted=True)
        if not attribute.is_deleted:
            attribute.is_deleted = True
            attribute.save(update_fields=["is_deleted"])


def _soft_delete_basel_driver(driver: RiskDriver) -> None:
    with transaction.atomic():
        Option.all_objects.filter(attribute__risk_driver=driver, is_deleted=False).update(is_deleted=True)
        Attribute.all_objects.filter(risk_driver=driver, is_deleted=False).update(is_deleted=True)
        if not driver.is_deleted:
            driver.is_deleted = True
            driver.save(update_fields=["is_deleted"])


def _soft_delete_basel_section(section: Section) -> None:
    with transaction.atomic():
        Option.all_objects.filter(attribute__risk_driver__section=section, is_deleted=False).update(is_deleted=True)
        Attribute.all_objects.filter(risk_driver__section=section, is_deleted=False).update(is_deleted=True)
        RiskDriver.all_objects.filter(section=section, is_deleted=False).update(is_deleted=True)
        if not section.is_deleted:
            section.is_deleted = True
            section.save(update_fields=["is_deleted"])


# ============================================================================
# FORMS
# ============================================================================

class SectionForm(forms.ModelForm):
    class Meta:
        model = Section
        fields = ["name", "display_order"]
        widgets = {
            "name": forms.TextInput(attrs={"style": "width: 100%;"}),
            "display_order": forms.NumberInput(attrs={"style": "width: 100%;"}),
        }
        help_texts = {
            "name": "Section name (e.g., 'Customer Demographics', 'Production & Marketing')",
            "display_order": "Order in which this section should appear (lower numbers first)",
        }


class RiskDriverForm(forms.ModelForm):
    class Meta:
        model = RiskDriver
        fields = ["name", "display_order"]
        widgets = {
            "name": forms.TextInput(attrs={"style": "width: 100%;"}),
            "display_order": forms.NumberInput(attrs={"style": "width: 100%;"}),
        }
        help_texts = {
            "name": "Risk driver name (e.g., 'Farm Location and Activities according to region')",
            "display_order": "Order in which this risk driver should appear (lower numbers first)",
        }


class AttributeForm(forms.ModelForm):
    class Meta:
        model = Attribute
        fields = [
            "label",
            "help_text",
            "group_label",
            "input_type",
            "scoring_rule",
            "weight_percent",
            "display_order",
            "requires_document",
        ]
        widgets = {
            "label": forms.TextInput(attrs={"style": "width: 100%;"}),
            "help_text": forms.Textarea(attrs={"rows": 3, "style": "width: 100%;"}),
            "group_label": forms.TextInput(attrs={"style": "width: 100%;"}),
            "input_type": forms.Select(attrs={"style": "width: 100%;"}),
            "scoring_rule": forms.Select(attrs={"style": "width: 100%;"}),
            "weight_percent": forms.NumberInput(attrs={"step": "0.01", "style": "width: 100%;"}),
            "display_order": forms.NumberInput(attrs={"style": "width: 100%;"}),
            "requires_document": forms.CheckboxInput(attrs={"style": "width: auto;"}),
        }
        help_texts = {
            "label": "Attribute label/question text",
            "help_text": "Optional help text for this attribute",
            "group_label": "Optional group label (e.g., 'Natural Region I', 'Natural Region II(a)')",
            "input_type": "Choose radio for a single choice or checkbox to allow one or many selections.",
            "scoring_rule": "Use a retail combo rule when this attribute should allow special checkbox scoring instead of a normal single-choice radio selection.",
            "weight_percent": "Weight as a percentage of the total score (e.g., 6.00, 5.00). The Risk Driver weight will be the sum of all its attributes' weights.",
            "display_order": "Order in which this attribute should appear (lower numbers first)",
            "requires_document": "If enabled, users will be able to upload supporting documents when selecting an option for this attribute.",
        }


class OptionForm(forms.ModelForm):
    class Meta:
        model = Option
        fields = ["label", "allocated_score", "display_order", "is_default"]
        widgets = {
            "label": forms.TextInput(attrs={"style": "width: 100%;"}),
            "allocated_score": forms.NumberInput(attrs={"step": "0.01", "style": "width: 100%;"}),
            "display_order": forms.NumberInput(attrs={"style": "width: 100%;"}),
            "is_default": forms.CheckboxInput(),
        }
        help_texts = {
            "label": "Option label (e.g., 'Intensive Mixed Farming', 'Plantation/Forestry/Fruits/Flowers')",
            "allocated_score": "Allocated score for this option (e.g., 10.00, 9.00, 8.00)",
            "display_order": "Order in which this option should appear (lower numbers first)",
            "is_default": "Whether this option is selected by default",
        }


def _build_template_tree(template: BaselScoreSheetTemplate):
    sections = list(
        template.sections.all()
        .prefetch_related("risk_drivers__attributes__options")
        .order_by("display_order", "id")
    )
    for section in sections:
        section.sorted_risk_drivers = list(section.risk_drivers.all().order_by("display_order", "id"))
        for driver in section.sorted_risk_drivers:
            driver.sorted_attributes = list(driver.attributes.all().order_by("display_order", "id"))
            for attribute in driver.sorted_attributes:
                attribute.sorted_options = list(attribute.options.all().order_by("display_order", "id"))
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


def _next_section_code(template: BaselScoreSheetTemplate) -> str:
    used_codes = set(template.sections.values_list("code", flat=True))
    candidate_index = 1
    while True:
        candidate = _number_to_letters(candidate_index)
        if candidate not in used_codes:
            return candidate
        candidate_index += 1


def _next_risk_driver_code(section: Section) -> str:
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


def _build_basel_builder_context(template, *, form_state=None):
    form_state = form_state or {}
    sections = _build_template_tree(template)

    driver_forms = {}
    attribute_forms = {}
    option_forms = {}
    section_edit_forms = {}
    driver_edit_forms = {}
    attribute_edit_forms = {}
    option_edit_forms = {}
    grade_band_edit_forms = {}

    for section in sections:
        section_edit_forms[section.id] = form_state.get(
            ("edit_section", section.id),
            SectionForm(prefix=f"edit-section-{section.id}", instance=section),
        )
        driver_forms[section.id] = form_state.get(
            ("driver", section.id),
            RiskDriverForm(prefix=f"driver-{section.id}", initial={"display_order": _next_display_order(section.risk_drivers.all())}),
        )
        for driver in section.risk_drivers.all().order_by("display_order", "id"):
            driver_edit_forms[driver.id] = form_state.get(
                ("edit_driver", driver.id),
                RiskDriverForm(prefix=f"edit-driver-{driver.id}", instance=driver),
            )
            attribute_forms[driver.id] = form_state.get(
                ("attribute", driver.id),
                AttributeForm(prefix=f"attribute-{driver.id}", initial={"display_order": _next_display_order(driver.attributes.all())}),
            )
            for attribute in driver.attributes.all().order_by("display_order", "id"):
                attribute_edit_forms[attribute.id] = form_state.get(
                    ("edit_attribute", attribute.id),
                    AttributeForm(prefix=f"edit-attribute-{attribute.id}", instance=attribute),
                )
                option_forms[attribute.id] = form_state.get(
                    ("option", attribute.id),
                    OptionForm(prefix=f"option-{attribute.id}", initial={"display_order": _next_display_order(attribute.options.all())}),
                )
                for option in attribute.options.all().order_by("display_order", "id"):
                    option_edit_forms[option.id] = form_state.get(
                        ("edit_option", option.id),
                        OptionForm(prefix=f"edit-option-{option.id}", instance=option),
                    )

    for band in template.grade_bands.all().order_by("display_order", "id"):
        grade_band_edit_forms[band.id] = form_state.get(
            ("edit_grade_band", band.id),
            GradeBandForm(prefix=f"edit-grade-band-{band.id}", instance=band),
        )

    context = {
        "template": template,
        "sections": sections,
        "section_form": form_state.get(
            ("section", None),
            SectionForm(prefix="section", initial={"display_order": _next_display_order(template.sections.all())}),
        ),
        "driver_forms": driver_forms,
        "attribute_forms": attribute_forms,
        "option_forms": option_forms,
        "section_edit_forms": section_edit_forms,
        "driver_edit_forms": driver_edit_forms,
        "attribute_edit_forms": attribute_edit_forms,
        "option_edit_forms": option_edit_forms,
        "grade_band_form": form_state.get(
            ("grade_band", None),
            GradeBandForm(prefix="grade-band", initial={"display_order": _next_display_order(template.grade_bands.all())}),
        ),
        "grade_bands": template.grade_bands.all().order_by("display_order", "id"),
        "grade_band_edit_forms": grade_band_edit_forms,
        "section_count": len(sections),
        "driver_count": RiskDriver.objects.filter(section__template=template).count(),
        "attribute_count": Attribute.objects.filter(risk_driver__section__template=template).count(),
        "option_count": Option.objects.filter(attribute__risk_driver__section__template=template).count(),
        "grade_band_count": template.grade_bands.count(),
        "active_anchor": form_state.get("active_anchor", ""),
    }
    return context


@login_required
def basel_template_builder_view(request: HttpRequest, template_id: int) -> HttpResponse:
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)

    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:basel_template_detail", template_id=template.id)

    form_state = {}

    if request.method == "POST":
        _ensure_basel_template_edit_started(template, request.user)
        action = request.POST.get("builder_action")

        if action in {"move_section_up", "move_section_down"}:
            section = get_object_or_404(Section, id=request.POST.get("section_id"), template=template)
            direction = "up" if action.endswith("_up") else "down"
            moved = _reorder_siblings(template.sections.all(), section.id, direction)
            if moved:
                log_basel_template_audit(request.user, "reorder_section", template, f"Moved section '{section.code}' {direction} in the Basel builder.")
                messages.success(request, f"Section '{section.code}' moved {direction}.")
            else:
                messages.info(request, f"Section '{section.code}' is already at the {'top' if direction == 'up' else 'bottom'}.")
            return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#section-{section.id}")
        elif action in {"move_driver_up", "move_driver_down"}:
            driver = get_object_or_404(RiskDriver, id=request.POST.get("driver_id"), section__template=template)
            direction = "up" if action.endswith("_up") else "down"
            moved = _reorder_siblings(driver.section.risk_drivers.all(), driver.id, direction)
            if moved:
                log_basel_template_audit(request.user, "reorder_risk_driver", template, f"Moved risk driver '{driver.code}' {direction} in the Basel builder.")
                messages.success(request, f"Risk driver '{driver.code}' moved {direction}.")
            else:
                messages.info(request, f"Risk driver '{driver.code}' is already at the {'top' if direction == 'up' else 'bottom'}.")
            return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#driver-{driver.id}")
        elif action in {"move_attribute_up", "move_attribute_down"}:
            attribute = get_object_or_404(Attribute, id=request.POST.get("attribute_id"), risk_driver__section__template=template)
            direction = "up" if action.endswith("_up") else "down"
            moved = _reorder_siblings(attribute.risk_driver.attributes.all(), attribute.id, direction)
            if moved:
                log_basel_template_audit(request.user, "reorder_attribute", template, f"Moved attribute '{attribute.label}' {direction} in the Basel builder.")
                messages.success(request, f"Attribute '{attribute.label}' moved {direction}.")
            else:
                messages.info(request, f"Attribute '{attribute.label}' is already at the {'top' if direction == 'up' else 'bottom'}.")
            return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#attribute-{attribute.id}")
        elif action in {"move_option_up", "move_option_down"}:
            option = get_object_or_404(Option, id=request.POST.get("option_id"), attribute__risk_driver__section__template=template)
            direction = "up" if action.endswith("_up") else "down"
            moved = _reorder_siblings(option.attribute.options.all(), option.id, direction)
            if moved:
                log_basel_template_audit(request.user, "reorder_option", template, f"Moved option '{option.label}' {direction} in the Basel builder.")
                messages.success(request, f"Option '{option.label}' moved {direction}.")
            else:
                messages.info(request, f"Option '{option.label}' is already at the {'top' if direction == 'up' else 'bottom'}.")
            return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#attribute-{option.attribute_id}")
        elif action in {"move_grade_band_up", "move_grade_band_down"}:
            grade_band = get_object_or_404(GradeBand, id=request.POST.get("grade_band_id"), template=template)
            direction = "up" if action.endswith("_up") else "down"
            moved = _reorder_siblings(template.grade_bands.all(), grade_band.id, direction)
            if moved:
                log_basel_template_audit(request.user, "reorder_grade_band", template, f"Moved grade band '{grade_band.grade_code}' {direction} in the Basel builder.")
                messages.success(request, f"Grade band '{grade_band.grade_code}' moved {direction}.")
            else:
                messages.info(request, f"Grade band '{grade_band.grade_code}' is already at the {'top' if direction == 'up' else 'bottom'}.")
            return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#grade-bands")
        elif action == "add_section":
            form = SectionForm(request.POST, prefix="section")
            if form.is_valid():
                section = form.save(commit=False)
                section.template = template
                section.code = _next_section_code(template)
                section.save()
                log_basel_template_audit(request.user, "create_section", template, f"Created section '{section.code}' from the Basel builder.")
                messages.success(request, f"Section '{section.code}' added to the Basel II template.")
                return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#add-driver-{section.id}")
            form_state[("section", None)] = form
            form_state["active_anchor"] = "add-section"
        elif action == "update_section":
            section = get_object_or_404(Section, id=request.POST.get("section_id"), template=template)
            form = SectionForm(request.POST, prefix=f"edit-section-{section.id}", instance=section)
            if form.is_valid():
                form.save()
                log_basel_template_audit(request.user, "update_section", template, f"Updated section '{section.code}' from the Basel builder.")
                messages.success(request, f"Section '{section.code}' updated.")
                return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#section-{section.id}")
            form_state[("edit_section", section.id)] = form
            form_state["active_anchor"] = f"section-{section.id}"
        elif action == "delete_section":
            section = get_object_or_404(Section, id=request.POST.get("section_id"), template=template)
            section_code = section.code
            _soft_delete_basel_section(section)
            log_basel_template_audit(request.user, "delete_section", template, f"Deleted section '{section_code}' from the Basel builder.")
            messages.success(request, f"Section '{section_code}' deleted.")
            return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}")
        elif action == "add_driver":
            section = get_object_or_404(Section, id=request.POST.get("section_id"), template=template)
            form = RiskDriverForm(request.POST, prefix=f"driver-{section.id}")
            if form.is_valid():
                driver = form.save(commit=False)
                driver.section = section
                driver.code = _next_risk_driver_code(section)
                driver.max_score = Decimal("0")
                driver.weight_percent = Decimal("0")
                driver.save()
                log_basel_template_audit(request.user, "create_risk_driver", template, f"Created risk driver '{driver.code}' from the Basel builder.")
                messages.success(request, f"Risk driver '{driver.code}' added under section '{section.code}'.")
                return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#add-attribute-{driver.id}")
            form_state[("driver", section.id)] = form
            form_state["active_anchor"] = f"add-driver-{section.id}"
        elif action == "update_driver":
            driver = get_object_or_404(RiskDriver, id=request.POST.get("driver_id"), section__template=template)
            form = RiskDriverForm(request.POST, prefix=f"edit-driver-{driver.id}", instance=driver)
            if form.is_valid():
                form.save()
                log_basel_template_audit(request.user, "update_risk_driver", template, f"Updated risk driver '{driver.code}' from the Basel builder.")
                messages.success(request, f"Risk driver '{driver.code}' updated.")
                return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#driver-{driver.id}")
            form_state[("edit_driver", driver.id)] = form
            form_state["active_anchor"] = f"driver-{driver.id}"
        elif action == "delete_driver":
            driver = get_object_or_404(RiskDriver, id=request.POST.get("driver_id"), section__template=template)
            driver_code = driver.code
            section_id = driver.section_id
            _soft_delete_basel_driver(driver)
            log_basel_template_audit(request.user, "delete_risk_driver", template, f"Deleted risk driver '{driver_code}' from the Basel builder.")
            messages.success(request, f"Risk driver '{driver_code}' deleted.")
            return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#section-{section_id}")
        elif action == "add_attribute":
            driver = get_object_or_404(RiskDriver, id=request.POST.get("driver_id"), section__template=template)
            form = AttributeForm(request.POST, prefix=f"attribute-{driver.id}")
            if form.is_valid():
                attribute = form.save(commit=False)
                attribute.risk_driver = driver
                attribute.group_label = (attribute.label or "").strip()
                if not attribute.code:
                    import re
                    words = re.findall(r'\b\w', attribute.label.strip())
                    base_code = ''.join(words).upper()[:50] or f"ATTR_{attribute.display_order}"
                    code = base_code
                    counter = 1
                    while Attribute.objects.filter(risk_driver=driver, code=code).exists():
                        code = f"{base_code}_{counter}"
                        counter += 1
                    attribute.code = code
                attribute.data_type = "choice"
                attribute.input_type = "checkbox" if attribute.scoring_rule else (attribute.input_type or "radio")
                attribute.is_required = True
                attribute.save()
                log_basel_template_audit(request.user, "create_attribute", template, f"Created attribute '{attribute.label}' from the Basel builder.")
                messages.success(request, f"Attribute '{attribute.label}' added under risk driver '{driver.code}'.")
                return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#add-option-{attribute.id}")
            form_state[("attribute", driver.id)] = form
            form_state["active_anchor"] = f"add-attribute-{driver.id}"
        elif action == "update_attribute":
            attribute = get_object_or_404(Attribute, id=request.POST.get("attribute_id"), risk_driver__section__template=template)
            form = AttributeForm(request.POST, prefix=f"edit-attribute-{attribute.id}", instance=attribute)
            if form.is_valid():
                saved_attribute = form.save(commit=False)
                saved_attribute.group_label = (saved_attribute.label or "").strip()
                saved_attribute.input_type = "checkbox" if saved_attribute.scoring_rule else (saved_attribute.input_type or "radio")
                saved_attribute.save()
                log_basel_template_audit(request.user, "update_attribute", template, f"Updated attribute '{attribute.label}' from the Basel builder.")
                messages.success(request, f"Attribute '{attribute.label}' updated.")
                return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#attribute-{attribute.id}")
            form_state[("edit_attribute", attribute.id)] = form
            form_state["active_anchor"] = f"attribute-{attribute.id}"
        elif action == "delete_attribute":
            attribute = get_object_or_404(Attribute, id=request.POST.get("attribute_id"), risk_driver__section__template=template)
            attr_label = attribute.label
            driver_id = attribute.risk_driver_id
            _soft_delete_basel_attribute(attribute)
            log_basel_template_audit(request.user, "delete_attribute", template, f"Deleted attribute '{attr_label}' from the Basel builder.")
            messages.success(request, f"Attribute '{attr_label}' deleted.")
            return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#driver-{driver_id}")
        elif action == "add_option":
            attribute = get_object_or_404(Attribute, id=request.POST.get("attribute_id"), risk_driver__section__template=template)
            form = OptionForm(request.POST, prefix=f"option-{attribute.id}")
            if form.is_valid():
                option = form.save(commit=False)
                option.attribute = attribute
                if not option.value:
                    option.value = option.label
                option.save()
                log_basel_template_audit(request.user, "create_option", template, f"Created option '{option.label}' from the Basel builder.")
                messages.success(request, f"Option '{option.label}' added under attribute '{attribute.label}'.")
                return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#add-option-{attribute.id}")
            form_state[("option", attribute.id)] = form
            form_state["active_anchor"] = f"add-option-{attribute.id}"
        elif action == "update_option":
            option = get_object_or_404(Option, id=request.POST.get("option_id"), attribute__risk_driver__section__template=template)
            form = OptionForm(request.POST, prefix=f"edit-option-{option.id}", instance=option)
            if form.is_valid():
                saved_option = form.save(commit=False)
                if not saved_option.value:
                    saved_option.value = saved_option.label
                saved_option.save()
                log_basel_template_audit(request.user, "update_option", template, f"Updated option '{saved_option.label}' from the Basel builder.")
                messages.success(request, f"Option '{saved_option.label}' updated.")
                return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#attribute-{saved_option.attribute_id}")
            form_state[("edit_option", option.id)] = form
            form_state["active_anchor"] = f"attribute-{option.attribute_id}"
        elif action == "delete_option":
            option = get_object_or_404(Option, id=request.POST.get("option_id"), attribute__risk_driver__section__template=template)
            option_label = option.label
            attribute_id = option.attribute_id
            option.is_deleted = True
            option.save(update_fields=["is_deleted"])
            log_basel_template_audit(request.user, "delete_option", template, f"Deleted option '{option_label}' from the Basel builder.")
            messages.success(request, f"Option '{option_label}' deleted.")
            return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#attribute-{attribute_id}")
        elif action == "add_grade_band":
            form = GradeBandForm(request.POST, prefix="grade-band")
            if form.is_valid():
                grade_band = form.save(commit=False)
                grade_band.template = template
                grade_band.save()
                log_basel_template_audit(request.user, "create_grade_band", template, f"Created grade band '{grade_band.grade_code}' from the Basel builder.")
                messages.success(request, f"Grade band '{grade_band.grade_code}' added to the Basel II template.")
                return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#grade-bands")
            form_state[("grade_band", None)] = form
            form_state["active_anchor"] = "grade-bands"
        elif action == "update_grade_band":
            grade_band = get_object_or_404(GradeBand, id=request.POST.get("grade_band_id"), template=template)
            form = GradeBandForm(request.POST, prefix=f"edit-grade-band-{grade_band.id}", instance=grade_band)
            if form.is_valid():
                form.save()
                log_basel_template_audit(request.user, "update_grade_band", template, f"Updated grade band '{grade_band.grade_code}' from the Basel builder.")
                messages.success(request, f"Grade band '{grade_band.grade_code}' updated.")
                return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#grade-bands")
            form_state[("edit_grade_band", grade_band.id)] = form
            form_state["active_anchor"] = "grade-bands"
        elif action == "delete_grade_band":
            grade_band = get_object_or_404(GradeBand, id=request.POST.get("grade_band_id"), template=template)
            grade_code = grade_band.grade_code
            grade_band.delete()
            log_basel_template_audit(request.user, "delete_grade_band", template, f"Deleted grade band '{grade_code}' from the Basel builder.")
            messages.success(request, f"Grade band '{grade_code}' deleted.")
            return redirect(f"{reverse('scorecard:basel_template_builder', kwargs={'template_id': template.id})}#grade-bands")

    return render(
        request,
        "credit_scoreshifts/template_builder/basel_template_builder.html",
        _build_basel_builder_context(template, form_state=form_state),
    )


# ============================================================================
# SECTION MANAGEMENT VIEWS
# ============================================================================

@login_required
def section_list_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """List all sections for a template."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    sections = template.sections.all().order_by("display_order")

    # Calculate total weight across all sections
    total_weight = Decimal("0")
    total_max_score = Decimal("0")
    for section in sections:
        total_weight += section.get_total_weight_percent()
        total_max_score += section.get_total_max_score()
    
    # Calculate difference from 100% for warning messages
    weight_diff = total_weight - Decimal("100")

    context = {
        "template": template,
        "sections": sections,
        "total_weight": total_weight,
        "total_max_score": total_max_score,
        "weight_diff": weight_diff,
    }

    return render(request, "credit_scoreshifts/sections_creation/section_list.html", context)


@login_required
def section_create_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """Create a new section for a template."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:basel_template_detail", template_id=template.id)

    if request.method == "POST":
        form = SectionForm(request.POST)
        if form.is_valid():
            section = form.save(commit=False)
            section.template = template
            section.code = _next_section_code(template)
            section.save()
            log_basel_template_audit(request.user, "create_section", template, f"Created section '{section.code}'.")
            messages.success(request, f"Section '{section.code}' created successfully!")
            return redirect("scorecard:section_list", template_id=template.id)
    else:
        # Set default display_order to be after the last one
        last_order = template.sections.aggregate(models.Max("display_order"))["display_order__max"]
        form = SectionForm(initial={"display_order": (last_order or 0) + 1})

    return render(
        request,
        "credit_scoreshifts/sections_creation/section_edit.html",
        {"form": form, "template": template, "is_new": True},
    )


@login_required
def section_edit_view(request: HttpRequest, template_id: int, section_id: int) -> HttpResponse:
    """Edit an existing section."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:section_list", template_id=template.id)

    if request.method == "POST":
        form = SectionForm(request.POST, instance=section)
        if form.is_valid():
            form.save()
            log_basel_template_audit(request.user, "update_section", template, f"Updated section '{section.code}'.")
            messages.success(request, f"Section '{section.code}' updated successfully!")
            return redirect("scorecard:section_list", template_id=template.id)
    else:
        form = SectionForm(instance=section)

    return render(
        request,
        "credit_scoreshifts/sections_creation/section_edit.html",
        {"form": form, "template": template, "section": section, "is_new": False},
    )


@login_required
def section_delete_view(request: HttpRequest, template_id: int, section_id: int) -> HttpResponse:
    """Delete a section."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:section_list", template_id=template.id)

    if request.method == "POST":
        section_code = section.code
        _soft_delete_basel_section(section)
        log_basel_template_audit(request.user, "delete_section", template, f"Deleted section '{section_code}'.")
        messages.success(request, f"Section '{section_code}' deleted successfully!")
        return redirect("scorecard:section_list", template_id=template.id)

    return render(
        request,
        "credit_scoreshifts/sections_creation/section_delete.html",
        {"template": template, "section": section},
    )


# ============================================================================
# RISK DRIVER MANAGEMENT VIEWS
# ============================================================================

@login_required
def risk_driver_list_view(request: HttpRequest, template_id: int, section_id: int) -> HttpResponse:
    """List all risk drivers for a section."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    risk_drivers = section.risk_drivers.all().order_by("display_order")

    context = {
        "template": template,
        "section": section,
        "risk_drivers": risk_drivers,
    }

    return render(request, "credit_scoreshifts/sections_creation/risk_driver_list.html", context)


@login_required
def risk_driver_create_view(request: HttpRequest, template_id: int, section_id: int) -> HttpResponse:
    """Create a new risk driver for a section."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:risk_driver_list", template_id=template.id, section_id=section.id)

    if request.method == "POST":
        form = RiskDriverForm(request.POST)
        if form.is_valid():
            risk_driver = form.save(commit=False)
            risk_driver.section = section
            risk_driver.code = _next_risk_driver_code(section)
            risk_driver.max_score = Decimal("0")  # Placeholder - will be calculated dynamically
            risk_driver.weight_percent = Decimal("0")  # Placeholder - will be calculated from attributes
            risk_driver.save()
            log_basel_template_audit(request.user, "create_risk_driver", template, f"Created risk driver '{risk_driver.code}' in section '{section.code}'.")
            messages.success(request, f"Risk driver '{risk_driver.code}' created successfully!")
            return redirect("scorecard:risk_driver_list", template_id=template.id, section_id=section.id)
    else:
        # Set default display_order to be after the last one
        last_order = section.risk_drivers.aggregate(models.Max("display_order"))["display_order__max"]
        form = RiskDriverForm(initial={"display_order": (last_order or 0) + 1})

    return render(
        request,
        "credit_scoreshifts/sections_creation/risk_driver_edit.html",
        {"form": form, "template": template, "section": section, "is_new": True},
    )


@login_required
def risk_driver_edit_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int
) -> HttpResponse:
    """Edit an existing risk driver."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    risk_driver = get_object_or_404(RiskDriver, id=driver_id, section=section)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:risk_driver_list", template_id=template.id, section_id=section.id)

    if request.method == "POST":
        form = RiskDriverForm(request.POST, instance=risk_driver)
        if form.is_valid():
            form.save()
            log_basel_template_audit(request.user, "update_risk_driver", template, f"Updated risk driver '{risk_driver.code}'.")
            messages.success(request, f"Risk driver '{risk_driver.code}' updated successfully!")
            return redirect("scorecard:risk_driver_list", template_id=template.id, section_id=section.id)
    else:
        form = RiskDriverForm(instance=risk_driver)

    return render(
        request,
        "credit_scoreshifts/sections_creation/risk_driver_edit.html",
        {"form": form, "template": template, "section": section, "risk_driver": risk_driver, "is_new": False},
    )


@login_required
def risk_driver_delete_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int
) -> HttpResponse:
    """Delete a risk driver."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    risk_driver = get_object_or_404(RiskDriver, id=driver_id, section=section)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:risk_driver_list", template_id=template.id, section_id=section.id)

    if request.method == "POST":
        driver_code = risk_driver.code
        _soft_delete_basel_driver(risk_driver)
        log_basel_template_audit(request.user, "delete_risk_driver", template, f"Deleted risk driver '{driver_code}'.")
        messages.success(request, f"Risk driver '{driver_code}' deleted successfully!")
        return redirect("scorecard:risk_driver_list", template_id=template.id, section_id=section.id)

    return render(
        request,
        "credit_scoreshifts/sections_creation/risk_driver_delete.html",
        {"template": template, "section": section, "risk_driver": risk_driver},
    )


# ============================================================================
# ATTRIBUTE MANAGEMENT VIEWS
# ============================================================================

@login_required
def attribute_list_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int
) -> HttpResponse:
    """List all attributes for a risk driver."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    risk_driver = get_object_or_404(RiskDriver, id=driver_id, section=section)
    attributes = risk_driver.attributes.all().order_by("display_order")

    context = {
        "template": template,
        "section": section,
        "risk_driver": risk_driver,
        "attributes": attributes,
    }

    return render(request, "credit_scoreshifts/sections_creation/attribute_list.html", context)


@login_required
def attribute_create_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int
) -> HttpResponse:
    """Create a new attribute for a risk driver."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    risk_driver = get_object_or_404(RiskDriver, id=driver_id, section=section)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:attribute_list", template_id=template.id, section_id=section.id, driver_id=driver_id)

    if request.method == "POST":
        form = AttributeForm(request.POST)
        if form.is_valid():
            attribute = form.save(commit=False)
            attribute.risk_driver = risk_driver
            
            # Auto-generate code if not provided (generate from label or use sequence)
            if not attribute.code:
                # Generate code from label: take first letters of words, uppercase, max 50 chars
                import re
                label = attribute.label.strip()
                # Remove special characters, take first letter of each word
                words = re.findall(r'\b\w', label)
                base_code = ''.join(words).upper()[:50]
                if not base_code:
                    base_code = f"ATTR_{attribute.display_order}"
                
                # Ensure uniqueness within the risk driver
                counter = 1
                code = base_code
                while Attribute.objects.filter(risk_driver=risk_driver, code=code).exists():
                    code = f"{base_code}_{counter}"
                    counter += 1
                attribute.code = code
            
            # Set auto-generated fields
            attribute.data_type = "choice"
            attribute.input_type = "checkbox" if attribute.scoring_rule else (attribute.input_type or "radio")
            attribute.is_required = True
            
            attribute.save()
            log_basel_template_audit(request.user, "create_attribute", template, f"Created attribute '{attribute.label}' in risk driver '{risk_driver.code}'.")
            messages.success(request, f"Attribute '{attribute.label}' created successfully!")
            return redirect(
                "scorecard:attribute_list",
                template_id=template.id,
                section_id=section.id,
                driver_id=risk_driver.id,
            )
    else:
        # Set default display_order to be after the last one
        last_order = risk_driver.attributes.aggregate(models.Max("display_order"))["display_order__max"]
        form = AttributeForm(initial={"display_order": (last_order or 0) + 1})

    return render(
        request,
        "credit_scoreshifts/sections_creation/attribute_edit.html",
        {"form": form, "template": template, "section": section, "risk_driver": risk_driver, "is_new": True},
    )


@login_required
def attribute_edit_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int, attribute_id: int
) -> HttpResponse:
    """Edit an existing attribute."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    risk_driver = get_object_or_404(RiskDriver, id=driver_id, section=section)
    attribute = get_object_or_404(Attribute, id=attribute_id, risk_driver=risk_driver)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:attribute_list", template_id=template.id, section_id=section.id, driver_id=driver_id)

    if request.method == "POST":
        form = AttributeForm(request.POST, instance=attribute)
        if form.is_valid():
            form.save()
            log_basel_template_audit(request.user, "update_attribute", template, f"Updated attribute '{attribute.label}'.")
            messages.success(request, f"Attribute '{attribute.label}' updated successfully!")
            return redirect(
                "scorecard:attribute_list",
                template_id=template.id,
                section_id=section.id,
                driver_id=risk_driver.id,
            )
    else:
        form = AttributeForm(instance=attribute)

    return render(
        request,
        "credit_scoreshifts/sections_creation/attribute_edit.html",
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
def attribute_delete_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int, attribute_id: int
) -> HttpResponse:
    """Delete an attribute."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    risk_driver = get_object_or_404(RiskDriver, id=driver_id, section=section)
    attribute = get_object_or_404(Attribute, id=attribute_id, risk_driver=risk_driver)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:attribute_list", template_id=template.id, section_id=section.id, driver_id=driver_id)

    if request.method == "POST":
        attr_label = attribute.label
        _soft_delete_basel_attribute(attribute)
        log_basel_template_audit(request.user, "delete_attribute", template, f"Deleted attribute '{attr_label}'.")
        messages.success(request, f"Attribute '{attr_label}' deleted successfully!")
        return redirect(
            "scorecard:attribute_list",
            template_id=template.id,
            section_id=section.id,
            driver_id=risk_driver.id,
        )

    return render(
        request,
        "credit_scoreshifts/sections_creation/attribute_delete.html",
        {"template": template, "section": section, "risk_driver": risk_driver, "attribute": attribute},
    )


# ============================================================================
# OPTION MANAGEMENT VIEWS
# ============================================================================

class OptionForm(forms.ModelForm):
    class Meta:
        model = Option
        fields = ["label", "value", "allocated_score", "display_order", "is_default"]
        widgets = {
            "label": forms.TextInput(attrs={"style": "width: 100%;"}),
            "value": forms.TextInput(attrs={"style": "width: 100%;"}),
            "allocated_score": forms.NumberInput(attrs={"step": "0.01", "style": "width: 100%;"}),
            "display_order": forms.NumberInput(attrs={"style": "width: 100%;"}),
            "is_default": forms.CheckboxInput(),
        }
        help_texts = {
            "label": "Option label (e.g., 'Intensive Mixed Farming', 'Plantation/Forestry/Fruits/Flowers')",
            "value": "Value stored in POST (usually same as label)",
            "allocated_score": "Allocated score for this option (e.g., 10.00, 9.00, 8.00)",
            "display_order": "Order in which this option should appear (lower numbers first)",
            "is_default": "Whether this option is selected by default",
        }


@login_required
def option_list_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int, attribute_id: int
) -> HttpResponse:
    """List all options for an attribute."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    risk_driver = get_object_or_404(RiskDriver, id=driver_id, section=section)
    attribute = get_object_or_404(Attribute, id=attribute_id, risk_driver=risk_driver)
    options = attribute.options.all().order_by("display_order")

    context = {
        "template": template,
        "section": section,
        "risk_driver": risk_driver,
        "attribute": attribute,
        "options": options,
    }

    return render(request, "credit_scoreshifts/sections_creation/option_list.html", context)


@login_required
def option_create_view(
    request: HttpRequest, template_id: int, section_id: int, driver_id: int, attribute_id: int
) -> HttpResponse:
    """Create a new option for an attribute."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    risk_driver = get_object_or_404(RiskDriver, id=driver_id, section=section)
    attribute = get_object_or_404(Attribute, id=attribute_id, risk_driver=risk_driver)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:option_list", template_id=template.id, section_id=section.id, driver_id=driver_id, attribute_id=attribute_id)

    if request.method == "POST":
        form = OptionForm(request.POST)
        if form.is_valid():
            option = form.save(commit=False)
            option.attribute = attribute
            if not option.value:
                option.value = option.label
            option.save()
            log_basel_template_audit(request.user, "create_option", template, f"Created option '{option.label}' for attribute '{attribute.label}'.")
            messages.success(request, f"Option '{option.label}' created successfully!")
            return redirect(
                "scorecard:option_list",
                template_id=template.id,
                section_id=section.id,
                driver_id=risk_driver.id,
                attribute_id=attribute.id,
            )
    else:
        # Set default display_order to be after the last one
        last_order = attribute.options.aggregate(models.Max("display_order"))["display_order__max"]
        form = OptionForm(initial={"display_order": (last_order or 0) + 1, "allocated_score": 0})

    return render(
        request,
        "credit_scoreshifts/sections_creation/option_edit.html",
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
def option_edit_view(
    request: HttpRequest,
    template_id: int,
    section_id: int,
    driver_id: int,
    attribute_id: int,
    option_id: int,
) -> HttpResponse:
    """Edit an existing option."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    risk_driver = get_object_or_404(RiskDriver, id=driver_id, section=section)
    attribute = get_object_or_404(Attribute, id=attribute_id, risk_driver=risk_driver)
    option = get_object_or_404(Option, id=option_id, attribute=attribute)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:option_list", template_id=template.id, section_id=section.id, driver_id=driver_id, attribute_id=attribute_id)

    if request.method == "POST":
        form = OptionForm(request.POST, instance=option)
        if form.is_valid():
            form.save()
            log_basel_template_audit(request.user, "update_option", template, f"Updated option '{option.label}'.")
            messages.success(request, f"Option '{option.label}' updated successfully!")
            return redirect(
                "scorecard:option_list",
                template_id=template.id,
                section_id=section.id,
                driver_id=risk_driver.id,
                attribute_id=attribute.id,
            )
    else:
        form = OptionForm(instance=option)

    return render(
        request,
        "credit_scoreshifts/sections_creation/option_edit.html",
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
def option_delete_view(
    request: HttpRequest,
    template_id: int,
    section_id: int,
    driver_id: int,
    attribute_id: int,
    option_id: int,
) -> HttpResponse:
    """Delete an option."""
    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id)
    section = get_object_or_404(Section, id=section_id, template=template)
    risk_driver = get_object_or_404(RiskDriver, id=driver_id, section=section)
    attribute = get_object_or_404(Attribute, id=attribute_id, risk_driver=risk_driver)
    option = get_object_or_404(Option, id=option_id, attribute=attribute)
    
    if not _check_template_editable(template, request.user, request):
        return redirect("scorecard:option_list", template_id=template.id, section_id=section.id, driver_id=driver_id, attribute_id=attribute_id)

    if request.method == "POST":
        option_label = option.label
        option.is_deleted = True
        option.save(update_fields=["is_deleted"])
        log_basel_template_audit(request.user, "delete_option", template, f"Deleted option '{option_label}'.")
        messages.success(request, f"Option '{option_label}' deleted successfully!")
        return redirect(
            "scorecard:option_list",
            template_id=template.id,
            section_id=section.id,
            driver_id=risk_driver.id,
            attribute_id=attribute.id,
        )

    return render(
        request,
        "credit_scoreshifts/sections_creation/option_delete.html",
        {
            "template": template,
            "section": section,
            "risk_driver": risk_driver,
            "attribute": attribute,
            "option": option,
        },
    )
