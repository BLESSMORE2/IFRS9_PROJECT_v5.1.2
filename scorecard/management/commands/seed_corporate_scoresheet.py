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
        "Seed the Basel Corporate Credit Scoresheet template (ACCSC1-17) "
        "with sections, risk drivers, attributes, options, and grade bands."
    )

    def handle(self, *args, **options):
        template, created = BaselScoreSheetTemplate.objects.get_or_create(
            code="ACCSC1-17",
            defaults={
                "name": "Corporate Credit Scoresheet",
                "description": "Basel II corporate credit scoresheet as per ACCSC1-17.",
                "is_active": True,
            },
        )
        if created:
            self.stdout.write(
                self.style.SUCCESS("Created BaselScoreSheetTemplate ACCSC1-17")
            )
        else:
            self.stdout.write(
                self.style.WARNING("BaselScoreSheetTemplate ACCSC1-17 already exists")
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
        ) -> None:
            attribute, _ = Attribute.objects.get_or_create(
                risk_driver=driver,
                code=code,
                defaults={
                    "label": label,
                    "group_label": group_label,
                    "data_type": "choice",
                    "input_type": "radio",
                    "is_required": True,
                    "weight_percent": weight_percent,
                    "display_order": Attribute.objects.filter(
                        risk_driver=driver
                    ).count()
                    + 1,
                },
            )

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

        section_a = get_section("A", "Quantitative Measures", 1)
        section_b = get_section("B", "Qualitative Measures", 2)

        driver_1 = create_risk_driver(
            section_a,
            code="1",
            name="Turnover & Profitability",
            weight_percent=Decimal("11.00"),
            max_score=Decimal("27"),
            order=1,
        )
        driver_2 = create_risk_driver(
            section_a,
            code="2",
            name="Availability of Financial Statements & Audit status",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("6"),
            order=2,
        )
        driver_3 = create_risk_driver(
            section_a,
            code="3",
            name="Liquidity",
            weight_percent=Decimal("10.00"),
            max_score=Decimal("20"),
            order=3,
        )
        driver_4 = create_risk_driver(
            section_a,
            code="4",
            name="Capitalization (commensurate with size, complexity and nature of business of client)",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("5"),
            order=4,
        )
        driver_5 = create_risk_driver(
            section_a,
            code="5",
            name="Loan Repayment track record",
            weight_percent=Decimal("10.00"),
            max_score=Decimal("20"),
            order=5,
        )
        driver_6 = create_risk_driver(
            section_a,
            code="6",
            name="Debtors and creditors concentration risk",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("5"),
            order=6,
        )
        driver_7 = create_risk_driver(
            section_a,
            code="7",
            name="Debt/Equity Ratio - (Estimates gearing and cost of borrowing/funding)",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("10"),
            order=7,
        )
        driver_8 = create_risk_driver(
            section_a,
            code="8",
            name="Cashflow management",
            weight_percent=Decimal("8.00"),
            max_score=Decimal("18"),
            order=8,
        )
        driver_9 = create_risk_driver(
            section_a,
            code="9",
            name="Trading Stock Management",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("6"),
            order=9,
        )
        driver_10 = create_risk_driver(
            section_a,
            code="10",
            name="Collateral Security (adequacy and quality)",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("10"),
            order=10,
        )

        driver_11 = create_risk_driver(
            section_b,
            code="11",
            name="Operating Capacity utilization",
            weight_percent=Decimal("9.00"),
            max_score=Decimal("22"),
            order=1,
        )
        driver_12 = create_risk_driver(
            section_b,
            code="12",
            name="Management",
            weight_percent=Decimal("12.00"),
            max_score=Decimal("25"),
            order=2,
        )
        driver_13 = create_risk_driver(
            section_b,
            code="13",
            name="Business Operations diversification & Industry attractiveness",
            weight_percent=Decimal("9.00"),
            max_score=Decimal("17"),
            order=3,
        )
        driver_14 = create_risk_driver(
            section_b,
            code="14",
            name="Credit Reference Bureau Check (CRB)",
            weight_percent=Decimal("4.00"),
            max_score=Decimal("10"),
            order=4,
        )
        driver_15 = create_risk_driver(
            section_b,
            code="15",
            name="Ownership & Degree of control",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("5"),
            order=5,
        )
        driver_16 = create_risk_driver(
            section_b,
            code="16",
            name="AML/CFT, World Check",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("5"),
            order=6,
        )
        # The source sheet labels this as "15. FCB Clearance" after item 16.
        # We keep a unique code while preserving the original business label.
        driver_17 = create_risk_driver(
            section_b,
            code="15A",
            name="FCB Clearance",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("4"),
            order=7,
        )

        ensure_attribute(
            driver_1,
            code="TURNOVER",
            label="Turnover",
            group_label="a) Turnover",
            weight_percent=Decimal("6.00"),
            options_data=[
                ("i) Company generating sufficient income to cover liabilities", Decimal("15"), 1),
                ("ii) Turnover fluctuating with risk of deficiency", Decimal("10"), 2),
                (
                    "iii) Turnover insufficient to support liabilities without secondary sources",
                    Decimal("8"),
                    3,
                ),
                (
                    "iv) Turnover in consistent decline and now unable to support liabilities with no secondary sources",
                    Decimal("3"),
                    4,
                ),
                ("v) Ceased operations - no turnover", Decimal("0"), 5),
            ],
        )
        ensure_attribute(
            driver_1,
            code="PROFITABILITY",
            label="Profitability",
            group_label="b) Profitability",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("i) Profitable trading in the last 3 financial years", Decimal("12"), 1),
                (
                    "ii) Fluctuating profitability and losses in the last 3 years",
                    Decimal("9"),
                    2,
                ),
                (
                    "iii) Loss making position in last 3 years but losses reducing",
                    Decimal("5"),
                    3,
                ),
                (
                    "iv) Consistent and increasing losses in the last 3 years",
                    Decimal("2"),
                    4,
                ),
                ("v) Company ceased operations", Decimal("0"), 5),
            ],
        )
        ensure_attribute(
            driver_2,
            code="FINANCIAL_STATEMENTS_AUDIT",
            label="Availability of Financial Statements & Audit status",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Unqualified Audited Accounts", Decimal("6"), 1),
                (
                    "ii) Audited Financials out of date (i.e. not for preceding financial year)",
                    Decimal("4"),
                    2,
                ),
                ("iii) Unaudited financials/Qualified", Decimal("2"), 3),
                ("iv) No accounts submitted", Decimal("1"), 4),
                ("v) No longer going concern and no financials", Decimal("0"), 5),
            ],
        )
        ensure_attribute(
            driver_3,
            code="LIQUIDITY",
            label="Liquidity",
            weight_percent=Decimal("10.00"),
            options_data=[
                (
                    "i) Strong (Current ratio is in line with industry standard and improving)",
                    Decimal("20"),
                    1,
                ),
                (
                    "ii) Good (Current ratio is in line with industry standard and stable)",
                    Decimal("15"),
                    2,
                ),
                (
                    "iii) Fair (Current Ratio in line with industry standard but declining)",
                    Decimal("10"),
                    3,
                ),
                ("iv) Weak (Current ratio is below industry standard)", Decimal("5"), 4),
                ("v) Poor (current ratio is negative)", Decimal("0"), 5),
            ],
        )
        ensure_attribute(
            driver_4,
            code="CAPITALIZATION",
            label="Capitalization (commensurate with size, complexity and nature of business of client)",
            weight_percent=Decimal("3.00"),
            options_data=[
                (
                    "i) Adequately capitalized and strong balance sheet (Shareholders' equity greater than other funding sources and matched fixed assets)",
                    Decimal("5"),
                    1,
                ),
                (
                    "ii) Inadequately capitalized but strong balance sheet (debt financing sources greater than shareholders' equity but matched in fixed assets)",
                    Decimal("3"),
                    2,
                ),
                (
                    "iii) Adequately capitalized but weak balance sheet (Shareholders' equity greater than other funding sources but not matched in fixed assets)",
                    Decimal("1"),
                    3,
                ),
                (
                    "iv) Inadequately capitalized and weak balance sheet (Shareholders' equity less than other sources of funding with no matching fixed assets)",
                    Decimal("0"),
                    4,
                ),
            ],
        )
        ensure_attribute(
            driver_5,
            code="LOAN_REPAYMENT_TRACK_RECORD",
            label="Loan Repayment track record",
            weight_percent=Decimal("10.00"),
            options_data=[
                ("i) Always repaid all loans on time and all loans are current", Decimal("20"), 1),
                (
                    "ii) Delayed payment of previous loans (all or other) but eventually paid and all loans are current",
                    Decimal("15"),
                    2,
                ),
                (
                    "iii) Had incidence of paying after debt restructuring/OR restructured facility",
                    Decimal("10"),
                    3,
                ),
                (
                    "iv) Had incidence of paying through legal /debt recovery process and all loans are current",
                    Decimal("5"),
                    4,
                ),
                ("v) Currently in arrears but not NPL", Decimal("0"), 5),
                (
                    "vi) Has record of default on past loans (default judgments or otherwise)",
                    Decimal("-5"),
                    6,
                ),
                ("vii) Currently NPL", Decimal("-10"), 7),
            ],
        )
        ensure_attribute(
            driver_6,
            code="DEBTORS_CREDITORS_CONCENTRATION",
            label="Debtors and creditors concentration risk",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Well spread or cash business", Decimal("5"), 1),
                ("ii) Fair spread", Decimal("4"), 2),
                (
                    "iii) Concentrated but reliance on them not cause for concern",
                    Decimal("3"),
                    3,
                ),
                (
                    "iv) Few but heavily concentrated and considered vulnerable",
                    Decimal("1"),
                    4,
                ),
                ("v) Ceased operations", Decimal("0"), 5),
            ],
        )
        ensure_attribute(
            driver_7,
            code="DEBT_EQUITY_RATIO",
            label="Debt/Equity Ratio - (Estimates gearing and cost of borrowing/funding)",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("i) Low (50% and below)", Decimal("10"), 1),
                ("ii) Fair (51% - 79%)", Decimal("7"), 2),
                ("iii) High but manageable (80% - 100%)", Decimal("5"), 3),
                ("iv) High and unmanageable (above 100%)", Decimal("0"), 4),
            ],
        )
        ensure_attribute(
            driver_8,
            code="CASHFLOW_PROJECTIONS",
            label="Cashflow Projections",
            group_label="a) Cashflow Projections",
            weight_percent=Decimal("4.00"),
            options_data=[
                (
                    "i) Adequate and achievable even with adverse changes in operating environment",
                    Decimal("10"),
                    1,
                ),
                (
                    "ii) Adequate and achievable provided no adverse changes in operating environment",
                    Decimal("7"),
                    2,
                ),
                (
                    "iii) Adequate but achieving them is doubtful under current circumstances",
                    Decimal("5"),
                    3,
                ),
                ("iv) Not adequate under current circumstances", Decimal("2"), 4),
                ("v) Ceased operations - no cashflows forecasts", Decimal("0"), 5),
            ],
        )
        ensure_attribute(
            driver_8,
            code="CASHFLOW_STATEMENTS",
            label="Cashflow statements",
            group_label="b) Cashflow statements",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("i) Business generating sufficient cashflow from operations", Decimal("8"), 1),
                (
                    "ii) Operating cashflows not sufficient but supported by investments",
                    Decimal("5"),
                    2,
                ),
                (
                    "iii) Both operating and investment cashflows are not sufficient",
                    Decimal("2"),
                    3,
                ),
                ("iv) Ceased operations", Decimal("0"), 4),
            ],
        )
        ensure_attribute(
            driver_9,
            code="TRADING_STOCK_MANAGEMENT",
            label="Trading Stock Management",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("i) In line with industry standards", Decimal("6"), 1),
                ("ii) High for level of business", Decimal("4"), 2),
                ("iii) Stocks low for level of business", Decimal("3"), 3),
                ("iv) Frequent stock outs", Decimal("2"), 4),
                ("v) Significant obsolete stock", Decimal("1"), 5),
                ("vi) Ceased trading", Decimal("0"), 6),
            ],
        )
        ensure_attribute(
            driver_10,
            code="COLLATERAL_SECURITY",
            label="Collateral Security (adequacy and quality)",
            weight_percent=Decimal("5.00"),
            options_data=[
                (
                    "i) Secured with no other lenders or pari pasu with other lenders",
                    Decimal("10"),
                    1,
                ),
                ("ii) Secured but bank ranks behind other lenders", Decimal("8"), 2),
                (
                    "iii) Negative pledge (i.e. secured by movable assets) with strong balance sheet",
                    Decimal("5"),
                    3,
                ),
                ("iv) Unsecured lending", Decimal("1"), 4),
            ],
        )

        ensure_attribute(
            driver_11,
            code="OPERATING_CAPACITY_UTILIZATION",
            label="Operating Capacity utilization",
            weight_percent=Decimal("9.00"),
            options_data=[
                ("i) Operating above industry average", Decimal("22"), 1),
                ("ii) Operating at industry average", Decimal("15"), 2),
                ("iii) Below industry average but viable", Decimal("10"), 3),
                ("iv) Operating below viable capacity", Decimal("5"), 4),
                ("v) Ceased Operations", Decimal("0"), 5),
            ],
        )
        ensure_attribute(
            driver_12,
            code="MANAGEMENT",
            label="Management",
            weight_percent=Decimal("12.00"),
            options_data=[
                ("i) Experienced Management with good succession plan", Decimal("25"), 1),
                (
                    "ii) Experienced Management with poor/no succession plan",
                    Decimal("15"),
                    2,
                ),
                ("iii) Inexperienced but qualified Management", Decimal("10"), 3),
                (
                    "iv) Management lacks both experience and relevant qualifications",
                    Decimal("5"),
                    4,
                ),
                ("v) Caretaker Management in place - experiencing challenges", Decimal("0"), 5),
            ],
        )
        ensure_attribute(
            driver_13,
            code="BUSINESS_DIVERSIFICATION",
            label="Business Operations diversification",
            group_label="a) Business Operations diversification",
            weight_percent=Decimal("3.00"),
            options_data=[
                (
                    "i) Well diversified company (more than 3 business/product lines)",
                    Decimal("7"),
                    1,
                ),
                (
                    "ii) Fairly diversified company (2 or 3 business/product lines)",
                    Decimal("5"),
                    2,
                ),
                (
                    "iii) Undiversified business (single business/product line)",
                    Decimal("2"),
                    3,
                ),
            ],
        )
        ensure_attribute(
            driver_13,
            code="INDUSTRY_ATTRACTIVENESS",
            label="Industry Attractiveness - based on industry lifecycle",
            group_label="b) Industry Attractiveness - based on industry lifecycle",
            weight_percent=Decimal("6.00"),
            options_data=[
                ("i) Maturity stage", Decimal("10"), 1),
                ("ii) Growth Stage", Decimal("7"), 2),
                ("iii) Birth/Inception/Early maturity stage", Decimal("5"), 3),
                ("iv) Decline Stage", Decimal("1"), 4),
            ],
        )
        ensure_attribute(
            driver_14,
            code="CRB_CHECK",
            label="Credit Reference Bureau Check (CRB)",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("i) No other borrowing", Decimal("10"), 1),
                ("ii) Borrowed with all facilities current", Decimal("8"), 2),
                (
                    "iii) Borrowed with facilities current but has record of dispute",
                    Decimal("6"),
                    3,
                ),
                ("iv) Borrowed but has arrears on repayments", Decimal("2"), 4),
                ("v) All borrowings in arrears", Decimal("-5"), 5),
            ],
        )
        ensure_attribute(
            driver_15,
            code="OWNERSHIP_CONTROL",
            label="Ownership & Degree of control",
            weight_percent=Decimal("3.00"),
            options_data=[
                (
                    "i) Holding Company/Group of companies/Parastatal/State Owned Enterprises/Other State arms/departments",
                    Decimal("5"),
                    1,
                ),
                ("ii) Subsidiary of a holding Company", Decimal("3"), 2),
                ("iii) Stand Alone Company/affiliate company", Decimal("2"), 3),
                ("iv) Unincorporated body", Decimal("1"), 4),
            ],
        )
        ensure_attribute(
            driver_16,
            code="AML_CFT_WORLD_CHECK",
            label="AML/CFT, World Check",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Clean AML/CFT record & No PEP link", Decimal("5"), 1),
                ("ii) Clean AML/CFT record but PEP linked", Decimal("3"), 2),
                ("iii) Assessed AML/CFT risk is considered high", Decimal("2"), 3),
                ("iv) Has documented AML/CFT trace", Decimal("1"), 4),
            ],
        )
        ensure_attribute(
            driver_17,
            code="FCB_CLEARANCE",
            label="FCB Clearance",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) No trace", Decimal("4"), 1),
                ("ii) Traced with record 5 yrs old", Decimal("2"), 2),
                ("iii) Traced with record less than 5 years", Decimal("0"), 3),
            ],
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Sections, risk drivers, attributes, and options ensured for ACCSC1-17"
            )
        )

        # Assumption: Basel grade bands are standardized across score sheets and are
        # therefore reused here as percentage bands against total weighted score.
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

        self.stdout.write(self.style.SUCCESS("Grade bands ensured for ACCSC1-17"))
