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
        "Seed the IFRS9 PD Microfinance scorecard template "
        "(IFRS9PD-MFINANCE-001) with sections, risk drivers, attributes, and options."
    )

    def handle(self, *args, **options):
        template, created = IFRS9ScoreSheetTemplate.objects.get_or_create(
            code="IFRS9PD-MFINANCE-001",
            defaults={
                "name": "IFRS9PD Scorecard - MFinance",
                "description": (
                    "IFRS9 PD microfinance scorecard seeded from the "
                    "'PD Scorecard - MFinance' worksheet."
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
                    "Created IFRS9ScoreSheetTemplate IFRS9PD-MFINANCE-001"
                )
            )
        else:
            if template.status != "approved":
                template.status = "approved"
                template.save(update_fields=["status"])
                self.stdout.write(
                    self.style.SUCCESS(
                        "Updated IFRS9ScoreSheetTemplate IFRS9PD-MFINANCE-001 "
                        "status to 'approved'"
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        "IFRS9ScoreSheetTemplate IFRS9PD-MFINANCE-001 already exists"
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

        section_a = get_section("A", "Microfinance Probability of Default Score Card", 1)

        driver_1 = create_risk_driver(
            section_a,
            code="1",
            name="Nature of Borrower/Business Activity",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("8"),
            order=1,
        )
        driver_2 = create_risk_driver(
            section_a,
            code="2",
            name="Experience (No. of years in business)",
            weight_percent=Decimal("4.00"),
            max_score=Decimal("5"),
            order=2,
        )
        driver_3 = create_risk_driver(
            section_a,
            code="3",
            name="Business and residential address status",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("4"),
            order=3,
        )
        driver_4 = create_risk_driver(
            section_a,
            code="4",
            name="Age of project promoter/proprietor/keyman",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("5"),
            order=4,
        )
        driver_5 = create_risk_driver(
            section_a,
            code="5",
            name="Succession Planning",
            weight_percent=Decimal("10.00"),
            max_score=Decimal("4"),
            order=5,
        )
        driver_6 = create_risk_driver(
            section_a,
            code="6",
            name="Borrower character (willingness to pay)",
            weight_percent=Decimal("20.00"),
            max_score=Decimal("3"),
            order=6,
        )
        driver_7 = create_risk_driver(
            section_a,
            code="7",
            name="Investment Decision Making",
            weight_percent=Decimal("6.00"),
            max_score=Decimal("5"),
            order=7,
        )
        driver_8 = create_risk_driver(
            section_a,
            code="8",
            name="Size of Loan",
            weight_percent=Decimal("8.00"),
            max_score=Decimal("6"),
            order=8,
        )
        driver_9 = create_risk_driver(
            section_a,
            code="9",
            name="Loan Tenor",
            weight_percent=Decimal("4.00"),
            max_score=Decimal("4"),
            order=9,
        )
        driver_10 = create_risk_driver(
            section_a,
            code="10",
            name="Default/Repayment History",
            weight_percent=Decimal("15.00"),
            max_score=Decimal("6"),
            order=10,
        )
        driver_11 = create_risk_driver(
            section_a,
            code="11",
            name="Diversion of Loan funds",
            weight_percent=Decimal("8.00"),
            max_score=Decimal("4"),
            order=11,
        )
        driver_12 = create_risk_driver(
            section_a,
            code="12",
            name="Alternative sources of income",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("3"),
            order=12,
        )
        driver_13 = create_risk_driver(
            section_a,
            code="13",
            name="No. of dependents",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("5"),
            order=13,
        )

        ensure_attribute(
            driver_1,
            code="NATURE_OF_BORROWER",
            label="Nature of Borrower/Business Activity",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("a) Salary based Microfinance loans (consumer loans)", Decimal("1"), 1),
                ("b) Manufacturing", Decimal("2"), 2),
                ("c) Services", Decimal("3"), 3),
                ("d) Retailing", Decimal("4"), 4),
                ("e) Cross border", Decimal("5"), 5),
                ("f) Vendors", Decimal("6"), 6),
                ("g) Agriculture", Decimal("7"), 7),
                ("h) Other", Decimal("8"), 8),
            ],
        )
        ensure_attribute(
            driver_2,
            code="BUSINESS_EXPERIENCE",
            label="Experience (No. of years in business)",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("a) More than 2 years", Decimal("1"), 1),
                ("b) more than 1 year but less than 2 years", Decimal("2"), 2),
                ("c) 6 months to 12 months", Decimal("3"), 3),
                ("d) 1 month to 6 months", Decimal("4"), 4),
                ("e) start up businesses", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_3,
            code="ADDRESS_STATUS",
            label="Business and residential address status",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("a) Fixed business premises and residential abode", Decimal("1"), 1),
                ("b) No fixed business premises but fixed residential abode", Decimal("2"), 2),
                ("c) Fixed business premises but no fixed residential abode", Decimal("3"), 3),
                ("d) No fixed business and residential abode", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_4,
            code="PROMOTER_AGE",
            label="Age of project promoter/proprietor/keyman",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("a) 36 - 45 Years", Decimal("1"), 1),
                ("b) 45 - 60 Years", Decimal("2"), 2),
                ("c) 26 - 35 Years", Decimal("3"), 3),
                ("d) 18 - 25 Years", Decimal("4"), 4),
                ("e) Above 60 Years", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_5,
            code="SUCCESSION_PLANNING",
            label="Succession Planning",
            weight_percent=Decimal("10.00"),
            options_data=[
                ("a)Formal support", Decimal("1"), 1),
                ("b) Informal family support", Decimal("2"), 2),
                ("c) Informal support by colleagues/other traders", Decimal("3"), 3),
                ("d) One man operation", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_6,
            code="BORROWER_CHARACTER",
            label="Borrower character (willingness to pay)",
            weight_percent=Decimal("20.00"),
            options_data=[
                ("a) Known sober habits and socially stable", Decimal("1"), 1),
                ("b) Unknown habits but verifiable good background", Decimal("2"), 2),
                ("c) Not of sober habits", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_7,
            code="INVESTMENT_DECISION_MAKING",
            label="Investment Decision Making",
            weight_percent=Decimal("6.00"),
            options_data=[
                ("a) Project supported professionally", Decimal("1"), 1),
                ("b) Sound investment decisions with good supervisory controls", Decimal("2"), 2),
                ("c) Investment decisions have resulted in reduced returns", Decimal("3"), 3),
                ("d) Investment decisions good but financial discipline is weak", Decimal("4"), 4),
                (
                    "e) Poor investment decisions accompanied by poor financial discipline",
                    Decimal("5"),
                    5,
                ),
            ],
        )
        ensure_attribute(
            driver_8,
            code="SIZE_OF_LOAN",
            label="Size of Loan",
            weight_percent=Decimal("8.00"),
            options_data=[
                ("a) Up to $100", Decimal("1"), 1),
                ("b) $101 - $300", Decimal("2"), 2),
                ("c) $301 - $500", Decimal("3"), 3),
                ("d) $501 - $700", Decimal("4"), 4),
                ("e) $701 - $1,000", Decimal("5"), 5),
                ("f) Above $1,000", Decimal("6"), 6),
            ],
        )
        ensure_attribute(
            driver_9,
            code="LOAN_TENOR",
            label="Loan Tenor",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("a) Up to 30 days", Decimal("1"), 1),
                ("b) 31 - 90 Days", Decimal("2"), 2),
                ("c) 91 - 180 days", Decimal("3"), 3),
                ("d) Above 180 days", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_10,
            code="DEFAULT_REPAYMENT_HISTORY",
            label="Default/Repayment History",
            weight_percent=Decimal("15.00"),
            options_data=[
                ("a) Always paid loans as per agreement and up to date", Decimal("1"), 1),
                ("b)Currently no arreas but running facility was restructured", Decimal("2"), 2),
                ("c) Currently no arrears but has default history", Decimal("3"), 3),
                ("d) Currently in arrears but arrears within 30 days overdue", Decimal("4"), 4),
                (
                    "e) Currently in arrears - more than 30 days but less than 90 days",
                    Decimal("5"),
                    5,
                ),
                ("f) Currently in arrears - more than 90 days overdue (NPL)", Decimal("6"), 6),
            ],
        )
        ensure_attribute(
            driver_11,
            code="DIVERSION_OF_LOAN_FUNDS",
            label="Diversion of Loan funds",
            weight_percent=Decimal("8.00"),
            options_data=[
                ("a) No history of diversion of funds", Decimal("1"), 1),
                ("b) Diverted but due to emergency", Decimal("2"), 2),
                ("c) Diverted once but reformed", Decimal("3"), 3),
                ("d) Habitually diverts borrowed funds", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_12,
            code="ALTERNATIVE_INCOME_SOURCES",
            label="Alternative sources of income",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("a) Receives regular alternative income", Decimal("1"), 1),
                ("b) Receives irregular alternative income", Decimal("2"), 2),
                ("c) No other alternative sources of income", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_13,
            code="NUMBER_OF_DEPENDENTS",
            label="No. of dependents",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("a) None", Decimal("1"), 1),
                ("b) 1 Dependant", Decimal("2"), 2),
                ("c) 2 - 3 Dependant", Decimal("3"), 3),
                ("d) 4 - 6 Dependant", Decimal("4"), 4),
                ("e) 6 or more Dependants", Decimal("5"), 5),
            ],
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Sections, risk drivers, attributes, and options ensured for "
                "IFRS9PD-MFINANCE-001"
            )
        )
