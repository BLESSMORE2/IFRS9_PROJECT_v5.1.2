from datetime import date
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.test import SimpleTestCase
from django.test import TestCase
from django.urls import reverse
from django.template.loader import get_template

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
from scorecard.functions_view.historical_scores import (
    _active_exposure_sources_for_reporting_date,
    _historical_api_import_readiness,
    _historical_date_has_all_seed_rows,
    HistoricalScoreCaptureResult,
    build_historical_score_seed_rows,
    capture_historical_scores,
    run_due_historical_score_capture,
)
from scorecard.functions_view.customers import (
    _parse_overdraft_upload_rows_from_values,
    build_without_score_customer_snapshot_for_branch_scope,
)
from scorecard.context_processors import _build_scorecard_route_access
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
    MainCustomer,
    ManualOverdraftCustomer,
    IFRS9Evaluation,
    IFRS9ScoreSheetTemplate,
    ScorecardWorkflowApprovalSetting,
    ScorecardUserBranchAccess,
)
from scorecard.workflow_approval import (
    can_user_self_review_score_submission,
    get_scorecard_workflow_approval_settings,
)
from scorecard.permission_catalog import (
    DEFAULT_ROLE_DEFINITIONS,
    ROUTE_PERMISSION_MAP,
    SCORECARD_PERMISSION_DEFINITIONS,
)
from scorecard.urls import _route_permission_required


class ValidationReportPermissionTests(SimpleTestCase):
    @staticmethod
    def _request_with_permissions(*permissions):
        granted = set(permissions)
        request = RequestFactory().get("/scorecard/reports/")
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            has_perm=lambda permission: permission in granted,
        )
        return request

    def test_validation_routes_have_independent_permissions(self):
        self.assertEqual(
            ROUTE_PERMISSION_MAP["ifrs9_results_basel_validations"],
            "scorecard.view_basel_validation_reports",
        )
        self.assertEqual(
            ROUTE_PERMISSION_MAP["ifrs9_results_basel_validations_download"],
            "scorecard.view_basel_validation_reports",
        )
        self.assertEqual(
            ROUTE_PERMISSION_MAP["ifrs9_results_ifrs9_validations"],
            "scorecard.view_ifrs9_validation_reports",
        )
        self.assertEqual(
            ROUTE_PERMISSION_MAP["ifrs9_results_ifrs9_validations_download"],
            "scorecard.view_ifrs9_validation_reports",
        )
        self.assertEqual(
            ROUTE_PERMISSION_MAP["ifrs9_results_home"],
            "scorecard.view_ifrs9_results",
        )

    def test_validation_routes_use_reports_urls(self):
        self.assertEqual(
            reverse("scorecard:ifrs9_results_basel_validations"),
            "/scorecard/reports/npl-migration/",
        )
        self.assertEqual(
            reverse("scorecard:ifrs9_results_ifrs9_validations"),
            "/scorecard/reports/ifrs9-scores-validation/",
        )

    def test_reports_sidebar_is_independent_from_ifrs9_results_access(self):
        request = RequestFactory().get("/scorecard/reports/npl-migration/")
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            has_perm=lambda permission: permission == "scorecard.view_basel_validation_reports",
        )

        route_access, nav_visibility = _build_scorecard_route_access(request)

        self.assertTrue(nav_visibility["reports"])
        self.assertFalse(nav_visibility["ifrs9_results"])
        self.assertTrue(route_access["ifrs9_results_basel_validations"])
        self.assertFalse(route_access["ifrs9_results_ifrs9_validations"])

    def test_validation_roles_are_independent(self):
        roles = {item["name"]: item for item in DEFAULT_ROLE_DEFINITIONS}
        self.assertEqual(
            roles["NPL Migration Report Viewer"]["permissions"],
            ["scorecard.view_basel_validation_reports"],
        )
        self.assertEqual(
            roles["IFRS9 Scores Validation Viewer"]["permissions"],
            ["scorecard.view_ifrs9_validation_reports"],
        )
        self.assertEqual(
            roles["IFRS9 Results Viewer"]["permissions"],
            ["scorecard.view_ifrs9_results"],
        )

    def test_validation_permission_labels_use_report_names(self):
        definitions = {
            item["codename"]: item
            for item in SCORECARD_PERMISSION_DEFINITIONS
        }
        self.assertEqual(
            definitions["view_basel_validation_reports"]["label"],
            "Can view NPL migration report",
        )
        self.assertEqual(
            definitions["view_ifrs9_validation_reports"]["label"],
            "Can view IFRS9 scores validation",
        )

    def test_report_route_guards_allow_only_matching_permission(self):
        npl_guard = _route_permission_required(
            "scorecard.view_basel_validation_reports"
        )(lambda request: "npl-access-granted")
        ifrs9_guard = _route_permission_required(
            "scorecard.view_ifrs9_validation_reports"
        )(lambda request: "ifrs9-access-granted")

        npl_request = self._request_with_permissions(
            "scorecard.view_basel_validation_reports"
        )
        ifrs9_request = self._request_with_permissions(
            "scorecard.view_ifrs9_validation_reports"
        )
        no_permission_request = self._request_with_permissions()

        self.assertEqual(npl_guard(npl_request), "npl-access-granted")
        self.assertEqual(ifrs9_guard(ifrs9_request), "ifrs9-access-granted")
        with self.assertRaises(PermissionDenied):
            npl_guard(ifrs9_request)
        with self.assertRaises(PermissionDenied):
            ifrs9_guard(npl_request)
        with self.assertRaises(PermissionDenied):
            npl_guard(no_permission_request)
        with self.assertRaises(PermissionDenied):
            ifrs9_guard(no_permission_request)

    def test_ifrs9_report_permission_controls_reports_sidebar_independently(self):
        request = self._request_with_permissions(
            "scorecard.view_ifrs9_validation_reports"
        )

        route_access, nav_visibility = _build_scorecard_route_access(request)

        self.assertTrue(nav_visibility["reports"])
        self.assertFalse(nav_visibility["ifrs9_results"])
        self.assertFalse(route_access["ifrs9_results_basel_validations"])
        self.assertTrue(route_access["ifrs9_results_ifrs9_validations"])

    def test_ifrs9_results_navigation_excludes_validation_links(self):
        for template_name in (
            "credit_scoreshifts/ifrs9_results_home.html",
            "credit_scoreshifts/ifrs9_results_extract.html",
            "credit_scoreshifts/ifrs9_results_ecl_summary.html",
        ):
            source = get_template(template_name).template.source
            self.assertNotIn("ifrs9_results_basel_validations", source)
            self.assertNotIn("ifrs9_results_ifrs9_validations", source)


class ValidationReportPerformanceTests(SimpleTestCase):
    class _HistoricalRowsQuerySet:
        def __init__(self, rows):
            self.rows = rows
            self.filter_calls = []
            self.values_fields = ()
            self.ordering = ()

        def filter(self, **kwargs):
            self.filter_calls.append(kwargs)
            return self

        def values(self, *fields):
            self.values_fields = fields
            return self

        def order_by(self, *fields):
            self.ordering = fields
            return [dict(row) for row in self.rows]

    def test_basel_comparison_loads_both_dates_in_one_index_ordered_query(self):
        from scorecard.functions_view.basel_validations import _historical_values_for_dates

        previous_date = date(2026, 2, 28)
        current_date = date(2026, 4, 30)
        queryset = self._HistoricalRowsQuerySet(
            [
                {
                    "reporting_date": previous_date,
                    "branch_name": "BINDURA",
                    "customer_id": "1001",
                    "customer_name": "Previous Customer",
                    "basel_ii_score": Decimal("60"),
                    "basel_ii_grade": "B1",
                    "basel_override_grade": "",
                    "ifrs_9_score": Decimal("30"),
                    "has_active_loan": True,
                    "has_active_overdraft": False,
                },
                {
                    "reporting_date": current_date,
                    "branch_name": "BINDURA",
                    "customer_id": "1001",
                    "customer_name": "Current Customer",
                    "basel_ii_score": Decimal("62"),
                    "basel_ii_grade": "B1",
                    "basel_override_grade": "",
                    "ifrs_9_score": Decimal("31"),
                    "has_active_loan": True,
                    "has_active_overdraft": False,
                },
            ]
        )

        previous_rows, current_rows = _historical_values_for_dates(
            queryset,
            previous_date,
            current_date,
        )

        self.assertEqual(len(queryset.filter_calls), 1)
        self.assertEqual(
            queryset.filter_calls[0],
            {"reporting_date__in": (previous_date, current_date)},
        )
        self.assertEqual(
            queryset.ordering,
            ("reporting_date", "branch_name", "customer_id"),
        )
        self.assertEqual(previous_rows[0]["customer_name"], "Previous Customer")
        self.assertEqual(current_rows[0]["customer_name"], "Current Customer")

    def test_ifrs9_comparison_loads_both_dates_in_one_index_ordered_query(self):
        from scorecard.functions_view.ifrs9_validations import _historical_values_for_dates

        previous_date = date(2026, 2, 28)
        current_date = date(2026, 4, 30)
        queryset = self._HistoricalRowsQuerySet(
            [
                {
                    "reporting_date": previous_date,
                    "branch_name": "BINGA",
                    "customer_id": "2001",
                    "customer_name": "Previous IFRS9 Customer",
                    "ifrs_9_score": Decimal("25"),
                },
                {
                    "reporting_date": current_date,
                    "branch_name": "BINGA",
                    "customer_id": "2001",
                    "customer_name": "Current IFRS9 Customer",
                    "ifrs_9_score": Decimal("28"),
                },
            ]
        )

        previous_rows, current_rows = _historical_values_for_dates(
            queryset,
            previous_date,
            current_date,
        )

        self.assertEqual(len(queryset.filter_calls), 1)
        self.assertEqual(
            queryset.ordering,
            ("reporting_date", "branch_name", "customer_id"),
        )
        self.assertEqual(previous_rows[0]["customer_name"], "Previous IFRS9 Customer")
        self.assertEqual(current_rows[0]["customer_name"], "Current IFRS9 Customer")

    def test_repeated_basel_comparison_reuses_cached_payload(self):
        from scorecard.functions_view import basel_validations

        cache_key = "validation-payload:test:basel-reuse"
        cache.delete(cache_key)
        payload = {"summary": {"matched_customers": 1}, "details": []}

        with (
            patch.object(basel_validations, "validation_payload_cache_key", return_value=cache_key),
            patch.object(basel_validations, "_build_validation_payload", return_value=payload) as build_payload,
        ):
            first = basel_validations._get_validation_payload(
                object(),
                date(2026, 2, 28),
                date(2026, 4, 30),
                ["BINDURA"],
                "",
            )
            second = basel_validations._get_validation_payload(
                object(),
                date(2026, 2, 28),
                date(2026, 4, 30),
                ["BINDURA"],
                "",
            )

        self.assertEqual(first, payload)
        self.assertEqual(second, payload)
        build_payload.assert_called_once()
        cache.delete(cache_key)

    def test_payload_cache_key_changes_when_historical_data_changes(self):
        from scorecard.functions_view.validation_export_cache import validation_payload_cache_key

        class FingerprintQuerySet:
            def __init__(self, row_count):
                self.row_count = row_count

            def filter(self, **kwargs):
                return self

            def aggregate(self, **kwargs):
                return {"row_count": self.row_count, "latest_update": None}

        arguments = {
            "report_name": "basel",
            "previous_date": date(2026, 2, 28),
            "current_date": date(2026, 4, 30),
            "branch_names": ["BINDURA", "BINGA"],
            "selected_branch": "",
        }
        original_key = validation_payload_cache_key(
            queryset=FingerprintQuerySet(100),
            **arguments,
        )
        changed_key = validation_payload_cache_key(
            queryset=FingerprintQuerySet(101),
            **arguments,
        )

        self.assertNotEqual(original_key, changed_key)

    def test_validation_apply_updates_results_without_page_navigation(self):
        templates = (
            (
                "credit_scoreshifts/basel_validations.html",
                "validation-filter-form",
                "validation-report-content",
            ),
            (
                "credit_scoreshifts/ifrs9_validations.html",
                "iv-filter-form",
                "iv-report-content",
            ),
        )

        for template_name, form_id, content_id in templates:
            source = get_template(template_name).template.source
            self.assertIn(f'id="{form_id}"', source)
            self.assertIn(f'id="{content_id}"', source)
            self.assertIn("filterForm.addEventListener('submit'", source)
            self.assertIn("event.preventDefault();", source)
            self.assertIn("'X-Requested-With': 'XMLHttpRequest'", source)
            self.assertIn("reportContent.innerHTML = incomingContent.innerHTML", source)
            self.assertIn("window.history.replaceState", source)

    def test_basel_grade_population_keeps_previous_and_current_counts_separate(self):
        from scorecard.functions_view import basel_validations

        previous_date = date(2026, 2, 28)
        current_date = date(2026, 4, 30)

        def historical_row(customer_id, grade, score):
            return {
                "branch_name": "BINDURA",
                "customer_id": customer_id,
                "customer_name": f"Customer {customer_id}",
                "basel_ii_score": Decimal(score),
                "basel_ii_grade": grade,
                "basel_override_grade": "",
                "ifrs_9_score": None,
                "has_active_loan": True,
                "has_active_overdraft": False,
            }

        previous_rows = [
            historical_row("1001", "A1", "80"),
            historical_row("1002", "B1", "65"),
        ]
        current_rows = [
            historical_row("1001", "A1", "81"),
            historical_row("1002", "B2", "60"),
            historical_row("1003", "C", "35"),
        ]

        with patch.object(
            basel_validations,
            "_historical_values_for_dates",
            return_value=(previous_rows, current_rows),
        ):
            payload = basel_validations._build_validation_payload(
                object(),
                previous_date,
                current_date,
            )

        self.assertEqual(
            payload["grade_distribution_totals"],
            {"previous_count": 2, "current_count": 3, "change": 1},
        )
        distribution = {row["grade"]: row for row in payload["grade_distribution"]}
        self.assertEqual(distribution["B1"]["previous_count"], 1)
        self.assertEqual(distribution["B1"]["current_count"], 0)
        self.assertEqual(distribution["B2"]["previous_count"], 0)
        self.assertEqual(distribution["B2"]["current_count"], 1)

        template_source = get_template(
            "credit_scoreshifts/basel_validations.html"
        ).template.source
        self.assertIn("Grade Population by Date", template_source)
        self.assertIn("Previous Population", template_source)
        self.assertIn("Current Population", template_source)
        self.assertIn("Matched-Customer Grade Migration Matrix", template_source)

    def test_ifrs9_score_population_keeps_previous_and_current_counts_separate(self):
        from scorecard.functions_view import ifrs9_validations

        previous_date = date(2026, 2, 28)
        current_date = date(2026, 4, 30)

        def historical_row(customer_id, score):
            return {
                "branch_name": "BINDURA",
                "customer_id": customer_id,
                "customer_name": f"Customer {customer_id}",
                "ifrs_9_score": Decimal(score),
            }

        previous_rows = [
            historical_row("1001", "10"),
            historical_row("1002", "30"),
        ]
        current_rows = [
            historical_row("1001", "15"),
            historical_row("1002", "50"),
            historical_row("1003", "85"),
        ]

        with patch.object(
            ifrs9_validations,
            "_historical_values_for_dates",
            return_value=(previous_rows, current_rows),
        ):
            payload = ifrs9_validations._build_validation_payload(
                object(),
                previous_date,
                current_date,
            )

        self.assertEqual(
            payload["score_distribution_totals"],
            {"previous_count": 2, "current_count": 3, "change": 1},
        )
        distribution = {row["band"]: row for row in payload["score_distribution"]}
        self.assertEqual(distribution["0% - 20%"]["previous_count"], 1)
        self.assertEqual(distribution["0% - 20%"]["current_count"], 1)
        self.assertEqual(distribution[">20% - 40%"]["previous_count"], 1)
        self.assertEqual(distribution[">20% - 40%"]["current_count"], 0)

        workbook = ifrs9_validations._build_validation_workbook(
            payload,
            payload["details"],
            "ALL ASSIGNED BRANCHES",
            "",
            "",
            "",
        )
        population_sheet = workbook["Score Population"]
        self.assertEqual(
            population_sheet["B4"].value,
            f"Previous Population ({previous_date})",
        )
        self.assertEqual(
            population_sheet["C4"].value,
            f"Current Population ({current_date})",
        )
        self.assertEqual(
            [cell.value for cell in population_sheet[population_sheet.max_row]],
            ["Total score population", 2, 3, 1, None, None, None, None],
        )

        template_source = get_template(
            "credit_scoreshifts/ifrs9_validations.html"
        ).template.source
        self.assertIn("Score Population by Date", template_source)
        self.assertIn("Previous Population", template_source)
        self.assertIn("Current Population", template_source)
        self.assertIn("matched customers only", template_source)


class ManualOverdraftCustomerUploadTests(SimpleTestCase):
    def test_upload_parser_accepts_customer_id_and_skips_incomplete_rows(self):
        rows, skipped, errors = _parse_overdraft_upload_rows_from_values(
            [
                ["Branch Name", "Customer ID", "Customer Name", "Account Number"],
                ["8TH AVENUE", "1001", "ABC TEST", "OD123"],
                ["8TH AVENUE", "", "MISSING CODE", ""],
            ]
        )

        self.assertEqual(
            rows,
            [
                {
                    "branch_name": "8TH AVENUE",
                    "customer_code": "1001",
                    "customer_name": "ABC TEST",
                    "account_number": "OD123",
                }
            ],
        )
        self.assertEqual(skipped, 1)
        self.assertEqual(errors, ["Row 3: Branch Name, Customer Code, and Customer Name are required."])

    def test_upload_parser_accepts_branch_name_column_without_branch_code(self):
        rows, skipped, errors = _parse_overdraft_upload_rows_from_values(
            [
                ["Branch Name", "Customer Code", "Customer Name", "Account Number"],
                ["8TH AVENUE", "1002", "BRANCH CUSTOMER", "OD456"],
            ]
        )

        self.assertEqual(
            rows,
            [
                {
                    "branch_name": "8TH AVENUE",
                    "customer_code": "1002",
                    "customer_name": "BRANCH CUSTOMER",
                    "account_number": "OD456",
                }
            ],
        )
        self.assertEqual(skipped, 0)
        self.assertEqual(errors, [])


class ManualOverdraftWithoutScoreTests(TestCase):
    def setUp(self):
        cache.clear()
        self.branch = BankBranch.objects.create(
            branch_code="OD001",
            branch_name="OD TEST BRANCH",
            bank_name="AFC",
        )
        MainCustomer.objects.create(
            reporting_date=date(2026, 9, 30),
            customer_ref_code="ODCUST001",
            customer_name="OD CUSTOMER ONE",
            branch_code=self.branch.branch_code,
            branch_name=self.branch.branch_name,
            branch_description=self.branch.branch_name,
            has_overdraft=True,
            overdraft_count=1,
            is_active_for_scoring=True,
        )
        ManualOverdraftCustomer.objects.create(
            customer_code="ODCUST001",
            customer_name="OD CUSTOMER ONE",
            branch_code=self.branch.branch_code,
            branch_name=self.branch.branch_name,
            account_number="ODACC001",
        )

    def test_manual_overdraft_customers_are_included_when_api_sources_are_disabled(self):
        snapshot = build_without_score_customer_snapshot_for_branch_scope(
            [self.branch],
            {"include_loans": False, "include_overdrafts": False},
        )

        self.assertEqual(snapshot["total_stage_rows"], 1)
        self.assertEqual([row["customer_ref_code"] for row in snapshot["basel_rows"]], ["ODCUST001"])
        self.assertEqual([row["customer_ref_code"] for row in snapshot["ifrs9_rows"]], ["ODCUST001"])
        self.assertTrue(snapshot["basel_rows"][0]["has_overdraft"])
        self.assertEqual(snapshot["basel_rows"][0]["primary_account_number"], "ODACC001")



class HistoricalScoreCaptureDateTests(SimpleTestCase):
    @patch("scorecard.functions_view.historical_scores.HistoricalScore")
    def test_manual_refresh_can_preserve_non_month_end_reporting_date(self, historical_score):
        historical_score.objects.update_or_create.return_value = (SimpleNamespace(), True)
        selected_date = date(2026, 6, 28)
        seed_rows = [
            {
                "branch_name": "8TH AVENUE",
                "customer_name": "TEST CUSTOMER",
                "customer_id": "TEST001",
                "basel_ii_score": Decimal("60.00"),
                "basel_ii_grade": "B1",
                "basel_override_grade": "",
                "ifrs_9_score": None,
                "has_active_loan": True,
                "has_active_overdraft": False,
            }
        ]

        result = capture_historical_scores(
            selected_date,
            seed_rows=seed_rows,
            coverage_totals={"basel_only": 1},
            preserve_reporting_date=True,
        )

        self.assertEqual(result.reporting_date, selected_date)
        self.assertEqual(
            historical_score.objects.update_or_create.call_args.kwargs["reporting_date"],
            selected_date,
        )


class ManualOverdraftHistoricalExposureTests(SimpleTestCase):
    @patch("scorecard.functions_view.historical_scores.ManualOverdraftCustomer")
    @patch("scorecard.functions_view.historical_scores.CustomerOverdraft")
    @patch("scorecard.functions_view.historical_scores.CustomerLoan")
    def test_manual_overdraft_customer_is_included_after_api_month_end_is_available(
        self,
        customer_loan,
        customer_overdraft,
        manual_overdraft_customer,
    ):
        customer_loan.objects.filter.return_value.exclude.return_value.exclude.return_value.values_list.return_value.distinct.return_value = [
            ("APILOAN001", "API BRANCH")
        ]
        customer_overdraft.objects.filter.return_value.exclude.return_value.exclude.return_value.values_list.return_value.distinct.return_value = []
        manual_overdraft_customer.objects.exclude.return_value.values_list.return_value.distinct.return_value = [
            ("ODCUST001", "OD TEST BRANCH")
        ]

        exposure_sources = _active_exposure_sources_for_reporting_date(date(2025, 1, 31))

        customer_flags = exposure_sources["by_customer"]["odcust001"]
        branch_flags = exposure_sources["by_customer_branch"][("odcust001", "od test branch")]
        self.assertFalse(customer_flags["loan"])
        self.assertTrue(customer_flags["overdraft"])
        self.assertFalse(branch_flags["loan"])
        self.assertTrue(branch_flags["overdraft"])

    @patch("scorecard.functions_view.historical_scores.ManualOverdraftCustomer")
    @patch("scorecard.functions_view.historical_scores.CustomerOverdraft")
    @patch("scorecard.functions_view.historical_scores.CustomerLoan")
    def test_manual_overdraft_customer_waits_when_api_month_end_is_unavailable(
        self,
        customer_loan,
        customer_overdraft,
        manual_overdraft_customer,
    ):
        customer_loan.objects.filter.return_value.exclude.return_value.exclude.return_value.values_list.return_value.distinct.return_value = []
        customer_overdraft.objects.filter.return_value.exclude.return_value.exclude.return_value.values_list.return_value.distinct.return_value = []

        exposure_sources = _active_exposure_sources_for_reporting_date(date(2025, 1, 31))

        self.assertEqual(exposure_sources["by_customer"], {})
        self.assertEqual(exposure_sources["by_customer_branch"], {})
        manual_overdraft_customer.objects.exclude.assert_not_called()

    @patch("scorecard.functions_view.historical_scores.IFRS9Evaluation")
    @patch("scorecard.functions_view.historical_scores.CreditEvaluation")
    @patch("scorecard.functions_view.historical_scores._active_exposure_sources_for_reporting_date")
    def test_manual_overdraft_exposure_builds_one_combined_historical_score_row_after_api_ready(
        self,
        active_exposure_sources,
        credit_evaluation,
        ifrs9_evaluation,
    ):
        active_exposure_sources.return_value = {
            "by_customer": {"odcust001": {"loan": False, "overdraft": True}},
            "by_customer_branch": {
                ("odcust001", "manual branch"): {"loan": False, "overdraft": True}
            },
        }
        credit_evaluation.objects.filter.return_value.order_by.return_value = [
            SimpleNamespace(
                branch_name="SCORING BRANCH",
                customer_id="ODCUST001",
                customer_name="OD CUSTOMER ONE",
                status="approved",
                approved_weighted_percent=None,
                total_weighted_percent=Decimal("61.25"),
                final_grade="B2",
                override_grade="",
            )
        ]
        ifrs9_evaluation.objects.filter.return_value.order_by.return_value = [
            SimpleNamespace(
                branch_name="SCORING BRANCH",
                customer_id="ODCUST001",
                customer_name="OD CUSTOMER ONE",
                status="approved",
                approved_weighted_percent=None,
                total_weighted_percent=Decimal("34.50"),
            )
        ]

        rows, coverage = build_historical_score_seed_rows(date(2025, 1, 31))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["branch_name"], "SCORING BRANCH")
        self.assertEqual(rows[0]["customer_id"], "ODCUST001")
        self.assertEqual(rows[0]["basel_ii_score"], Decimal("61.25"))
        self.assertEqual(rows[0]["ifrs_9_score"], Decimal("34.50"))
        self.assertFalse(rows[0]["has_active_loan"])
        self.assertTrue(rows[0]["has_active_overdraft"])
        self.assertEqual(coverage, {"both": 1})

    @patch("scorecard.functions_view.historical_scores.HistoricalScore")
    def test_late_api_exposure_flag_marks_existing_manual_snapshot_incomplete(self, historical_score):
        existing_rows = historical_score.objects.filter.return_value
        existing_rows.count.return_value = 1
        existing_rows.values_list.return_value = [
            ("SCORING BRANCH", "ODCUST001", False, True)
        ]
        seed_rows = [
            {
                "branch_name": "SCORING BRANCH",
                "customer_id": "ODCUST001",
                "has_active_loan": True,
                "has_active_overdraft": True,
            }
        ]

        is_complete = _historical_date_has_all_seed_rows(date(2026, 8, 31), seed_rows)

        self.assertFalse(is_complete)

    @patch("scorecard.functions_view.historical_scores.ApiImportRun.objects")
    def test_historical_import_gate_accepts_completed_successful_source(self, import_runs):
        from django.utils import timezone

        completed_at = timezone.now()
        wrong_date_run = SimpleNamespace(
            parameters_used={"reporting_date": "2026-08-30"},
            endpoint=SimpleNamespace(target_table="customer_loan"),
            status="success",
            fetched=999,
            completed_at=completed_at,
        )
        completed_run = SimpleNamespace(
            parameters_used={"reporting_date": "2026-08-31"},
            endpoint=SimpleNamespace(target_table="customer_loan"),
            status="success",
            fetched=100,
            completed_at=completed_at,
        )
        import_runs.select_related.return_value.filter.return_value.order_by.return_value = [
            wrong_date_run,
            completed_run,
        ]

        readiness = _historical_api_import_readiness(date(2026, 8, 31))

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["successful_sources_with_data"], ["customer_loan"])

    @patch("scorecard.functions_view.historical_scores.ApiImportRun.objects")
    def test_historical_import_gate_blocks_while_another_source_is_running(self, import_runs):
        from django.utils import timezone

        completed_at = timezone.now()
        completed_loan = SimpleNamespace(
            parameters_used={"reporting_date": "2026-08-31"},
            endpoint=SimpleNamespace(target_table="customer_loan"),
            status="success",
            fetched=100,
            completed_at=completed_at,
        )
        running_overdraft = SimpleNamespace(
            parameters_used={"reporting_date": "2026-08-31"},
            endpoint=SimpleNamespace(target_table="customer_overdraft"),
            status="running",
            fetched=0,
            completed_at=None,
        )
        import_runs.select_related.return_value.filter.return_value.order_by.return_value = [
            running_overdraft,
            completed_loan,
        ]

        readiness = _historical_api_import_readiness(date(2026, 8, 31))

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["incomplete_targets"], ["customer_overdraft"])

    @patch("scorecard.functions_view.historical_scores.capture_historical_scores")
    @patch("scorecard.functions_view.historical_scores._historical_date_has_all_seed_rows")
    @patch("scorecard.functions_view.historical_scores.build_historical_score_seed_rows")
    @patch("scorecard.functions_view.historical_scores._historical_api_import_readiness")
    def test_scheduler_rechecks_only_latest_due_month_for_late_api_data(
        self,
        import_readiness,
        build_seed_rows,
        historical_date_complete,
        capture_scores,
    ):
        from datetime import datetime

        from django.utils import timezone

        august_month_end = date(2026, 8, 31)
        seed_rows = [
            {
                "branch_name": "SCORING BRANCH",
                "customer_id": "LATE001",
                "has_active_loan": True,
                "has_active_overdraft": False,
            }
        ]
        coverage = {"basel_only": 1}
        import_readiness.return_value = {"ready": True}
        build_seed_rows.return_value = (seed_rows, coverage)
        historical_date_complete.return_value = False
        capture_scores.return_value = HistoricalScoreCaptureResult(
            reporting_date=august_month_end,
            created=1,
            updated=0,
            both=0,
            basel_only=1,
            ifrs9_only=0,
        )
        now = timezone.make_aware(
            datetime(2026, 9, 23, 10, 0),
            timezone.get_current_timezone(),
        )

        result = run_due_historical_score_capture(now=now)

        build_seed_rows.assert_called_once_with(august_month_end)
        historical_date_complete.assert_called_once_with(august_month_end, seed_rows)
        capture_scores.assert_called_once_with(
            august_month_end,
            seed_rows=seed_rows,
            coverage_totals=coverage,
        )
        self.assertTrue(result["performed"])
        self.assertEqual(result["captured_reporting_dates"], [august_month_end])
        self.assertEqual(result["created"], 1)

    @patch("scorecard.functions_view.historical_scores.capture_historical_scores")
    @patch("scorecard.functions_view.historical_scores.build_historical_score_seed_rows")
    @patch("scorecard.functions_view.historical_scores._historical_api_import_readiness")
    def test_scheduler_waits_for_latest_month_end_api_data_before_manual_capture(
        self,
        import_readiness,
        build_seed_rows,
        capture_scores,
    ):
        from datetime import datetime

        from django.utils import timezone

        august_month_end = date(2026, 8, 31)
        import_readiness.return_value = {"ready": True}
        build_seed_rows.return_value = ([], {})
        now = timezone.make_aware(
            datetime(2026, 9, 23, 10, 0),
            timezone.get_current_timezone(),
        )

        result = run_due_historical_score_capture(now=now)

        build_seed_rows.assert_called_once_with(august_month_end)
        capture_scores.assert_not_called()
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], "no_active_exposures")
        self.assertEqual(result["checked_reporting_dates"], [august_month_end])

    @patch("scorecard.functions_view.historical_scores.build_historical_score_seed_rows")
    @patch("scorecard.functions_view.historical_scores._historical_api_import_readiness")
    def test_scheduler_does_not_read_exposure_rows_until_import_is_complete(
        self,
        import_readiness,
        build_seed_rows,
    ):
        from datetime import datetime

        from django.utils import timezone

        august_month_end = date(2026, 8, 31)
        import_readiness.return_value = {
            "ready": False,
            "source_statuses": {
                "customer_loan": {"status": "running", "fetched": 0, "completed_at": None}
            },
            "incomplete_targets": ["customer_loan"],
            "successful_sources_with_data": [],
        }
        now = timezone.make_aware(
            datetime(2026, 9, 23, 10, 0),
            timezone.get_current_timezone(),
        )

        result = run_due_historical_score_capture(now=now)

        import_readiness.assert_called_once_with(august_month_end)
        build_seed_rows.assert_not_called()
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], "api_import_incomplete")


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


class NotificationFilterTests(SimpleTestCase):
    def test_auto_score_updates_appears_as_module_filter_choice(self):
        from scorecard.functions_view.notifications import _notification_category_choices

        self.assertIn(
            ("auto_score_updates", "Auto Score Updates"),
            _notification_category_choices(),
        )

    def test_auto_score_updates_filter_maps_to_event_code(self):
        from scorecard.functions_view.notifications import _apply_notification_category_filter

        class FakeQuerySet:
            def __init__(self):
                self.filters = []

            def filter(self, **kwargs):
                self.filters.append(kwargs)
                return self

        queryset = FakeQuerySet()
        result = _apply_notification_category_filter(queryset, "auto_score_updates")

        self.assertIs(result, queryset)
        self.assertEqual(queryset.filters, [{"event_code": "score_auto_update_completed"}])

    def test_auto_score_update_notification_module_label_is_specific(self):
        from scorecard.functions_view.notifications import _notification_module_label
        from scorecard.models import ScorecardNotification

        notification = SimpleNamespace(
            event_code="score_auto_update_completed",
            category=ScorecardNotification.CATEGORY_SCORING,
            metadata={},
        )

        self.assertEqual(_notification_module_label(notification), "Auto Score Updates")


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

    def test_auto_refresh_defers_notification_until_full_cycle_completes(self):
        from datetime import datetime, time

        from django.utils import timezone

        from scorecard.functions_view import score_auto_refresh

        class FakeSettings:
            auto_refresh_autofilled_scores_enabled = True
            auto_refresh_autofilled_scores_frequency = "daily"
            auto_refresh_autofilled_scores_time = time(2, 0)
            auto_refresh_autofilled_scores_weekday = 0
            auto_refresh_autofilled_scores_month_day = 1
            auto_refresh_autofilled_scores_batch_size = 1000
            auto_refresh_autofilled_scores_basel_cursor_id = 0
            auto_refresh_autofilled_scores_ifrs9_cursor_id = 0
            auto_refresh_autofilled_scores_pending_updates = []
            auto_refresh_autofilled_scores_last_run_at = None

            def save(self, update_fields=None):
                self.saved_update_fields = list(update_fields or [])

        settings_obj = FakeSettings()
        calls = {"notify": 0}

        def fake_engine(**_kwargs):
            return {
                "checked_count": 1,
                "updated_items": [
                    {
                        "score_type": "Basel II",
                        "customer_code": "C001",
                        "customer_name": "Customer One",
                        "branch_name": "8TH AVENUE",
                        "template_code": "ARCSC1-17",
                        "changed_fields": ["Repayment History: Old -> New"],
                        "version": 2,
                    }
                ],
                "basel_cursor_id": 10,
                "ifrs9_cursor_id": 0,
                "basel_cycle_complete": False,
                "ifrs9_cycle_complete": True,
            }

        original_get_settings = score_auto_refresh.get_scorecard_workflow_approval_settings
        original_load_engine = score_auto_refresh._load_auto_refresh_engine
        original_notify = score_auto_refresh.notify_auto_update_completed
        try:
            score_auto_refresh.get_scorecard_workflow_approval_settings = lambda: settings_obj
            score_auto_refresh._load_auto_refresh_engine = lambda: fake_engine
            score_auto_refresh.notify_auto_update_completed = lambda *_args, **_kwargs: calls.__setitem__("notify", calls["notify"] + 1)

            now = timezone.make_aware(datetime(2026, 9, 3, 3, 0), timezone.get_current_timezone())
            result = score_auto_refresh.run_due_autofilled_score_refresh(now=now)
        finally:
            score_auto_refresh.get_scorecard_workflow_approval_settings = original_get_settings
            score_auto_refresh._load_auto_refresh_engine = original_load_engine
            score_auto_refresh.notify_auto_update_completed = original_notify

        self.assertFalse(result["cycle_complete"])
        self.assertEqual(result["notification"]["reason"], "cycle_in_progress")
        self.assertEqual(result["cycle_updates_pending"], 1)
        self.assertEqual(calls["notify"], 0)
        self.assertEqual(len(settings_obj.auto_refresh_autofilled_scores_pending_updates), 1)

    def test_auto_refresh_sends_one_notification_when_full_cycle_completes(self):
        from datetime import datetime, time

        from django.utils import timezone

        from scorecard.functions_view import score_auto_refresh

        class FakeSettings:
            auto_refresh_autofilled_scores_enabled = True
            auto_refresh_autofilled_scores_frequency = "daily"
            auto_refresh_autofilled_scores_time = time(2, 0)
            auto_refresh_autofilled_scores_weekday = 0
            auto_refresh_autofilled_scores_month_day = 1
            auto_refresh_autofilled_scores_batch_size = 1000
            auto_refresh_autofilled_scores_basel_cursor_id = 10
            auto_refresh_autofilled_scores_ifrs9_cursor_id = 0
            auto_refresh_autofilled_scores_pending_updates = [
                {
                    "score_type": "Basel II",
                    "customer_code": "C001",
                    "customer_name": "Customer One",
                    "branch_name": "8TH AVENUE",
                    "template_code": "ARCSC1-17",
                    "changed_fields": "Repayment History: Old -> New",
                    "version": "2",
                }
            ]
            auto_refresh_autofilled_scores_last_run_at = None

            def save(self, update_fields=None):
                self.saved_update_fields = list(update_fields or [])

        settings_obj = FakeSettings()
        notify_calls = []

        def fake_engine(**_kwargs):
            return {
                "checked_count": 1,
                "updated_items": [
                    {
                        "score_type": "IFRS9",
                        "customer_code": "C002",
                        "customer_name": "Customer Two",
                        "branch_name": "8TH AVENUE",
                        "template_code": "IFRS9PD-RETAIL-001",
                        "changed_fields": ["Loan Amount: Old -> New"],
                        "version": 3,
                    }
                ],
                "basel_cursor_id": 0,
                "ifrs9_cursor_id": 0,
                "basel_cycle_complete": True,
                "ifrs9_cycle_complete": True,
            }

        def fake_notify(items, **kwargs):
            notify_calls.append((list(items), kwargs))
            return {"notified": True, "updated": len(items), "document_name": "combined.csv"}

        original_get_settings = score_auto_refresh.get_scorecard_workflow_approval_settings
        original_load_engine = score_auto_refresh._load_auto_refresh_engine
        original_notify = score_auto_refresh.notify_auto_update_completed
        try:
            score_auto_refresh.get_scorecard_workflow_approval_settings = lambda: settings_obj
            score_auto_refresh._load_auto_refresh_engine = lambda: fake_engine
            score_auto_refresh.notify_auto_update_completed = fake_notify

            now = timezone.make_aware(datetime(2026, 9, 3, 3, 0), timezone.get_current_timezone())
            result = score_auto_refresh.run_due_autofilled_score_refresh(now=now)
        finally:
            score_auto_refresh.get_scorecard_workflow_approval_settings = original_get_settings
            score_auto_refresh._load_auto_refresh_engine = original_load_engine
            score_auto_refresh.notify_auto_update_completed = original_notify

        self.assertTrue(result["cycle_complete"])
        self.assertEqual(result["cycle_updated_count"], 2)
        self.assertEqual(result["cycle_updates_pending"], 0)
        self.assertEqual(len(notify_calls), 1)
        self.assertEqual(len(notify_calls[0][0]), 2)
        self.assertEqual(settings_obj.auto_refresh_autofilled_scores_pending_updates, [])

    def test_auto_update_notification_uses_score_admin_permissions_only(self):
        from scorecard.functions_view import score_auto_refresh_notifications as notifications

        self.assertEqual(
            notifications._permission_codes_for_item({"score_type": "Basel II"}),
            ("scorecard.reopen_basel_scores",),
        )
        self.assertEqual(
            notifications._permission_codes_for_item({"score_type": "IFRS9"}),
            ("scorecard.reopen_ifrs9_scores",),
        )

    def test_auto_update_notification_does_not_target_score_makers_or_checkers(self):
        from scorecard.functions_view import score_auto_refresh_notifications as notifications

        maker = SimpleNamespace(pk=1, email="maker@example.com", is_active=True)
        checker = SimpleNamespace(pk=2, email="checker@example.com", is_active=True)
        admin = SimpleNamespace(pk=3, email="admin@example.com", is_active=True)
        superuser = SimpleNamespace(pk=4, email="super@example.com", is_active=True, is_superuser=True)

        class FakeUserManager:
            def filter(self, **kwargs):
                if kwargs.get("is_superuser") is True:
                    return [superuser]
                return []

        class FakeUserModel:
            objects = FakeUserManager()

        original_get_user_model = notifications.get_user_model
        original_collect_permission_users = notifications._collect_permission_users
        try:
            notifications.get_user_model = lambda: FakeUserModel
            notifications._collect_permission_users = lambda _items: [admin]
            recipients = notifications._collect_notification_users(
                [
                    {
                        "score_type": "Basel II",
                        "customer_code": "C001",
                        "branch_name": "8TH AVENUE",
                        "maker": maker,
                        "checker": checker,
                    }
                ],
                actor=maker,
            )
        finally:
            notifications.get_user_model = original_get_user_model
            notifications._collect_permission_users = original_collect_permission_users

        self.assertEqual(recipients, [superuser, admin])
        self.assertNotIn(maker, recipients)
        self.assertNotIn(checker, recipients)

    def test_score_admin_permissions_can_open_notifications(self):
        from scorecard.context_processors import _user_can_access_scorecard_route

        basel_admin = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            has_perm=lambda permission: permission == "scorecard.reopen_basel_scores",
        )
        ifrs9_admin = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            has_perm=lambda permission: permission == "scorecard.reopen_ifrs9_scores",
        )
        checker_only = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            has_perm=lambda permission: permission == "scorecard.review_basel_scores",
        )

        self.assertTrue(_user_can_access_scorecard_route(basel_admin, "notifications"))
        self.assertTrue(_user_can_access_scorecard_route(ifrs9_admin, "notifications"))
        self.assertFalse(_user_can_access_scorecard_route(checker_only, "notifications"))


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
