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
        "Seed the IFRS9 PD Schools scorecard template "
        "(IFRS9PD-SCHOOLS-001) with sections, risk drivers, attributes, and options."
    )

    def handle(self, *args, **options):
        template, created = IFRS9ScoreSheetTemplate.objects.get_or_create(
            code="IFRS9PD-SCHOOLS-001",
            defaults={
                "name": "IFRS9PD Scorecard - Schools",
                "description": (
                    "IFRS9 PD schools scorecard seeded from the "
                    "'PD Scorecard - Schools' worksheet."
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
                self.style.SUCCESS("Created IFRS9ScoreSheetTemplate IFRS9PD-SCHOOLS-001")
            )
        else:
            if template.status != "approved":
                template.status = "approved"
                template.save(update_fields=["status"])
                self.stdout.write(
                    self.style.SUCCESS(
                        "Updated IFRS9ScoreSheetTemplate IFRS9PD-SCHOOLS-001 "
                        "status to 'approved'"
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        "IFRS9ScoreSheetTemplate IFRS9PD-SCHOOLS-001 already exists"
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

        # The worksheet title says "Retail Probability of Default Scorecard", but the
        # factor list clearly defines a schools scorecard, so we name the section
        # after the actual business content.
        section_a = get_section("A", "Schools Probability of Default Scorecard", 1)

        driver_1 = create_risk_driver(
            section_a,
            code="1",
            name="Ownership/Governance",
            weight_percent=Decimal("10.00"),
            max_score=Decimal("4"),
            order=1,
        )
        driver_2 = create_risk_driver(
            section_a,
            code="2",
            name="Type of School",
            weight_percent=Decimal("10.00"),
            max_score=Decimal("2"),
            order=2,
        )
        driver_3 = create_risk_driver(
            section_a,
            code="3",
            name="Enrolment Status",
            weight_percent=Decimal("30.00"),
            max_score=Decimal("3"),
            order=3,
        )
        driver_4 = create_risk_driver(
            section_a,
            code="4",
            name="Enrolment Level/Size",
            weight_percent=Decimal("20.00"),
            max_score=Decimal("5"),
            order=4,
        )
        driver_5 = create_risk_driver(
            section_a,
            code="5",
            name="Other Income Generating Activities",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("3"),
            order=5,
        )
        driver_6 = create_risk_driver(
            section_a,
            code="6",
            name="Operating Surplus",
            weight_percent=Decimal("8.00"),
            max_score=Decimal("3"),
            order=6,
        )
        driver_7 = create_risk_driver(
            section_a,
            code="7",
            name="Loan Repayment Track Record",
            weight_percent=Decimal("10.00"),
            max_score=Decimal("5"),
            order=7,
        )
        driver_8 = create_risk_driver(
            section_a,
            code="8",
            name="Security",
            weight_percent=Decimal("7.00"),
            max_score=Decimal("3"),
            order=8,
        )

        ensure_attribute(
            driver_1,
            code="OWNERSHIP_GOVERNANCE",
            label="Ownership/Governance",
            weight_percent=Decimal("10.00"),
            options_data=[
                ("i) Government/ Local Austority", Decimal("1"), 1),
                ("ii) Mission/International", Decimal("2"), 2),
                ("iii) Trust Owned", Decimal("3"), 3),
                ("iv) Private Family Owned Schools", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_2,
            code="TYPE_OF_SCHOOL",
            label="Type of School",
            weight_percent=Decimal("10.00"),
            options_data=[
                ("i) Secondary", Decimal("1"), 1),
                ("ii) Primary", Decimal("2"), 2),
            ],
        )
        ensure_attribute(
            driver_3,
            code="ENROLMENT_STATUS",
            label="Enrolment Status",
            weight_percent=Decimal("30.00"),
            options_data=[
                ("i) Boarding", Decimal("1"), 1),
                ("ii) Mixed Enrolment", Decimal("2"), 2),
                ("iii) Day", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_4,
            code="ENROLMENT_LEVEL_SIZE",
            label="Enrolment Level/Size",
            weight_percent=Decimal("20.00"),
            options_data=[
                ("i) Over 1,000 Students", Decimal("1"), 1),
                ("ii) 500 - 999 Students", Decimal("2"), 2),
                ("iii) 300 - 499 Students", Decimal("2"), 3),
                ("iv) 100 - 299 Students", Decimal("4"), 4),
                ("v) Less than 100 Students", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_5,
            code="OTHER_INCOME_GENERATING_ACTIVITIES",
            label="Other Income Generating Activities",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("i) Consistent income generating projects", Decimal("1"), 1),
                ("ii) Income generating projects far and wide inbtween", Decimal("2"), 2),
                ("iii) No additional income generating projects", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_6,
            code="OPERATING_SURPLUS",
            label="Operating Surplus",
            weight_percent=Decimal("8.00"),
            options_data=[
                ("i) School constantly records surpluses", Decimal("1"), 1),
                ("ii) School at times records surplus", Decimal("2"), 2),
                ("iii) School operates in deficit", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_7,
            code="LOAN_REPAYMENT_TRACK_RECORD",
            label="Loan Repayment Track Record",
            weight_percent=Decimal("10.00"),
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
            driver_8,
            code="SECURITY",
            label="Security",
            weight_percent=Decimal("7.00"),
            options_data=[
                ("i) Tangible security adequately covering exposure", Decimal("1"), 1),
                ("ii) Partially covered by tangible security", Decimal("2"), 2),
                ("iii) Wholly unsecured exposure", Decimal("3"), 3),
            ],
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Sections, risk drivers, attributes, and options ensured for "
                "IFRS9PD-SCHOOLS-001"
            )
        )
