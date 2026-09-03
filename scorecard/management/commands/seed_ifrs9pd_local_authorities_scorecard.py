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
        "Seed the IFRS9 PD Local Authorities scorecard template "
        "(IFRS9PD-LOCALAUTHORITIES-001) with sections, risk drivers, attributes, and options."
    )

    def handle(self, *args, **options):
        template, created = IFRS9ScoreSheetTemplate.objects.get_or_create(
            code="IFRS9PD-LOCALAUTHORITIES-001",
            defaults={
                "name": "IFRS9PD Scorecard - Local Authorities",
                "description": (
                    "IFRS9 PD local authorities scorecard seeded from the "
                    "'PD Scorecard -Local Authorities' worksheet."
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
                    "Created IFRS9ScoreSheetTemplate IFRS9PD-LOCALAUTHORITIES-001"
                )
            )
        else:
            if template.status != "approved":
                template.status = "approved"
                template.save(update_fields=["status"])
                self.stdout.write(
                    self.style.SUCCESS(
                        "Updated IFRS9ScoreSheetTemplate IFRS9PD-LOCALAUTHORITIES-001 "
                        "status to 'approved'"
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        "IFRS9ScoreSheetTemplate IFRS9PD-LOCALAUTHORITIES-001 already exists"
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

        # The worksheet title references "Consumer Borrowing", but the factor list
        # is clearly for local authorities, so we label the section accordingly.
        section_a = get_section("A", "Local Authorities PD Scorecard", 1)

        # The worksheet's "Maximum Possible score" values are inconsistent in a few
        # places, so the driver max_score values below follow the highest option score.
        driver_1 = create_risk_driver(
            section_a,
            code="1",
            name="Revenue Generation",
            weight_percent=Decimal("20.00"),
            max_score=Decimal("4"),
            order=1,
        )
        driver_2 = create_risk_driver(
            section_a,
            code="2",
            name="Operating Surplus",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("6"),
            order=2,
        )
        driver_3 = create_risk_driver(
            section_a,
            code="3",
            name="Financial statements",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("4"),
            order=3,
        )
        driver_4 = create_risk_driver(
            section_a,
            code="4",
            name="Liquidity",
            weight_percent=Decimal("30.00"),
            max_score=Decimal("4"),
            order=4,
        )
        driver_5 = create_risk_driver(
            section_a,
            code="5",
            name="Debt/Reserves ratio",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("4"),
            order=5,
        )
        driver_6 = create_risk_driver(
            section_a,
            code="6",
            name="Loan repayment track record",
            weight_percent=Decimal("15.00"),
            max_score=Decimal("6"),
            order=6,
        )
        driver_7 = create_risk_driver(
            section_a,
            code="7",
            name="Management",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("5"),
            order=7,
        )
        driver_8 = create_risk_driver(
            section_a,
            code="8",
            name="Governance",
            weight_percent=Decimal("10.00"),
            max_score=Decimal("3"),
            order=8,
        )
        driver_9 = create_risk_driver(
            section_a,
            code="9",
            name="Other Income generating projects",
            weight_percent=Decimal("4.00"),
            max_score=Decimal("3"),
            order=9,
        )
        driver_10 = create_risk_driver(
            section_a,
            code="10",
            name="Classification of local authority",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("4"),
            order=10,
        )
        driver_11 = create_risk_driver(
            section_a,
            code="11",
            name="Security",
            weight_percent=Decimal("4.00"),
            max_score=Decimal("5"),
            order=11,
        )

        ensure_attribute(
            driver_1,
            code="REVENUE_GENERATION",
            label="Revenue Generation",
            weight_percent=Decimal("20.00"),
            options_data=[
                ("Sustained upward trend - last three years", Decimal("1"), 1),
                ("Overall upward trend - last three years", Decimal("2"), 2),
                ("Stagnant", Decimal("3"), 3),
                ("Declining revenue - last three years", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_2,
            code="OPERATING_SURPLUS",
            label="Operating Surplus",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("Sustained upward trend", Decimal("1"), 1),
                ("Overall upward trend", Decimal("2"), 2),
                ("Stable", Decimal("3"), 3),
                ("Falling", Decimal("5"), 4),
                ("Deficit", Decimal("6"), 5),
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
                ("Not available", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_4,
            code="LIQUIDITY",
            label="Liquidity",
            weight_percent=Decimal("30.00"),
            options_data=[
                ("Strong (more than 2:1)", Decimal("1"), 1),
                ("Good (between 1.5:1 and 2:1)", Decimal("2"), 2),
                ("Weak (between 1:1 and 1.49:1)", Decimal("3"), 3),
                ("Poor (less than 1:1)", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_5,
            code="DEBT_RESERVES_RATIO",
            label="Debt/Reserves ratio",
            weight_percent=Decimal("5.00"),
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
            weight_percent=Decimal("15.00"),
            options_data=[
                ("Always repaid loans on time from primary source", Decimal("1"), 1),
                ("Delayed but eventually paid", Decimal("2"), 2),
                ("Paid after restructuring", Decimal("3"), 3),
                ("Paid through litigation", Decimal("4"), 4),
                ("Currently in arrears but not NPL", Decimal("5"), 5),
                ("NPL", Decimal("6"), 6),
            ],
        )
        ensure_attribute(
            driver_7,
            code="MANAGEMENT",
            label="Management",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("Qualified & experienced", Decimal("1"), 1),
                ("Qualified but inexperienced", Decimal("2"), 2),
                ("Unqualified but experienced", Decimal("3"), 3),
                ("Unqualified & inexperienced", Decimal("4"), 4),
                ("Non operational business", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_8,
            code="GOVERNANCE",
            label="Governance",
            weight_percent=Decimal("10.00"),
            options_data=[
                ("Autonomous decision making", Decimal("1"), 1),
                ("Semi autonomous decision making", Decimal("2"), 2),
                ("Decisions made by line ministry", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_9,
            code="OTHER_INCOME_PROJECTS",
            label="Other Income generating projects",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("Steady flow of additional profitable income generating projects", Decimal("1"), 1),
                ("Sporadic additional income generating projects", Decimal("2"), 2),
                ("No other income generating projects", Decimal("3"), 3),
            ],
        )
        # The sheet header row for this factor embeds summary values; the option rows
        # make the factor structure clearer, so the attribute below follows them.
        ensure_attribute(
            driver_10,
            code="LOCAL_AUTHORITY_CLASSIFICATION",
            label="Classification of local authority",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("City Councils", Decimal("1"), 1),
                ("Town Councils", Decimal("2"), 2),
                ("Rural District Councils", Decimal("4"), 3),
            ],
        )
        ensure_attribute(
            driver_11,
            code="SECURITY",
            label="Security",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("Full Cash cover", Decimal("1"), 1),
                ("Fully secured by immovable property with first charge", Decimal("2"), 2),
                ("Partially secured", Decimal("3"), 3),
                ("NGCB", Decimal("4"), 4),
                ("Unsecured", Decimal("5"), 5),
            ],
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Sections, risk drivers, attributes, and options ensured for "
                "IFRS9PD-LOCALAUTHORITIES-001"
            )
        )
