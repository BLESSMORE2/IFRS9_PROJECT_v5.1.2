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
    help = "Seed the Basel Farmers Credit Scoresheet template (AFCSC1-17) with sections, risk drivers and grade bands."

    def handle(self, *args, **options):
        template, created = BaselScoreSheetTemplate.objects.get_or_create(
            code="AFCSC1-17",
            defaults={
                "name": "Farmers Credit Scoresheet",
                "description": "Basel II farmers credit scoresheet as per AFCSC1-17.",
                "version": "1.0",
                "is_active": True,
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS(
                "Created BaselScoreSheetTemplate AFCSC1-17"))
        else:
            self.stdout.write(self.style.WARNING(
                "BaselScoreSheetTemplate AFCSC1-17 already exists"))

        # Helper to create or get a section
        def get_section(code: str, name: str, order: int) -> Section:
            section, _ = Section.objects.get_or_create(
                template=template,
                code=code,
                defaults={"name": name, "display_order": order},
            )
            return section

        # Helper to create or get a risk driver
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
            driver.name = name
            driver.weight_percent = weight_percent
            driver.max_score = max_score
            driver.display_order = order
            driver.save(
                update_fields=["name", "weight_percent", "max_score", "display_order"]
            )
            return driver

        # Sections
        section_a = get_section("A", "Customer Demographics", 1)
        section_b = get_section("B", "Production & Marketing", 2)
        section_c = get_section("C", "Financial Indicators", 3)
        section_d = get_section("D", "Credit Indicators / History", 4)

        # Section A: Customer Demographics (total 21%)
        driver_1 = create_risk_driver(
            section_a,
            code="1",
            name="Farm Location and Activities according to region",
            weight_percent=Decimal("10.00"),
            max_score=Decimal("10"),
            order=1,
        )
        driver_2 = create_risk_driver(
            section_a,
            code="2",
            name="Land Tenure",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("5"),
            order=2,
        )
        driver_3 = create_risk_driver(
            section_a,
            code="3",
            name="Expert knowledge / expertise of farmer",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("5"),
            order=3,
        )
        driver_4 = create_risk_driver(
            section_a,
            code="4",
            name="Management Style",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("5"),
            order=4,
        )
        driver_5 = create_risk_driver(
            section_a,
            code="5",
            name="Banking History",
            weight_percent=Decimal("2.00"),
            max_score=Decimal("5"),
            order=5,
        )

        # Section B: Production & Marketing (total 37%)
        driver_6a = create_risk_driver(
            section_b,
            code="6a",
            name="No. of years in farming",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("5"),
            order=1,
        )
        driver_6b = create_risk_driver(
            section_b,
            code="6b",
            name="Production History - Yields (past 3 seasons)",
            weight_percent=Decimal("12.00"),
            max_score=Decimal("20"),
            order=2,
        )
        driver_6c = create_risk_driver(
            section_b,
            code="6c",
            name="Production History - market deliveries (past 3 seasons)",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("5"),
            order=3,
        )
        driver_7 = create_risk_driver(
            section_b,
            code="7",
            name="Farming Technique",
            weight_percent=Decimal("7.00"),
            max_score=Decimal("10"),
            order=4,
        )
        driver_8a = create_risk_driver(
            section_b,
            code="8a",
            name="Machinery & Equipment - Adequacy and state of repair",
            weight_percent=Decimal("6.00"),
            max_score=Decimal("10"),
            order=5,
        )
        driver_8b = create_risk_driver(
            section_b,
            code="8b",
            name="Machinery & Equipment - Mechanization (efficiency of operations)",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("5"),
            order=6,
        )
        driver_8c = create_risk_driver(
            section_b,
            code="8c",
            name="Machinery & Equipment - Labour force",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("5"),
            order=7,
        )

        # Section C: Financial Indicators (total 15%)
        driver_9 = create_risk_driver(
            section_c,
            code="9",
            name="Profitability / Trading Surplus",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("8"),
            order=1,
        )
        driver_10 = create_risk_driver(
            section_c,
            code="10",
            name="Cashflows",
            weight_percent=Decimal("6.00"),
            max_score=Decimal("10"),
            order=2,
        )
        driver_11 = create_risk_driver(
            section_c,
            code="11",
            name="Financing Strategies",
            weight_percent=Decimal("4.00"),
            max_score=Decimal("7"),
            order=3,
        )

        # Section D: Credit Indicators / History (total 27%)
        driver_12 = create_risk_driver(
            section_d,
            code="12",
            name="Loan Tenor",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("5"),
            order=1,
        )
        driver_13 = create_risk_driver(
            section_d,
            code="13",
            name="Purpose of Loan",
            weight_percent=Decimal("3.00"),
            max_score=Decimal("5"),
            order=2,
        )
        driver_14 = create_risk_driver(
            section_d,
            code="14",
            name="Repayment History",
            weight_percent=Decimal("15.00"),
            max_score=Decimal("25"),
            order=3,
        )
        driver_15 = create_risk_driver(
            section_d,
            code="15",
            name="Collateral Security",
            weight_percent=Decimal("6.00"),
            max_score=Decimal("10"),
            order=4,
        )

        self.stdout.write(self.style.SUCCESS(
            "Sections and risk drivers ensured for AFCSC1-17"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 1:
        # "Farm Location and Activities according to region"
        # ------------------------------------------------------------------
        # Helper to create attribute + options
        def ensure_region_attribute(
            driver: RiskDriver,
            code: str,
            label: str,
            options: list[tuple[str, Decimal, int]],
        ) -> None:
            """
            options: list of tuples (option_label, allocated_score, display_order)
            """

            attr, _ = Attribute.objects.get_or_create(
                risk_driver=driver,
                code=code,
                defaults={
                    "label": label,
                    "data_type": "choice",
                    "input_type": "radio",
                    "is_required": True,
                    "display_order": len(
                        Attribute.objects.filter(risk_driver=driver)
                    )
                    + 1,
                    "weight_percent": driver.weight_percent,
                },
            )
            attr.label = label
            attr.data_type = "choice"
            attr.input_type = "radio"
            attr.is_required = True
            attr.weight_percent = driver.weight_percent
            if not attr.display_order:
                attr.display_order = len(Attribute.objects.filter(risk_driver=driver))
            attr.save(
                update_fields=[
                    "label",
                    "data_type",
                    "input_type",
                    "is_required",
                    "weight_percent",
                    "display_order",
                ]
            )

            for opt_label, score, order in options:
                Option.objects.get_or_create(
                    attribute=attr,
                    display_order=order,
                    label=opt_label,
                    defaults={
                        "value": opt_label,
                        "allocated_score": score,
                    },
                )

        # Natural Region I
        ensure_region_attribute(
            driver_1,
            code="1. Farm Location and Activities",
            label="1. Farm Location and Activities according to region ",
            options=[
                (
                    "(a) Natural Region I--- Intensive Mixed Farming (dairy, cash crops, tea, coffee, fruits, potatoes, pears, etc)",
                    Decimal("10"),
                    1,
                ),
                (
                    "(a) Natural Region I--- Plantation/Forestry/Fruits/Flowers (e.g. proteas)",
                    Decimal("9"),
                    1,
                ),
                (
                    "(a) Natural Region I--- Intensive Livestock farming",
                    Decimal("8"),
                    1,
                ),
                (
                    "(a) Natural Region I--- Crop Production - (maize, tobacco, soya beans, wheat)",
                    Decimal("6"),
                    1,
                ),
                (
                    "(a) Natural Region I--- Other Crops/farming activities",
                    Decimal("4"),
                    1,
                ),
                (
                    "",
                    Decimal("0"),
                    1,
                ),
                ("(b) Natural Region II--Mixed Farming", Decimal("10"), 2),
                (
                    "(b) Natural Region II--Suitable crop production (tobacco, maize, soya beans, wheat, barley, cash crops, cotton)",
                    Decimal("9"),
                    2,
                ),
                ("(b) Natural Region II--Livestock farming (including dairy)",
                 Decimal("7"), 2),
                ("(b) Natural Region II--Plantation", Decimal("5"), 2),
                ("(b) Natural Region II--Other crops/farming activities", Decimal("4"), 2),

                (
                    "",
                    Decimal("0"),
                    2,
                ),

                ("(c) Natural Region II---Mixed Farming", Decimal("10"), 3),
                (
                    "(c) Natural Region II---Irrigated suitable crop production (tobacco, maize, soya beans, wheat, barley, cash crops, cotton, citrus)",
                    Decimal("9"),
                    3,
                ),
                ("(c) Natural Region II--- Livestock farming (including dairy)",
                 Decimal("8"), 3),
                (
                    "(c) Natural Region II--- Dryland crop production of suitable crops & varieties",
                    Decimal("6"),
                    3,
                ),
                ("(c) Natural Region II--- Other crops/farming activities", Decimal("4"), 3),
                (
                    "",
                    Decimal("0"),
                    3,
                ),

                ("(d) Natural Region III--- Mixed Farming", Decimal("10"), 4),
                (
                    "(d) Natural Region III--- Suitable irrigated crop production (cash crops, small grain crops, cotton, sugarcane)",
                    Decimal("8"),
                    4,
                ),
                (
                    "(d) Natural Region III--- Dryland drought tolerant suitable crops and fodder",
                    Decimal("6"),
                    4,
                ),
                ("(d) Natural Region III--- Livestock Farming (cattle ranching)",
                 Decimal("5"), 4),
                ("(d) Natural Region III--- Other crops/farming activities",
                 Decimal("3"), 4),

                 (
                    "",
                    Decimal("0"),
                    4,
                ),

                (
                    "(e) Natural Region IV--- Livestock Farming (cattle ranching and wildlife)",
                    Decimal("10"),
                    5,
                ),
                (
                    "(e) Natural Region IV--- Irrigated suitable crops (such as sugarcane) and fodder crops",
                    Decimal("7"),
                    5,
                ),
                ("(e) Natural Region IV--- Forestry (timber)", Decimal("6"), 5),
                (
                    "Suitable drought tolerant dryland varieties (pearl millet, finger millet, sorghum, cotton)",
                    Decimal("4"),
                    5,
                ),
                ("(e) Natural Region IV--- Other crops/farming activities", Decimal("2"), 5),

(
                    "",
                    Decimal("0"),
                    5,
                ),

                (
                    "(f) Natural Region V--- Livestock farming in tsetsefly free areas (cattle ranching & wildlife)",
                    Decimal("10"),
                    6,
                ),
                ("(f) Natural Region V--- Forestry (timber)", Decimal("6"), 6),
                ("(f) Natural Region V--- Other farming activities", Decimal("1"), 6),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 1 (Farm Location and Activities)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 2: "Land Tenure"
        # ------------------------------------------------------------------
        def ensure_simple_attribute(
            driver: RiskDriver,
            code: str,
            label: str,
            options: list[tuple[str, Decimal, int]],
        ) -> None:
            """
            Helper for simple attributes (no group_label).
            options: list of tuples (option_label, allocated_score, display_order)
            """
            attr, _ = Attribute.objects.get_or_create(
                risk_driver=driver,
                code=code,
                defaults={
                    "label": label,
                    "data_type": "choice",
                    "input_type": "radio",
                    "is_required": True,
                    "display_order": len(Attribute.objects.filter(risk_driver=driver)) + 1,
                    "weight_percent": driver.weight_percent,
                },
            )
            attr.label = label
            attr.data_type = "choice"
            attr.input_type = "radio"
            attr.is_required = True
            attr.weight_percent = driver.weight_percent
            if not attr.display_order:
                attr.display_order = len(Attribute.objects.filter(risk_driver=driver))
            attr.save(
                update_fields=[
                    "label",
                    "data_type",
                    "input_type",
                    "is_required",
                    "weight_percent",
                    "display_order",
                ]
            )

            for opt_label, score, order in options:
                Option.objects.get_or_create(
                    attribute=attr,
                    display_order=order,
                    label=opt_label,
                    defaults={
                        "value": opt_label,
                        "allocated_score": score,
                    },
                )

        # Land Tenure
        ensure_simple_attribute(
            driver_2,
            code="LAND_TENURE",
            label="Land Tenure",
            options=[
                ("Undesignated own farm with title", Decimal("5"), 1),
                ("Peri urban/Urban intensive farming - greenhouse", Decimal("4"), 2),
                ("A2 Farm with own offer letter", Decimal("3"), 3),
                ("A1 Farm with own offer letter", Decimal("2"), 4),
                ("Other - e.g. resettlement areas, rural/tribal lands", Decimal("1"), 5),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 2 (Land Tenure)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 3: "Expert knowledge / expertise of farmer"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_3,
            code="EXPERT_KNOWLEDGE",
            label="Expert knowledge / expertise of farmer",
            options=[
                ("Fully experienced farmer with relevant professional training",
                 Decimal("5"), 1),
                ("On the job training experience supported by professional farming managers", Decimal(
                    "4"), 2),
                ("Formal relevant professional education supported with experienced managers", Decimal(
                    "3"), 3),
                ("Relies on hired relevant skills and experience", Decimal("2"), 4),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 3 (Expert knowledge / expertise of farmer)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 4: "Management Style"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_4,
            code="MANAGEMENT_STYLE",
            label="Management Style",
            options=[
                ("Full time resident farmer", Decimal("5"), 1),
                ("Part time resident farmer", Decimal("3"), 2),
                ("Full time non-resident farmer", Decimal("2"), 3),
                ("Part time non-resident farmer", Decimal("1"), 4),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 4 (Management Style)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 5: "Banking History"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_5,
            code="BANKING_HISTORY",
            label="Banking History",
            options=[
                ("Total Banking with a single bank", Decimal("5"), 1),
                ("Borrows from one bank only but has other deposit accounts elsewhere", Decimal(
                    "2"), 2),
                ("Multi-banked with borrowing relations in more than 1 bank",
                 Decimal("1"), 3),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 5 (Banking History)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 6a: "No. of years in farming"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_6a,
            code="YEARS_FARMING",
            label="No. of years in farming",
            options=[
                ("Over 5 years", Decimal("5"), 1),
                ("4 - 5 Years", Decimal("4"), 2),
                ("3 - 4 Years", Decimal("3"), 3),
                ("2 - 3 Years", Decimal("2"), 4),
                ("Less than 2 Years", Decimal("1"), 5),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 6a (No. of years in farming)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 6b: "Production History - Yields (past 3 seasons)"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_6b,
            code="YIELDS_HISTORY",
            label="Production History - Yields (past 3 seasons)",
            options=[
                ("High - above expected standard yield given crop, variety & region",
                 Decimal("20"), 1),
                ("Medium - within average of expected standard yield given crop, variety and region", Decimal("15"), 2),
                ("Fluctuating but within expected yield given crop, variety and region", Decimal(
                    "10"), 3),
                ("Break-even yield", Decimal("5"), 4),
                ("Static yields", Decimal("3"), 5),
                ("Low and below expected standard yields given crop, variety & region", Decimal(
                    "0"), 6),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 6b (Production History - Yields)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 6c: "Production History - market deliveries (past 3 seasons)"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_6c,
            code="DELIVERIES_HISTORY",
            label="Production History - market deliveries (past 3 seasons)",
            options=[
                ("Deliveries consistent with production", Decimal("5"), 1),
                ("Deliveries to market inconsistent with production but covered loans", Decimal(
                    "3"), 2),
                ("Deliveries to market declining", Decimal("2"), 3),
                ("Erratic deliveries suggesting side marketing", Decimal("0"), 4),
                ("No deliveries in the last three seasons", Decimal("-5"), 5),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 6c (Production History - market deliveries)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 7: "Farming Technique"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_7,
            code="FARMING_TECHNIQUE",
            label="Farming Technique",
            options=[
                ("100% irrigated farming (functional)/Non Cropping Farming e.g. cattle ranching, poultry, piggery, forestry, etc", Decimal("10"), 1),
                ("Partially irrigated farming but dominated by irrigation", Decimal("8"), 2),
                ("Dryland farming but region receives consistent reliable rains",
                 Decimal("6"), 3),
                ("Partially irrigated but dominated by dryland", Decimal("3"), 4),
                ("Dryland farming or dysfunctional irrigation", Decimal("1"), 5),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 7 (Farming Technique)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 8a: "Machinery & Equipment - Adequacy and state of repair"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_8a,
            code="MACHINERY_ADEQUACY",
            label="Machinery & Equipment - Adequacy and state of repair",
            options=[
                ("adequate and operational", Decimal("10"), 1),
                ("adequate but frequent breakdowns and requires replacement",
                 Decimal("7"), 2),
                ("inadequate but operational and hiring", Decimal("5"), 3),
                ("Inadequate, frequently breaking down and requires replacement",
                 Decimal("2"), 4),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 8a (Machinery & Equipment - Adequacy and state of repair)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 8b: "Machinery & Equipment - Mechanization (efficiency of operations)"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_8b,
            code="MECHANIZATION",
            label="Machinery & Equipment - Mechanization (efficiency of operations)",
            options=[
                ("Fully mechanized operations (capital intensive)", Decimal("5"), 1),
                ("Partially mechanized (most operations)", Decimal("4"), 2),
                ("Labour intensive with minimum challenges", Decimal("2"), 3),
                ("Level of mechanization not sufficient to support farming activity", Decimal(
                    "0"), 4),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 8b (Machinery & Equipment - Mechanization)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 8c: "Machinery & Equipment - Labour force"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_8c,
            code="LABOUR_FORCE",
            label="Machinery & Equipment - Labour force",
            options=[
                ("Skills and quantity resident at the farm", Decimal("5"), 1),
                ("Skills and quantity not resident but readily available", Decimal("4"), 2),
                ("Labour is seasonal", Decimal("2"), 3),
                ("Right skills and quantity of manpower insufficient", Decimal("0"), 4),
                ("Farmer facing skills and labour flight", Decimal("-2"), 5),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 8c (Machinery & Equipment - Labour force)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 9: "Profitability / Trading Surplus"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_9,
            code="PROFITABILITY",
            label="Profitability / Trading Surplus",
            options=[
                ("Profitable farming operations", Decimal("8"), 1),
                ("Loss making operations but improving", Decimal("5"), 2),
                ("Loss making operations and worsening", Decimal("0"), 3),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 9 (Profitability / Trading Surplus)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 10: "Cashflows"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_10,
            code="CASHFLOWS",
            label="Cashflows",
            options=[
                ("Farmer operating with surplus cashflows", Decimal("10"), 1),
                ("Cashflows only meeting operations", Decimal("8"), 2),
                ("Farmer operating with negative cashflows but supplements from other businesses", Decimal(
                    "5"), 3),
                ("Farmer operating with negative cashflows and heavily borrowed",
                 Decimal("0"), 4),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 10 (Cashflows)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 11: "Financing Strategies"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_11,
            code="FINANCING_STRATEGIES",
            label="Financing Strategies",
            options=[
                ("Funding from own resources with minimum borrowing", Decimal("7"), 1),
                ("Blended funding - bank loans and own resources", Decimal("5"), 2),
                ("Bank loans only", Decimal("4"), 3),
                ("Contract farming and only borrows for small working capital needs", Decimal(
                    "3"), 4),
                ("Mixed strategies including credit facilities from suppliers of inputs", Decimal(
                    "2"), 5),
                ("Other - specify them", Decimal("1"), 6),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 11 (Financing Strategies)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 12: "Loan Tenor"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_12,
            code="LOAN_TENOR",
            label="Loan Tenor",
            options=[
                ("1 Year", Decimal("5"), 1),
                ("2 Years", Decimal("4"), 2),
                ("3 Years", Decimal("3"), 3),
                ("4 Years", Decimal("2"), 4),
                ("More than 4 Years", Decimal("1"), 5),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 12 (Loan Tenor)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 13: "Purpose of Loan"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_13,
            code="PURPOSE_OF_LOAN",
            label="Purpose of Loan",
            options=[
                ("Working Capital", Decimal("5"), 1),
                ("Irrigation Development", Decimal("4"), 2),
                ("Capital Expenditure - acquisition of equipment", Decimal("3"), 3),
                ("Capital expenditure - expansion", Decimal("1"), 4),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 13 (Purpose of Loan)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 14: "Repayment History"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_14,
            code="REPAYMENT_HISTORY",
            label="Repayment History",
            options=[
                ("Previous credit facilities paid on time and in full", Decimal("25"), 1),
                ("Piecemeal settlement of previous facilities", Decimal("10"), 2),
                ("Previous credit facilities paid after restructuring", Decimal("5"), 3),
                ("Defaulted on previous loans and paid through litigation", Decimal("0"), 4),
                ("Currently in arrears", Decimal("-3"), 5),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 14 (Repayment History)"))

        # ------------------------------------------------------------------
        # Attributes & Options for Risk Driver 15: "Collateral Security"
        # ------------------------------------------------------------------
        ensure_simple_attribute(
            driver_15,
            code="COLLATERAL_SECURITY",
            label="Collateral Security",
            options=[
                ("Tangible security value is above 140% of loan amount or cash cover", Decimal(
                    "10"), 1),
                ("Tangible security value is less than 100% but more than 50% of loan amount", Decimal(
                    "5"), 2),
                ("Collateral security are movable assets (NGCB)", Decimal("2"), 3),
                ("No collateral security", Decimal("0"), 4),
            ],
        )

        self.stdout.write(self.style.SUCCESS(
            "Attributes and options seeded for Risk Driver 15 (Collateral Security)"))

        # Grade bands
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

        self.stdout.write(self.style.SUCCESS(
            "Grade bands ensured for AFCSC1-17"))
