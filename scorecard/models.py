from datetime import time as datetime_time
from decimal import Decimal
import os

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Max, Q
from django.utils import timezone

from scorecard.grade_utils import normalize_grade_fields
from scorecard.permission_catalog import SCORECARD_PERMISSION_DEFINITIONS

class BaselScoreSheetTemplate(models.Model):
    """
    Configuration for a Basel II credit score sheet (e.g. Farmers Credit Scoresheet AFCSC1-17).
    This is the main template definition that everything else hangs off.
    """

    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    version = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)

    # Formula configuration (same for all sections)
    # ACTUAL_SCORE = ALLOCATED_SCORE
    # WEIGHTED_SCORE = ACTUAL_SCORE / Highest Possible Score * WEIGHT
    # PROOF = validation / audit rule
    formula_actual_score = models.CharField(
        max_length=255,
        default="ALLOCATED_SCORE",
        help_text="Formula for ACTUAL_SCORE (default: ALLOCATED_SCORE)",
    )
    formula_weighted_score = models.CharField(
        max_length=255,
        default="ACTUAL_SCORE / Highest_Possible_Score * WEIGHT",
        help_text="Formula for WEIGHTED_SCORE (default: ACTUAL_SCORE / Highest_Possible_Score * WEIGHT)",
    )
    formula_proof = models.CharField(
        max_length=255,
        default="IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE",
        help_text="Formula for PROOF validation/audit rule",
    )

    # Maker-Checker Workflow Fields
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('in_progress', 'In Progress'),
        ('submitted', 'Submitted for Review'),
        ('returned', 'Returned for Changes'),
        ('approved', 'Approved'),
        ('cancelled', 'Cancelled'),
    ]
    
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='approved',  # Existing templates are considered approved
        help_text="Status of the template workflow"
    )
    
    maker = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='made_templates',
        null=True,
        blank=True,
        help_text="User responsible for creating/editing this template"
    )
    
    checker = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='checked_templates',
        null=True,
        blank=True,
        help_text="User responsible for reviewing and approving this template"
    )
    
    submitted_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='submitted_templates',
        null=True,
        blank=True,
        help_text="User who submitted this template for review"
    )
    
    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this template was submitted for review"
    )
    
    approved_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='approved_templates',
        null=True,
        blank=True,
        help_text="User who approved this template"
    )
    
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this template was approved"
    )
    
    returned_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='returned_templates',
        null=True,
        blank=True,
        help_text="User who returned this template for changes"
    )
    
    returned_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this template was returned for changes"
    )
    
    return_reason = models.TextField(
        blank=True,
        help_text="Reason for returning the template for changes"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table="SCORECARD_BASEL_SCORE_SHEET_TEMPLATE"
        verbose_name_plural = "Basel Score Sheet Templates"

    def __str__(self) -> str:
        return f"{self.code} - {self.name} (v{self.version})" if self.version else f"{self.code} - {self.name}"
    
    def can_be_edited_by(self, user):
        """Check if user can edit this template"""
        if not getattr(user, "is_authenticated", False):
            return False
        can_manage = bool(
            getattr(user, "is_superuser", False)
            or getattr(user, "has_perm", lambda perm: False)("scorecard.manage_basel_templates")
        )
        # Cancelled templates cannot be edited
        if self.status == 'cancelled':
            return False
        # Submitted templates are locked (must be withdrawn first)
        if self.status == 'submitted':
            return False
        # Approved templates can be edited (will create new version)
        if self.status == 'approved' or self.status is None or self.status == '':
            return can_manage or self.maker == user or self.submitted_by == user or self.maker is None
        # Draft, in_progress, returned can be edited by maker or superuser
        if self.status in ['draft', 'in_progress', 'returned']:
            return self.maker == user or self.submitted_by == user or can_manage or self.maker is None
        return False
    
    def can_be_reviewed_by(self, user):
        """Check if user can review this template"""
        if self.status != 'submitted':
            return False
        if not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False):
            return True
        if not getattr(user, "has_perm", lambda perm: False)("scorecard.review_basel_templates"):
            return False
        checker_id = getattr(self, "checker_id", None)
        user_id = getattr(user, "id", None)
        if checker_id and checker_id != user_id:
            return False
        return True


class ActiveSectionManager(models.Manager):
    """Default manager that hides soft-deleted sections from active template editing."""

    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class ActiveIFRS9SectionManager(models.Manager):
    """Default manager that hides soft-deleted IFRS9 sections from active editing."""

    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class ActiveSoftDeleteManager(models.Manager):
    """Default manager for active records on soft-deletable configuration models."""

    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class Section(models.Model):
    """
    High-level grouping such as:
    - Customer Demographics
    - Production & Marketing
    - Financial Indicators
    - Credit Indicators / History
    """

    template = models.ForeignKey(
        BaselScoreSheetTemplate, on_delete=models.CASCADE, related_name="sections"
    )
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    display_order = models.PositiveIntegerField(default=1)
    is_deleted = models.BooleanField(default=False)

    objects = ActiveSectionManager()
    all_objects = models.Manager()

    class Meta:
        db_table="SCORECARD_SECTION"
        ordering = ["display_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["template", "code"],
                condition=Q(is_deleted=False),
                name="scorecard_unique_active_section_code",
            )
        ]

    def __str__(self) -> str:
        return f"{self.template.code}::{self.code} - {self.name}"

    def get_total_weight_percent(self) -> Decimal:
        """
        Calculate the total weight percentage from all attributes across all risk drivers in this section.
        """
        total = Decimal("0")
        for driver in self.risk_drivers.all():
            total += driver.get_total_weight_percent()
        return total

    def get_total_max_score(self) -> Decimal:
        """
        Calculate the total maximum score by summing all attributes' maximum scores across all risk drivers in this section.
        Each attribute's maximum score is the highest allocated_score from all its options.
        This mirrors how get_total_weight_percent() sums all attributes' weights.
        """
        total = Decimal("0")
        for attribute in Attribute.objects.filter(risk_driver__section=self):
            total += attribute.get_highest_possible_score()
        return total


class RiskDriver(models.Model):
    """
    Risk driver inside a section, e.g.:
    - Farm Location and Activities according to region
    - Land Tenure
    - Farming Technique
    Each has a weight (%) in the overall score sheet and a maximum possible score.
    """

    section = models.ForeignKey(
        Section, on_delete=models.CASCADE, related_name="risk_drivers"
    )
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)

    # Weight as a percentage of the total score (e.g. 10.00, 3.00 etc)
    weight_percent = models.DecimalField(max_digits=6, decimal_places=2)

    # Maximum possible raw score for this driver (e.g. 10, 30, 45, 160)
    max_score = models.DecimalField(max_digits=10, decimal_places=2)

    display_order = models.PositiveIntegerField(default=1)
    is_deleted = models.BooleanField(default=False)

    objects = ActiveSoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        db_table="SCORECARD_RISK_DRIVER"
        ordering = ["display_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["section", "code"],
                condition=Q(is_deleted=False),
                name="scorecard_unique_active_risk_driver_code",
            )
        ]

    def __str__(self) -> str:
        return f"{self.section.template.code}::{self.section.code}::{self.code} - {self.name}"

    def get_total_weight_percent(self) -> Decimal:
        """
        Calculate the total weight percentage from all attributes.
        This replaces the manual weight_percent field on RiskDriver.
        The driver's weight is the sum of all its attributes' weights.
        """
        from django.db.models import Sum
        total = self.attributes.aggregate(Sum("weight_percent"))["weight_percent__sum"]
        return Decimal(str(total)) if total else Decimal("0")

    def get_max_score(self) -> Decimal:
        """
        Calculate the maximum possible score from all attributes in this driver.
        """
        total = Decimal("0")
        for attribute in self.attributes.all():
            total += attribute.get_highest_possible_score()
        return total


class Attribute(models.Model):
    """
    Individual question/attribute under a risk driver.
    For example, "Natural Region I", "Land Tenure", "Repayment History" etc.
    The actual selectable choices (with scores) are stored as Options.
    """

    DATA_TYPE_CHOICES = [
        ("choice", "Choice"),
        ("integer", "Integer"),
        ("decimal", "Decimal"),
        ("text", "Text"),
    ]

    INPUT_TYPE_CHOICES = [
        ("radio", "Radio"),
        ("select", "Select"),
        ("checkbox", "Checkbox"),
    ]

    SCORING_RULE_CHOICES = [
        ("", "Standard single choice"),
        ("retail_liquidity_combo", "Retail liquidity combo"),
        ("retail_inventory_combo", "Retail inventory combo"),
    ]

    risk_driver = models.ForeignKey(
        RiskDriver, on_delete=models.CASCADE, related_name="attributes"
    )
    code = models.CharField(max_length=50)
    label = models.CharField(max_length=255)
    help_text = models.TextField(blank=True)

    data_type = models.CharField(
        max_length=20, choices=DATA_TYPE_CHOICES, default="choice"
    )
    input_type = models.CharField(
        max_length=20, choices=INPUT_TYPE_CHOICES, default="radio"
    )
    scoring_rule = models.CharField(
        max_length=50,
        choices=SCORING_RULE_CHOICES,
        blank=True,
        default="",
    )
    is_required = models.BooleanField(default=True)

    # Optional grouping, for things like "Natural Region I", "Natural Region II(a)" etc.
    group_label = models.CharField(max_length=255, blank=True)

    # Weight as a percentage of the total score (e.g. 6.00, 5.00 etc)
    # The RiskDriver's weight will be calculated as the sum of all its attributes' weights
    weight_percent = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    display_order = models.PositiveIntegerField(default=1)
    is_deleted = models.BooleanField(default=False)

    # Whether this attribute requires document upload when an option is selected
    requires_document = models.BooleanField(
        default=False,
        help_text="If enabled, users will be able to upload supporting documents when selecting an option for this attribute."
    )

    objects = ActiveSoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        db_table="SCORECARD_ATTRIBUTE"
        ordering = ["display_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["risk_driver", "code"],
                condition=Q(is_deleted=False),
                name="scorecard_unique_active_attribute_code",
            )
        ]

    def __str__(self) -> str:
        return f"{self.risk_driver}::{self.code} - {self.label}"

    @property
    def uses_checkbox_scoring(self) -> bool:
        return self.input_type == "checkbox" or bool(self.scoring_rule)

    def get_highest_possible_score(self) -> Decimal:
        options = list(self.options.all().order_by("display_order", "id"))
        if self.scoring_rule == "retail_liquidity_combo":
            total = Decimal("0")
            if len(options) >= 1:
                total += Decimal(str(options[0].allocated_score or 0))
            if len(options) >= 3:
                total += Decimal(str(options[2].allocated_score or 0))
            return total
        if self.scoring_rule == "retail_inventory_combo":
            return sum(
                (Decimal(str(option.allocated_score or 0)) for option in options[:3]),
                Decimal("0"),
            )

        max_score = self.options.aggregate(Max("allocated_score"))["allocated_score__max"]
        return Decimal(str(max_score or 0))

    def get_scoring_rule_note(self) -> str:
        if self.scoring_rule == "retail_liquidity_combo":
            return "Multivalue scores - d(i) or d(ii) plus d(iii). But d(iv) is cored alone"
        if self.scoring_rule == "retail_inventory_combo":
            return "score i to iii as appropriate or score iv or v only"
        return ""


class Option(models.Model):
    """
    Allowed values for an attribute and their allocated scores.
    For example:
    - "Intensive Mixed Farming" -> score 10
    - "Static yields" -> score 3
    - "No deliveries in the last three seasons" -> score -5
    """

    attribute = models.ForeignKey(
        Attribute, on_delete=models.CASCADE, related_name="options"
    )
    label = models.CharField(max_length=255)
    # Value stored in POST; usually same as ID but we keep a logical value too.
    value = models.CharField(max_length=255, blank=True)

    allocated_score = models.DecimalField(max_digits=10, decimal_places=2)
    display_order = models.PositiveIntegerField(default=1)
    is_default = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)

    objects = ActiveSoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        db_table="SCORECARD_OPTION"
        ordering = ["display_order", "id"]

    def __str__(self) -> str:
        return f"{self.attribute} -> {self.label} ({self.allocated_score})"


class GradeBand(models.Model):
    """
    Mapping from total weighted percentage to a grade.
    Example:
    - 85% & Above -> A1
    - [80% - 84%] -> A2
    - Below 31% -> E
    """

    template = models.ForeignKey(
        BaselScoreSheetTemplate, on_delete=models.CASCADE, related_name="grade_bands"
    )
    grade_code = models.CharField(max_length=10)
    description = models.CharField(max_length=255, blank=True)

    min_percent = models.DecimalField(max_digits=6, decimal_places=2)
    max_percent = models.DecimalField(max_digits=6, decimal_places=2)

    display_order = models.PositiveIntegerField(default=1)

    class Meta:
        db_table="SCORECARD_GRADE_BAND"
        ordering = ["display_order", "id"]

    def save(self, *args, **kwargs):
        normalize_grade_fields(self, "grade_code")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.template.code} {self.grade_code} [{self.min_percent}-{self.max_percent}]"


class CreditEvaluation(models.Model):
    """
    One completed score sheet for a specific customer at a branch,
    using a particular Basel score sheet template.
    """
    
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('in_progress', 'In Progress'),
        ('submitted', 'Submitted (Pending Review)'),
        ('returned', 'Returned (Needs Changes)'),
        ('approved', 'Approved (Completed)'),
        ('cancelled', 'Cancelled / Voided'),
        ('completed', 'Completed'),  # Keep for backward compatibility
    ]

    template = models.ForeignKey(
        BaselScoreSheetTemplate,
        on_delete=models.PROTECT,
        related_name="evaluations",
        null=True,
        blank=True,
    )
    template_section_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Cached template name used for reporting alongside template_id.",
    )

    # Minimal branch & customer info – can be replaced with actual FK models later.
    branch_name = models.CharField(max_length=255)
    customer_name = models.CharField(max_length=255)
    customer_id = models.CharField(max_length=100)

    total_raw_score = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    total_weighted_percent = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )
    final_grade = models.CharField(max_length=10, blank=True)
    
    # Status field for draft/in-progress/completed workflow
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='draft',
        help_text="Status of the questionnaire workflow"
    )
    
    # Maker-Checker Workflow Fields
    maker = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='made_evaluations',
        null=True,
        blank=True,
        help_text="User who created/edited this evaluation"
    )
    checker = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='checked_evaluations',
        null=True,
        blank=True,
        help_text="User assigned to review this evaluation"
    )
    
    # Submission tracking
    submitted_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='submitted_evaluations',
        null=True,
        blank=True,
        help_text="User who submitted for review"
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    
    # Approval tracking
    approved_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='approved_evaluations',
        null=True,
        blank=True,
        help_text="User who approved this evaluation"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    
    # Return/Rejection tracking
    returned_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='returned_evaluations',
        null=True,
        blank=True,
        help_text="User who returned this evaluation for changes"
    )
    returned_at = models.DateTimeField(null=True, blank=True)
    return_reason = models.TextField(
        blank=True,
        help_text="Reason/comments for returning the evaluation"
    )
    
    # Versioning
    version = models.IntegerField(
        default=1,
        help_text="Version number - increments on each resubmission"
    )
    resubmission_count = models.IntegerField(
        default=0,
        help_text="Number of times this evaluation has been resubmitted"
    )
    
    # Questionnaire period/date (optional)
    questionnaire_period = models.DateField(
        null=True,
        blank=True,
        help_text="Period/date for this questionnaire (e.g., assessment date)"
    )
    
    # Previous values (preserved when editing)
    previous_weighted_percent = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Previous weighted percent before last edit"
    )
    previous_grade = models.CharField(
        max_length=10, blank=True,
        help_text="Previous grade before last edit"
    )
    
    # Approved values (last approved scores - shown as "current" when status is 'submitted')
    approved_weighted_percent = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Last approved weighted percent (shown as current when status is 'submitted')"
    )
    approved_grade = models.CharField(
        max_length=10, blank=True,
        help_text="Last approved grade (shown as current when status is 'submitted')"
    )
    override_grade = models.CharField(
        max_length=10,
        blank=True,
        help_text="Optional administrative override grade shown separately from the calculated Basel grade."
    )
    override_comments = models.TextField(
        blank=True,
        help_text="Comments supporting the Basel override grade."
    )
    override_document = models.FileField(
        upload_to="basel_grade_overrides/%Y/%m/%d/",
        null=True,
        blank=True,
        help_text="Optional supporting document for the Basel override grade."
    )
    override_document_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Original filename for the Basel override supporting document."
    )
    override_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.SET_NULL,
        related_name='basel_grade_overrides',
        null=True,
        blank=True,
        help_text="User who last captured the Basel override grade."
    )
    override_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the Basel override grade was last captured."
    )
    autofill_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Saved auto-fill summary, source profile snapshot, and user overrides."
    )
    
    def get_current_weighted_percent(self):
        """Get the current weighted percent to display based on status"""
        if self.status == 'submitted':
            # When submitted, show the last approved score (not the new submitted score)
            # If no approved score exists yet (first submission), return None to show only pending
            return self.approved_weighted_percent if self.approved_weighted_percent is not None else None
        elif self.status == 'returned':
            # When returned, show the last approved score (the rejected submission is not shown as current)
            # This preserves the approved state until a new submission is approved
            return self.approved_weighted_percent if self.approved_weighted_percent is not None else None
        else:
            # When approved or other statuses, show the actual current score
            return self.total_weighted_percent
    
    def get_current_grade(self):
        """Get the current grade to display based on status"""
        if self.status == 'submitted':
            # When submitted, show the last approved grade (not the new submitted grade)
            # If no approved grade exists yet (first submission), return None to show only pending
            return self.approved_grade if self.approved_grade else None
        elif self.status == 'returned':
            # When returned, show the last approved grade (the rejected submission is not shown as current)
            # This preserves the approved state until a new submission is approved
            return self.approved_grade if self.approved_grade else None
        else:
            # When approved or other statuses, show the actual current grade
            return self.final_grade
    
    def get_pending_weighted_percent(self):
        """Get the pending weighted percent (new submitted score) when status is 'submitted'"""
        if self.status == 'submitted':
            # Get the score from the latest unapproved version (which contains the newly submitted scores)
            # Don't use total_weighted_percent as it may contain old approved scores
            latest_unapproved_version = self.versions.filter(is_approved=False).order_by('-version_number').first()
            if latest_unapproved_version:
                return latest_unapproved_version.total_weighted_percent
            # Fallback: if no version exists, return None (shouldn't happen)
            return None
        return None
    
    def get_pending_grade(self):
        """Get the pending grade (new submitted grade) when status is 'submitted'"""
        if self.status == 'submitted':
            # Get the grade from the latest unapproved version (which contains the newly submitted scores)
            # Don't use final_grade as it may contain old approved grades
            latest_unapproved_version = self.versions.filter(is_approved=False).order_by('-version_number').first()
            if latest_unapproved_version:
                return latest_unapproved_version.final_grade
            # Fallback: if no version exists, return None (shouldn't happen)
            return None
        return None

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table="SCORECARD_CREDIT_EVALUATION"
        ordering = ["-created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch_name", "customer_id"],
                name="uq_credit_eval_branch_customer",
            ),
        ]
        indexes = [
            models.Index(fields=["branch_name", "status"], name="SC_CEVAL_BRANCH_STATUS_IDX"),
            models.Index(fields=["status", "submitted_at"], name="SC_CEVAL_STATUS_SUB_AT_IDX"),
            models.Index(fields=["status", "created_at"], name="SC_CEVAL_STATUS_CREATED_IDX"),
            models.Index(fields=["template", "status"], name="SC_CEVAL_TEMPLATE_STATUS_IDX"),
        ]

    def save(self, *args, **kwargs):
        normalize_grade_fields(self, "final_grade", "previous_grade", "approved_grade", "override_grade")
        if self.template_id and self.template is not None:
            self.template_section_name = self.template.name or ""
        else:
            self.template_section_name = ""
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        template_code = self.template.code if self.template_id and self.template is not None else "EXTERNAL_IMPORT"
        return f"{template_code} eval for {self.customer_name} at {self.branch_name} on {self.created_at.date()}"
    
    def can_be_edited_by(self, user):
        """Check if user can edit this evaluation"""
        if not getattr(user, "is_authenticated", False):
            return False
        can_manage = bool(
            getattr(user, "is_superuser", False)
            or getattr(user, "has_perm", lambda perm: False)("scorecard.manage_basel_scores")
        )
        if self.status == 'cancelled':
            return False
        if self.status == 'approved':
            return can_manage or self.maker == user or self.submitted_by == user
        if self.status in ['draft', 'in_progress', 'returned']:
            return can_manage or self.maker == user or self.submitted_by == user
        return False
    
    def can_be_reviewed_by(self, user):
        """Check if user can review this evaluation"""
        if self.status != 'submitted':
            return False
        if not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False):
            return True
        if not getattr(user, "has_perm", lambda perm: False)("scorecard.review_basel_scores"):
            return False
        checker_id = getattr(self, "checker_id", None)
        user_id = getattr(user, "id", None)
        if checker_id and checker_id != user_id:
            return False
        get_accessible_branches = getattr(user, "get_accessible_branches", None)
        if callable(get_accessible_branches):
            accessible_branches = get_accessible_branches()
            if accessible_branches.exists():
                branch_names = set(accessible_branches.values_list("branch_name", flat=True))
                if self.branch_name not in branch_names:
                    return False
        return True
    
    def get_completion_percentage(self):
        """Calculate completion percentage based on required attributes"""
        if not hasattr(self, '_completion_cache'):
            if not self.template_id or self.template is None:
                self._completion_cache = 100
                return self._completion_cache
            # Get all required attributes from sections -> risk_drivers -> attributes
            from scorecard.models import Attribute
            total_required = Attribute.objects.filter(
                risk_driver__section__template=self.template,
                is_required=True
            ).count()
            if total_required == 0:
                return 100
            answered_required = self.attribute_responses.filter(
                attribute__is_required=True
            ).exclude(raw_value='').exclude(raw_value__isnull=True).count()
            self._completion_cache = int((answered_required / total_required) * 100) if total_required > 0 else 0
        return self._completion_cache


class EvaluationWorkflowHistory(models.Model):
    """
    Audit log for all workflow status changes in the maker-checker process.
    Records every status transition with user, timestamp, and comments.
    """
    evaluation = models.ForeignKey(
        CreditEvaluation,
        on_delete=models.CASCADE,
        related_name='workflow_history'
    )
    
    ACTION_CHOICES = [
        ('created', 'Created'),
        ('saved_draft', 'Saved Draft'),
        ('submitted', 'Submitted for Review'),
        ('returned', 'Returned for Changes'),
        ('approved', 'Approved'),
        ('reopened', 'Reopened'),
        ('cancelled', 'Cancelled'),
        ('withdrawn', 'Withdrawn'),
        ('reassigned', 'Reassigned'),
    ]
    
    action = models.CharField(
        max_length=20,
        choices=ACTION_CHOICES,
        help_text="Action taken on the evaluation"
    )
    
    from_status = models.CharField(
        max_length=20,
        choices=CreditEvaluation.STATUS_CHOICES,
        null=True,
        blank=True,
        help_text="Previous status"
    )
    
    to_status = models.CharField(
        max_length=20,
        choices=CreditEvaluation.STATUS_CHOICES,
        help_text="New status"
    )
    
    performed_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='workflow_actions',
        help_text="User who performed this action"
    )
    
    comments = models.TextField(
        blank=True,
        help_text="Comments or reason for this action"
    )
    
    created_at = models.DateTimeField(default=timezone.now)
    
    class Meta:
        db_table = "SCORECARD_EVALUATION_WORKFLOW_HISTORY"
        ordering = ['-created_at', '-id']
        verbose_name_plural = "Evaluation Workflow History"
    
    def __str__(self) -> str:
        return f"{self.evaluation.customer_name} - {self.get_action_display()} ({self.created_at.date()})"


class SectionScore(models.Model):
    """
    Calculated scores per section for an evaluation.
    """

    evaluation = models.ForeignKey(
        CreditEvaluation, on_delete=models.CASCADE, related_name="section_scores"
    )
    section = models.ForeignKey(
        Section, on_delete=models.PROTECT, related_name="section_scores"
    )

    raw_score = models.DecimalField(max_digits=10, decimal_places=2)
    weighted_percent = models.DecimalField(max_digits=6, decimal_places=2)

    class Meta:
        db_table="SCORECARD_SECTION_SCORE"
    def __str__(self) -> str:
        return f"{self.evaluation_id}::{self.section.code} - {self.raw_score}/{self.weighted_percent}%"


class RiskDriverScore(models.Model):
    """
    Calculated scores per risk driver for an evaluation.
    """

    evaluation = models.ForeignKey(
        CreditEvaluation, on_delete=models.CASCADE, related_name="driver_scores"
    )
    risk_driver = models.ForeignKey(
        RiskDriver, on_delete=models.PROTECT, related_name="driver_scores"
    )

    raw_score = models.DecimalField(max_digits=10, decimal_places=2)
    weighted_percent = models.DecimalField(max_digits=6, decimal_places=2)
    proof = models.CharField(
        max_length=50,
        blank=True,
        help_text="PROOF validation result (WEIGHT or ERROR)",
    )

    class Meta:
        db_table="SCORECARD_RISK_DRIVER_SCORE"
    def __str__(self) -> str:
        return f"{self.evaluation_id}::{self.risk_driver.code} - {self.raw_score}/{self.weighted_percent}%"


class AttributeResponse(models.Model):
    """
    Captured response for each attribute for an evaluation.
    Stores the option chosen plus the score applied, so that
    we can reconstruct the evaluation later for audit.
    """

    evaluation = models.ForeignKey(
        CreditEvaluation, on_delete=models.CASCADE, related_name="attribute_responses"
    )
    attribute = models.ForeignKey(
        Attribute, on_delete=models.PROTECT, related_name="responses"
    )
    option = models.ForeignKey(
        Option,
        on_delete=models.PROTECT,
        related_name="responses",
        null=True,
        blank=True,
    )

    raw_value = models.CharField(
        max_length=255,
        blank=True,
        help_text="Raw value provided. For choice attributes this mirrors the option value.",
    )
    allocated_score = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table="SCORECARD_ATTRIBUTE_RESPONSE"

    def __str__(self) -> str:
        return f"{self.evaluation_id}::{self.attribute.code} -> {self.allocated_score}"


def get_document_upload_path(instance, filename):
    """
    Generate upload path for attribute response documents.
    Format: basel_II_documents/{customer_code}/{year}/{month}/{day}/{filename}
    """
    # Get customer code from the evaluation
    # Handle case where attribute_response might not be fully loaded yet
    try:
        if hasattr(instance, 'attribute_response') and instance.attribute_response:
            if hasattr(instance.attribute_response, 'evaluation') and instance.attribute_response.evaluation:
                customer_code = instance.attribute_response.evaluation.customer_id or 'unknown'
            else:
                customer_code = 'unknown'
        else:
            customer_code = 'unknown'
    except (AttributeError, ValueError):
        customer_code = 'unknown'
    
    # Sanitize customer code to remove any invalid characters for file paths
    customer_code = "".join(c for c in str(customer_code) if c.isalnum() or c in ('-', '_')).strip()
    if not customer_code:
        customer_code = 'unknown'
    
    # Get date components
    now = timezone.now()
    year = now.strftime('%Y')
    month = now.strftime('%m')
    day = now.strftime('%d')
    
    # Return path: basel_II_documents/{customer_code}/{year}/{month}/{day}/{filename}
    return os.path.join('basel_II_documents', customer_code, year, month, day, filename)


def get_scorecard_document_upload_path(instance, filename):
    """
    Generate upload path for general scorecard documents.
    Format: scorecard_documents/{year}/{month}/{day}/{filename}
    """
    # Get date components
    now = timezone.now()
    year = now.strftime('%Y')
    month = now.strftime('%m')
    day = now.strftime('%d')
    
    # Sanitize filename to remove any invalid characters
    safe_filename = "".join(c for c in filename if c.isalnum() or c in ('-', '_', '.')).strip()
    if not safe_filename:
        safe_filename = filename
    
    # Return path: scorecard_documents/{year}/{month}/{day}/{filename}
    return os.path.join('scorecard_documents', year, month, day, safe_filename)


class AttributeResponseDocument(models.Model):
    """
    Supporting documents uploaded for a specific attribute response.
    Multiple documents can be uploaded for a single attribute response.
    """
    attribute_response = models.ForeignKey(
        AttributeResponse,
        on_delete=models.CASCADE,
        related_name="documents"
    )
    file = models.FileField(
        upload_to=get_document_upload_path,
        help_text="Supporting document file"
    )
    file_name = models.CharField(
        max_length=255,
        help_text="Original filename"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_documents"
    )

    class Meta:
        db_table = "SCORECARD_ATTRIBUTE_RESPONSE_DOCUMENT"
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return f"{self.attribute_response} - {self.file_name}"


class ScorecardDocument(models.Model):
    """
    General scorecard documents that can be uploaded and managed independently.
    These are not tied to specific questionnaire responses.
    """
    title = models.CharField(
        max_length=255,
        help_text="Title or name for this document"
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description of the document"
    )
    file = models.FileField(
        upload_to=get_scorecard_document_upload_path,
        help_text="Document file"
    )
    file_name = models.CharField(
        max_length=255,
        help_text="Original filename"
    )
    category = models.CharField(
        max_length=100,
        blank=True,
        help_text="Optional category for organizing documents"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_scorecard_documents"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this document is active and visible"
    )

    class Meta:
        db_table = "SCORECARD_DOCUMENT"
        ordering = ["-uploaded_at"]
        verbose_name = "Scorecard Document"
        verbose_name_plural = "Scorecard Documents"

    def __str__(self) -> str:
        return f"{self.title} - {self.file_name}"


class EvaluationHistory(models.Model):
    """
    Historical record of all previous weighted scores and grades for an evaluation.
    Each time an evaluation is edited, the previous values are saved here.
    This allows tracking of all changes over time, not just the most recent previous values.
    """
    evaluation = models.ForeignKey(
        CreditEvaluation,
        on_delete=models.CASCADE,
        related_name="history_records"
    )
    
    # The values that were stored before the edit
    weighted_percent = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Weighted percent at this point in time"
    )
    grade = models.CharField(
        max_length=10, blank=True,
        help_text="Grade at this point in time"
    )
    raw_score = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Raw score at this point in time"
    )
    
    # Timestamp when this history record was created (when the edit happened)
    recorded_at = models.DateTimeField(default=timezone.now)
    
    class Meta:
        db_table="SCORECARD_EVALUATION_HISTORY"
        ordering = ["-recorded_at"]
        verbose_name = "Evaluation History"
        verbose_name_plural = "Evaluation Histories"

    def save(self, *args, **kwargs):
        normalize_grade_fields(self, "grade")
        super().save(*args, **kwargs)
    
    def __str__(self) -> str:
        return f"{self.evaluation_id} - {self.weighted_percent}% ({self.grade}) at {self.recorded_at}"


class EvaluationVersion(models.Model):
    """
    Complete snapshot of an evaluation at a specific point in time.
    Stores all attribute responses, driver scores, section scores, and summary data.
    Each time an evaluation is submitted or edited, a new version is created.
    This allows full comparison between versions.
    """
    evaluation = models.ForeignKey(
        CreditEvaluation,
        on_delete=models.CASCADE,
        related_name="versions"
    )
    
    version_number = models.PositiveIntegerField(
        help_text="Version number (1 for initial submission, increments on each edit)"
    )
    
    # Flag to mark if this version was approved
    is_approved = models.BooleanField(
        default=False,
        help_text="True if this version was approved by a checker"
    )
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this version was approved"
    )
    approved_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_versions',
        help_text="User who approved this version"
    )
    
    # Summary scores at this version
    total_raw_score = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    total_weighted_percent = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )
    final_grade = models.CharField(max_length=10, blank=True)
    
    # Metadata
    created_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="evaluation_versions"
    )
    created_at = models.DateTimeField(default=timezone.now)
    change_description = models.TextField(
        blank=True,
        help_text="Description of what changed in this version"
    )
    autofill_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Auto-fill summary and customer profile snapshot captured for this version."
    )
    
    class Meta:
        db_table = "SCORECARD_EVALUATION_VERSION"
        ordering = ["evaluation", "-version_number"]
        unique_together = [["evaluation", "version_number"]]
        verbose_name = "Evaluation Version"
        verbose_name_plural = "Evaluation Versions"
        indexes = [
            models.Index(fields=["evaluation", "is_approved", "version_number"], name="SC_EVAL_VER_APPROVAL_IDX"),
        ]

    def save(self, *args, **kwargs):
        normalize_grade_fields(self, "final_grade")
        super().save(*args, **kwargs)
    
    def __str__(self) -> str:
        return f"{self.evaluation_id} - Version {self.version_number} ({self.total_weighted_percent}% - {self.final_grade})"


class EvaluationVersionAttributeResponse(models.Model):
    """
    Attribute response snapshot for a specific evaluation version.
    """
    version = models.ForeignKey(
        EvaluationVersion,
        on_delete=models.CASCADE,
        related_name="attribute_responses"
    )
    attribute = models.ForeignKey(
        Attribute,
        on_delete=models.PROTECT,
        related_name="version_responses"
    )
    option = models.ForeignKey(
        Option,
        on_delete=models.PROTECT,
        related_name="version_responses",
        null=True,
        blank=True,
    )
    raw_value = models.CharField(max_length=255, blank=True)
    allocated_score = models.DecimalField(max_digits=10, decimal_places=2)
    
    class Meta:
        db_table = "SCORECARD_EVAL_VERSION_ATTR_RESPONSE"
    
    def __str__(self) -> str:
        return f"Version {self.version.version_number} - {self.attribute.code} -> {self.allocated_score}"


class EvaluationVersionDriverScore(models.Model):
    """
    Risk driver score snapshot for a specific evaluation version.
    """
    version = models.ForeignKey(
        EvaluationVersion,
        on_delete=models.CASCADE,
        related_name="driver_scores"
    )
    risk_driver = models.ForeignKey(
        RiskDriver,
        on_delete=models.PROTECT,
        related_name="version_scores"
    )
    raw_score = models.DecimalField(max_digits=10, decimal_places=2)
    weighted_percent = models.DecimalField(max_digits=6, decimal_places=2)
    proof = models.CharField(max_length=50, blank=True)
    
    class Meta:
        db_table = "SCORECARD_EVAL_VERSION_DRIVER_SCORE"
    
    def __str__(self) -> str:
        return f"Version {self.version.version_number} - {self.risk_driver.code} - {self.raw_score}/{self.weighted_percent}%"


class EvaluationVersionSectionScore(models.Model):
    """
    Section score snapshot for a specific evaluation version.
    """
    version = models.ForeignKey(
        EvaluationVersion,
        on_delete=models.CASCADE,
        related_name="section_scores"
    )
    section = models.ForeignKey(
        Section,
        on_delete=models.PROTECT,
        related_name="version_scores"
    )
    raw_score = models.DecimalField(max_digits=10, decimal_places=2)
    weighted_percent = models.DecimalField(max_digits=6, decimal_places=2)
    
    class Meta:
        db_table = "SCORECARD_EVAL_VERSION_SECTION_SCORE"
    
    def __str__(self) -> str:
        return f"Version {self.version.version_number} - {self.section.code} - {self.raw_score}/{self.weighted_percent}%"


class TemplateVersion(models.Model):
    """
    Complete snapshot of a template structure at a specific point in time.
    Stores all sections, risk drivers, attributes, and options.
    Each time a template is edited and submitted, a new version is created.
    """
    template = models.ForeignKey(
        BaselScoreSheetTemplate,
        on_delete=models.CASCADE,
        related_name="versions"
    )
    
    version_number = models.PositiveIntegerField(
        help_text="Version number (1 for initial submission, increments on each edit)"
    )
    
    # Flag to mark if this version was approved
    is_approved = models.BooleanField(
        default=False,
        help_text="True if this version was approved by a checker"
    )
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this version was approved"
    )
    approved_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_template_versions',
        help_text="User who approved this version"
    )
    
    # Template metadata snapshot
    template_code = models.CharField(
        max_length=50,
        blank=True,
        help_text="Template code at time of version creation"
    )
    template_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Template name at time of version creation"
    )
    template_description = models.TextField(
        blank=True,
        help_text="Template description at time of version creation"
    )
    template_version = models.CharField(
        max_length=20,
        blank=True,
        help_text="Template version string at time of version creation"
    )
    
    # Formula configuration snapshot (same for all sections)
    formula_actual_score = models.CharField(
        max_length=255,
        blank=True,
        help_text="Formula for ACTUAL_SCORE at time of version creation"
    )
    formula_weighted_score = models.CharField(
        max_length=255,
        blank=True,
        help_text="Formula for WEIGHTED_SCORE at time of version creation"
    )
    formula_proof = models.CharField(
        max_length=255,
        blank=True,
        help_text="Formula for PROOF at time of version creation"
    )
    
    # Metadata
    created_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="template_versions"
    )
    created_at = models.DateTimeField(default=timezone.now)
    change_description = models.TextField(
        blank=True,
        help_text="Description of what changed in this version"
    )
    
    class Meta:
        db_table = "SCORECARD_TEMPLATE_VERSION"
        ordering = ["template", "-version_number"]
        unique_together = [["template", "version_number"]]
        verbose_name = "Template Version"
        verbose_name_plural = "Template Versions"
    
    def __str__(self) -> str:
        return f"{self.template.code} - Version {self.version_number}"


class TemplateVersionSection(models.Model):
    """Section snapshot for a specific template version."""
    version = models.ForeignKey(
        TemplateVersion,
        on_delete=models.CASCADE,
        related_name="sections"
    )
    section = models.ForeignKey(
        Section,
        on_delete=models.PROTECT,
        related_name="template_version_sections"
    )
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    display_order = models.PositiveIntegerField(default=1)
    
    class Meta:
        db_table = "SCORECARD_TEMPLATE_VERSION_SECTION"
    
    def __str__(self) -> str:
        return f"Version {self.version.version_number} - Section {self.code}"


class TemplateVersionRiskDriver(models.Model):
    """Risk driver snapshot for a specific template version."""
    version = models.ForeignKey(
        TemplateVersion,
        on_delete=models.CASCADE,
        related_name="risk_drivers"
    )
    risk_driver = models.ForeignKey(
        RiskDriver,
        on_delete=models.PROTECT,
        related_name="template_version_drivers"
    )
    section_version = models.ForeignKey(
        TemplateVersionSection,
        on_delete=models.CASCADE,
        related_name="risk_drivers"
    )
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    weight_percent = models.DecimalField(max_digits=6, decimal_places=2)
    max_score = models.DecimalField(max_digits=10, decimal_places=2)
    display_order = models.PositiveIntegerField(default=1)
    
    class Meta:
        db_table = "SCORECARD_TEMPLATE_VERSION_RISK_DRIVER"
    
    def __str__(self) -> str:
        return f"Version {self.version.version_number} - Driver {self.code}"


class TemplateVersionAttribute(models.Model):
    """Attribute snapshot for a specific template version."""
    version = models.ForeignKey(
        TemplateVersion,
        on_delete=models.CASCADE,
        related_name="attributes"
    )
    attribute = models.ForeignKey(
        Attribute,
        on_delete=models.PROTECT,
        related_name="template_version_attributes"
    )
    driver_version = models.ForeignKey(
        TemplateVersionRiskDriver,
        on_delete=models.CASCADE,
        related_name="attributes"
    )
    code = models.CharField(max_length=50)
    label = models.CharField(max_length=255)
    help_text = models.TextField(blank=True)
    data_type = models.CharField(max_length=20, default="choice")
    input_type = models.CharField(max_length=20, default="radio")
    is_required = models.BooleanField(default=True)
    group_label = models.CharField(max_length=255, blank=True)
    weight_percent = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    display_order = models.PositiveIntegerField(default=1)
    requires_document = models.BooleanField(default=False)
    
    class Meta:
        db_table = "SCORECARD_TEMPLATE_VERSION_ATTRIBUTE"
    
    def __str__(self) -> str:
        return f"Version {self.version.version_number} - Attribute {self.code}"


class TemplateVersionOption(models.Model):
    """Option snapshot for a specific template version."""
    version = models.ForeignKey(
        TemplateVersion,
        on_delete=models.CASCADE,
        related_name="options"
    )
    option = models.ForeignKey(
        Option,
        on_delete=models.PROTECT,
        related_name="template_version_options"
    )
    attribute_version = models.ForeignKey(
        TemplateVersionAttribute,
        on_delete=models.CASCADE,
        related_name="options"
    )
    label = models.CharField(max_length=255)
    value = models.CharField(max_length=255)
    allocated_score = models.DecimalField(max_digits=10, decimal_places=2)
    display_order = models.PositiveIntegerField(default=1)
    is_default = models.BooleanField(default=False)
    
    class Meta:
        db_table = "SCORECARD_TEMPLATE_VERSION_OPTION"
    
    def __str__(self) -> str:
        return f"Version {self.version.version_number} - Option {self.label}"


class TemplateWorkflowHistory(models.Model):
    """
    Audit log for all workflow status changes in the template maker-checker process.
    Records every status transition with user, timestamp, and comments.
    """
    template = models.ForeignKey(
        BaselScoreSheetTemplate,
        on_delete=models.CASCADE,
        related_name='workflow_history'
    )
    
    ACTION_CHOICES = [
        ('created', 'Created'),
        ('saved_draft', 'Saved Draft'),
        ('submitted', 'Submitted for Review'),
        ('returned', 'Returned for Changes'),
        ('approved', 'Approved'),
        ('reopened', 'Reopened'),
        ('cancelled', 'Cancelled'),
        ('withdrawn', 'Withdrawn'),
        ('reassigned', 'Reassigned'),
    ]
    
    action = models.CharField(
        max_length=20,
        choices=ACTION_CHOICES,
        help_text="Action taken on the template"
    )
    
    from_status = models.CharField(
        max_length=20,
        choices=BaselScoreSheetTemplate.STATUS_CHOICES,
        null=True,
        blank=True,
        help_text="Previous status"
    )
    
    to_status = models.CharField(
        max_length=20,
        choices=BaselScoreSheetTemplate.STATUS_CHOICES,
        help_text="New status"
    )
    
    performed_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='template_workflow_actions',
        help_text="User who performed this action"
    )
    
    performed_at = models.DateTimeField(default=timezone.now)
    
    comments = models.TextField(
        blank=True,
        help_text="Optional comments or reason for this action"
    )
    
    class Meta:
        db_table = "SCORECARD_TEMPLATE_WORKFLOW_HISTORY"
        ordering = ["-performed_at"]
        verbose_name = "Template Workflow History"
        verbose_name_plural = "Template Workflow Histories"
    
    def __str__(self) -> str:
        return f"{self.template.code} - {self.action} ({self.from_status} → {self.to_status}) by {self.performed_by}"


# ============================================================================
# IFRS9 SCORE CONFIGURATION MODELS
# ============================================================================

class IFRS9ScoreSheetTemplate(models.Model):
    """
    Configuration for an IFRS9 credit score sheet.
    This is the main template definition that everything else hangs off.
    Similar to BaselScoreSheetTemplate but for IFRS9 scoring.
    """

    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    version = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)

    # Formula configuration (same for all sections)
    # ACTUAL_SCORE = ALLOCATED_SCORE
    # WEIGHTED_SCORE = ACTUAL_SCORE / Highest Possible Score * WEIGHT
    # PROOF = validation / audit rule
    formula_actual_score = models.CharField(
        max_length=255,
        default="ALLOCATED_SCORE",
        help_text="Formula for ACTUAL_SCORE (default: ALLOCATED_SCORE)",
    )
    formula_weighted_score = models.CharField(
        max_length=255,
        default="ACTUAL_SCORE / Highest_Possible_Score * WEIGHT",
        help_text="Formula for WEIGHTED_SCORE (default: ACTUAL_SCORE / Highest_Possible_Score * WEIGHT)",
    )
    formula_proof = models.CharField(
        max_length=255,
        default="IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE",
        help_text="Formula for PROOF validation/audit rule",
    )

    # Maker-Checker Workflow Fields
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('in_progress', 'In Progress'),
        ('submitted', 'Submitted for Review'),
        ('returned', 'Returned for Changes'),
        ('approved', 'Approved'),
        ('cancelled', 'Cancelled'),
    ]
    
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='approved',  # Existing templates are considered approved
        help_text="Status of the template workflow"
    )
    
    maker = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='made_ifrs9_templates',
        null=True,
        blank=True,
        help_text="User responsible for creating/editing this template"
    )
    
    checker = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='checked_ifrs9_templates',
        null=True,
        blank=True,
        help_text="User responsible for reviewing and approving this template"
    )
    
    submitted_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='submitted_ifrs9_templates',
        null=True,
        blank=True,
        help_text="User who submitted this template for review"
    )
    
    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this template was submitted for review"
    )
    
    approved_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='approved_ifrs9_templates',
        null=True,
        blank=True,
        help_text="User who approved this template"
    )
    
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this template was approved"
    )
    
    returned_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='returned_ifrs9_templates',
        null=True,
        blank=True,
        help_text="User who returned this template for changes"
    )
    
    returned_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this template was returned for changes"
    )
    
    return_reason = models.TextField(
        blank=True,
        help_text="Reason for returning the template for changes"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table="SCORECARD_IFRS9_SCORES_TEMPLATE"
        verbose_name_plural = "IFRS9 Score Sheet Templates"

    def __str__(self) -> str:
        return f"{self.code} - {self.name} (v{self.version})" if self.version else f"{self.code} - {self.name}"
    
    def can_be_edited_by(self, user):
        """Check if user can edit this template"""
        if not getattr(user, "is_authenticated", False):
            return False
        can_manage = bool(
            getattr(user, "is_superuser", False)
            or getattr(user, "has_perm", lambda perm: False)("scorecard.manage_ifrs9_templates")
        )
        # Cancelled templates cannot be edited
        if self.status == 'cancelled':
            return False
        # Submitted templates are locked (must be withdrawn first)
        if self.status == 'submitted':
            return False
        # Approved templates can be edited (will create new version)
        if self.status == 'approved' or self.status is None or self.status == '':
            return can_manage or self.maker == user or self.submitted_by == user or self.maker is None
        # Draft, in_progress, returned can be edited by maker or superuser
        if self.status in ['draft', 'in_progress', 'returned']:
            return self.maker == user or self.submitted_by == user or can_manage or self.maker is None
        return False
    
    def can_be_reviewed_by(self, user):
        """Check if user can review this template"""
        if self.status != 'submitted':
            return False
        if not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False):
            return True
        if not getattr(user, "has_perm", lambda perm: False)("scorecard.review_ifrs9_templates"):
            return False
        checker_id = getattr(self, "checker_id", None)
        user_id = getattr(user, "id", None)
        if checker_id and checker_id != user_id:
            return False
        return True


class IFRS9Section(models.Model):
    """
    High-level grouping for IFRS9 score sheets.
    """

    template = models.ForeignKey(
        IFRS9ScoreSheetTemplate, on_delete=models.CASCADE, related_name="sections"
    )
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    display_order = models.PositiveIntegerField(default=1)
    is_deleted = models.BooleanField(default=False)

    objects = ActiveIFRS9SectionManager()
    all_objects = models.Manager()

    class Meta:
        db_table="SCORECARD_IFRS9_SCORES_SECTION"
        ordering = ["display_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["template", "code"],
                condition=Q(is_deleted=False),
                name="scorecard_unique_active_ifrs9_section_code",
            )
        ]

    def __str__(self) -> str:
        return f"{self.template.code}::{self.code} - {self.name}"

    def get_total_weight_percent(self) -> Decimal:
        """
        Calculate the total weight percentage from all attributes across all risk drivers in this section.
        """
        from django.db.models import Sum
        total = IFRS9Attribute.objects.filter(
            risk_driver__section=self
        ).aggregate(Sum("weight_percent"))["weight_percent__sum"]
        return Decimal(str(total)) if total else Decimal("0")

    def get_total_max_score(self) -> Decimal:
        """
        Calculate the total maximum score by summing all attributes' maximum scores across all risk drivers in this section.
        Each attribute's maximum score is the highest allocated_score from all its options.
        """
        from django.db.models import Max
        
        total = Decimal("0")
        for attribute in IFRS9Attribute.objects.filter(risk_driver__section=self):
            max_score = IFRS9Option.objects.filter(
                attribute=attribute
            ).aggregate(Max("allocated_score"))["allocated_score__max"]
            if max_score is not None:
                total += Decimal(str(max_score))
        return total


class IFRS9RiskDriver(models.Model):
    """
    Risk driver inside an IFRS9 section.
    Each has a weight (%) in the overall score sheet and a maximum possible score.
    """

    section = models.ForeignKey(
        IFRS9Section, on_delete=models.CASCADE, related_name="risk_drivers"
    )
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)

    # Weight as a percentage of the total score (e.g. 10.00, 3.00 etc)
    weight_percent = models.DecimalField(max_digits=6, decimal_places=2)

    # Maximum possible raw score for this driver (e.g. 10, 30, 45, 160)
    max_score = models.DecimalField(max_digits=10, decimal_places=2)

    display_order = models.PositiveIntegerField(default=1)
    is_deleted = models.BooleanField(default=False)

    objects = ActiveSoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        db_table="SCORECARD_IFRS9_SCORES_RISK_DRIVER"
        ordering = ["display_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["section", "code"],
                condition=Q(is_deleted=False),
                name="scorecard_unique_active_ifrs9_risk_driver_code",
            )
        ]

    def __str__(self) -> str:
        return f"{self.section.template.code}::{self.section.code}::{self.code} - {self.name}"

    def get_total_weight_percent(self) -> Decimal:
        """
        Calculate the total weight percentage from all attributes.
        The driver's weight is the sum of all its attributes' weights.
        """
        from django.db.models import Sum
        total = self.attributes.aggregate(Sum("weight_percent"))["weight_percent__sum"]
        return Decimal(str(total)) if total else Decimal("0")

    def get_max_score(self) -> Decimal:
        """
        Calculate the maximum possible score from all attributes.
        Returns the single highest allocated_score from all options across all attributes.
        """
        from django.db.models import Max
        
        max_score = IFRS9Option.objects.filter(
            attribute__risk_driver=self
        ).aggregate(Max("allocated_score"))["allocated_score__max"]
        return Decimal(str(max_score)) if max_score is not None else Decimal("0")


class IFRS9Attribute(models.Model):
    """
    Individual question/attribute under an IFRS9 risk driver.
    The actual selectable choices (with scores) are stored as IFRS9Options.
    """

    DATA_TYPE_CHOICES = [
        ("choice", "Choice"),
        ("integer", "Integer"),
        ("decimal", "Decimal"),
        ("text", "Text"),
    ]

    INPUT_TYPE_CHOICES = [
        ("radio", "Radio"),
        ("select", "Select"),
        ("checkbox", "Checkbox"),
    ]

    risk_driver = models.ForeignKey(
        IFRS9RiskDriver, on_delete=models.CASCADE, related_name="attributes"
    )
    code = models.CharField(max_length=50)
    label = models.CharField(max_length=255)
    help_text = models.TextField(blank=True)

    data_type = models.CharField(
        max_length=20, choices=DATA_TYPE_CHOICES, default="choice"
    )
    input_type = models.CharField(
        max_length=20, choices=INPUT_TYPE_CHOICES, default="radio"
    )
    is_required = models.BooleanField(default=True)

    # Optional grouping
    group_label = models.CharField(max_length=255, blank=True)

    # Weight as a percentage of the total score (e.g. 6.00, 5.00 etc)
    weight_percent = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    display_order = models.PositiveIntegerField(default=1)
    is_deleted = models.BooleanField(default=False)

    # Whether this attribute requires document upload when an option is selected
    requires_document = models.BooleanField(
        default=False,
        help_text="If enabled, users will be able to upload supporting documents when selecting an option for this attribute."
    )

    objects = ActiveSoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        db_table="SCORECARD_IFRS9_SCORES_ATTRIBUTE"
        ordering = ["display_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["risk_driver", "code"],
                condition=Q(is_deleted=False),
                name="scorecard_unique_active_ifrs9_attribute_code",
            )
        ]

    def __str__(self) -> str:
        return f"{self.risk_driver}::{self.code} - {self.label}"


class IFRS9Option(models.Model):
    """
    Allowed values for an IFRS9 attribute and their allocated scores.
    """

    attribute = models.ForeignKey(
        IFRS9Attribute, on_delete=models.CASCADE, related_name="options"
    )
    label = models.CharField(max_length=255)
    value = models.CharField(max_length=255, blank=True)

    allocated_score = models.DecimalField(max_digits=10, decimal_places=2)
    display_order = models.PositiveIntegerField(default=1)
    is_default = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)

    objects = ActiveSoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        db_table="SCORECARD_IFRS9_SCORES_OPTION"
        ordering = ["display_order", "id"]

    def __str__(self) -> str:
        return f"{self.attribute} -> {self.label} ({self.allocated_score})"


class IFRS9GradeBand(models.Model):
    """
    Mapping from total weighted percentage to a grade for IFRS9 templates.
    """

    template = models.ForeignKey(
        IFRS9ScoreSheetTemplate, on_delete=models.CASCADE, related_name="grade_bands"
    )
    grade_code = models.CharField(max_length=10)
    description = models.CharField(max_length=255, blank=True)

    min_percent = models.DecimalField(max_digits=6, decimal_places=2)
    max_percent = models.DecimalField(max_digits=6, decimal_places=2)

    display_order = models.PositiveIntegerField(default=1)

    class Meta:
        db_table="SCORECARD_IFRS9_SCORES_GRADE_BAND"
        ordering = ["display_order", "id"]

    def save(self, *args, **kwargs):
        normalize_grade_fields(self, "grade_code")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.template.code} {self.grade_code} [{self.min_percent}-{self.max_percent}]"


class IFRS9TemplateVersion(models.Model):
    """
    Complete snapshot of an IFRS9 template structure at a specific point in time.
    Stores all sections, risk drivers, attributes, and options.
    Each time a template is edited and submitted, a new version is created.
    """
    template = models.ForeignKey(
        IFRS9ScoreSheetTemplate,
        on_delete=models.CASCADE,
        related_name="versions"
    )
    
    version_number = models.PositiveIntegerField(
        help_text="Version number (1 for initial submission, increments on each edit)"
    )
    
    # Flag to mark if this version was approved
    is_approved = models.BooleanField(
        default=False,
        help_text="True if this version was approved by a checker"
    )
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this version was approved"
    )
    approved_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_ifrs9_template_versions',
        help_text="User who approved this version"
    )
    
    # Template metadata snapshot
    template_code = models.CharField(
        max_length=50,
        blank=True,
        help_text="Template code at time of version creation"
    )
    template_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Template name at time of version creation"
    )
    template_description = models.TextField(
        blank=True,
        help_text="Template description at time of version creation"
    )
    template_version = models.CharField(
        max_length=20,
        blank=True,
        help_text="Template version string at time of version creation"
    )
    
    # Formula configuration snapshot (same for all sections)
    formula_actual_score = models.CharField(
        max_length=255,
        blank=True,
        help_text="Formula for ACTUAL_SCORE at time of version creation"
    )
    formula_weighted_score = models.CharField(
        max_length=255,
        blank=True,
        help_text="Formula for WEIGHTED_SCORE at time of version creation"
    )
    formula_proof = models.CharField(
        max_length=255,
        blank=True,
        help_text="Formula for PROOF at time of version creation"
    )
    
    # Metadata
    created_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ifrs9_template_versions"
    )
    created_at = models.DateTimeField(default=timezone.now)
    change_description = models.TextField(
        blank=True,
        help_text="Description of what changed in this version"
    )
    
    class Meta:
        db_table = "SCORECARD_IFRS9_SCORES_TEMPLATE_VERSION"
        ordering = ["template", "-version_number"]
        unique_together = [["template", "version_number"]]
        verbose_name = "IFRS9 Template Version"
        verbose_name_plural = "IFRS9 Template Versions"
    
    def __str__(self) -> str:
        return f"{self.template.code} - Version {self.version_number}"


class IFRS9TemplateVersionSection(models.Model):
    """Section snapshot for a specific IFRS9 template version."""
    version = models.ForeignKey(
        IFRS9TemplateVersion,
        on_delete=models.CASCADE,
        related_name="sections"
    )
    section = models.ForeignKey(
        IFRS9Section,
        on_delete=models.PROTECT,
        related_name="template_version_sections"
    )
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    display_order = models.PositiveIntegerField(default=1)
    
    class Meta:
        db_table = "SCORECARD_IFRS9_SCORES_TEMPLATE_VERSION_SECTION"
    
    def __str__(self) -> str:
        return f"Version {self.version.version_number} - Section {self.code}"


class IFRS9TemplateVersionRiskDriver(models.Model):
    """Risk driver snapshot for a specific IFRS9 template version."""
    version = models.ForeignKey(
        IFRS9TemplateVersion,
        on_delete=models.CASCADE,
        related_name="risk_drivers"
    )
    risk_driver = models.ForeignKey(
        IFRS9RiskDriver,
        on_delete=models.PROTECT,
        related_name="template_version_drivers"
    )
    section_version = models.ForeignKey(
        IFRS9TemplateVersionSection,
        on_delete=models.CASCADE,
        related_name="risk_drivers"
    )
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    weight_percent = models.DecimalField(max_digits=6, decimal_places=2)
    max_score = models.DecimalField(max_digits=10, decimal_places=2)
    display_order = models.PositiveIntegerField(default=1)
    
    class Meta:
        db_table = "SCORECARD_IFRS9_SCORES_TEMPLATE_VERSION_RISK_DRIVER"
    
    def __str__(self) -> str:
        return f"Version {self.version.version_number} - Driver {self.code}"


class IFRS9TemplateVersionAttribute(models.Model):
    """Attribute snapshot for a specific IFRS9 template version."""
    version = models.ForeignKey(
        IFRS9TemplateVersion,
        on_delete=models.CASCADE,
        related_name="attributes"
    )
    attribute = models.ForeignKey(
        IFRS9Attribute,
        on_delete=models.PROTECT,
        related_name="template_version_attributes"
    )
    driver_version = models.ForeignKey(
        IFRS9TemplateVersionRiskDriver,
        on_delete=models.CASCADE,
        related_name="attributes"
    )
    code = models.CharField(max_length=50)
    label = models.CharField(max_length=255)
    help_text = models.TextField(blank=True)
    data_type = models.CharField(max_length=20, default="choice")
    input_type = models.CharField(max_length=20, default="radio")
    is_required = models.BooleanField(default=True)
    group_label = models.CharField(max_length=255, blank=True)
    weight_percent = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    display_order = models.PositiveIntegerField(default=1)
    requires_document = models.BooleanField(default=False)
    
    class Meta:
        db_table = "SCORECARD_IFRS9_SCORES_TEMPLATE_VERSION_ATTRIBUTE"
    
    def __str__(self) -> str:
        return f"Version {self.version.version_number} - Attribute {self.code}"


class IFRS9TemplateVersionOption(models.Model):
    """Option snapshot for a specific IFRS9 template version."""
    version = models.ForeignKey(
        IFRS9TemplateVersion,
        on_delete=models.CASCADE,
        related_name="options"
    )
    option = models.ForeignKey(
        IFRS9Option,
        on_delete=models.PROTECT,
        related_name="template_version_options"
    )
    attribute_version = models.ForeignKey(
        IFRS9TemplateVersionAttribute,
        on_delete=models.CASCADE,
        related_name="options"
    )
    label = models.CharField(max_length=255)
    value = models.CharField(max_length=255)
    allocated_score = models.DecimalField(max_digits=10, decimal_places=2)
    display_order = models.PositiveIntegerField(default=1)
    is_default = models.BooleanField(default=False)
    
    class Meta:
        db_table = "SCORECARD_IFRS9_SCORES_TEMPLATE_VERSION_OPTION"
    
    def __str__(self) -> str:
        return f"Version {self.version.version_number} - Option {self.label}"


class IFRS9TemplateWorkflowHistory(models.Model):
    """
    Audit log for all workflow status changes in the IFRS9 template maker-checker process.
    Records every status transition with user, timestamp, and comments.
    """
    template = models.ForeignKey(
        IFRS9ScoreSheetTemplate,
        on_delete=models.CASCADE,
        related_name='workflow_history'
    )
    
    ACTION_CHOICES = [
        ('created', 'Created'),
        ('saved_draft', 'Saved Draft'),
        ('submitted', 'Submitted for Review'),
        ('returned', 'Returned for Changes'),
        ('approved', 'Approved'),
        ('reopened', 'Reopened'),
        ('cancelled', 'Cancelled'),
        ('withdrawn', 'Withdrawn'),
        ('reassigned', 'Reassigned'),
    ]
    
    action = models.CharField(
        max_length=20,
        choices=ACTION_CHOICES,
        help_text="Action taken on the template"
    )
    
    from_status = models.CharField(
        max_length=20,
        choices=IFRS9ScoreSheetTemplate.STATUS_CHOICES,
        null=True,
        blank=True,
        help_text="Previous status"
    )
    
    to_status = models.CharField(
        max_length=20,
        choices=IFRS9ScoreSheetTemplate.STATUS_CHOICES,
        help_text="New status"
    )
    
    performed_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='ifrs9_template_workflow_actions',
        help_text="User who performed this action"
    )
    
    performed_at = models.DateTimeField(default=timezone.now)
    
    comments = models.TextField(
        blank=True,
        help_text="Optional comments or reason for this action"
    )
    
    class Meta:
        db_table = "SCORECARD_IFRS9_SCORES_TEMPLATE_WORKFLOW_HISTORY"
        ordering = ["-performed_at"]
        verbose_name = "IFRS9 Template Workflow History"
        verbose_name_plural = "IFRS9 Template Workflow Histories"
    
    def __str__(self) -> str:
        return f"{self.template.code} - {self.action} ({self.from_status} → {self.to_status}) by {self.performed_by}"


# ============================================================================
# IFRS9 EVALUATION MODELS (Similar to Basel CreditEvaluation)
# ============================================================================

class IFRS9Evaluation(models.Model):
    """
    One completed IFRS9 score sheet for a specific customer at a branch,
    using a particular IFRS9 score sheet template.
    Similar to CreditEvaluation but for IFRS9 scoring.
    """
    
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('in_progress', 'In Progress'),
        ('submitted', 'Submitted (Pending Review)'),
        ('returned', 'Returned (Needs Changes)'),
        ('approved', 'Approved (Completed)'),
        ('cancelled', 'Cancelled / Voided'),
        ('completed', 'Completed'),  # Keep for backward compatibility
    ]

    template = models.ForeignKey(
        IFRS9ScoreSheetTemplate,
        on_delete=models.PROTECT,
        related_name="evaluations",
        null=True,
        blank=True,
    )
    template_section_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Cached template name used for reporting alongside template_id.",
    )

    # Minimal branch & customer info – can be replaced with actual FK models later.
    branch_name = models.CharField(max_length=255)
    customer_name = models.CharField(max_length=255)
    customer_id = models.CharField(max_length=100)

    total_raw_score = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    total_weighted_percent = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )
    final_grade = models.CharField(max_length=10, blank=True)
    
    # Status field for draft/in-progress/completed workflow
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='draft',
        help_text="Status of the IFRS9 score form workflow"
    )
    
    # Maker-Checker Workflow Fields
    maker = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='made_ifrs9_evaluations',
        null=True,
        blank=True,
        help_text="User who created/edited this evaluation"
    )
    checker = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='checked_ifrs9_evaluations',
        null=True,
        blank=True,
        help_text="User assigned to review this evaluation"
    )
    
    # Submission tracking
    submitted_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='submitted_ifrs9_evaluations',
        null=True,
        blank=True,
        help_text="User who submitted for review"
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    
    # Approval tracking
    approved_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='approved_ifrs9_evaluations',
        null=True,
        blank=True,
        help_text="User who approved this evaluation"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    
    # Return/Rejection tracking
    returned_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='returned_ifrs9_evaluations',
        null=True,
        blank=True,
        help_text="User who returned this evaluation for changes"
    )
    returned_at = models.DateTimeField(null=True, blank=True)
    return_reason = models.TextField(
        blank=True,
        help_text="Reason/comments for returning the evaluation"
    )
    
    # Versioning
    version = models.IntegerField(
        default=1,
        help_text="Version number - increments on each resubmission"
    )
    resubmission_count = models.IntegerField(
        default=0,
        help_text="Number of times this evaluation has been resubmitted"
    )
    
    # Score form period/date (optional)
    score_form_period = models.DateField(
        null=True,
        blank=True,
        help_text="Period/date for this score form (e.g., assessment date)"
    )
    
    # Previous values (preserved when editing)
    previous_weighted_percent = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Previous weighted percent before last edit"
    )
    previous_grade = models.CharField(
        max_length=10, blank=True,
        help_text="Previous grade before last edit"
    )
    
    # Approved values (last approved scores - shown as "current" when status is 'submitted')
    approved_weighted_percent = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Last approved weighted percent (shown as current when status is 'submitted')"
    )
    approved_grade = models.CharField(
        max_length=10, blank=True,
        help_text="Last approved grade (shown as current when status is 'submitted')"
    )
    autofill_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Saved auto-fill summary, source profile snapshot, and user overrides."
    )
    
    def get_current_weighted_percent(self):
        """Get the current weighted percent to display based on status"""
        if self.status == 'submitted':
            return self.approved_weighted_percent if self.approved_weighted_percent is not None else None
        elif self.status == 'returned':
            return self.approved_weighted_percent if self.approved_weighted_percent is not None else None
        else:
            return self.total_weighted_percent
    
    def get_current_grade(self):
        """Get the current grade to display based on status"""
        if self.status == 'submitted':
            return self.approved_grade if self.approved_grade else None
        elif self.status == 'returned':
            return self.approved_grade if self.approved_grade else None
        else:
            return self.final_grade
    
    def get_pending_weighted_percent(self):
        """Get the pending weighted percent (new submitted score) when status is 'submitted'"""
        if self.status == 'submitted':
            latest_unapproved_version = self.versions.filter(is_approved=False).order_by('-version_number').first()
            if latest_unapproved_version:
                return latest_unapproved_version.total_weighted_percent
            return None
        return None
    
    def get_pending_grade(self):
        """Get the pending grade (new submitted grade) when status is 'submitted'"""
        if self.status == 'submitted':
            latest_unapproved_version = self.versions.filter(is_approved=False).order_by('-version_number').first()
            if latest_unapproved_version:
                return latest_unapproved_version.final_grade
            return None
        return None

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table="SCORECARD_IFRS9_EVALUATION"
        ordering = ["-created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch_name", "customer_id"],
                name="uq_ifrs9_eval_branch_customer",
            ),
        ]
        indexes = [
            models.Index(fields=["branch_name", "status"], name="SC_IFRS9_BRANCH_STATUS_IDX"),
            models.Index(fields=["status", "submitted_at"], name="SC_IFRS9_STATUS_SUB_AT_IDX"),
            models.Index(fields=["status", "created_at"], name="SC_IFRS9_STATUS_CREATED_IDX"),
            models.Index(fields=["template", "status"], name="SC_IFRS9_TEMPLATE_STATUS_IDX"),
        ]

    def save(self, *args, **kwargs):
        normalize_grade_fields(self, "final_grade", "previous_grade", "approved_grade")
        if self.template_id and self.template is not None:
            self.template_section_name = self.template.name or ""
        else:
            self.template_section_name = ""
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        template_code = self.template.code if self.template_id and self.template is not None else "EXTERNAL_IMPORT"
        return f"{template_code} eval for {self.customer_name} at {self.branch_name} on {self.created_at.date()}"
    
    def can_be_edited_by(self, user):
        """Check if user can edit this evaluation"""
        if not getattr(user, "is_authenticated", False):
            return False
        can_manage = bool(
            getattr(user, "is_superuser", False)
            or getattr(user, "has_perm", lambda perm: False)("scorecard.manage_ifrs9_scores")
        )
        if self.status == 'cancelled':
            return False
        if self.status == 'approved':
            return can_manage or self.maker == user or self.submitted_by == user
        if self.status in ['draft', 'in_progress', 'returned']:
            return can_manage or self.maker == user or self.submitted_by == user
        return False
    
    def can_be_reviewed_by(self, user):
        """Check if user can review this evaluation"""
        if self.status != 'submitted':
            return False
        if not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False):
            return True
        if not getattr(user, "has_perm", lambda perm: False)("scorecard.review_ifrs9_scores"):
            return False
        checker_id = getattr(self, "checker_id", None)
        user_id = getattr(user, "id", None)
        if checker_id and checker_id != user_id:
            return False
        get_accessible_branches = getattr(user, "get_accessible_branches", None)
        if callable(get_accessible_branches):
            accessible_branches = get_accessible_branches()
            if accessible_branches.exists():
                branch_names = set(accessible_branches.values_list("branch_name", flat=True))
                if self.branch_name not in branch_names:
                    return False
        return True
    
    def get_completion_percentage(self):
        """Calculate completion percentage based on required attributes"""
        if not hasattr(self, '_completion_cache'):
            if not self.template_id or self.template is None:
                self._completion_cache = 100
                return self._completion_cache
            total_required = IFRS9Attribute.objects.filter(
                risk_driver__section__template=self.template,
                is_required=True
            ).count()
            if total_required == 0:
                return 100
            answered_required = self.attribute_responses.filter(
                attribute__is_required=True
            ).exclude(raw_value='').exclude(raw_value__isnull=True).count()
            self._completion_cache = int((answered_required / total_required) * 100) if total_required > 0 else 0
        return self._completion_cache


class HistoricalScore(models.Model):
    """
    Month-end score snapshot built from the latest approved/current Basel and IFRS9 evaluation tables.
    Keeps one historical row per reporting date, branch, and customer.
    """

    reporting_date = models.DateField()
    branch_name = models.CharField(max_length=255)
    customer_name = models.CharField(max_length=255)
    customer_id = models.CharField(max_length=100)
    basel_ii_score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    basel_ii_grade = models.CharField(max_length=10, blank=True)
    basel_override_grade = models.CharField(
        max_length=10,
        blank=True,
        help_text="Optional Basel grade override captured in the historical score snapshot.",
    )
    ifrs_9_score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_HISTORICAL_SCORES"
        ordering = ["-reporting_date", "branch_name", "customer_id"]
        permissions = [
            (item["codename"], item["label"])
            for item in SCORECARD_PERMISSION_DEFINITIONS
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["reporting_date", "branch_name", "customer_id"],
                name="uq_hist_scores_reporting_branch_customer",
            ),
        ]

    def save(self, *args, **kwargs):
        normalize_grade_fields(self, "basel_ii_grade", "basel_override_grade")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.reporting_date:%Y-%m-%d} | {self.branch_name} | {self.customer_id}"


class IFRS9EvaluationWorkflowHistory(models.Model):
    """
    Audit log for all workflow status changes in the IFRS9 evaluation maker-checker process.
    Records every status transition with user, timestamp, and comments.
    """
    evaluation = models.ForeignKey(
        IFRS9Evaluation,
        on_delete=models.CASCADE,
        related_name='workflow_history'
    )
    
    ACTION_CHOICES = [
        ('created', 'Created'),
        ('saved_draft', 'Saved Draft'),
        ('submitted', 'Submitted for Review'),
        ('returned', 'Returned for Changes'),
        ('approved', 'Approved'),
        ('reopened', 'Reopened'),
        ('cancelled', 'Cancelled'),
        ('withdrawn', 'Withdrawn'),
        ('reassigned', 'Reassigned'),
    ]
    
    action = models.CharField(
        max_length=20,
        choices=ACTION_CHOICES,
        help_text="Action taken on the evaluation"
    )
    
    from_status = models.CharField(
        max_length=20,
        choices=IFRS9Evaluation.STATUS_CHOICES,
        null=True,
        blank=True,
        help_text="Previous status"
    )
    
    to_status = models.CharField(
        max_length=20,
        choices=IFRS9Evaluation.STATUS_CHOICES,
        help_text="New status"
    )
    
    performed_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.PROTECT,
        related_name='ifrs9_workflow_actions',
        help_text="User who performed this action"
    )
    
    comments = models.TextField(
        blank=True,
        help_text="Comments or reason for this action"
    )
    
    created_at = models.DateTimeField(default=timezone.now)
    
    class Meta:
        db_table = "SCORECARD_IFRS9_EVALUATION_WORKFLOW_HISTORY"
        ordering = ['-created_at', '-id']
        verbose_name_plural = "IFRS9 Evaluation Workflow History"
    
    def __str__(self) -> str:
        return f"{self.evaluation.customer_name} - {self.get_action_display()} ({self.created_at.date()})"


class IFRS9SectionScore(models.Model):
    """
    Calculated scores per section for an IFRS9 evaluation.
    """
    evaluation = models.ForeignKey(
        IFRS9Evaluation, on_delete=models.CASCADE, related_name="section_scores"
    )
    section = models.ForeignKey(
        IFRS9Section, on_delete=models.PROTECT, related_name="section_scores"
    )

    raw_score = models.DecimalField(max_digits=10, decimal_places=2)
    weighted_percent = models.DecimalField(max_digits=6, decimal_places=2)

    class Meta:
        db_table="SCORECARD_IFRS9_SECTION_SCORE"
    def __str__(self) -> str:
        return f"{self.evaluation_id}::{self.section.code} - {self.raw_score}/{self.weighted_percent}%"


class IFRS9RiskDriverScore(models.Model):
    """
    Calculated scores per risk driver for an IFRS9 evaluation.
    """
    evaluation = models.ForeignKey(
        IFRS9Evaluation, on_delete=models.CASCADE, related_name="driver_scores"
    )
    risk_driver = models.ForeignKey(
        IFRS9RiskDriver, on_delete=models.PROTECT, related_name="driver_scores"
    )

    raw_score = models.DecimalField(max_digits=10, decimal_places=2)
    weighted_percent = models.DecimalField(max_digits=6, decimal_places=2)
    proof = models.CharField(
        max_length=50,
        blank=True,
        help_text="PROOF validation result (WEIGHT or ERROR)",
    )

    class Meta:
        db_table="SCORECARD_IFRS9_RISK_DRIVER_SCORE"
    def __str__(self) -> str:
        return f"{self.evaluation_id}::{self.risk_driver.code} - {self.raw_score}/{self.weighted_percent}%"


class IFRS9AttributeResponse(models.Model):
    """
    Captured response for each attribute for an IFRS9 evaluation.
    Stores the option chosen plus the score applied, so that
    we can reconstruct the evaluation later for audit.
    """
    evaluation = models.ForeignKey(
        IFRS9Evaluation, on_delete=models.CASCADE, related_name="attribute_responses"
    )
    attribute = models.ForeignKey(
        IFRS9Attribute, on_delete=models.PROTECT, related_name="responses"
    )
    option = models.ForeignKey(
        IFRS9Option,
        on_delete=models.PROTECT,
        related_name="responses",
        null=True,
        blank=True,
    )

    raw_value = models.CharField(
        max_length=255,
        blank=True,
        help_text="Raw value provided. For choice attributes this mirrors the option value.",
    )
    allocated_score = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table="SCORECARD_IFRS9_ATTRIBUTE_RESPONSE"

    def __str__(self) -> str:
        return f"{self.evaluation_id}::{self.attribute.code} -> {self.allocated_score}"


def get_ifrs9_document_upload_path(instance, filename):
    """
    Generate upload path for IFRS9 attribute response documents.
    Format: ifrs9_documents/{customer_code}/{year}/{month}/{day}/{filename}
    """
    try:
        if hasattr(instance, 'attribute_response') and instance.attribute_response:
            if hasattr(instance.attribute_response, 'evaluation') and instance.attribute_response.evaluation:
                customer_code = instance.attribute_response.evaluation.customer_id or 'unknown'
            else:
                customer_code = 'unknown'
        else:
            customer_code = 'unknown'
    except (AttributeError, ValueError):
        customer_code = 'unknown'
    
    customer_code = "".join(c for c in str(customer_code) if c.isalnum() or c in ('-', '_')).strip()
    if not customer_code:
        customer_code = 'unknown'
    
    now = timezone.now()
    year = now.strftime('%Y')
    month = now.strftime('%m')
    day = now.strftime('%d')
    
    return os.path.join('ifrs9_documents', customer_code, year, month, day, filename)


class IFRS9AttributeResponseDocument(models.Model):
    """
    Supporting documents uploaded for a specific IFRS9 attribute response.
    Multiple documents can be uploaded for a single attribute response.
    """
    attribute_response = models.ForeignKey(
        IFRS9AttributeResponse,
        on_delete=models.CASCADE,
        related_name="documents"
    )
    file = models.FileField(
        upload_to=get_ifrs9_document_upload_path,
        help_text="Supporting document file"
    )
    file_name = models.CharField(
        max_length=255,
        help_text="Original filename"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_ifrs9_documents"
    )

    class Meta:
        db_table = "SCORECARD_IFRS9_ATTRIBUTE_RESPONSE_DOCUMENT"
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return f"{self.attribute_response} - {self.file_name}"


class IFRS9EvaluationHistory(models.Model):
    """
    Historical record of all previous weighted scores and grades for an IFRS9 evaluation.
    Each time an evaluation is edited, the previous values are saved here.
    """
    evaluation = models.ForeignKey(
        IFRS9Evaluation,
        on_delete=models.CASCADE,
        related_name="history_records"
    )
    
    weighted_percent = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Weighted percent at this point in time"
    )
    grade = models.CharField(
        max_length=10, blank=True,
        help_text="Grade at this point in time"
    )
    raw_score = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Raw score at this point in time"
    )
    
    recorded_at = models.DateTimeField(default=timezone.now)
    
    class Meta:
        db_table="SCORECARD_IFRS9_EVALUATION_HISTORY"
        ordering = ["-recorded_at"]
        verbose_name = "IFRS9 Evaluation History"
        verbose_name_plural = "IFRS9 Evaluation Histories"

    def save(self, *args, **kwargs):
        normalize_grade_fields(self, "grade")
        super().save(*args, **kwargs)
    
    def __str__(self) -> str:
        return f"{self.evaluation_id} - {self.weighted_percent}% ({self.grade}) at {self.recorded_at}"


class IFRS9EvaluationVersion(models.Model):
    """
    Complete snapshot of an IFRS9 evaluation at a specific point in time.
    Stores all attribute responses, driver scores, section scores, and summary data.
    """
    evaluation = models.ForeignKey(
        IFRS9Evaluation,
        on_delete=models.CASCADE,
        related_name="versions"
    )
    
    version_number = models.PositiveIntegerField(
        help_text="Version number (1 for initial submission, increments on each edit)"
    )
    
    is_approved = models.BooleanField(
        default=False,
        help_text="True if this version was approved by a checker"
    )
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this version was approved"
    )
    approved_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_ifrs9_versions',
        help_text="User who approved this version"
    )
    
    total_raw_score = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    total_weighted_percent = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )
    final_grade = models.CharField(max_length=10, blank=True)
    
    created_by = models.ForeignKey(
        'Users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_ifrs9_versions',
        help_text="User who created this version"
    )
    created_at = models.DateTimeField(default=timezone.now)
    change_description = models.TextField(
        blank=True,
        help_text="Description of what changed in this version"
    )
    autofill_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Auto-fill summary and customer profile snapshot captured for this version."
    )
    
    class Meta:
        db_table = "SCORECARD_IFRS9_EVALUATION_VERSION"
        ordering = ['-version_number', '-created_at']
        verbose_name = "IFRS9 Evaluation Version"
        verbose_name_plural = "IFRS9 Evaluation Versions"
        indexes = [
            models.Index(fields=["evaluation", "is_approved", "version_number"], name="SC_IFRS9_VER_APPROVAL_IDX"),
        ]

    def save(self, *args, **kwargs):
        normalize_grade_fields(self, "final_grade")
        super().save(*args, **kwargs)
    
    def __str__(self) -> str:
        return f"{self.evaluation_id} - Version {self.version_number} ({self.created_at.date()})"


class IFRS9EvaluationVersionAttributeResponse(models.Model):
    """
    Snapshot of an attribute response at a specific version.
    """
    version = models.ForeignKey(
        IFRS9EvaluationVersion,
        on_delete=models.CASCADE,
        related_name="attribute_responses"
    )
    attribute = models.ForeignKey(
        IFRS9Attribute,
        on_delete=models.PROTECT
    )
    option = models.ForeignKey(
        IFRS9Option,
        on_delete=models.PROTECT,
        null=True,
        blank=True
    )
    raw_value = models.CharField(max_length=255, blank=True)
    allocated_score = models.DecimalField(max_digits=10, decimal_places=2)
    
    class Meta:
        db_table = "SCORECARD_IFRS9_EVALUATION_VERSION_ATTR_RESPONSE"


class IFRS9EvaluationVersionDriverScore(models.Model):
    """
    Snapshot of a driver score at a specific version.
    """
    version = models.ForeignKey(
        IFRS9EvaluationVersion,
        on_delete=models.CASCADE,
        related_name="driver_scores"
    )
    risk_driver = models.ForeignKey(
        IFRS9RiskDriver,
        on_delete=models.PROTECT
    )
    raw_score = models.DecimalField(max_digits=10, decimal_places=2)
    weighted_percent = models.DecimalField(max_digits=6, decimal_places=2)
    proof = models.CharField(max_length=50, blank=True)
    
    class Meta:
        db_table = "SCORECARD_IFRS9_EVALUATION_VERSION_DRIVER_SCORE"


class IFRS9EvaluationVersionSectionScore(models.Model):
    """
    Snapshot of a section score at a specific version.
    """
    version = models.ForeignKey(
        IFRS9EvaluationVersion,
        on_delete=models.CASCADE,
        related_name="section_scores"
    )
    section = models.ForeignKey(
        IFRS9Section,
        on_delete=models.PROTECT
    )
    raw_score = models.DecimalField(max_digits=10, decimal_places=2)
    weighted_percent = models.DecimalField(max_digits=6, decimal_places=2)
    
    class Meta:
        db_table = "SCORECARD_IFRS9_EVALUATION_VERSION_SECTION_SCORE"


class BankBranch(models.Model):
    """
    Bank branch information model.
    Stores branch details including name, code, bank name, address, and contact information.
    """
    
    branch_name = models.CharField(max_length=120)
    branch_code = models.CharField(max_length=20)
    bank_name = models.CharField(max_length=120)
    
    # Optional Address Fields
    address = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    province = models.CharField(max_length=100, blank=True, null=True)
    zipcode = models.CharField(max_length=20, blank=True, null=True)
    
    # Optional Contact Fields (numbers only)
    landline = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        validators=[RegexValidator(r'^\d+$', 'Numbers only')]
    )
    mobile = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        validators=[RegexValidator(r'^\d+$', 'Numbers only')]
    )
    
    
    class Meta:
        db_table = "SCORECARD_BANK_BRANCH"  # ALL CAPS, NO SPACES
        verbose_name_plural = "Bank Branches"
        ordering = ["bank_name", "branch_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch_name"],
                name="uq_scorecard_bank_branch_name",
            ),
        ]
    
    def __str__(self) -> str:
        return f"{self.bank_name} - {self.branch_name}"


class ScorecardUserBranchAccess(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="scorecard_branch_access_entries",
    )
    branch = models.ForeignKey(
        BankBranch,
        on_delete=models.CASCADE,
        related_name="scorecard_user_access_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_USER_BRANCH_ACCESS"
        verbose_name = "Scorecard User Branch Access"
        verbose_name_plural = "Scorecard User Branch Access"
        ordering = ["branch__bank_name", "branch__branch_name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "branch"],
                name="uq_scorecard_user_branch_access",
            )
        ]

    def __str__(self) -> str:
        user_label = getattr(self.user, "email", None) or getattr(self.user, "name", None) or "User"
        return f"{user_label} -> {self.branch}"


class CustomerCorporate(models.Model):
    """
    Detailed corporate customer profile populated from the corporate client API.
    Preserves the full source payload alongside the branch-aware main customer table.
    """

    client_code = models.CharField(max_length=30, unique=True)
    client_name = models.CharField(max_length=255)
    resident_status = models.CharField(max_length=10, blank=True, null=True)
    organization_qualifier = models.CharField(max_length=20, blank=True, null=True)
    swift_code = models.CharField(max_length=50, blank=True, null=True)
    industry_code = models.CharField(max_length=20, blank=True, null=True)
    sub_industry_code = models.CharField(max_length=20, blank=True, null=True)
    nature_of_business_1 = models.CharField(max_length=100, blank=True, null=True)
    nature_of_business_2 = models.CharField(max_length=100, blank=True, null=True)
    nature_of_business_3 = models.CharField(max_length=100, blank=True, null=True)
    investment_currency = models.CharField(max_length=10, blank=True, null=True)
    investment_amount = models.DecimalField(max_digits=22, decimal_places=3, blank=True, null=True)
    capital_currency = models.CharField(max_length=10, blank=True, null=True)
    authorized_capital = models.DecimalField(max_digits=22, decimal_places=3, blank=True, null=True)
    issued_capital = models.DecimalField(max_digits=22, decimal_places=3, blank=True, null=True)
    paid_up_capital = models.DecimalField(max_digits=22, decimal_places=3, blank=True, null=True)
    net_worth_amount = models.DecimalField(max_digits=22, decimal_places=3, blank=True, null=True)
    incorporation_date = models.DateField(blank=True, null=True)
    incorporation_country = models.CharField(max_length=10, blank=True, null=True)
    registration_number = models.CharField(max_length=100, blank=True, null=True)
    registration_date = models.DateField(blank=True, null=True)
    registration_authority = models.CharField(max_length=255, blank=True, null=True)
    registration_expiry_date = models.DateField(blank=True, null=True)
    registered_office_address_1 = models.CharField(max_length=255, blank=True, null=True)
    registered_office_address_2 = models.CharField(max_length=255, blank=True, null=True)
    registered_office_address_3 = models.CharField(max_length=255, blank=True, null=True)
    registered_office_address_4 = models.CharField(max_length=255, blank=True, null=True)
    registered_office_address_5 = models.CharField(max_length=255, blank=True, null=True)
    is_trade_finance_client = models.CharField(max_length=10, blank=True, null=True)
    vostro_exchange_house = models.CharField(max_length=10, blank=True, null=True)
    import_export_code = models.CharField(max_length=100, blank=True, null=True)
    commercial_business_identifier = models.CharField(max_length=100, blank=True, null=True)
    business_entity_identifier = models.CharField(max_length=100, blank=True, null=True)
    years_in_business = models.IntegerField(blank=True, null=True)
    gross_turnover = models.DecimalField(max_digits=22, decimal_places=3, blank=True, null=True)
    employee_size = models.IntegerField(blank=True, null=True)
    number_of_offices = models.IntegerField(blank=True, null=True)
    is_scheduled_bank = models.CharField(max_length=10, blank=True, null=True)
    is_sovereign = models.CharField(max_length=10, blank=True, null=True)
    sovereign_type = models.CharField(max_length=50, blank=True, null=True)
    country_code = models.CharField(max_length=10, blank=True, null=True)
    is_central_state = models.CharField(max_length=10, blank=True, null=True)
    is_public_sector = models.CharField(max_length=10, blank=True, null=True)
    is_primary_dealer = models.CharField(max_length=10, blank=True, null=True)
    is_multilateral_bank = models.CharField(max_length=10, blank=True, null=True)
    connected_person_investment_number = models.CharField(max_length=50, blank=True, null=True)
    bank_type = models.CharField(max_length=50, blank=True, null=True)
    cooperative_bank_type = models.CharField(max_length=50, blank=True, null=True)
    bank_code = models.CharField(max_length=50, blank=True, null=True)
    weaker_section_code = models.CharField(max_length=50, blank=True, null=True)
    source_of_funds = models.CharField(max_length=50, blank=True, null=True)
    purpose_of_account_opening = models.CharField(max_length=50, blank=True, null=True)
    branch_code = models.CharField(max_length=50, blank=True, null=True)
    branch_name = models.CharField(max_length=150, blank=True, null=True)
    raw_payload = models.JSONField(blank=True, null=True)
    source_last_sync_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_API_CUSTOMER_CORPORATE"
        verbose_name = "Customer Corporate"
        verbose_name_plural = "Customer Corporate"
        ordering = ["client_name", "client_code"]

    def __str__(self) -> str:
        return f"{self.client_name} ({self.client_code})"


class CustomerIndividual(models.Model):
    """
    Detailed individual customer profile populated from the individual client API.
    Preserves the full source payload alongside the branch-aware main customer table.
    """

    client_code = models.CharField(max_length=30, unique=True)
    first_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    surname = models.CharField(max_length=100, blank=True, null=True)
    middle_name = models.CharField(max_length=100, blank=True, null=True)
    work_sector_code = models.CharField(max_length=50, blank=True, null=True)
    father_name = models.CharField(max_length=100, blank=True, null=True)
    birth_date = models.DateField(blank=True, null=True)
    birth_place_code = models.CharField(max_length=50, blank=True, null=True)
    birth_place_name = models.CharField(max_length=100, blank=True, null=True)
    gender = models.CharField(max_length=10, blank=True, null=True)
    marital_status = models.CharField(max_length=10, blank=True, null=True)
    religion_code = models.CharField(max_length=20, blank=True, null=True)
    nationality_code = models.CharField(max_length=10, blank=True, null=True)
    resident_status = models.CharField(max_length=10, blank=True, null=True)
    language_code = models.CharField(max_length=20, blank=True, null=True)
    is_illiterate = models.CharField(max_length=10, blank=True, null=True)
    is_disabled = models.CharField(max_length=10, blank=True, null=True)
    fax_address_required = models.CharField(max_length=10, blank=True, null=True)
    phone_home = models.CharField(max_length=30, blank=True, null=True)
    phone_office = models.CharField(max_length=30, blank=True, null=True)
    phone_office_alt = models.CharField(max_length=30, blank=True, null=True)
    extension_number = models.CharField(max_length=20, blank=True, null=True)
    mobile_number = models.CharField(max_length=30, blank=True, null=True)
    fax_number = models.CharField(max_length=30, blank=True, null=True)
    email_primary = models.EmailField(blank=True, null=True)
    email_secondary = models.EmailField(blank=True, null=True)
    employment_type = models.CharField(max_length=20, blank=True, null=True)
    pension_flag = models.CharField(max_length=20, blank=True, null=True)
    bank_relationship_flag = models.CharField(max_length=20, blank=True, null=True)
    employee_number = models.CharField(max_length=50, blank=True, null=True)
    occupation_code = models.CharField(max_length=50, blank=True, null=True)
    employer_code = models.CharField(max_length=50, blank=True, null=True)
    employer_name = models.CharField(max_length=255, blank=True, null=True)
    employer_address_1 = models.CharField(max_length=255, blank=True, null=True)
    employer_address_2 = models.CharField(max_length=255, blank=True, null=True)
    employer_address_3 = models.CharField(max_length=255, blank=True, null=True)
    employer_address_4 = models.CharField(max_length=255, blank=True, null=True)
    employer_address_5 = models.CharField(max_length=255, blank=True, null=True)
    designation_code = models.CharField(max_length=50, blank=True, null=True)
    annual_income = models.DecimalField(max_digits=22, decimal_places=3, blank=True, null=True)
    income_slab = models.CharField(max_length=20, blank=True, null=True)
    accommodation_type = models.CharField(max_length=20, blank=True, null=True)
    accommodation_other = models.CharField(max_length=255, blank=True, null=True)
    owns_two_wheeler = models.CharField(max_length=10, blank=True, null=True)
    owns_car = models.CharField(max_length=10, blank=True, null=True)
    insurance_info = models.CharField(max_length=50, blank=True, null=True)
    pid_inv_number = models.CharField(max_length=50, blank=True, null=True)
    poverty_flag = models.CharField(max_length=10, blank=True, null=True)
    employer_reference_code = models.CharField(max_length=50, blank=True, null=True)
    application_number = models.CharField(max_length=100, blank=True, null=True)
    account_purpose = models.CharField(max_length=50, blank=True, null=True)
    source_of_funds = models.CharField(max_length=50, blank=True, null=True)
    branch_code = models.CharField(max_length=50, blank=True, null=True)
    branch_name = models.CharField(max_length=150, blank=True, null=True)
    raw_payload = models.JSONField(blank=True, null=True)
    source_last_sync_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_API_CUSTOMER_INDIVIDUAL"
        verbose_name = "Customer Individual"
        verbose_name_plural = "Customer Individual"
        ordering = ["surname", "first_name", "client_code"]

    def __str__(self) -> str:
        display_name = " ".join(
            part for part in [self.first_name, self.middle_name, self.surname or self.last_name] if part
        ).strip()
        return f"{display_name or self.client_code} ({self.client_code})"


class CustomerLoan(models.Model):
    """
    Loan account profile populated from the loans API.
    A customer can have multiple loan records, keyed by loan_id.
    """

    reporting_date = models.DateField(blank=True, null=True)
    customer_code = models.CharField(max_length=30)
    customer_name = models.CharField(max_length=255, blank=True, null=True)
    branch_description = models.CharField(max_length=150, blank=True, null=True)
    product_code = models.CharField(max_length=50, blank=True, null=True)
    product_name = models.CharField(max_length=255, blank=True, null=True)
    product_category = models.CharField(max_length=255, blank=True, null=True)
    loan_id = models.CharField(max_length=50, unique=True)
    branch_code = models.CharField(max_length=50, blank=True, null=True)
    branch_name = models.CharField(max_length=150, blank=True, null=True)
    account_number = models.CharField(max_length=80, blank=True, null=True)
    currency_code = models.CharField(max_length=20, blank=True, null=True)
    sector_code = models.CharField(max_length=50, blank=True, null=True)
    facility_sector = models.CharField(max_length=255, blank=True, null=True)
    portfolio_name = models.CharField(max_length=255, blank=True, null=True)
    portfolio_code = models.CharField(max_length=50, blank=True, null=True)
    loan_type = models.CharField(max_length=100, blank=True, null=True)
    collateral_type = models.CharField(max_length=255, blank=True, null=True)
    past_due_indicator = models.CharField(max_length=50, blank=True, null=True)
    npl_indicator_current = models.CharField(max_length=50, blank=True, null=True)
    npl_indicator_prev = models.CharField(max_length=50, blank=True, null=True)
    npl_indicator_additions = models.CharField(max_length=50, blank=True, null=True)
    interest_frequency_unit = models.CharField(max_length=50, blank=True, null=True)
    interest_payment_type = models.CharField(max_length=100, blank=True, null=True)
    day_count_indicator = models.CharField(max_length=50, blank=True, null=True)
    amortization_repayment_type = models.CharField(max_length=100, blank=True, null=True)
    amortization_term_unit = models.CharField(max_length=50, blank=True, null=True)
    repayment_month = models.CharField(max_length=50, blank=True, null=True)
    start_date = models.DateField(blank=True, null=True)
    maturity_date = models.DateField(blank=True, null=True)
    next_payment_date = models.DateField(blank=True, null=True)
    last_payment_date = models.DateField(blank=True, null=True)
    restructure_date = models.DateField(blank=True, null=True)
    final_disbursement_date = models.DateField(blank=True, null=True)
    customer_target = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    current_interest_rate = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    effective_interest_rate = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    accrued_interest = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    suspended_interest = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    penalty_interest = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    percent = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    loan_amount = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    loan_balance = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    outstanding_balance = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    current_outstanding_balance = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    undrawn_amount = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    collateral_amount = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    overdue_amount = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    repayment_amount = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    installment_amount = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    pd_percent = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    lgd_percent = models.DecimalField(max_digits=24, decimal_places=6, blank=True, null=True)
    delinquent_days = models.IntegerField(blank=True, null=True)
    raw_payload = models.JSONField(blank=True, null=True)
    source_last_sync_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_API_CUSTOMER_LOAN"
        verbose_name = "Customer Loan"
        verbose_name_plural = "Customer Loan"
        ordering = ["customer_name", "loan_id"]
        indexes = [
            models.Index(fields=["reporting_date", "customer_code"], name="SC_CLOAN_DATE_CODE_IDX"),
            models.Index(fields=["reporting_date", "branch_description"], name="SC_CLOAN_DATE_BR_IDX"),
        ]

    def __str__(self) -> str:
        return f"{self.customer_name or self.customer_code} ({self.loan_id})"


class CustomerOverdraft(models.Model):
    """
    Overdraft limit profile populated from the overdraft limits API.
    A customer can have multiple overdraft records, keyed by account_number.
    """

    reporting_date = models.DateField(blank=True, null=True)
    customer_code = models.CharField(max_length=30)
    customer_name = models.CharField(max_length=255, blank=True, null=True)
    branch_description = models.CharField(max_length=150, blank=True, null=True)
    ac_category = models.CharField(max_length=50, blank=True, null=True)
    account_number = models.CharField(max_length=50, unique=True)
    raw_payload = models.JSONField(blank=True, null=True)
    source_last_sync_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_API_CUSTOMER_OVERDRAFT"
        verbose_name = "Customer Overdraft"
        verbose_name_plural = "Customer Overdraft"
        ordering = ["customer_name", "account_number"]
        indexes = [
            models.Index(fields=["reporting_date", "customer_code"], name="SC_COD_DATE_CODE_IDX"),
            models.Index(fields=["reporting_date", "branch_description"], name="SC_COD_DATE_BR_IDX"),
        ]

    def __str__(self) -> str:
        return f"{self.customer_name or self.customer_code} ({self.account_number})"


class MainCustomer(models.Model):
    """
    Branch-aware scorecard customer base.
    One row per reporting_date + customer_ref_code + branch_code.
    """

    reporting_date = models.DateField()
    customer_ref_code = models.CharField(max_length=30)
    customer_name = models.CharField(max_length=255)
    customer_type = models.CharField(max_length=30, blank=True, null=True)
    branch_code = models.CharField(max_length=50, blank=True, default="")
    branch_name = models.CharField(max_length=150, blank=True, default="")
    branch_description = models.CharField(max_length=150)

    resident_status = models.CharField(max_length=20, blank=True, null=True)
    nationality_code = models.CharField(max_length=20, blank=True, null=True)
    national_id = models.CharField(max_length=50, blank=True, null=True)
    registration_number = models.CharField(max_length=100, blank=True, null=True)
    industry_code = models.CharField(max_length=20, blank=True, null=True)
    sub_industry_code = models.CharField(max_length=20, blank=True, null=True)
    occupation_code = models.CharField(max_length=50, blank=True, null=True)
    gender = models.CharField(max_length=10, blank=True, null=True)
    birth_date = models.DateField(blank=True, null=True)
    marital_status = models.CharField(max_length=20, blank=True, null=True)
    employment_type = models.CharField(max_length=20, blank=True, null=True)
    annual_income = models.DecimalField(max_digits=22, decimal_places=3, blank=True, null=True)
    income_slab = models.CharField(max_length=20, blank=True, null=True)
    accommodation_type = models.CharField(max_length=20, blank=True, null=True)
    designation_code = models.CharField(max_length=50, blank=True, null=True)
    work_sector_code = models.CharField(max_length=50, blank=True, null=True)
    employer_code = models.CharField(max_length=50, blank=True, null=True)
    bank_relationship_flag = models.CharField(max_length=20, blank=True, null=True)
    pension_flag = models.CharField(max_length=20, blank=True, null=True)
    source_of_funds = models.CharField(max_length=50, blank=True, null=True)
    account_purpose = models.CharField(max_length=50, blank=True, null=True)
    employer_name = models.CharField(max_length=255, blank=True, null=True)
    mobile = models.CharField(max_length=30, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)

    loan_count = models.PositiveIntegerField(default=0)
    overdraft_count = models.PositiveIntegerField(default=0)
    has_loan = models.BooleanField(default=False)
    has_overdraft = models.BooleanField(default=False)
    primary_loan_id = models.CharField(max_length=50, blank=True, null=True)
    primary_account_number = models.CharField(max_length=50, blank=True, null=True)

    last_synced_at = models.DateTimeField(blank=True, null=True)
    is_active_for_scoring = models.BooleanField(default=True)

    class Meta:
        db_table = "SCORECARD_MAIN_CUSTOMER"
        verbose_name = "Main Customer"
        verbose_name_plural = "Main Customers"
        ordering = ["reporting_date", "branch_name", "customer_name", "customer_ref_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["reporting_date", "customer_ref_code", "branch_code"],
                name="UQ_SCORECARD_MAIN_CUSTOMER_KEY",
            )
        ]
        indexes = [
            models.Index(fields=["reporting_date"], name="SC_MAIN_CUSTOMER_DATE_IDX"),
            models.Index(fields=["customer_ref_code"], name="SC_MAIN_CUSTOMER_REF_IDX"),
            models.Index(fields=["branch_code"], name="SC_MAIN_CUSTOMER_BRANCH_IDX"),
            models.Index(fields=["branch_code", "customer_ref_code"], name="SC_MAIN_CUST_BR_REF_IDX"),
            models.Index(fields=["branch_name", "customer_name"], name="SC_MAIN_CUST_BR_NAME_IDX"),
        ]

    def __str__(self) -> str:
        return f"{self.customer_name} ({self.customer_ref_code}) - {self.branch_name or self.branch_description}"


class ApiConfiguration(models.Model):
    name = models.CharField(max_length=100, default="Primary API Configuration")
    base_url = models.URLField(max_length=500)
    auth_header_name = models.CharField(max_length=100, default="X-API-KEY")
    auth_secret_key = models.CharField(max_length=255)
    timeout_seconds = models.PositiveIntegerField(default=60)
    test_timeout_seconds = models.PositiveIntegerField(default=5)
    browser_timeout_seconds = models.PositiveIntegerField(default=7)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_API_CONFIGURATION"
        verbose_name = "API Configuration"
        verbose_name_plural = "API Configurations"
        ordering = ["-updated_at", "-id"]

    def __str__(self) -> str:
        return self.name


class ApiConfigurationParameter(models.Model):
    configuration = models.ForeignKey(
        ApiConfiguration,
        on_delete=models.CASCADE,
        related_name="parameters",
    )
    name = models.CharField(max_length=100)
    default_value = models.CharField(max_length=255, blank=True, default="")
    display_order = models.PositiveIntegerField(default=1)
    is_required = models.BooleanField(default=False)
    use_for_testing = models.BooleanField(default=True)
    use_for_retrieval = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_API_CONFIGURATION_PARAMETER"
        verbose_name = "API Configuration Parameter"
        verbose_name_plural = "API Configuration Parameters"
        ordering = ["display_order", "name", "id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.configuration.name})"


class ApiEndpoint(models.Model):
    TARGET_CUSTOMER_CORPORATE = "customer_corporate"
    TARGET_CUSTOMER_INDIVIDUAL = "customer_individual"
    TARGET_CUSTOMER_LOAN = "customer_loan"
    TARGET_CUSTOMER_OVERDRAFT = "customer_overdraft"
    TARGET_TABLE_CHOICES = [
        (TARGET_CUSTOMER_CORPORATE, "CustomerCorporate"),
        (TARGET_CUSTOMER_INDIVIDUAL, "CustomerIndividual"),
        (TARGET_CUSTOMER_LOAN, "CustomerLoan"),
        (TARGET_CUSTOMER_OVERDRAFT, "CustomerOverdraft"),
    ]

    configuration = models.ForeignKey(
        ApiConfiguration,
        on_delete=models.CASCADE,
        related_name="endpoints",
    )
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=50, unique=True)
    path = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    http_method = models.CharField(max_length=10, default="GET")
    target_table = models.CharField(max_length=50, choices=TARGET_TABLE_CHOICES, blank=True)
    default_query_template = models.TextField(blank=True, null=True)
    parameters = models.ManyToManyField(
        ApiConfigurationParameter,
        blank=True,
        related_name="endpoints",
    )
    is_active = models.BooleanField(default=True)
    last_tested_at = models.DateTimeField(blank=True, null=True)
    last_test_status = models.CharField(max_length=20, blank=True)
    last_test_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_API_ENDPOINT"
        verbose_name = "API Endpoint"
        verbose_name_plural = "API Endpoints"
        ordering = ["name", "code"]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class ApiImportSchedule(models.Model):
    FREQUENCY_DAILY = "daily"
    FREQUENCY_WEEKLY = "weekly"
    FREQUENCY_MONTHLY = "monthly"
    FREQUENCY_CHOICES = [
        (FREQUENCY_DAILY, "Daily"),
        (FREQUENCY_WEEKLY, "Weekly"),
        (FREQUENCY_MONTHLY, "Monthly"),
    ]

    REPORTING_DATE_ENDPOINT_DEFAULT = "endpoint_default"
    REPORTING_DATE_RUN_DATE = "run_date"
    REPORTING_DATE_PREVIOUS_DAY = "previous_day"
    REPORTING_DATE_FIXED = "fixed_date"
    REPORTING_DATE_CHOICES = [
        (REPORTING_DATE_ENDPOINT_DEFAULT, "Use endpoint default"),
        (REPORTING_DATE_RUN_DATE, "Use run date"),
        (REPORTING_DATE_PREVIOUS_DAY, "Use previous day"),
        (REPORTING_DATE_FIXED, "Use fixed date"),
    ]

    WEEKDAY_CHOICES = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    name = models.CharField(max_length=150)
    endpoint = models.ForeignKey(
        ApiEndpoint,
        on_delete=models.CASCADE,
        related_name="import_schedules",
    )
    frequency = models.CharField(max_length=20, choices=FREQUENCY_CHOICES, default=FREQUENCY_DAILY)
    run_time = models.TimeField()
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES, blank=True, null=True)
    day_of_month = models.PositiveSmallIntegerField(blank=True, null=True)
    reporting_date_mode = models.CharField(
        max_length=30,
        choices=REPORTING_DATE_CHOICES,
        default=REPORTING_DATE_ENDPOINT_DEFAULT,
    )
    fixed_reporting_date = models.DateField(blank=True, null=True)
    extra_query_string = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(blank=True, null=True)
    last_status = models.CharField(max_length=20, blank=True)
    last_message = models.TextField(blank=True)
    last_duration_seconds = models.FloatField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_API_IMPORT_SCHEDULE"
        verbose_name = "API Import Schedule"
        verbose_name_plural = "API Import Schedules"
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.endpoint.name})"


class ApiMainSyncConfiguration(models.Model):
    name = models.CharField(max_length=100, default="Main Customer Auto Sync")
    delay_minutes = models.PositiveIntegerField(default=30)
    is_active = models.BooleanField(default=True)
    corporate_endpoint = models.ForeignKey(
        "scorecard.ApiEndpoint",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="main_sync_corporate_configurations",
    )
    individual_endpoint = models.ForeignKey(
        "scorecard.ApiEndpoint",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="main_sync_individual_configurations",
    )
    last_run_at = models.DateTimeField(blank=True, null=True)
    last_status = models.CharField(max_length=20, blank=True)
    last_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_API_MAIN_SYNC_CONFIG"
        verbose_name = "API Main Sync Configuration"
        verbose_name_plural = "API Main Sync Configuration"

    def __str__(self) -> str:
        return self.name


class ApiMainSyncRun(models.Model):
    SOURCE_MANUAL = "manual"
    SOURCE_SCHEDULER = "scheduler"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manual"),
        (SOURCE_SCHEDULER, "Scheduler"),
    ]

    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
    ]

    reporting_date = models.DateField()
    run_source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_SCHEDULER)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="api_main_sync_runs",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    rows_synced = models.PositiveIntegerField(default=0)
    source_details = models.JSONField(blank=True, null=True)
    detail_message = models.TextField(blank=True)
    failure_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_API_MAIN_SYNC_RUN"
        verbose_name = "API Main Sync Run"
        verbose_name_plural = "API Main Sync Runs"
        ordering = ["-started_at", "-id"]

    def __str__(self) -> str:
        return f"Main sync {self.reporting_date:%Y-%m-%d} [{self.get_run_source_display()}]"


class ApiImportRun(models.Model):
    SOURCE_MANUAL = "manual"
    SOURCE_SCHEDULE = "schedule"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manual"),
        (SOURCE_SCHEDULE, "Scheduled"),
    ]

    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_STOPPED = "stopped"
    STATUS_RETRIED = "retried"
    STATUS_CHOICES = [
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
        (STATUS_STOPPED, "Stopped"),
        (STATUS_RETRIED, "Retried"),
    ]

    endpoint = models.ForeignKey(
        ApiEndpoint,
        on_delete=models.CASCADE,
        related_name="import_runs",
    )
    schedule = models.ForeignKey(
        ApiImportSchedule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_runs",
    )
    run_source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="api_import_runs",
    )
    target_table = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    fetched = models.PositiveIntegerField(default=0)
    created = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    unchanged = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    duplicate_skipped = models.PositiveIntegerField(default=0)
    missing_required_skipped = models.PositiveIntegerField(default=0)
    parameters_used = models.JSONField(blank=True, null=True)
    failure_message = models.TextField(blank=True)
    failure_details = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_API_IMPORT_RUN"
        verbose_name = "API Import Run"
        verbose_name_plural = "API Import Runs"
        ordering = ["-started_at", "-id"]

    def __str__(self) -> str:
        return f"{self.endpoint.name} [{self.get_run_source_display()}] {self.started_at:%Y-%m-%d %H:%M}"


class ApiSchedulerServiceStatus(models.Model):
    service_name = models.CharField(max_length=100, unique=True, default="django_api_scheduler_service")
    scheduler_enabled = models.BooleanField(default=True)
    run_import_schedules = models.BooleanField(default=True)
    run_main_customer_sync = models.BooleanField(default=True)
    run_historical_score_capture = models.BooleanField(default=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    last_check_at = models.DateTimeField(null=True, blank=True)
    check_interval_seconds = models.PositiveIntegerField(default=30)
    retry_limit = models.PositiveIntegerField(default=3)
    retry_delay_seconds = models.PositiveIntegerField(default=30)
    last_status = models.CharField(max_length=20, blank=True)
    last_message = models.TextField(blank=True)
    last_run_count = models.PositiveIntegerField(default=0)
    last_started_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_API_SCHEDULER_STATUS"
        verbose_name = "API Scheduler Service Status"
        verbose_name_plural = "API Scheduler Service Status"

    def __str__(self) -> str:
        return self.service_name


class ApiSchedulerServiceLog(models.Model):
    LEVEL_INFO = "info"
    LEVEL_SUCCESS = "success"
    LEVEL_WARNING = "warning"
    LEVEL_ERROR = "error"
    LEVEL_CHOICES = [
        (LEVEL_INFO, "Info"),
        (LEVEL_SUCCESS, "Success"),
        (LEVEL_WARNING, "Warning"),
        (LEVEL_ERROR, "Error"),
    ]

    service_name = models.CharField(max_length=100, default="django_api_scheduler_service")
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default=LEVEL_INFO)
    event_code = models.CharField(max_length=80, blank=True)
    message = models.TextField()
    details = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "SCORECARD_API_SCHEDULER_LOG"
        verbose_name = "API Scheduler Service Log"
        verbose_name_plural = "API Scheduler Service Logs"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["service_name", "created_at"], name="SC_SCHED_LOG_TS_IDX"),
            models.Index(fields=["level", "created_at"], name="SC_SCHED_LOG_LEVEL_TS_IDX"),
        ]

    def __str__(self) -> str:
        return f"{self.service_name} [{self.level}] {self.created_at:%Y-%m-%d %H:%M:%S}"


class ScorecardNotification(models.Model):
    CATEGORY_SCORING = "scoring"
    CATEGORY_API = "api"
    CATEGORY_SYNC = "sync"
    CATEGORY_CUSTOMER = "customer"
    CATEGORY_ADMIN = "admin"
    CATEGORY_CHOICES = [
        (CATEGORY_SCORING, "Scoring Workflow"),
        (CATEGORY_API, "API Import"),
        (CATEGORY_SYNC, "Main Sync"),
        (CATEGORY_CUSTOMER, "Customer Quality"),
        (CATEGORY_ADMIN, "Admin"),
    ]

    LEVEL_INFO = "info"
    LEVEL_ACTION = "action"
    LEVEL_WARNING = "warning"
    LEVEL_CRITICAL = "critical"
    LEVEL_CHOICES = [
        (LEVEL_INFO, "Info"),
        (LEVEL_ACTION, "Action Required"),
        (LEVEL_WARNING, "Warning"),
        (LEVEL_CRITICAL, "Critical"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="scorecard_notifications",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_scorecard_notifications",
    )
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default=LEVEL_INFO)
    event_code = models.CharField(max_length=80)
    title = models.CharField(max_length=255)
    message = models.TextField()
    action_url = models.CharField(max_length=500, blank=True)
    action_label = models.CharField(max_length=100, blank=True)
    branch_name = models.CharField(max_length=150, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "SCORECARD_NOTIFICATION"
        verbose_name = "Scorecard Notification"
        verbose_name_plural = "Scorecard Notifications"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "is_read"], name="SC_NOTIFY_USER_READ_IDX"),
            models.Index(fields=["category", "level"], name="SC_NOTIFY_CAT_LEVEL_IDX"),
            models.Index(fields=["created_at"], name="SC_NOTIFY_CREATED_IDX"),
            models.Index(fields=["user", "branch_name", "is_read"], name="SC_NOTIFY_USR_BR_RD_IDX"),
            models.Index(fields=["branch_name", "created_at"], name="SC_NOTIFY_BR_TS_IDX"),
        ]

    def __str__(self) -> str:
        return f"{self.user} - {self.title}"


class ScorecardEmailConfiguration(models.Model):
    WITHOUT_SCORE_EMAIL_DAILY = "daily"
    WITHOUT_SCORE_EMAIL_WEEKLY = "weekly"
    WITHOUT_SCORE_EMAIL_MONTHLY = "monthly"
    WITHOUT_SCORE_EMAIL_FREQUENCY_CHOICES = [
        (WITHOUT_SCORE_EMAIL_DAILY, "Daily"),
        (WITHOUT_SCORE_EMAIL_WEEKLY, "Weekly"),
        (WITHOUT_SCORE_EMAIL_MONTHLY, "Monthly"),
    ]

    name = models.CharField(max_length=100, default="Primary Scorecard Email Configuration")
    is_enabled = models.BooleanField(default=True)
    send_workflow_emails = models.BooleanField(default=True)
    send_failure_emails = models.BooleanField(default=True)
    checker_pending_reminder_hours = models.PositiveIntegerField(default=24)
    without_score_summary_frequency = models.CharField(
        max_length=20,
        choices=WITHOUT_SCORE_EMAIL_FREQUENCY_CHOICES,
        default=WITHOUT_SCORE_EMAIL_DAILY,
    )
    without_score_summary_hour = models.PositiveSmallIntegerField(default=8)
    without_score_summary_weekday = models.PositiveSmallIntegerField(
        default=0,
        help_text="0 is Monday and 6 is Sunday.",
    )
    without_score_summary_month_day = models.PositiveSmallIntegerField(default=1)
    without_score_summary_last_sent_at = models.DateTimeField(null=True, blank=True)
    schedule_failure_repeat_threshold = models.PositiveIntegerField(default=3)
    schedule_failure_repeat_window_hours = models.PositiveIntegerField(default=24)
    application_base_url = models.URLField(max_length=500, blank=True, default="")
    smtp_host = models.CharField(max_length=255, blank=True)
    smtp_port = models.PositiveIntegerField(default=587)
    smtp_username = models.CharField(max_length=255, blank=True)
    smtp_password = models.CharField(max_length=1024, blank=True)
    smtp_use_tls = models.BooleanField(default=True)
    smtp_use_ssl = models.BooleanField(default=False)
    from_email_override = models.EmailField(blank=True)
    reply_to_email = models.EmailField(blank=True)
    footer_text = models.TextField(
        blank=True,
        default="This email was sent from the scorecard system because the event needs attention or action.",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_scorecard_email_configurations",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_EMAIL_CONFIGURATION"
        verbose_name = "Scorecard Email Configuration"
        verbose_name_plural = "Scorecard Email Configuration"

    def __str__(self) -> str:
        return self.name


class ScorecardWorkflowApprovalSetting(models.Model):
    name = models.CharField(
        max_length=120,
        default="Primary Scorecard Workflow Approval Settings",
    )
    basel_score_superuser_auto_approve = models.BooleanField(default=True)
    basel_score_admin_auto_approve = models.BooleanField(default=False)
    basel_score_reviewer_auto_approve = models.BooleanField(default=True)
    ifrs9_score_superuser_auto_approve = models.BooleanField(default=True)
    ifrs9_score_admin_auto_approve = models.BooleanField(default=False)
    ifrs9_score_reviewer_auto_approve = models.BooleanField(default=True)
    basel_template_superuser_auto_approve = models.BooleanField(default=True)
    basel_template_admin_auto_approve = models.BooleanField(default=False)
    basel_template_reviewer_auto_approve = models.BooleanField(default=True)
    ifrs9_template_superuser_auto_approve = models.BooleanField(default=True)
    ifrs9_template_admin_auto_approve = models.BooleanField(default=False)
    ifrs9_template_reviewer_auto_approve = models.BooleanField(default=True)
    enforce_score_counterpart_completion = models.BooleanField(
        default=True,
        help_text="Require an officer to complete the missing Basel/IFRS9 counterpart for their own submitted customer before starting another customer.",
    )
    prevent_cross_branch_duplicate_scoring = models.BooleanField(
        default=False,
        help_text="When enabled, a customer already scored in any branch cannot be scored again in another branch and can only be previewed in read-only mode.",
    )
    without_score_list_include_loans = models.BooleanField(
        default=True,
        help_text="Include CustomerLoan staged customers in the Without Basel and Without IFRS9 customer lists.",
    )
    without_score_list_include_overdrafts = models.BooleanField(
        default=True,
        help_text="Include CustomerOverdraft staged customers in the Without Basel and Without IFRS9 customer lists.",
    )
    single_branch_submission_reviewer_notifications = models.BooleanField(
        default=False,
        help_text="When enabled, Basel/IFRS9 score submission notifications go only to reviewers with exactly one matching branch assignment.",
    )
    single_branch_without_score_maker_notifications = models.BooleanField(
        default=False,
        help_text="When enabled, scheduled missing-score emails go only to score makers with exactly one matching branch assignment.",
    )
    auto_refresh_autofilled_scores_enabled = models.BooleanField(
        default=False,
        help_text=(
            "When enabled, the scheduler refreshes approved Basel and IFRS9 scores when API-backed "
            "auto-filled fields change, creates a new approved version, and keeps manual responses untouched."
        ),
    )
    auto_refresh_autofilled_scores_frequency = models.CharField(
        max_length=20,
        default="daily",
        help_text="How often the scheduled auto-refresh should run: daily, weekly, or monthly.",
    )
    auto_refresh_autofilled_scores_time = models.TimeField(
        default=datetime_time(2, 0),
        help_text="Local time of day when the scheduled auto-refresh should run.",
    )
    auto_refresh_autofilled_scores_weekday = models.PositiveSmallIntegerField(
        default=0,
        help_text="Weekday for weekly auto-refresh runs. 0 is Monday and 6 is Sunday.",
    )
    auto_refresh_autofilled_scores_month_day = models.PositiveSmallIntegerField(
        default=1,
        help_text="Day of month for monthly auto-refresh runs.",
    )
    auto_refresh_autofilled_scores_batch_size = models.PositiveIntegerField(
        default=1000,
        help_text="Maximum approved Basel and IFRS9 score forms to check per scheduled auto-refresh run.",
    )
    auto_refresh_autofilled_scores_basel_cursor_id = models.PositiveIntegerField(
        default=0,
        help_text="Internal resume cursor for batched Basel score auto-refresh runs.",
    )
    auto_refresh_autofilled_scores_ifrs9_cursor_id = models.PositiveIntegerField(
        default=0,
        help_text="Internal resume cursor for batched IFRS9 score auto-refresh runs.",
    )
    auto_refresh_autofilled_scores_last_run_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time the scheduled auto-refresh completed.",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_scorecard_workflow_approval_settings",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_WORKFLOW_APPROVAL_SETTING"
        verbose_name = "Scorecard Workflow Approval Setting"
        verbose_name_plural = "Scorecard Workflow Approval Settings"

    def __str__(self) -> str:
        return self.name


class ScorecardEmailTemplate(models.Model):
    CATEGORY_WORKFLOW = "workflow"
    CATEGORY_FAILURE = "failure"
    CATEGORY_CHOICES = [
        (CATEGORY_WORKFLOW, "Workflow"),
        (CATEGORY_FAILURE, "Failure"),
    ]

    event_code = models.CharField(max_length=80, unique=True)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default=CATEGORY_WORKFLOW)
    description = models.TextField(blank=True)
    is_enabled = models.BooleanField(default=True)
    subject_template = models.TextField()
    text_body_template = models.TextField()
    html_body_template = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_scorecard_email_templates",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_EMAIL_TEMPLATE"
        verbose_name = "Scorecard Email Template"
        verbose_name_plural = "Scorecard Email Templates"
        ordering = ["category", "name", "event_code"]
        indexes = [
            models.Index(fields=["category", "is_enabled"], name="SC_EMAIL_TMPL_CAT_IDX"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.event_code})"


class ScorecardEmailSendLog(models.Model):
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
    ]

    event_code = models.CharField(max_length=80)
    event_name = models.CharField(max_length=150, blank=True)
    event_category = models.CharField(max_length=20, choices=ScorecardEmailTemplate.CATEGORY_CHOICES, blank=True)
    recipient_email = models.EmailField()
    recipient_name = models.CharField(max_length=255, blank=True)
    subject = models.CharField(max_length=255)
    text_body = models.TextField()
    html_body = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_SUCCESS)
    error_message = models.TextField(blank=True)
    related_reference = models.CharField(max_length=120, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    configuration = models.ForeignKey(
        ScorecardEmailConfiguration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="send_logs",
    )
    retried_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="retry_attempts",
    )
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "SCORECARD_EMAIL_SEND_LOG"
        verbose_name = "Scorecard Email Send Log"
        verbose_name_plural = "Scorecard Email Send Logs"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "created_at"], name="SC_EMAIL_LOG_STATUS_IDX"),
            models.Index(fields=["event_code", "created_at"], name="SC_EMAIL_LOG_EVENT_IDX"),
            models.Index(fields=["recipient_email"], name="SC_EMAIL_LOG_RECIPIENT_IDX"),
        ]

    def __str__(self) -> str:
        return f"{self.event_code} -> {self.recipient_email} ({self.status})"


class ScorecardUserAuditTrail(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scorecard_audit_entries",
    )
    model_name = models.CharField(max_length=100)
    action = models.CharField(max_length=50)
    object_id = models.CharField(max_length=255, null=True, blank=True)
    branch_name = models.CharField(max_length=150, blank=True, default="")
    change_description = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "SCORECARD_USERS_AUDITTRAIL"
        verbose_name = "Scorecard Audit Trail"
        verbose_name_plural = "Scorecard Audit Trails"
        ordering = ["-timestamp", "-id"]
        indexes = [
            models.Index(fields=["model_name", "timestamp"], name="SC_AUDIT_MODEL_TS_IDX"),
            models.Index(fields=["action", "timestamp"], name="SC_AUDIT_ACTION_TS_IDX"),
            models.Index(fields=["user", "timestamp"], name="SC_AUDIT_USER_TS_IDX"),
            models.Index(fields=["branch_name", "timestamp"], name="SC_AUDIT_BR_TS_IDX"),
        ]

    def __str__(self) -> str:
        actor = getattr(self.user, "email", None) or getattr(self.user, "username", None) or "System"
        return f"{actor} {self.action} {self.model_name} on {self.timestamp}"
