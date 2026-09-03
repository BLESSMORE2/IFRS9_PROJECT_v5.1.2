from datetime import date

from django.test import TestCase

from scorecard.functions_view.customers import _get_stage_customer_population_snapshot
from scorecard.models import BankBranch, CustomerLoan


class StageCustomerPopulationSnapshotTests(TestCase):
    def test_snapshot_uses_branch_description_for_stage_loans(self):
        branch = BankBranch.objects.create(
            bank_name="Test Bank",
            branch_code="001",
            branch_name="HEAD OFFICE",
        )
        CustomerLoan.objects.create(
            reporting_date=date(2026, 1, 31),
            customer_code="C001",
            customer_name="Test Customer",
            branch_description="HEAD OFFICE",
            loan_id="loan-001",
        )

        snapshot = _get_stage_customer_population_snapshot([branch])

        self.assertEqual(snapshot["reporting_date"], date(2026, 1, 31))
        self.assertEqual(len(snapshot["rows"]), 1)
        self.assertEqual(snapshot["rows"][0]["customer_ref_code"], "C001")
        self.assertEqual(snapshot["rows"][0]["branch_name"], "HEAD OFFICE")
