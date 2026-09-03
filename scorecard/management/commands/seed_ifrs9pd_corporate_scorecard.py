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
        "Seed the IFRS9 PD Corporate scorecard template "
        "(IFRS9PD-CORPORATE-001) with sections, risk drivers, attributes, and options."
    )

    def handle(self, *args, **options):
        template, created = IFRS9ScoreSheetTemplate.objects.get_or_create(
            code="IFRS9PD-CORPORATE-001",
            defaults={
                "name": "IFRS9PD Scorecard - Corporate",
                "description": (
                    "IFRS9 PD corporate scorecard seeded from the "
                    "'PD Scorecard - Corporate' worksheet."
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
                    "Created IFRS9ScoreSheetTemplate IFRS9PD-CORPORATE-001"
                )
            )
        else:
            if template.status != "approved":
                template.status = "approved"
                template.save(update_fields=["status"])
                self.stdout.write(
                    self.style.SUCCESS(
                        "Updated IFRS9ScoreSheetTemplate IFRS9PD-CORPORATE-001 "
                        "status to 'approved'"
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        "IFRS9ScoreSheetTemplate IFRS9PD-CORPORATE-001 already exists"
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

        section_a = get_section("A", "Corporate ECL Model", 1)

        driver_1 = create_risk_driver(
            section_a,
            code="1",
            name="Turnover",
            weight_percent=Decimal("20.00"),
            max_score=Decimal("5"),
            order=1,
        )
        driver_2 = create_risk_driver(
            section_a,
            code="2",
            name="Profitability",
            weight_percent=Decimal("15.00"),
            max_score=Decimal("6"),
            order=2,
        )
        # Source sheet's "Maximum Possible score" says 2, but the options run 0-3.
        # We seed the full option range so runtime weighted scoring stays consistent.
        driver_3 = create_risk_driver(
            section_a,
            code="3",
            name="Financial statements",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("3"),
            order=3,
        )
        driver_4 = create_risk_driver(
            section_a,
            code="4",
            name="Liquidity",
            weight_percent=Decimal("15.00"),
            max_score=Decimal("5"),
            order=4,
        )
        driver_5 = create_risk_driver(
            section_a,
            code="5",
            name="Debt/Equity ratio",
            weight_percent=Decimal("7.00"),
            max_score=Decimal("4"),
            order=5,
        )
        driver_6 = create_risk_driver(
            section_a,
            code="6",
            name="Loan repayment track record",
            weight_percent=Decimal("8.00"),
            max_score=Decimal("6"),
            order=6,
        )
        driver_7 = create_risk_driver(
            section_a,
            code="7",
            name="Capitalisation",
            weight_percent=Decimal("4.00"),
            max_score=Decimal("5"),
            order=7,
        )
        driver_8 = create_risk_driver(
            section_a,
            code="8",
            name="Management",
            weight_percent=Decimal("12.00"),
            max_score=Decimal("5"),
            order=8,
        )
        driver_9 = create_risk_driver(
            section_a,
            code="9",
            name="Industry factors",
            weight_percent=Decimal("7.00"),
            max_score=Decimal("10"),
            order=9,
        )
        driver_10 = create_risk_driver(
            section_a,
            code="10",
            name="Product range/diversification",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("3"),
            order=10,
        )
        driver_11 = create_risk_driver(
            section_a,
            code="11",
            name="Security",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("4"),
            order=11,
        )

        ensure_attribute(
            driver_1,
            code="TURNOVER",
            label="Turnover",
            weight_percent=Decimal("20.00"),
            options_data=[
                (
                    "Sustained upward trend (increase more than twice inflation rate)",
                    Decimal("1"),
                    1,
                ),
                (
                    "Overall upward trend (increase more than inflation rate but less than twice)",
                    Decimal("2"),
                    2,
                ),
                ("Stable growth (in line with inflation)", Decimal("3"), 3),
                ("Downward trend", Decimal("4"), 4),
                ("Ceased Trading", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_2,
            code="PROFITABILITY",
            label="Profitability",
            weight_percent=Decimal("15.00"),
            options_data=[
                (
                    "Sustained upward trend (increase more than twice inflation rate)",
                    Decimal("1"),
                    1,
                ),
                (
                    "Overall upward trend (increase more than inflation rate but less than twice)",
                    Decimal("2"),
                    2,
                ),
                ("Fluctuating but Stable (in line with inflation)", Decimal("3"), 3),
                ("Downward trend", Decimal("4"), 4),
                ("Falling", Decimal("5"), 5),
                ("Loss", Decimal("6"), 6),
            ],
        )
        ensure_attribute(
            driver_3,
            code="FINANCIAL_STATEMENTS",
            label="Financial statements",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("Recently audited and not qualified", Decimal("0"), 1),
                ("Recently audited and qualified", Decimal("1"), 2),
                ("Unaudited", Decimal("2"), 3),
                ("Unavailable", Decimal("3"), 4),
            ],
        )
        ensure_attribute(
            driver_4,
            code="LIQUIDITY",
            label="Liquidity",
            weight_percent=Decimal("15.00"),
            options_data=[
                (
                    "Strong (Current ratio in line with industry standard & improving)",
                    Decimal("1"),
                    1,
                ),
                (
                    "Good (Current ratio in line with industry standard and stable)",
                    Decimal("2"),
                    2,
                ),
                (
                    "Fair (Current ratio in line with industry standard but declining)",
                    Decimal("3"),
                    3,
                ),
                ("Weak (Current ratio below industry standard)", Decimal("4"), 4),
                ("Poor (Current ratio is negative)", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_5,
            code="DEBT_EQUITY_RATIO",
            label="Debt/Equity ratio",
            weight_percent=Decimal("7.00"),
            options_data=[
                ("Low (below 50%)", Decimal("1"), 1),
                ("Fair (50% to 79%)", Decimal("2"), 2),
                ("High & manageable (80% to 100%)", Decimal("3"), 3),
                ("High & unmanageable (above 100%)", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_6,
            code="LOAN_REPAYMENT_TRACK_RECORD",
            label="Loan repayment track record",
            weight_percent=Decimal("8.00"),
            options_data=[
                ("Always repaid on loans on time from primary source", Decimal("1"), 1),
                ("Delayed but eventually paid", Decimal("2"), 2),
                ("Paid after restructuring", Decimal("3"), 3),
                ("Currently in arrears but not NPL", Decimal("4"), 4),
                ("Paid through Litigation", Decimal("5"), 5),
                ("NPL", Decimal("6"), 6),
            ],
        )
        ensure_attribute(
            driver_7,
            code="CAPITALISATION",
            label="Capitalisation",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("Adequately capitalized & strong balance sheet", Decimal("1"), 1),
                ("Adequately capitalized but weak balance sheet", Decimal("2"), 2),
                ("Inadequately capitalized but strong balance sheet", Decimal("3"), 3),
                ("Inadequately capitalized with weak balance sheet", Decimal("4"), 4),
                ("Insolvent", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_8,
            code="MANAGEMENT",
            label="Management",
            weight_percent=Decimal("12.00"),
            options_data=[
                ("Experienced Management with good succession plan", Decimal("1"), 1),
                ("Experienced Management with poor/no succession plan", Decimal("2"), 2),
                ("Inexperienced but qualified Management", Decimal("3"), 3),
                ("Management lacks both experience and relevant qualifications", Decimal("4"), 4),
                ("Caretaker Management in place - experiencing challenges", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_9,
            code="BUSINESS_LIFE_CYCLE_STAGE",
            label="Business/industry life cycle stage",
            group_label="9.1 Business/industry life cycle stage",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("Maturity stage", Decimal("1"), 1),
                ("Growth stage", Decimal("2"), 2),
                ("Birth Stage", Decimal("3"), 3),
                ("Decline stage", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_9,
            code="INDUSTRY_COMPETITIVENESS",
            label="Competitiveness of the industry",
            group_label="9.2 Competitiveness of the industry",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("Monopoly", Decimal("1"), 1),
                ("Moderate competition", Decimal("2"), 2),
                ("Strong competition", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_9,
            code="SECTOR_FUTURE_PROSPECTS",
            label="Sector future prospects",
            group_label="9.3 Sector future prospects",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("Bright prospects", Decimal("1"), 1),
                ("Normal", Decimal("2"), 2),
                ("Uncertain future", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_10,
            code="PRODUCT_RANGE_DIVERSIFICATION",
            label="Product range/diversification",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("Wide (more than 3 products)", Decimal("1"), 1),
                ("Average ( 2 products)", Decimal("2"), 2),
                ("Narrow (1 product)", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_11,
            code="SECURITY",
            label="Security",
            weight_percent=Decimal("5.00"),
            options_data=[
                (
                    "Secured with no other lenders or pari pasu with other lenders",
                    Decimal("1"),
                    1,
                ),
                ("Secured but bank ranks behind other lenders", Decimal("2"), 2),
                (
                    "Negative pledge (i.e. secured by movable assets) with strong balance sheet",
                    Decimal("3"),
                    3,
                ),
                ("Unsecured lending", Decimal("4"), 4),
            ],
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Sections, risk drivers, attributes, and options ensured for "
                "IFRS9PD-CORPORATE-001"
            )
        )
