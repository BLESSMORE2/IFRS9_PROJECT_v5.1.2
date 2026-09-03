from decimal import Decimal

from django.core.management.base import BaseCommand

from scorecard.models import (
    Attribute,
    BaselScoreSheetTemplate,
    GradeBand,
    Option,
    RiskDriver,
    Section,
)


class Command(BaseCommand):
    help = (
        "Seed the Basel Retail Credit Scoresheet template (ARCSC1-17) "
        "with sections, risk drivers, attributes, options, and grade bands."
    )

    def handle(self, *args, **options):
        template, created = BaselScoreSheetTemplate.objects.get_or_create(
            code="ARCSC1-17",
            defaults={
                "name": "Retail Credit Scoresheet",
                "description": "Basel II retail credit scoresheet as per ARCSC1-17.",
                "is_active": True,
            },
        )
        if created:
            self.stdout.write(
                self.style.SUCCESS("Created BaselScoreSheetTemplate ARCSC1-17")
            )
        else:
            self.stdout.write(
                self.style.WARNING("BaselScoreSheetTemplate ARCSC1-17 already exists")
            )

        def get_section(code: str, name: str, order: int) -> Section:
            section, _ = Section.objects.get_or_create(
                template=template,
                code=code,
                defaults={"name": name, "display_order": order},
            )
            return section

        def create_risk_driver(
            section: Section,
            code: str,
            name: str,
            weight_percent: Decimal,
            max_score: Decimal,
            order: int,
        ) -> RiskDriver:
            driver, _ = RiskDriver.objects.get_or_create(
                section=section,
                code=code,
                defaults={
                    "name": name,
                    "weight_percent": weight_percent,
                    "max_score": max_score,
                    "display_order": order,
                },
            )
            return driver

        def ensure_attribute(
            driver: RiskDriver,
            code: str,
            label: str,
            weight_percent: Decimal,
            options_data: list[tuple[str, Decimal, int]],
            group_label: str = "",
            help_text: str = "",
            scoring_rule: str = "",
        ) -> None:
            attribute, _ = Attribute.objects.get_or_create(
                risk_driver=driver,
                code=code,
                defaults={
                    "label": label,
                    "group_label": group_label,
                    "help_text": help_text,
                    "data_type": "choice",
                    "input_type": "checkbox" if scoring_rule else "radio",
                    "scoring_rule": scoring_rule,
                    "is_required": True,
                    "weight_percent": weight_percent,
                    "display_order": Attribute.objects.filter(
                        risk_driver=driver
                    ).count()
                    + 1,
                },
            )

            attribute.label = label
            attribute.group_label = group_label
            attribute.help_text = help_text
            attribute.weight_percent = weight_percent
            attribute.input_type = "checkbox" if scoring_rule else "radio"
            attribute.scoring_rule = scoring_rule
            attribute.is_required = True
            attribute.save()

            for opt_label, score, order in options_data:
                Option.objects.get_or_create(
                    attribute=attribute,
                    display_order=order,
                    label=opt_label,
                    defaults={
                        "value": opt_label,
                        "allocated_score": score,
                    },
                )

        section_a = get_section("A", "Customer Demographics", 1)
        section_b = get_section("B", "Quantitative Measurements", 2)
        section_c = get_section("C", "Qualitative Measurements", 3)

        driver_1 = create_risk_driver(
            section_a,
            code="1",
            name="Legal Status (type of business organization)",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("5"),
            order=1,
        )
        driver_2 = create_risk_driver(
            section_a,
            code="2",
            name="No. of Years in Business",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("5"),
            order=2,
        )
        driver_3 = create_risk_driver(
            section_a,
            code="3",
            name="Size of organization",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("5"),
            order=3,
        )
        driver_4 = create_risk_driver(
            section_a,
            code="4",
            name="Onwership of operating premises",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("5"),
            order=4,
        )
        driver_5 = create_risk_driver(
            section_a,
            code="5",
            name="Banking Relations",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("5"),
            order=5,
        )
        driver_6 = create_risk_driver(
            section_a,
            code="6",
            name="Time with bank",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("5"),
            order=6,
        )

        driver_7 = create_risk_driver(
            section_b,
            code="7",
            name="Financials",
            weight_percent=Decimal("20.00"),
            max_score=Decimal("47"),
            order=1,
        )
        driver_8 = create_risk_driver(
            section_b,
            code="8",
            name="Capital Structure (Funding structure)",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("8"),
            order=2,
        )
        driver_9 = create_risk_driver(
            section_b,
            code="9",
            name="Working Capital Management",
            weight_percent=Decimal("19.00"),
            max_score=Decimal("43"),
            order=3,
        )
        driver_10 = create_risk_driver(
            section_b,
            code="10",
            name="Record Keeping",
            weight_percent=Decimal("1.00"),
            max_score=Decimal("3"),
            order=4,
        )
        driver_11 = create_risk_driver(
            section_b,
            code="11",
            name="Credit History",
            weight_percent=Decimal("21.00"),
            max_score=Decimal("51"),
            order=5,
        )
        driver_12 = create_risk_driver(
            section_b,
            code="12",
            name="Loan Cover Ratios",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("8"),
            order=6,
        )

        driver_13 = create_risk_driver(
            section_c,
            code="13",
            name="Management",
            weight_percent=Decimal("7.00"),
            max_score=Decimal("15"),
            order=1,
        )
        driver_14 = create_risk_driver(
            section_c,
            code="14",
            name="Succession Planning",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("10"),
            order=2,
        )
        driver_15 = create_risk_driver(
            section_c,
            code="15",
            name="Owner(s) own Credit History",
            weight_percent=Decimal("1.00"),
            max_score=Decimal("3"),
            order=3,
        )
        driver_16 = create_risk_driver(
            section_c,
            code="16",
            name="Key Buyer/Supplier dependencies",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("5"),
            order=4,
        )
        driver_17 = create_risk_driver(
            section_c,
            code="17",
            name="Product & Market Diversification",
            weight_percent=Decimal("4.00"),
            max_score=Decimal("10"),
            order=5,
        )
        driver_18 = create_risk_driver(
            section_c,
            code="18",
            name="FCB Clearance",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("5"),
            order=6,
        )

        ensure_attribute(
            driver_1,
            code="LEGAL_STATUS",
            label="Legal Status (type of business organization)",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Registered company/ Registered Association", Decimal("5"), 1),
                ("ii) Partnership", Decimal("4"), 2),
                ("iii) Sole proprietor", Decimal("3"), 3),
                ("iv) Unregistered informal body", Decimal("2"), 4),
                ("v) Other", Decimal("1"), 5),
            ],
        )
        ensure_attribute(
            driver_2,
            code="YEARS_IN_BUSINESS",
            label="No. of Years in Business",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) More than 4 Years", Decimal("5"), 1),
                ("ii) 3 - 4 Years", Decimal("4"), 2),
                ("iii) 2 - 3 Years", Decimal("3"), 3),
                ("iv) 1 -2 Years", Decimal("2"), 4),
                ("v) Less than 1 Year", Decimal("1"), 5),
                ("vi) Start up/Greenfield", Decimal("0"), 6),
            ],
        )
        ensure_attribute(
            driver_3,
            code="SIZE_OF_ORGANIZATION",
            label="Size of organization",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Large scale Retail entity", Decimal("5"), 1),
                ("ii) Medium Size Retail entity", Decimal("4"), 2),
                ("iii) Small scale family owned business", Decimal("3"), 3),
                ("iv) Cooperative", Decimal("2"), 4),
            ],
        )
        ensure_attribute(
            driver_4,
            code="OPERATING_PREMISES_OWNERSHIP",
            label="Onwership of operating premises",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Owner freehold or family property", Decimal("5"), 1),
                ("ii) Owner Mortgaged", Decimal("3"), 2),
                ("iii) Rented", Decimal("2"), 3),
                ("iv) Other - specify", Decimal("1"), 4),
            ],
        )
        ensure_attribute(
            driver_5,
            code="BANKING_RELATIONS",
            label="Banking Relations",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Total Banking with Agribank", Decimal("5"), 1),
                ("ii) Multi-banked but borrows from Agribank only", Decimal("3"), 2),
                ("iii) Multi-banked with other borrowing relations", Decimal("1"), 3),
                ("iv) No borrowing history with banks", Decimal("0"), 4),
            ],
        )
        ensure_attribute(
            driver_6,
            code="TIME_WITH_BANK",
            label="Time with bank",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) More than 5 Years", Decimal("5"), 1),
                ("ii) 3 - 4 Years", Decimal("4"), 2),
                ("iii) 1 - 2Years", Decimal("3"), 3),
                ("iv) Less than 1 Year", Decimal("2"), 4),
                ("v) New account", Decimal("1"), 5),
            ],
        )

        ensure_attribute(
            driver_7,
            code="FINANCIAL_STATEMENTS_AUDIT",
            label="Audited and availability of Financial Statements",
            group_label="a) Audited and availability of Financial Statements",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Audited", Decimal("2"), 1),
                ("ii) Unaudited", Decimal("1"), 2),
                ("iii) No financials submitted/available", Decimal("0"), 3),
            ],
        )
        ensure_attribute(
            driver_7,
            code="PROFITABILITY_HISTORY",
            label="Profitability - (3 Year History)",
            group_label="b) Profitability - (3 Year History)",
            weight_percent=Decimal("6.00"),
            options_data=[
                ("i) Profitable business enterprise", Decimal("15"), 1),
                ("ii) Declining profitability", Decimal("10"), 2),
                ("iii) Losses, but reducing", Decimal("5"), 3),
                ("iv) Increasing losses", Decimal("0"), 4),
                ("v) Business ceased operations/trading", Decimal("-5"), 5),
            ],
        )
        ensure_attribute(
            driver_7,
            code="BALANCE_SHEET_GROWTH",
            label="Balance Sheet Growth - (3 year history)",
            group_label="c) Balance Sheet Growth - (3 year history)",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Gradual organic growth", Decimal("5"), 1),
                ("ii) Balance sheet grew by capital injection", Decimal("2"), 2),
                ("iii) Relatively unchanged", Decimal("1"), 3),
                ("iv) Shrinking Balance Sheet", Decimal("0"), 4),
            ],
        )
        ensure_attribute(
            driver_7,
            code="CASHFLOW_ADEQUACY",
            label="Adequacy of cash flow generation and projection",
            group_label="d) Adequacy of cash flow generation and projection",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("i) Business projecting and generating adequate cashflows", Decimal("10"), 1),
                ("ii) Projected cashflows adequate but not being achieved", Decimal("8"), 2),
                ("iii) Both projected and generated Cashflows are declining", Decimal("5"), 3),
                ("iv)Underlying cash flow assumptions questionable and unreliable", Decimal("1"), 4),
                ("v) Business experiencing cash flow crisis", Decimal("-3"), 5),
            ],
        )
        ensure_attribute(
            driver_7,
            code="TURNOVER_HISTORY",
            label="Turnover - (3 Year History)",
            group_label="e) Turnover - (3 Year History)",
            weight_percent=Decimal("7.00"),
            options_data=[
                ("i) Turnover consistent and stable", Decimal("15"), 1),
                ("ii) Fluctuating but positive turnover", Decimal("10"), 2),
                ("iii) Turnover declining and now at critical level", Decimal("5"), 3),
                ("iv) Business ceased trading", Decimal("0"), 4),
            ],
        )

        ensure_attribute(
            driver_8,
            code="CAPITAL_STRUCTURE",
            label="Capital Structure (Funding structure)",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("i) Equity only", Decimal("8"), 1),
                ("ii) Equity is greater than Debt", Decimal("6"), 2),
                ("iii) Debt is greater than Equity", Decimal("4"), 3),
                ("iv) Debt Only", Decimal("0"), 4),
            ],
        )

        ensure_attribute(
            driver_9,
            code="DEBTORS_ANALYSIS",
            label="Debtors' Analysis",
            group_label="a) Debtors' Analysis",
            weight_percent=Decimal("2.00"),
            options_data=[
                (
                    "i) All debtors are current and within agreement/No debtirs (cash sales only)",
                    Decimal("4"),
                    1,
                ),
                ("ii) Overdue debtors are less than 50% of total debtors", Decimal("2"), 2),
                ("iii) Overdue debtors greater than 50%", Decimal("0"), 3),
            ],
        )
        ensure_attribute(
            driver_9,
            code="CREDITORS_ANALYSIS",
            label="Creditors Analysis",
            group_label="b) Creditors Analysis",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Current and wellspread/No creditors (cash purchases)", Decimal("3"), 1),
                ("ii) Overdue creditors less than 20% of total creditors", Decimal("2"), 2),
                ("iii) Overdue creditors more than 20% of total creditors", Decimal("0"), 3),
            ],
        )
        ensure_attribute(
            driver_9,
            code="DEBTORS_CREDITORS_MANAGEMENT",
            label="Debtors & Creditors Management",
            group_label="c) Debtors & Creditors Management",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Cash business only", Decimal("3"), 1),
                ("ii) Creditors period > debtors period", Decimal("2"), 2),
                ("iii) Debtors > creditors", Decimal("1"), 3),
                ("iv) Creditors > debtors", Decimal("0"), 4),
            ],
        )
        ensure_attribute(
            driver_9,
            code="LIQUIDITY_MANAGEMENT",
            label="Liquidity Management",
            group_label="d) Liquidity Management",
            help_text=(
                "Source note: score d(i) or d(ii) together with d(iii), "
                "while d(iv) is scored alone."
            ),
            weight_percent=Decimal("5.00"),
            scoring_rule="retail_liquidity_combo",
            options_data=[
                ("i) Acid test ratio is greater or equals to industry standard", Decimal("5"), 1),
                ("ii) Acid test ratio is less than industry standard but is positive", Decimal("2"), 2),
                ("iii) Current Ratio is greater or equals to industry standard", Decimal("5"), 3),
                ("iv) Technically insolvent", Decimal("-5"), 4),
            ],
        )
        ensure_attribute(
            driver_9,
            code="INVENTORY_MANAGEMENT",
            label="Inventory (Trading stock) Management",
            group_label="e) Inventory (Trading stock) Management",
            help_text=(
                "Source note: score i to iii as applicable, or score iv or v only."
            ),
            weight_percent=Decimal("7.00"),
            scoring_rule="retail_inventory_combo",
            options_data=[
                ("i) Inventory turnover consistent with nature of business and industry standard", Decimal("7"), 1),
                ("ii) Inventory kept at optimum level", Decimal("5"), 2),
                ("iii) Sound inventory management systems are in place", Decimal("4"), 3),
                ("iv) Frequent stock-outs/Or Ceased Operations", Decimal("0"), 4),
                ("v) Inventory dominated by obsolete stocks", Decimal("-7"), 5),
            ],
        )
        ensure_attribute(
            driver_9,
            code="CASH_MANAGEMENT",
            label="Cash Management",
            group_label="f) Cash Management",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("i) Sound cash management & controls in place", Decimal("7"), 1),
                ("ii) No cash management system but adequate security observed", Decimal("4"), 2),
                ("iii) Loose cash security + weak reconciliations", Decimal("2"), 3),
                ("iv) High cash security risks", Decimal("0"), 4),
            ],
        )

        ensure_attribute(
            driver_10,
            code="RECORD_KEEPING",
            label="Record Keeping",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Proper complete accounting records kept", Decimal("3"), 1),
                ("ii) Poor record keeping but recorded", Decimal("2"), 2),
                ("iii) Incomplete records - rely on owner memory", Decimal("1"), 3),
            ],
        )

        ensure_attribute(
            driver_11,
            code="LOAN_AMOUNT_TO_BALANCE_SHEET",
            label="Loan Amount vs Balance Sheet size",
            group_label="a) Loan Amount vs Balance Sheet size",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Loan amount < total assets", Decimal("5"), 1),
                ("ii) Loan amount = total assets", Decimal("4"), 2),
                ("iii) Loan amount > 1 x total assets", Decimal("3"), 3),
                ("iv) Loan amount > twice total assets", Decimal("2"), 4),
            ],
        )
        ensure_attribute(
            driver_11,
            code="PURPOSE_OF_LOAN",
            label="Purpose of Loan",
            group_label="b) Purpose of Loan",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("i) Working capital support", Decimal("8"), 1),
                ("ii) Mixed", Decimal("5"), 2),
                ("iii) Capital Expenditure", Decimal("3"), 3),
                ("iv) Refinancing", Decimal("0"), 4),
            ],
        )
        ensure_attribute(
            driver_11,
            code="LOAN_TENURE",
            label="Loan Tenure",
            group_label="c) Loan Tenure",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Less than 6 months", Decimal("5"), 1),
                ("ii) 1 -2 Years", Decimal("4"), 2),
                ("iii) 2 -3 Years", Decimal("3"), 3),
                ("iv) Over 3 Years", Decimal("2"), 4),
            ],
        )
        ensure_attribute(
            driver_11,
            code="REPAYMENT_METHOD",
            label="Repayment Method",
            group_label="d) Repayment Method",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Periodic payments (installments)", Decimal("3"), 1),
                ("ii) Bullet payment", Decimal("1"), 2),
            ],
        )
        ensure_attribute(
            driver_11,
            code="COLLATERAL_SECURITY",
            label="Collateral Security - type and quality",
            group_label="e) Collateral Security - type and quality",
            weight_percent=Decimal("4.00"),
            options_data=[
                (
                    "i) Tangible security value is above 140% of loan amount or cash cover",
                    Decimal("10"),
                    1,
                ),
                (
                    "ii) Tangible security value is les than 100% but more than 50% of loan amount",
                    Decimal("8"),
                    2,
                ),
                ("iii) Collateral security are movable assets (NGCB)", Decimal("5"), 3),
                ("iv) No collateral security", Decimal("0"), 4),
            ],
        )
        ensure_attribute(
            driver_11,
            code="REPAYMENT_HISTORY",
            label="Repayment History",
            group_label="g) Repayment History",
            weight_percent=Decimal("9.00"),
            options_data=[
                ("i) Paid all previous facilities with no difficulty", Decimal("20"), 1),
                ("v) New borrower with no traceable credit history", Decimal("10"), 2),
                ("iii) Struggled to pay previous facilities", Decimal("8"), 3),
                (
                    "iv) Previous facilities recovered through litigation/realization of security",
                    Decimal("5"),
                    4,
                ),
                ("ii) Restructured Facility", Decimal("-10"), 5),
                ("vi) Current NPL", Decimal("-20"), 6),
            ],
        )

        ensure_attribute(
            driver_12,
            code="INTEREST_COVER_RATIO",
            label="Interest cover ratio",
            group_label="a) Interest cover ratio",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Interest cover ratio is more than 2 times", Decimal("3"), 1),
                ("ii) Interest cover ratio is between 1 & 2 times", Decimal("2"), 2),
                ("iii) Interest cover ratio is less than once but positive", Decimal("1"), 3),
                ("iv) Negative Interest cover ratio", Decimal("0"), 4),
            ],
        )
        ensure_attribute(
            driver_12,
            code="LOAN_COVER_RATIO",
            label="Loan cover ratio",
            group_label="b) Loan cover ratio",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Loan cover ratio is more than 2 times", Decimal("5"), 1),
                ("ii) Loan cover ratio is more than once but less than twice", Decimal("4"), 2),
                ("iii) Loan cover ratio is less than once", Decimal("2"), 3),
                ("iv) Negative loan cover ratio", Decimal("0"), 4),
            ],
        )

        ensure_attribute(
            driver_13,
            code="MANAGEMENT",
            label="Management",
            weight_percent=Decimal("7.00"),
            options_data=[
                ("i) Experienced or professional management in place", Decimal("15"), 1),
                ("ii) Qualified Management but lacks relevant experience", Decimal("10"), 2),
                (
                    "iii) Management inexperienced and lacks relevant qualifications or skills",
                    Decimal("5"),
                    3,
                ),
                ("iv) Business under caretaker management", Decimal("0"), 4),
            ],
        )
        ensure_attribute(
            driver_14,
            code="SUCCESSION_PLANNING",
            label="Succession Planning",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("i) Sound and well documented succession planning", Decimal("10"), 1),
                ("ii) Succession planning documented but weak", Decimal("8"), 2),
                ("iii) No succession plan but being addressed", Decimal("5"), 3),
                ("iv) No succession plan - requires attention", Decimal("0"), 4),
            ],
        )
        ensure_attribute(
            driver_15,
            code="OWNERS_CREDIT_HISTORY",
            label="Owner(s) own Credit History",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Good - all facilities current with no defaults", Decimal("3"), 1),
                ("ii) Average - has previous delayed payments", Decimal("1"), 2),
                ("iii) Poor - defaulted in the past or has current past dues", Decimal("-3"), 3),
            ],
        )
        ensure_attribute(
            driver_16,
            code="KEY_DEPENDENCIES",
            label="Key Buyer/Supplier dependencies",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) No dependencies to cause concern", Decimal("5"), 1),
                ("ii) Key dependencies exist but mitigated legally", Decimal("3"), 2),
                ("iii) Key dependencies exist - business vulnerable", Decimal("1"), 3),
                ("iv) Business already being affected by dependencies", Decimal("0"), 4),
            ],
        )
        ensure_attribute(
            driver_17,
            code="PRODUCT_MARKET_DIVERSIFICATION",
            label="Product & Market Diversification",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("i) well diversified product range", Decimal("10"), 1),
                ("ii) Few products but diversified market", Decimal("7"), 2),
                ("iii) Few products - narrow market", Decimal("5"), 3),
                ("iv) Single product - vulnerable future", Decimal("2"), 4),
                ("v) Business ceased operations", Decimal("0"), 5),
            ],
        )
        ensure_attribute(
            driver_18,
            code="FCB_CLEARANCE",
            label="FCB Clearance",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) No trace", Decimal("5"), 1),
                ("ii) Traced", Decimal("-5"), 2),
            ],
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Sections, risk drivers, attributes, and options ensured for ARCSC1-17"
            )
        )

        grade_bands = [
            ("A1", "85% & Above", Decimal("85.00"), Decimal("100.00")),
            ("A2", "80% - 84%", Decimal("80.00"), Decimal("84.99")),
            ("A3", "75% - 79%", Decimal("75.00"), Decimal("79.99")),
            ("B1", "70% - 74%", Decimal("70.00"), Decimal("74.99")),
            ("B2", "65% - 69%", Decimal("65.00"), Decimal("69.99")),
            ("B3", "60% - 64%", Decimal("60.00"), Decimal("64.99")),
            ("B4", "51% - 59%", Decimal("51.00"), Decimal("59.99")),
            ("C", "45% - 50%", Decimal("45.00"), Decimal("50.99")),
            ("D", "31% - 44%", Decimal("31.00"), Decimal("44.99")),
            ("E", "Below 31%", Decimal("0.00"), Decimal("30.99")),
        ]

        for idx, (code, desc, min_p, max_p) in enumerate(grade_bands, start=1):
            GradeBand.objects.get_or_create(
                template=template,
                grade_code=code,
                min_percent=min_p,
                max_percent=max_p,
                defaults={
                    "description": desc,
                    "display_order": idx,
                },
            )

        self.stdout.write(self.style.SUCCESS("Grade bands ensured for ARCSC1-17"))
