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
        "Seed the IFRS9 PD Farming scorecard template "
        "(IFRS9PD-FARMING-001) with sections, risk drivers, attributes, and options."
    )

    def handle(self, *args, **options):
        template, created = IFRS9ScoreSheetTemplate.objects.get_or_create(
            code="IFRS9PD-FARMING-001",
            defaults={
                "name": "IFRS9PD Scorecard - Farming",
                "description": (
                    "IFRS9 PD farming scorecard seeded from the "
                    "'PD Scorecard - Farming' worksheet."
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
                    "Created IFRS9ScoreSheetTemplate IFRS9PD-FARMING-001"
                )
            )
        else:
            if template.status != "approved":
                template.status = "approved"
                template.save(update_fields=["status"])
                self.stdout.write(
                    self.style.SUCCESS(
                        "Updated IFRS9ScoreSheetTemplate IFRS9PD-FARMING-001 "
                        "status to 'approved'"
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        "IFRS9ScoreSheetTemplate IFRS9PD-FARMING-001 already exists"
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

        section_a = get_section("A", "Farming Probability of Default Scorecard", 1)

        driver_1 = create_risk_driver(
            section_a,
            code="1",
            name="Production History",
            weight_percent=Decimal("20.00"),
            max_score=Decimal("5"),
            order=1,
        )
        driver_2 = create_risk_driver(
            section_a,
            code="2",
            name="Multibanking Status",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("4"),
            order=2,
        )
        driver_3 = create_risk_driver(
            section_a,
            code="3",
            name="Experience",
            weight_percent=Decimal("8.00"),
            max_score=Decimal("4"),
            order=3,
        )
        driver_4 = create_risk_driver(
            section_a,
            code="4",
            name="Management",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("4"),
            order=4,
        )
        driver_5 = create_risk_driver(
            section_a,
            code="5",
            name="Marketing & Pricing of Produce",
            weight_percent=Decimal("15.00"),
            max_score=Decimal("5"),
            order=5,
        )
        driver_6 = create_risk_driver(
            section_a,
            code="6",
            name="Prevalence of Side Marketing",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("4"),
            order=6,
        )
        driver_7 = create_risk_driver(
            section_a,
            code="7",
            name="Loan Repayment History",
            weight_percent=Decimal("10.00"),
            max_score=Decimal("4"),
            order=7,
        )
        driver_8 = create_risk_driver(
            section_a,
            code="8",
            name="Surplus",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("5"),
            order=8,
        )
        driver_9 = create_risk_driver(
            section_a,
            code="9",
            name="Adequacy of Farming Assets/Resources",
            weight_percent=Decimal("10.00"),
            max_score=Decimal("4"),
            order=9,
        )
        driver_10 = create_risk_driver(
            section_a,
            code="10",
            name="Collateral Security",
            weight_percent=Decimal("12.00"),
            max_score=Decimal("4"),
            order=10,
        )
        driver_11 = create_risk_driver(
            section_a,
            code="11",
            name="Succession Planning",
            weight_percent=Decimal("10.00"),
            max_score=Decimal("3"),
            order=11,
        )

        ensure_attribute(
            driver_1,
            code="PRODUCTION_HISTORY",
            label="Production History",
            weight_percent=Decimal("20.00"),
            options_data=[
                ("i)Consistently good production history at current levels", Decimal("1"), 1),
                ("ii) Stable production history under an expansion plan", Decimal("2"), 2),
                ("iii) Currently expanding but production history now compromised", Decimal("3"), 3),
                ("iv) Good but declining production history", Decimal("4"), 4),
                ("v) Poor production history", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_2,
            code="MULTIBANKING_STATUS",
            label="Multibanking Status",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("i) No multi-banking relations", Decimal("1"), 1),
                ("ii) Multi-banked but borrowing under one relation", Decimal("2"), 2),
                ("iii) Multi-banked with various borrowing relations", Decimal("3"), 3),
                ("iv) No banking history", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_3,
            code="EXPERIENCE",
            label="Experience",
            weight_percent=Decimal("8.00"),
            options_data=[
                ("i) Seasoned farmer with over 5years farming experience", Decimal("1"), 1),
                (
                    "ii) Seasoned farmer with over 2 years but less than 5 years farming experience",
                    Decimal("2"),
                    2,
                ),
                ("iii) New farmer supported by experienced management", Decimal("3"), 3),
                ("iv) New farmer with no experience & relevant support", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_4,
            code="MANAGEMENT",
            label="Management",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("i) Managed by experienced resident farmer", Decimal("1"), 1),
                ("ii) Resident farmer assisted by qualified/experienced farm manager", Decimal("2"), 2),
                (
                    "iii) Non-resident farmer with resident experienced/qualified Manager",
                    Decimal("3"),
                    3,
                ),
                ("iv) Managed by non-resident farmer who is inexperienced/qualified", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_5,
            code="MARKETING_PRICING_OF_PRODUCE",
            label="Marketing & Pricing of Produce",
            weight_percent=Decimal("15.00"),
            options_data=[
                ("I) Guaranteed market with floor price", Decimal("1"), 1),
                ("ii) Readily available marekt with floor price", Decimal("2"), 2),
                ("iii) Readily available market but price determined by market forces", Decimal("3"), 3),
                ("iv) No ready market (or unstable) but prices always good", Decimal("4"), 4),
                ("v) Both market and prices unstable", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_6,
            code="PREVALENCE_OF_SIDE_MARKETING",
            label="Prevalence of Side Marketing",
            weight_percent=Decimal("3.00"),
            options_data=[
                (
                    "i) Produce not affected by side marketing or marketed under controlled market",
                    Decimal("1"),
                    1,
                ),
                ("ii) Despite controlled market, side marketing prevalent", Decimal("2"), 2),
                (
                    "iii) No controlled market, mitigation is achievable through ring fencing sale proceeds",
                    Decimal("3"),
                    3,
                ),
                ("iv) Open market and sale proceeds can hardly be ring fenced", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_7,
            code="LOAN_REPAYMENT_HISTORY",
            label="Loan Repayment History",
            weight_percent=Decimal("10.00"),
            options_data=[
                ("i) Has ALWAYS paid loans as per contract/agreement", Decimal("1"), 1),
                ("ii) Has had restrucrured loans but eventually paid", Decimal("2"), 2),
                ("iii) Currently in default but still performing", Decimal("3"), 3),
                ("iv) In arrears which are now NPL (Bad debt)", Decimal("4"), 4),
            ],
        )
        ensure_attribute(
            driver_8,
            code="SURPLUS",
            label="Surplus",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Farmer recorded surplus in the last 2 seasons", Decimal("1"), 1),
                (
                    "ii) Farmer recorded deficit in first season but had surplus in the second season",
                    Decimal("2"),
                    2,
                ),
                ("iii) Farmer recorded reducing deficit over the last 2 seasons.", Decimal("3"), 3),
                ("iv) Farmer's had surplus then deficit", Decimal("4"), 4),
                ("v) Deficit worsened in the last 2 seasons", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_9,
            code="ADEQUACY_OF_FARMING_ASSETS",
            label="Adequacy of Farming Assets/Resources",
            weight_percent=Decimal("10.00"),
            options_data=[
                ("i) Positive asset growth in the last 2 seasons", Decimal("1"), 1),
                ("ii) Assets reduced but still adequate to support level of activity", Decimal("2"), 2),
                (
                    "iii) Assets include obsolete assets impairing adequacy of equiment and machinery",
                    Decimal("3"),
                    3,
                ),
                ("iv) Farmer is stripping assets to meet financial obligations", Decimal("4"), 4),
            ],
        )
        # Source sheet options run from 0 to 4, so the effective max option score is 4.
        ensure_attribute(
            driver_10,
            code="COLLATERAL_SECURITY",
            label="Collateral Security",
            weight_percent=Decimal("12.00"),
            options_data=[
                ("i) Tangible security adequately covering total exposure", Decimal("0"), 1),
                ("ii) Partially secured by tangible security", Decimal("1"), 2),
                ("iii) Secured by intangible security", Decimal("2"), 3),
                ("iv) Unsecured but marketable commodity", Decimal("3"), 4),
                ("v) Unsecured common commodity", Decimal("4"), 5),
            ],
        )
        ensure_attribute(
            driver_11,
            code="SUCCESSION_PLANNING",
            label="Succession Planning",
            weight_percent=Decimal("10.00"),
            options_data=[
                ("i) Clear succession plan with groomed successor", Decimal("1"), 1),
                ("ii) Succession plan in place but grooming underway", Decimal("2"), 2),
                ("iii) No succession plan or grooming underway", Decimal("3"), 3),
            ],
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Sections, risk drivers, attributes, and options ensured for "
                "IFRS9PD-FARMING-001"
            )
        )
