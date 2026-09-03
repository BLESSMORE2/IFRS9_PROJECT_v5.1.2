from datetime import date
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

from django.contrib.auth.models import Permission
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.test import SimpleTestCase
from django.test import TestCase
from django.urls import reverse

import openpyxl

from IFRS9.models import stg_collateral_data, stg_loans_data, stg_payment_schedule
from scorecard.functions_view.ifrs9_supporting_data import (
    _process_collateral_upload,
    _process_payment_schedule_upload,
    _workspace_shell_context,
)
from Users.models import CustomUser
from scorecard.functions_view.basel_scores_form import (
    _can_auto_approve_basel_submission,
    _create_evaluation_version,
    _finalize_basel_auto_approval,
)
from scorecard.functions_view.dashboard import _build_dashboard_access
from scorecard.functions_view.scorecard_autofill import (
    _age_option,
    _gender_option,
    _resident_option,
    build_basel_autofill,
    build_ifrs9_autofill,
)
from scorecard.functions_view.settings import settings_workflow_approvals
from scorecard.functions_view.ifrs9_scores_form import (
    _can_auto_approve_ifrs9_submission,
    _create_ifrs9_evaluation_version,
    _finalize_ifrs9_auto_approval,
)
from scorecard.functions_view.template_maker_checker import (
    _can_auto_approve_basel_template_submission,
    _can_auto_approve_ifrs9_template_submission,
    _create_ifrs9_template_version,
    _create_template_version,
    _finalize_basel_template_auto_approval,
    _finalize_ifrs9_template_auto_approval,
)
from scorecard.models import (
    BaselScoreSheetTemplate,
    BankBranch,
    CreditEvaluation,
    CustomerCorporate,
    IFRS9Evaluation,
    IFRS9ScoreSheetTemplate,
    ScorecardWorkflowApprovalSetting,
    ScorecardUserBranchAccess,
)
from scorecard.workflow_approval import (
    can_user_self_review_score_submission,
    get_scorecard_workflow_approval_settings,
)


class AutofillOptionMappingTests(SimpleTestCase):
    def setUp(self):
        self.female_option = SimpleNamespace(
            id=1,
            label="i) Female",
            display_order=1,
        )
        self.male_option = SimpleNamespace(
            id=2,
            label="ii) Male",
            display_order=2,
        )
        self.gender_attribute = SimpleNamespace(
            options=[self.female_option, self.male_option],
        )

    def test_male_maps_to_database_male_option_not_female_substring(self):
        self.assertIs(_gender_option(self.gender_attribute, "M"), self.male_option)
        self.assertIs(_gender_option(self.gender_attribute, "male"), self.male_option)

    def test_female_maps_to_database_female_option(self):
        self.assertIs(_gender_option(self.gender_attribute, "F"), self.female_option)
        self.assertIs(_gender_option(self.gender_attribute, "female"), self.female_option)

    def test_unknown_gender_is_not_guessed(self):
        self.assertIsNone(_gender_option(self.gender_attribute, "C"))

    def test_residence_codes_map_to_the_correct_database_options(self):
        permanent = SimpleNamespace(id=11, label="i) Permanent Residence", display_order=1)
        temporary = SimpleNamespace(id=12, label="ii) Temporary Residence", display_order=2)
        non_resident = SimpleNamespace(id=13, label="iii) Non-Resident", display_order=3)
        attribute = SimpleNamespace(options=[permanent, temporary, non_resident])

        self.assertIs(_resident_option(attribute, "R"), permanent)
        self.assertIs(_resident_option(attribute, "T"), temporary)
        self.assertIs(_resident_option(attribute, "N"), non_resident)

    def test_age_uses_the_numeric_range_on_the_database_option(self):
        age_41_60 = SimpleNamespace(id=21, label="iii) 41 - 60 Years", display_order=1)
        above_60 = SimpleNamespace(id=22, label="iv) Above 60 Years", display_order=2)
        attribute = SimpleNamespace(options=[age_41_60, above_60])
        current_year = date.today().year

        self.assertIs(_age_option(attribute, date(current_year - 56, 1, 1)), age_41_60)
        self.assertIs(_age_option(attribute, date(current_year - 65, 1, 1)), above_60)

    @staticmethod
    def _attribute(attribute_id, code, label, option_labels):
        options = [
            SimpleNamespace(id=(attribute_id * 10) + index, label=option_label, display_order=index)
            for index, option_label in enumerate(option_labels, start=1)
        ]
        return SimpleNamespace(
            id=attribute_id,
            code=code,
            label=label,
            is_required=True,
            options=options,
        )

    @staticmethod
    def _selected_labels(result, attributes):
        options_by_id = {
            option.id: option.label
            for attribute in attributes
            for option in attribute.options
        }
        return {
            attribute.code: options_by_id[int(result.attribute_values[attribute.id])]
            for attribute in attributes
            if attribute.id in result.attribute_values
        }

    def test_requested_basel_corporate_templates_use_corporate_profile_options(self):
        main_customer = SimpleNamespace(customer_ref_code="100")

        farming_attribute = self._attribute(
            201,
            "YEARS_FARMING",
            "No. of years in farming",
            ["Over 5 years", "4 - 5 Years", "3 - 4 Years", "2 - 3 Years", "Less than 2 Years"],
        )
        farming_profile = CustomerCorporate(
            client_code="100",
            client_name="TEST FARMING PVT LTD",
            years_in_business=4,
        )
        farming_result = build_basel_autofill(
            "AFCSC1-17",
            main_customer,
            {1: [farming_attribute]},
            farming_profile,
        )
        self.assertEqual(
            self._selected_labels(farming_result, [farming_attribute])["YEARS_FARMING"],
            "4 - 5 Years",
        )

        ownership_attribute = self._attribute(
            202,
            "OWNERSHIP_CONTROL",
            "Ownership & Degree of control",
            [
                "i) Holding Company/Group of companies/Parastatal/State Owned Enterprises/Other State arms/departments",
                "ii) Subsidiary of a holding Company",
                "iii) Stand Alone Company/affiliate company",
                "iv) Unincorporated body",
            ],
        )
        corporate_profile = CustomerCorporate(
            client_code="100",
            client_name="TEST ENGINEERING PVT LTD",
            years_in_business=12,
        )
        corporate_result = build_basel_autofill(
            "ACCSC1-17",
            main_customer,
            {1: [ownership_attribute]},
            corporate_profile,
        )
        self.assertIn(
            "Stand Alone Company",
            self._selected_labels(corporate_result, [ownership_attribute])["OWNERSHIP_CONTROL"],
        )

        legal_attribute = self._attribute(
            203,
            "LEGAL_STATUS",
            "Legal Status",
            [
                "i) Registered company/ Registered Association",
                "ii) Partnership",
                "iii) Sole proprietor",
                "iv) Unregistered informal body",
                "v) Other",
            ],
        )
        years_attribute = self._attribute(
            204,
            "YEARS_IN_BUSINESS",
            "No. of Years in Business",
            [
                "i) More than 4 Years",
                "ii) 3 - 4 Years",
                "iii) 2 - 3 Years",
                "iv) 1 -2 Years",
                "v) Less than 1 Year",
                "vi) Start up/Greenfield",
            ],
        )
        size_attribute = self._attribute(
            205,
            "SIZE_OF_ORGANIZATION",
            "Size of organization",
            [
                "i) Large scale Retail entity",
                "ii) Medium Size Retail entity",
                "iii) Small scale family owned business",
                "iv) Cooperative",
            ],
        )
        cooperative_profile = CustomerCorporate(
            client_code="100",
            client_name="TEST CO-OPERATIVE",
            registration_number="REG-1",
            years_in_business=4,
        )
        retail_attributes = [legal_attribute, years_attribute, size_attribute]
        retail_result = build_basel_autofill(
            "ARCSC1-17",
            main_customer,
            {1: retail_attributes},
            cooperative_profile,
        )
        selected = self._selected_labels(retail_result, retail_attributes)
        self.assertIn("Registered company", selected["LEGAL_STATUS"])
        self.assertIn("3 - 4 Years", selected["YEARS_IN_BUSINESS"])
        self.assertIn("Cooperative", selected["SIZE_OF_ORGANIZATION"])

    def test_requested_ifrs9_corporate_templates_use_corporate_profile_options(self):
        main_customer = SimpleNamespace(customer_ref_code="200")

        cases = [
            (
                "IFRS9PD-CORPORATE-001",
                CustomerCorporate(client_code="200", client_name="TEST PVT LTD", years_in_business=8),
                self._attribute(301, "BUSINESS_LIFE_CYCLE_STAGE", "Business life cycle", ["Maturity stage", "Growth stage", "Birth Stage", "Decline stage"]),
                "Maturity stage",
            ),
            (
                "IFRS9PD-FARMING-001",
                CustomerCorporate(client_code="200", client_name="TEST FARM", years_in_business=4),
                self._attribute(302, "EXPERIENCE", "Experience", ["Seasoned farmer with over 5years farming experience", "Seasoned farmer with over 2 years but less than 5 years farming experience", "New farmer supported by experienced management"]),
                "over 2 years",
            ),
            (
                "IFRS9PD-LOCALAUTHORITIES-001",
                CustomerCorporate(client_code="200", client_name="TEST RURAL DISTRICT COUNCIL", years_in_business=20),
                self._attribute(303, "LOCAL_AUTHORITY_CLASSIFICATION", "Classification", ["City Councils", "Town Councils", "Rural District Councils"]),
                "Rural District Councils",
            ),
            (
                "IFRS9PD-MFINANCE-001",
                CustomerCorporate(client_code="200", client_name="TEST FARM", industry_code="00100", years_in_business=4),
                self._attribute(304, "NATURE_OF_BORROWER", "Nature", ["Salary based Microfinance loans", "Manufacturing", "Services", "Retailing", "Cross border", "Vendors", "Agriculture", "Other"]),
                "Agriculture",
            ),
            (
                "IFRS9PD-RETAIL-001",
                CustomerCorporate(client_code="200", client_name="TEST CO-OPERATIVE", years_in_business=4),
                self._attribute(305, "OWNERSHIP_STRUCTURE", "Ownership", ["Registered Company", "Partnership", "Sole Proprietor", "Cooperatives and other informal bodies"]),
                "Cooperatives",
            ),
            (
                "IFRS9PD-SCHOOLS-001",
                CustomerCorporate(client_code="200", client_name="TEST PRIMARY SCHOOL", years_in_business=4),
                self._attribute(306, "TYPE_OF_SCHOOL", "Type of School", ["Secondary", "Primary"]),
                "Primary",
            ),
            (
                "IFRS9PD-TERTIARY-001",
                CustomerCorporate(client_code="200", client_name="TEST STATE UNIVERSITY", years_in_business=8),
                self._attribute(307, "TYPE_OF_INSTITUTION", "Type of Institution", ["Universities", "Technical Colleges", "Teachers' College", "Vocational Training Colleges", "Others"]),
                "Universities",
            ),
        ]

        for template_code, profile, attribute, expected_label in cases:
            with self.subTest(template_code=template_code):
                result = build_ifrs9_autofill(
                    template_code,
                    main_customer,
                    {1: [attribute]},
                    profile,
                )
                selected = self._selected_labels(result, [attribute])
                self.assertIn(expected_label, selected[attribute.code])

    def test_ifrs9_templates_can_use_latest_loan_profile_options(self):
        main_customer = SimpleNamespace(customer_ref_code="300")
        profile = CustomerCorporate(client_code="300", client_name="TEST MICROFINANCE", years_in_business=5)
        loan = SimpleNamespace(
            reporting_date=date(2026, 6, 30),
            loan_id="LN300",
            loan_amount=Decimal("3000"),
            start_date=date(2026, 1, 1),
            maturity_date=date(2026, 10, 1),
            collateral_amount=Decimal("5000"),
            overdue_amount=Decimal("0"),
            last_payment_date=None,
            delinquent_days=0,
            npl_indicator_current="N",
            past_due_indicator="N",
        )
        attributes = [
            self._attribute(
                308,
                "SIZE_OF_LOAN",
                "Size of Loan",
                ["Below $1,000", "$1,000 - $5,000", "$5,000 - $10,000", "Above $10,000"],
            ),
            self._attribute(
                309,
                "LOAN_TENOR",
                "Loan Tenor",
                ["0 - 6 Months", "6 - 12 Months", "12 - 24 Months", "More than 48 Months"],
            ),
            self._attribute(
                310,
                "SECURITY",
                "Security",
                ["Tangible security adequately covering exposure", "Partially covered by tangible security", "Wholly unsecured exposure"],
            ),
            self._attribute(
                311,
                "REPAYMENT_TRACK_RECORD",
                "Loan Repayment Track Record",
                ["No arrears and repayments are up to date", "30 days in default", "Current NPL"],
            ),
        ]

        result = build_ifrs9_autofill(
            "IFRS9PD-MFINANCE-001",
            main_customer,
            {1: attributes},
            profile,
            loan=loan,
        )

        selected = self._selected_labels(result, attributes)
        self.assertIn("$1,000 - $5,000", selected["SIZE_OF_LOAN"])
        self.assertIn("6 - 12 Months", selected["LOAN_TENOR"])
        self.assertNotIn("SECURITY", selected)
        self.assertNotIn("REPAYMENT_TRACK_RECORD", selected)


class DashboardPermissionTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            email="dashboard-viewer@example.com",
            surname="Viewer",
            name="Dashboard",
            address="HQ",
            department="Risk",
            phone_number="2700000999",
        )
        self.request = RequestFactory().get("/scorecard/dashboard/content/")
        self.request.user = self.user

    def _grant(self, codename):
        permission = Permission.objects.get(
            content_type__app_label="scorecard",
            codename=codename,
        )
        self.user.user_permissions.add(permission)

    def test_workspace_access_keeps_existing_permission_based_dashboard_visibility(self):
        self._grant("access_scorecard_dashboard")

        dashboard_access = _build_dashboard_access(self.request)

        self.assertFalse(dashboard_access["full_dashboard"])
        self.assertFalse(dashboard_access["customers"])
        self.assertFalse(dashboard_access["basel_scores"])
        self.assertFalse(dashboard_access["ifrs9_scores"])
        self.assertFalse(dashboard_access["templates"])
        self.assertFalse(dashboard_access["operations"])

    def test_full_dashboard_permission_exposes_all_read_only_dashboard_content(self):
        self._grant("access_scorecard_dashboard")
        self._grant("view_full_scorecard_dashboard")

        dashboard_access = _build_dashboard_access(self.request)

        self.assertTrue(dashboard_access["full_dashboard"])
        self.assertTrue(dashboard_access["customers"])
        self.assertTrue(dashboard_access["basel_scores"])
        self.assertTrue(dashboard_access["ifrs9_scores"])
        self.assertTrue(dashboard_access["basel_templates"])
        self.assertTrue(dashboard_access["ifrs9_templates"])
        self.assertTrue(dashboard_access["operations"])
        self.assertTrue(dashboard_access["charts"])
        self.assertTrue(dashboard_access["recent_activity"])
        self.assertFalse(dashboard_access["quick_actions"])


class ScorecardAutoApprovalTests(TestCase):
    def _create_user(self, email: str, *, is_superuser: bool = False) -> CustomUser:
        if is_superuser:
            return CustomUser.objects.create_superuser(
                email=email,
                surname="Admin",
                name="Admin",
                address="HQ",
                department="Risk",
                phone_number=f"27{CustomUser.objects.count() + 1000}",
            )
        return CustomUser.objects.create_user(
            email=email,
            surname="User",
            name="User",
            address="HQ",
            department="Risk",
            phone_number=f"27{CustomUser.objects.count() + 1000}",
        )

    def _grant_scorecard_permission(self, user: CustomUser, codename: str) -> None:
        permission = Permission.objects.get(
            content_type__app_label="scorecard",
            codename=codename,
        )
        user.user_permissions.add(permission)

    def test_superuser_matches_basel_score_auto_approve_rule(self):
        user = self._create_user("basel-super@example.com", is_superuser=True)
        self.assertTrue(_can_auto_approve_basel_submission(user))

    def test_reviewer_permission_matches_ifrs9_score_auto_approve_rule(self):
        user = self._create_user("ifrs9-reviewer@example.com")
        self._grant_scorecard_permission(user, "review_ifrs9_scores")
        self.assertTrue(_can_auto_approve_ifrs9_submission(user))

    def test_admin_permission_matches_basel_score_auto_approve_rule(self):
        user = self._create_user("basel-admin@example.com")
        self._grant_scorecard_permission(user, "reopen_basel_scores")
        workflow_settings = get_scorecard_workflow_approval_settings()
        workflow_settings.basel_score_admin_auto_approve = True
        workflow_settings.save()

        self.assertTrue(_can_auto_approve_basel_submission(user))

    def test_basel_checker_permission_uses_checker_auto_approve_rule_not_admin_rule(self):
        user = self._create_user("basel-checker-only@example.com")
        self._grant_scorecard_permission(user, "review_basel_scores")
        workflow_settings = get_scorecard_workflow_approval_settings()
        workflow_settings.basel_score_admin_auto_approve = True
        workflow_settings.basel_score_reviewer_auto_approve = False
        workflow_settings.save()

        self.assertFalse(_can_auto_approve_basel_submission(user))

        workflow_settings.basel_score_reviewer_auto_approve = True
        workflow_settings.save()

        self.assertTrue(_can_auto_approve_basel_submission(user))

    def test_ifrs9_checker_permission_uses_checker_auto_approve_rule_not_admin_rule(self):
        user = self._create_user("ifrs9-checker-only@example.com")
        self._grant_scorecard_permission(user, "review_ifrs9_scores")
        workflow_settings = get_scorecard_workflow_approval_settings()
        workflow_settings.ifrs9_score_admin_auto_approve = True
        workflow_settings.ifrs9_score_reviewer_auto_approve = False
        workflow_settings.save()

        self.assertFalse(_can_auto_approve_ifrs9_submission(user))

        workflow_settings.ifrs9_score_reviewer_auto_approve = True
        workflow_settings.save()

        self.assertTrue(_can_auto_approve_ifrs9_submission(user))

    def test_finalize_basel_auto_approval_updates_evaluation_and_version(self):
        maker = self._create_user("basel-maker@example.com")
        checker = self._create_user("basel-checker@example.com")

        template = BaselScoreSheetTemplate.objects.create(
            code="BASEL-AUTO-001",
            name="Basel Auto Approval",
            status="approved",
        )
        evaluation = CreditEvaluation.objects.create(
            template=template,
            branch_name="HEAD OFFICE",
            customer_name="Test Basel Customer",
            customer_id="1001",
            maker=maker,
            checker=checker,
            status="submitted",
            total_raw_score=Decimal("12.00"),
            total_weighted_percent=Decimal("76.00"),
            final_grade="A1",
        )

        _create_evaluation_version(
            evaluation=evaluation,
            version_number=1,
            user=maker,
            change_description="Pending Basel submission",
            total_weighted_percent=Decimal("76.00"),
            final_grade="A1",
            total_raw_score=Decimal("12.00"),
        )

        _finalize_basel_auto_approval(
            evaluation,
            checker,
            old_status="submitted",
            comments="Auto-approved in test",
        )

        evaluation.refresh_from_db()
        approved_version = evaluation.versions.get(version_number=1)

        self.assertEqual(evaluation.status, "approved")
        self.assertEqual(evaluation.approved_by_id, checker.id)
        self.assertEqual(evaluation.approved_weighted_percent, Decimal("76.00"))
        self.assertEqual(evaluation.total_weighted_percent, Decimal("76.00"))
        self.assertEqual(evaluation.approved_grade, "A¹")
        self.assertEqual(evaluation.final_grade, "A¹")
        self.assertTrue(approved_version.is_approved)
        self.assertEqual(approved_version.approved_by_id, checker.id)
        self.assertEqual(
            evaluation.workflow_history.order_by("-created_at").first().action,
            "approved",
        )

    def test_finalize_ifrs9_auto_approval_updates_evaluation_and_version(self):
        maker = self._create_user("ifrs9-maker@example.com")
        checker = self._create_user("ifrs9-checker@example.com")

        template = IFRS9ScoreSheetTemplate.objects.create(
            code="IFRS9-AUTO-001",
            name="IFRS9 Auto Approval",
            status="approved",
        )
        evaluation = IFRS9Evaluation.objects.create(
            template=template,
            branch_name="HEAD OFFICE",
            customer_name="Test IFRS9 Customer",
            customer_id="2001",
            maker=maker,
            checker=checker,
            status="submitted",
            total_raw_score=Decimal("15.00"),
            total_weighted_percent=Decimal("82.00"),
            final_grade="",
        )

        _create_ifrs9_evaluation_version(
            evaluation=evaluation,
            version_number=1,
            user=maker,
            change_description="Pending IFRS9 submission",
            total_weighted_percent=Decimal("82.00"),
            final_grade="",
            total_raw_score=Decimal("15.00"),
        )

        _finalize_ifrs9_auto_approval(
            evaluation,
            checker,
            old_status="submitted",
            comments="Auto-approved in test",
        )

        evaluation.refresh_from_db()
        approved_version = evaluation.versions.get(version_number=1)

        self.assertEqual(evaluation.status, "approved")
        self.assertEqual(evaluation.approved_by_id, checker.id)
        self.assertEqual(evaluation.approved_weighted_percent, Decimal("82.00"))
        self.assertEqual(evaluation.total_weighted_percent, Decimal("82.00"))
        self.assertEqual(evaluation.approved_grade, "")
        self.assertTrue(approved_version.is_approved)
        self.assertEqual(approved_version.approved_by_id, checker.id)
        self.assertEqual(
            evaluation.workflow_history.order_by("-created_at").first().action,
            "approved",
        )

    def test_finalize_basel_template_auto_approval_updates_template_and_version(self):
        maker = self._create_user("basel-template-maker@example.com")
        checker = self._create_user("basel-template-checker@example.com")

        template = BaselScoreSheetTemplate.objects.create(
            code="BASEL-TEMPLATE-AUTO-001",
            name="Basel Template Auto Approval",
            maker=maker,
            checker=checker,
            status="submitted",
        )

        _create_template_version(
            template=template,
            version_number=1,
            user=maker,
            change_description="Pending Basel template submission",
        )

        _finalize_basel_template_auto_approval(
            template,
            checker,
            old_status="submitted",
            comments="Auto-approved in test",
        )

        template.refresh_from_db()
        approved_version = template.versions.get(version_number=1)

        self.assertEqual(template.status, "approved")
        self.assertEqual(template.approved_by_id, checker.id)
        self.assertTrue(approved_version.is_approved)
        self.assertEqual(approved_version.approved_by_id, checker.id)
        self.assertEqual(
            template.workflow_history.order_by("-performed_at").first().action,
            "approved",
        )

    def test_ifrs9_template_reviewer_permission_matches_auto_approve_rule(self):
        user = self._create_user("ifrs9-template-reviewer@example.com")
        self._grant_scorecard_permission(user, "review_ifrs9_templates")
        self.assertTrue(_can_auto_approve_ifrs9_template_submission(user))

    def test_admin_permission_matches_ifrs9_template_auto_approve_rule(self):
        user = self._create_user("ifrs9-template-admin@example.com")
        self._grant_scorecard_permission(user, "manage_ifrs9_templates")
        workflow_settings = get_scorecard_workflow_approval_settings()
        workflow_settings.ifrs9_template_admin_auto_approve = True
        workflow_settings.save()

        self.assertTrue(_can_auto_approve_ifrs9_template_submission(user))

    def test_finalize_ifrs9_template_auto_approval_updates_template_and_version(self):
        maker = self._create_user("ifrs9-template-maker@example.com")
        checker = self._create_user("ifrs9-template-checker@example.com")

        template = IFRS9ScoreSheetTemplate.objects.create(
            code="IFRS9-TEMPLATE-AUTO-001",
            name="IFRS9 Template Auto Approval",
            maker=maker,
            checker=checker,
            status="submitted",
        )

        _create_ifrs9_template_version(
            template=template,
            version_number=1,
            user=maker,
            change_description="Pending IFRS9 template submission",
        )

        _finalize_ifrs9_template_auto_approval(
            template,
            checker,
            old_status="submitted",
            comments="Auto-approved in test",
        )

        template.refresh_from_db()
        approved_version = template.versions.get(version_number=1)

        self.assertEqual(template.status, "approved")
        self.assertEqual(template.approved_by_id, checker.id)
        self.assertTrue(approved_version.is_approved)
        self.assertEqual(approved_version.approved_by_id, checker.id)
        self.assertEqual(
            template.workflow_history.order_by("-performed_at").first().action,
            "approved",
        )

    def test_reviewer_permission_matches_basel_template_auto_approve_rule(self):
        user = self._create_user("basel-template-reviewer@example.com")
        self._grant_scorecard_permission(user, "review_basel_templates")
        self.assertTrue(_can_auto_approve_basel_template_submission(user))

    def test_superuser_basel_score_auto_approve_can_be_disabled_from_settings(self):
        user = self._create_user("basel-super-disabled@example.com", is_superuser=True)
        workflow_settings = get_scorecard_workflow_approval_settings()
        workflow_settings.basel_score_superuser_auto_approve = False
        workflow_settings.save()

        self.assertFalse(_can_auto_approve_basel_submission(user))

    def test_reviewer_ifrs9_template_auto_approve_can_be_disabled_from_settings(self):
        user = self._create_user("ifrs9-template-disabled@example.com")
        self._grant_scorecard_permission(user, "review_ifrs9_templates")
        workflow_settings = get_scorecard_workflow_approval_settings()
        workflow_settings.ifrs9_template_reviewer_auto_approve = False
        workflow_settings.save()

        self.assertFalse(_can_auto_approve_ifrs9_template_submission(user))

    def test_superuser_cannot_self_review_basel_score_when_auto_approval_disabled(self):
        user = self._create_user("basel-self-review-disabled@example.com", is_superuser=True)
        workflow_settings = get_scorecard_workflow_approval_settings()
        workflow_settings.basel_score_superuser_auto_approve = False
        workflow_settings.save()

        self.assertFalse(
            can_user_self_review_score_submission(user, "basel_scores", "scorecard.review_basel_scores")
        )

    def test_reviewer_cannot_self_review_ifrs9_score_when_auto_approval_disabled(self):
        user = self._create_user("ifrs9-self-review-disabled@example.com")
        self._grant_scorecard_permission(user, "review_ifrs9_scores")
        workflow_settings = get_scorecard_workflow_approval_settings()
        workflow_settings.ifrs9_score_reviewer_auto_approve = False
        workflow_settings.save()

        self.assertFalse(
            can_user_self_review_score_submission(user, "ifrs9_scores", "scorecard.review_ifrs9_scores")
        )

    def test_permission_manager_can_update_workflow_rules_from_frontend(self):
        manager = self._create_user("permission-manager@example.com")
        self._grant_scorecard_permission(manager, "view_scorecard_workflow")
        self._grant_scorecard_permission(manager, "manage_scorecard_workflow")
        workflow_settings = get_scorecard_workflow_approval_settings()
        manager = CustomUser.objects.get(pk=manager.pk)
        self.assertTrue(manager.has_perm("scorecard.view_scorecard_workflow"))
        self.assertTrue(manager.has_perm("scorecard.manage_scorecard_workflow"))
        request = RequestFactory().post(
            reverse("scorecard:settings_workflow_approvals"),
            data={
                "basel_score_superuser_auto_approve": "on",
                "basel_score_admin_auto_approve": "on",
                "basel_score_reviewer_auto_approve": "",
                "ifrs9_score_superuser_auto_approve": "",
                "ifrs9_score_admin_auto_approve": "",
                "ifrs9_score_reviewer_auto_approve": "on",
                "basel_template_superuser_auto_approve": "",
                "basel_template_admin_auto_approve": "on",
                "basel_template_reviewer_auto_approve": "on",
                "ifrs9_template_superuser_auto_approve": "on",
                "ifrs9_template_admin_auto_approve": "",
                "ifrs9_template_reviewer_auto_approve": "",
            },
        )
        request.user = manager
        session_middleware = SessionMiddleware(lambda req: None)
        session_middleware.process_request(request)
        request.session.save()
        setattr(request, "_messages", FallbackStorage(request))

        response = settings_workflow_approvals(request)

        self.assertEqual(response.status_code, 302)
        workflow_settings.refresh_from_db()
        self.assertTrue(workflow_settings.basel_score_superuser_auto_approve)
        self.assertTrue(workflow_settings.basel_score_admin_auto_approve)
        self.assertFalse(workflow_settings.basel_score_reviewer_auto_approve)
        self.assertFalse(workflow_settings.ifrs9_score_superuser_auto_approve)
        self.assertFalse(workflow_settings.ifrs9_score_admin_auto_approve)
        self.assertTrue(workflow_settings.ifrs9_score_reviewer_auto_approve)
        self.assertFalse(workflow_settings.basel_template_superuser_auto_approve)
        self.assertTrue(workflow_settings.basel_template_admin_auto_approve)
        self.assertTrue(workflow_settings.basel_template_reviewer_auto_approve)
        self.assertTrue(workflow_settings.ifrs9_template_superuser_auto_approve)
        self.assertFalse(workflow_settings.ifrs9_template_admin_auto_approve)
        self.assertFalse(workflow_settings.ifrs9_template_reviewer_auto_approve)


class IFRS9SupportingDataWorkspaceTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = CustomUser.objects.create_superuser(
            email="supporting-data-admin@example.com",
            surname="Admin",
            name="Admin",
            address="HQ",
            department="Risk",
            phone_number="27999999991",
        )
        self.branch = BankBranch.objects.create(
            branch_name="Chegutu",
            branch_code="CHEGUTU",
            bank_name="Test Bank",
        )
        ScorecardUserBranchAccess.objects.create(user=self.user, branch=self.branch)
        self.loan = stg_loans_data.objects.create(
            fic_mis_date=date(2026, 4, 19),
            v_loan_id="LN001",
            v_cust_ref_code="CUST001",
            v_cust_name="Alpha Trading",
            v_prod_name="Working Capital",
            v_branch_code="CHEGUTU",
            v_branch_name="Chegutu",
            v_ccy_code="USD",
        )

    def _workspace_state(self):
        request = self.factory.get("/")
        request.user = self.user
        request.session = {}
        return _workspace_shell_context(request)

    def _build_excel_upload(self, headers, row, filename):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(headers)
        sheet.append(row)
        buffer = BytesIO()
        workbook.save(buffer)
        buffer.seek(0)
        return SimpleUploadedFile(
            filename,
            buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_collateral_template_download_returns_excel_workbook(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("scorecard:ifrs9_supporting_data_collateral_template"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn("ifrs9_collateral_upload_template.xlsx", response["Content-Disposition"])

    def test_payment_schedule_template_download_returns_excel_workbook(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("scorecard:ifrs9_supporting_data_payment_schedules_template"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn("ifrs9_payment_schedule_upload_template.xlsx", response["Content-Disposition"])

    def test_collateral_upload_processor_creates_branch_record(self):
        upload = self._build_excel_upload(
            ["loan_id", "reporting_date", "collateral_value", "collateral_type", "currency_code"],
            ["LN001", "2026-04-20", "250000", "PROPERTY", "USD"],
            "collateral.xlsx",
        )

        created_count, updated_count = _process_collateral_upload(self._workspace_state(), upload)

        self.assertEqual(created_count, 1)
        self.assertEqual(updated_count, 0)
        collateral = stg_collateral_data.objects.get(v_cust_ref_code="CUST001", fic_mis_date=date(2026, 4, 20))
        self.assertEqual(collateral.v_branch_code, "CHEGUTU")
        self.assertEqual(collateral.collateral_type, "PROPERTY")

    def test_payment_schedule_upload_processor_creates_branch_row(self):
        upload = self._build_excel_upload(
            [
                "loan_id",
                "reporting_date",
                "cash_flow_date",
                "total_cash_flow_amount",
                "currency_code",
            ],
            ["LN001", "2026-04-20", "2026-05-31", "10500", "USD"],
            "payment_schedule.xlsx",
        )

        created_count, updated_count = _process_payment_schedule_upload(self._workspace_state(), upload)

        self.assertEqual(created_count, 1)
        self.assertEqual(updated_count, 0)
        schedule = stg_payment_schedule.objects.get(
            v_loan_id="LN001",
            fic_mis_date=date(2026, 4, 20),
            d_cash_flow_date=date(2026, 5, 31),
        )
        self.assertEqual(schedule.n_cash_flow_amount, Decimal("10500"))
        self.assertEqual(schedule.v_ccy_code, "USD")


class ScoreAutoRefreshCursorSafetyTests(SimpleTestCase):
    def test_auto_refresh_does_not_stream_score_querysets_while_writing(self):
        import inspect

        from scorecard.functions_view import score_auto_refresh_engine

        source = inspect.getsource(score_auto_refresh_engine.run_autofilled_score_auto_update)

        self.assertNotIn(".iterator(", source)

    def test_auto_refresh_updates_previously_overridden_auto_field(self):
        from scorecard.functions_view.score_auto_refresh_engine import _changed_autofill_values

        attribute = SimpleNamespace(
            id=101,
            label="Loan Amount",
            options=[
                SimpleNamespace(id=1, label="Old loan band", value="old"),
                SimpleNamespace(id=2, label="Latest loan band", value="latest"),
            ],
        )

        updated_values, changed_fields = _changed_autofill_values(
            current_values={101: "1"},
            suggested_values={101: "2"},
            applied_attribute_ids=set(),
            overridden_attribute_ids={101},
            tracked_attribute_ids={101},
            attributes_by_id={101: attribute},
        )

        self.assertEqual(updated_values[101], "2")
        self.assertEqual(changed_fields, ["Loan Amount: Old loan band -> Latest loan band"])

    def test_candidate_window_advances_resume_cursor(self):
        from scorecard.functions_view.score_auto_refresh_engine import _candidate_window

        class FakeQuerySet:
            def __init__(self, ids):
                self.ids = list(ids)

            def filter(self, **kwargs):
                if "pk__gt" in kwargs:
                    return FakeQuerySet([item for item in self.ids if item > kwargs["pk__gt"]])
                return self

            def __getitem__(self, value):
                return FakeQuerySet(self.ids[value])

            def values_list(self, *_args, **_kwargs):
                return list(self.ids)

            def exists(self):
                return bool(self.ids)

        ids, cursor_id, cycle_complete = _candidate_window(
            FakeQuerySet([1, 2, 3, 4]),
            after_id=0,
            max_checked=2,
        )

        self.assertEqual(ids, [1, 2])
        self.assertEqual(cursor_id, 2)
        self.assertFalse(cycle_complete)

    def test_candidate_window_resets_cursor_when_cycle_completes(self):
        from scorecard.functions_view.score_auto_refresh_engine import _candidate_window

        class FakeQuerySet:
            def __init__(self, ids):
                self.ids = list(ids)

            def filter(self, **kwargs):
                if "pk__gt" in kwargs:
                    return FakeQuerySet([item for item in self.ids if item > kwargs["pk__gt"]])
                return self

            def __getitem__(self, value):
                return FakeQuerySet(self.ids[value])

            def values_list(self, *_args, **_kwargs):
                return list(self.ids)

            def exists(self):
                return bool(self.ids)

        ids, cursor_id, cycle_complete = _candidate_window(
            FakeQuerySet([1, 2, 3, 4]),
            after_id=2,
            max_checked=2,
        )

        self.assertEqual(ids, [3, 4])
        self.assertEqual(cursor_id, 0)
        self.assertTrue(cycle_complete)

    def test_candidate_window_restarts_when_saved_cursor_is_stale(self):
        from scorecard.functions_view.score_auto_refresh_engine import _candidate_window

        class FakeQuerySet:
            def __init__(self, ids):
                self.ids = list(ids)

            def filter(self, **kwargs):
                if "pk__gt" in kwargs:
                    return FakeQuerySet([item for item in self.ids if item > kwargs["pk__gt"]])
                return self

            def __getitem__(self, value):
                return FakeQuerySet(self.ids[value])

            def values_list(self, *_args, **_kwargs):
                return list(self.ids)

            def exists(self):
                return bool(self.ids)

        ids, cursor_id, cycle_complete = _candidate_window(
            FakeQuerySet([1, 2, 3, 4]),
            after_id=99,
            max_checked=2,
        )

        self.assertEqual(ids, [1, 2])
        self.assertEqual(cursor_id, 2)
        self.assertFalse(cycle_complete)

    def test_scheduler_passes_batch_and_cursor_values_to_refresh_engine(self):
        from datetime import datetime

        from django.utils import timezone

        from scorecard.functions_view.score_auto_refresh import _call_engine

        current_tz = timezone.get_current_timezone()
        now = timezone.make_aware(datetime(2026, 9, 3, 14, 30), current_tz)

        def engine(now=None, batch_size=None, basel_after_id=None, ifrs9_after_id=None):
            return {
                "now": now,
                "batch_size": batch_size,
                "basel_after_id": basel_after_id,
                "ifrs9_after_id": ifrs9_after_id,
            }

        result = _call_engine(
            engine,
            now,
            {
                "batch_size": 500,
                "basel_cursor_id": 123,
                "ifrs9_cursor_id": 456,
            },
        )

        self.assertEqual(result["now"], now)
        self.assertEqual(result["batch_size"], 500)
        self.assertEqual(result["basel_after_id"], 123)
        self.assertEqual(result["ifrs9_after_id"], 456)


class ScoreAutoRefreshScheduleSlotTests(SimpleTestCase):
    def test_daily_refresh_allows_new_time_slot_on_same_day(self):
        from datetime import datetime, time

        from django.utils import timezone

        from scorecard.functions_view.score_auto_refresh import _auto_refresh_due_reason

        current_tz = timezone.get_current_timezone()
        settings_obj = SimpleNamespace(
            auto_refresh_autofilled_scores_frequency="daily",
            auto_refresh_autofilled_scores_time=time(14, 7),
            auto_refresh_autofilled_scores_last_run_at=timezone.make_aware(
                datetime(2026, 9, 3, 13, 49),
                current_tz,
            ),
        )
        now = timezone.make_aware(datetime(2026, 9, 3, 14, 8), current_tz)

        self.assertEqual(_auto_refresh_due_reason(settings_obj, now), "")

    def test_daily_refresh_blocks_same_time_slot_on_same_day(self):
        from datetime import datetime, time

        from django.utils import timezone

        from scorecard.functions_view.score_auto_refresh import _auto_refresh_due_reason

        current_tz = timezone.get_current_timezone()
        settings_obj = SimpleNamespace(
            auto_refresh_autofilled_scores_frequency="daily",
            auto_refresh_autofilled_scores_time=time(14, 7),
            auto_refresh_autofilled_scores_last_run_at=timezone.make_aware(
                datetime(2026, 9, 3, 14, 7),
                current_tz,
            ),
        )
        now = timezone.make_aware(datetime(2026, 9, 3, 14, 8), current_tz)

        self.assertEqual(_auto_refresh_due_reason(settings_obj, now), "already_ran_today")
