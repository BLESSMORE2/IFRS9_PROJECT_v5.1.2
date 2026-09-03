from decimal import Decimal

from django.core.management.base import BaseCommand

from scorecard.models import (
    IFRS9Attribute,
    IFRS9Option,
    IFRS9RiskDriver,
    IFRS9ScoreSheetTemplate,
    IFRS9Section,
)


class Command(BaseCommand):
    help = (
        "Seed the IFRS9 PD Retail scorecard template "
        "(IFRS9PD-RETAIL-001) with sections, risk drivers, attributes, and options."
    )

    def handle(self, *args, **options):
        template, created = IFRS9ScoreSheetTemplate.objects.get_or_create(
            code="IFRS9PD-RETAIL-001",
            defaults={
                "name": "IFRS9PD Scorecard - Retail",
                "description": (
                    "IFRS9 PD retail scorecard seeded from the "
                    "'PD Scorecard - Retail' worksheet."
                ),
                "is_active": True,
                "status": "approved",
                "formula_actual_score": "ALLOCATED_SCORE",
                "formula_weighted_score": (
                    "ACTUAL_SCORE / Highest_Possible_Score * WEIGHT"
                ),
                "formula_proof": (
                    "IF(ACTUAL_SCORE = '', '', "
                    "IF(ACTUAL_SCORE != ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE"
                ),
            },
        )
        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    "Created IFRS9ScoreSheetTemplate IFRS9PD-RETAIL-001"
                )
            )
        else:
            if template.status != "approved":
                template.status = "approved"
                template.save(update_fields=["status"])
                self.stdout.write(
                    self.style.SUCCESS(
                        "Updated IFRS9ScoreSheetTemplate IFRS9PD-RETAIL-001 "
                        "status to 'approved'"
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        "IFRS9ScoreSheetTemplate IFRS9PD-RETAIL-001 already exists"
                    )
                )

        def get_section(code: str, name: str, order: int) -> IFRS9Section:
            section, _ = IFRS9Section.objects.get_or_create(
                template=template,
                code=code,
                defaults={"name": name, "display_order": order},
            )
            return section

        def create_risk_driver(
            section: IFRS9Section,
            code: str,
            name: str,
            weight_percent: Decimal,
            max_score: Decimal,
            order: int,
        ) -> IFRS9RiskDriver:
            driver, _ = IFRS9RiskDriver.objects.get_or_create(
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
            driver: IFRS9RiskDriver,
            code: str,
            label: str,
            weight_percent: Decimal,
            options_data: list[tuple[str, Decimal, int]],
            group_label: str = "",
            help_text: str = "",
        ) -> None:
            attribute, _ = IFRS9Attribute.objects.get_or_create(
                risk_driver=driver,
                code=code,
                defaults={
                    "label": label,
                    "group_label": group_label,
                    "help_text": help_text,
                    "data_type": "choice",
                    "input_type": "radio",
                    "is_required": True,
                    "weight_percent": weight_percent,
                    "display_order": IFRS9Attribute.objects.filter(
                        risk_driver=driver
                    ).count()
                    + 1,
                },
            )

            for opt_label, score, order in options_data:
                IFRS9Option.objects.get_or_create(
                    attribute=attribute,
                    display_order=order,
                    label=opt_label,
                    defaults={
                        "value": opt_label,
                        "allocated_score": score,
                    },
                )

        section_a = get_section("A", "Retail Probability of Default Scorecard", 1)

        driver_1 = create_risk_driver(
            section_a,
            code="1",
            name="Industry Attractiveness",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("5"),
            order=1,
        )
        driver_2 = create_risk_driver(
            section_a,
            code="2",
            name="Business Track Record (No. of Yrs)",
            weight_percent=Decimal("7.00"),
            max_score=Decimal("5"),
            order=2,
        )
        driver_3 = create_risk_driver(
            section_a,
            code="3",
            name="Banking History/Arrangements",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("4"),
            order=3,
        )
        driver_4 = create_risk_driver(
            section_a,
            code="4",
            name="Management",
            weight_percent=Decimal("4.00"),
            max_score=Decimal("3"),
            order=4,
        )
        driver_5 = create_risk_driver(
            section_a,
            code="5",
            name="Ownership Structure",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("4"),
            order=5,
        )
        driver_6 = create_risk_driver(
            section_a,
            code="6",
            name="Diversification - Product & Market",
            weight_percent=Decimal("6.00"),
            max_score=Decimal("5"),
            order=6,
        )
        driver_7 = create_risk_driver(
            section_a,
            code="7",
            name="Profitability",
            weight_percent=Decimal("8.00"),
            max_score=Decimal("3"),
            order=7,
        )
        driver_8 = create_risk_driver(
            section_a,
            code="8",
            name="Key Performance Ratios",
            weight_percent=Decimal("15.00"),
            max_score=Decimal("9"),
            order=8,
        )
        driver_9 = create_risk_driver(
            section_a,
            code="9",
            name="Loan Repayment Track Record",
            weight_percent=Decimal("25.00"),
            max_score=Decimal("5"),
            order=9,
        )
        driver_10 = create_risk_driver(
            section_a,
            code="10",
            name="Collateral Security",
            weight_percent=Decimal("20.00"),
            max_score=Decimal("3"),
            order=10,
        )

        ensure_attribute(
            driver_1,
            code="INDUSTRY_ATTRACTIVENESS",
            label="Industry Attractiveness",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("i) Niche market or sector", Decimal("1"), 1),
                (
                    "ii) Protected industry with high regulatory entry barriers (e.g. netwrok operators)",
                    Decimal("2"),
                    2,
                ),
                ("iii) Vibrant industry/sector characterized by few players", Decimal("3"), 3),
                ("iv) Vibrant industry with many players", Decimal("4"), 4),
                ("v) Saturated or declining industry/market/sector", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_2,
            code="BUSINESS_TRACK_RECORD",
            label="Business Track Record (No. of Yrs)",
            weight_percent=Decimal("7.00"),
            options_data=[
                ("i) Over 10 Years experience", Decimal("1"), 1),
                ("ii) 6 to 10 Years experience", Decimal("2"), 2),
                ("iii) 3 to 6 Years experience", Decimal("3"), 3),
                (
                    "iv) Less than 3 years but more than 12 months experience",
                    Decimal("4"),
                    4,
                ),
                ("v) Up to 12 months experience (including start ups)", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_3,
            code="BANKING_HISTORY",
            label="Banking History/Arrangements",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("i) 100% Banking with single Bank", Decimal("1"), 1),
                ("ii) Multibanked but borrowing with 1 bank only", Decimal("2"), 2),
                ("iii) Multibanked with multi interbank credit facilities", Decimal("3"), 3),
                ("iv) No current Banking relations", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_4,
            code="MANAGEMENT",
            label="Management",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("i) Competent management", Decimal("1"), 1),
                ("ii) Management lacks skills and/or experience", Decimal("3"), 2),
            ],
        )
        ensure_attribute(
            driver_5,
            code="OWNERSHIP_STRUCTURE",
            label="Ownership Structure",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("i) Registered Company", Decimal("1"), 1),
                ("ii) Partnership", Decimal("2"), 2),
                ("iii) Sole Proprietor", Decimal("3"), 3),
                ("iv) Cooperatives and other informal bodies", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_6,
            code="DIVERSIFICATION_PRODUCT_MARKET",
            label="Diversification - Product & Market",
            weight_percent=Decimal("6.00"),
            options_data=[
                ("i) Broad product range in various markets", Decimal("1"), 1),
                ("ii) Fair product range in a narrow market(s)", Decimal("2"), 2),
                ("iii) Narrow product range in wider market(s)", Decimal("3"), 3),
                ("iv) Narrow product range in narrow/single market or sector", Decimal("4"), 4),
                ("v) Single product", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_7,
            code="PROFITABILITY",
            label="Profitability",
            weight_percent=Decimal("8.00"),
            options_data=[
                ("i) Profitable trading", Decimal("1"), 1),
                ("ii) Profitable but reducing accumulated losses", Decimal("2"), 2),
                ("iii) Increasing losses", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_8,
            code="LIQUIDITY_RATIOS",
            label="Liquidity Ratios - acid test",
            group_label="a) Liquidity Ratios - acid test",
            weight_percent=Decimal("6.00"),
            options_data=[
                (
                    "i) Liquid assets equals/greater than current liabilities",
                    Decimal("0"),
                    1,
                ),
                (
                    "ii) Liquid assets are less than current liabilities but total assets greater than current liabilities",
                    Decimal("1"),
                    2,
                ),
                ("iii) Insolvent", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_8,
            code="GEARING",
            label="Gearing",
            group_label="b) Gearing",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("i) Low gearing ratio (below 50%)", Decimal("1"), 1),
                ("ii) Moderate gearing ratio (50 to 85%)", Decimal("2"), 2),
                ("iii) High gearing ratio (above 85%)", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_8,
            code="LOAN_COVER_RATIO",
            label="Loan cover ratio (principal & interest)",
            group_label="c) Loan cover ratio (principal & interest)",
            weight_percent=Decimal("6.00"),
            options_data=[
                ("i) Ratio above 2 times", Decimal("1"), 1),
                ("ii) Ratio between 1 - 2 times", Decimal("2"), 2),
                ("iii) Ratio less than 1", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_9,
            code="LOAN_REPAYMENT_TRACK_RECORD",
            label="Loan Repayment Track Record",
            weight_percent=Decimal("25.00"),
            options_data=[
                (
                    "i) Clean loan repayment history with no history of default",
                    Decimal("0"),
                    1,
                ),
                ("ii) Previous default history but currently up to date", Decimal("2"), 2),
                ("iii) Restructuring history but meeting obligations", Decimal("3"), 3),
                ("iv) Currently in arrears but still performing", Decimal("4"), 4),
                ("v) NPL (Bad Debt)", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_10,
            code="COLLATERAL_SECURITY",
            label="Collateral Security",
            weight_percent=Decimal("20.00"),
            options_data=[
                ("i) Tangible security adequately covering exposure", Decimal("1"), 1),
                (
                    "ii) Partially covered by tangible security (at least 60% of exposure)",
                    Decimal("2"),
                    2,
                ),
                ("iii) Wholly unsecured exposure", Decimal("3"), 3),
            ],
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Sections, risk drivers, attributes, and options ensured for "
                "IFRS9PD-RETAIL-001"
            )
        )
