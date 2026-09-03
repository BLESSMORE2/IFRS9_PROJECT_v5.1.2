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
        "Seed the IFRS9 PD Consumer Loans scorecard template "
        "(IFRS9PD-CONSUMER-001) with sections, risk drivers, attributes, and options."
    )

    def handle(self, *args, **options):
        template, created = IFRS9ScoreSheetTemplate.objects.get_or_create(
            code="IFRS9PD-CONSUMER-001",
            defaults={
                "name": "IFRS9PD Scorecard - Consumer Loans",
                "description": (
                    "IFRS9 PD scorecard for salary based consumer borrowing "
                    "seeded from the 'PD Scorecrad - Consumer Loans' worksheet."
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
                    "Created IFRS9ScoreSheetTemplate IFRS9PD-CONSUMER-001"
                )
            )
        else:
            if template.status != "approved":
                template.status = "approved"
                template.save(update_fields=["status"])
                self.stdout.write(
                    self.style.SUCCESS(
                        "Updated IFRS9ScoreSheetTemplate IFRS9PD-CONSUMER-001 "
                        "status to 'approved'"
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        "IFRS9ScoreSheetTemplate IFRS9PD-CONSUMER-001 already exists"
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

        section_a = get_section("A", "Salary Based Consumer Borrowing", 1)

        driver_1 = create_risk_driver(
            section_a,
            code="1",
            name="Personal Demographics",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("33"),
            order=1,
        )
        driver_2 = create_risk_driver(
            section_a,
            code="2",
            name="Residential Demographics",
            weight_percent=Decimal("5.00"),
            max_score=Decimal("26"),
            order=2,
        )
        driver_3 = create_risk_driver(
            section_a,
            code="3",
            name="Income",
            weight_percent=Decimal("30.00"),
            max_score=Decimal("45"),
            order=3,
        )
        driver_4 = create_risk_driver(
            section_a,
            code="4",
            name="Employment History",
            weight_percent=Decimal("15.00"),
            max_score=Decimal("40"),
            order=4,
        )
        driver_5 = create_risk_driver(
            section_a,
            code="5",
            name="Banking & Credit History",
            weight_percent=Decimal("25.00"),
            max_score=Decimal("95"),
            order=5,
        )
        driver_6 = create_risk_driver(
            section_a,
            code="6",
            name="Expected Economic Environmental Changes",
            weight_percent=Decimal("20.00"),
            max_score=Decimal("82"),
            order=6,
        )

        ensure_attribute(
            driver_1,
            code="GENDER",
            label="Gender",
            group_label="a) Gender",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Female", Decimal("1"), 1),
                ("ii) Male", Decimal("2"), 2),
            ],
        )
        ensure_attribute(
            driver_1,
            code="AGE",
            label="Age",
            group_label="b) Age",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) 41 - 60 Yrs", Decimal("1"), 1),
                ("ii) 31 - 40 Yrs", Decimal("2"), 2),
                ("iii) 26 - 30 Years", Decimal("3"), 3),
                ("iv) Above 60 Yrs", Decimal("4"), 4),
                ("v) 18 - 25 Yrs", Decimal("6"), 5),
            ],
        )
        ensure_attribute(
            driver_1,
            code="MARITAL_STATUS",
            label="Marital Status",
            group_label="c) Marital Status",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Maried", Decimal("1"), 1),
                ("ii) Widowed", Decimal("3"), 2),
                ("iii) Divorced", Decimal("5"), 3),
                ("iv) Separated", Decimal("7"), 4),
                ("v) Single", Decimal("8"), 5),
                ("vi) Other", Decimal("10"), 6),
            ],
        )
        ensure_attribute(
            driver_1,
            code="DEPENDANTS",
            label="No. of dependants",
            group_label="d) No. of dependants",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) None", Decimal("0"), 1),
                ("ii) 1 Dependant", Decimal("2"), 2),
                ("iii) 2 - 3 Dependant", Decimal("3"), 3),
                ("iv) 4 - 6 Dependant", Decimal("4"), 4),
                ("v) 6 or more Dependants", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_1,
            code="EDUCATION_LEVEL",
            label="Educational Level attained",
            group_label="e) Educational Level attained",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Degreed", Decimal("1"), 1),
                ("ii) Diploma", Decimal("3"), 2),
                ("iii) Certificate", Decimal("5"), 3),
                ("iv) A or O Level Education", Decimal("7"), 4),
                ("v) Below secondary Education", Decimal("10"), 5),
            ],
        )

        ensure_attribute(
            driver_2,
            code="CITIZENSHIP_RESIDENCE_STATUS",
            label="Citizenship/Residence Status",
            group_label="a) Citizenship/Residence Status",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Permanent Residence", Decimal("1"), 1),
                ("ii) Temporary Residence", Decimal("2"), 2),
                ("iii) Non- Resident", Decimal("3"), 3),
            ],
        )
        ensure_attribute(
            driver_2,
            code="RESIDENTIAL_STATUS",
            label="Residential Status",
            group_label="b) Residential Status",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Owner Freehold", Decimal("1"), 1),
                ("ii) Owner Mortgaged", Decimal("2"), 2),
                ("iii) Rent to buy", Decimal("3"), 3),
                ("iv) Company House", Decimal("4"), 4),
                ("v) Rented", Decimal("5"), 5),
                ("vi) Living with Parents", Decimal("6"), 6),
                ("vii) Other - Specify", Decimal("7"), 7),
            ],
        )
        ensure_attribute(
            driver_2,
            code="RESIDENCE_LOCATION",
            label="Location of residence",
            group_label="c)Location of residence",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Peri-Urban Plot", Decimal("1"), 1),
                ("ii) Low density", Decimal("3"), 2),
                ("iii) Medium Density", Decimal("5"), 3),
                ("iv) High Density", Decimal("6"), 4),
                ("v) Farming Land/Rural", Decimal("8"), 5),
                ("vi) Other", Decimal("10"), 6),
            ],
        )
        ensure_attribute(
            driver_2,
            code="TIME_AT_CURRENT_ADDRESS",
            label="Time at Current Address",
            group_label="d) Time at Current Address",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) More than 5 Years", Decimal("1"), 1),
                ("ii) 4 - 5 Years", Decimal("2"), 2),
                ("iii) 3 - 4 Years", Decimal("3"), 3),
                ("iv) 2 - 3 Years", Decimal("4"), 4),
                ("v) 1 - 2 Years", Decimal("5"), 5),
                ("vi) Below 1 Year", Decimal("6"), 6),
            ],
        )

        ensure_attribute(
            driver_3,
            code="NET_MONTHLY_INCOME",
            label="Net Monthly Income Levels",
            group_label="a) Net Monthly Income Levels",
            weight_percent=Decimal("20.00"),
            options_data=[
                ("i) Over $5,000", Decimal("1"), 1),
                ("ii) $1,001 - $5,000", Decimal("5"), 2),
                ("iii) $501 - $1,000", Decimal("10"), 3),
                ("iv) $251 - $500", Decimal("15"), 4),
                ("v) $250 and Below", Decimal("20"), 5),
            ],
        )
        # The worksheet's "worst possible score" row for income streams is mislabeled
        # as "iii) $501 - $1,000" with score 25. We preserve the actual option rows above
        # and use the highest option score as the effective worst score in the app.
        ensure_attribute(
            driver_3,
            code="INCOME_STREAMS",
            label="Income streams",
            group_label="b) Income streams",
            weight_percent=Decimal("10.00"),
            options_data=[
                ("i) Salary + other incomes", Decimal("1"), 1),
                ("ii) Salary income only", Decimal("5"), 2),
                ("iii)Non salary income -e.g. investments", Decimal("10"), 3),
                ("iv) Pension only", Decimal("20"), 4),
                ("v) Alimony, grants, social welfare grants, allowances etc", Decimal("25"), 5),
            ],
        )

        # Preserve the source sheet display order, which lists a), c), b), d).
        ensure_attribute(
            driver_4,
            code="OCCUPATIONAL_STATUS",
            label="Occupational/Professional Status",
            group_label="a) Occupational/Professional Status",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Salaried employee", Decimal("1"), 1),
                ("ii) Career Investor with regular monthly returns", Decimal("3"), 2),
                ("iii) Business Owner", Decimal("5"), 3),
                ("iv) Consultant", Decimal("6"), 4),
                ("v) Commission Worker", Decimal("7"), 5),
                ("vi) Informal trader", Decimal("9"), 6),
                ("vii) Pensioner/Retired", Decimal("10"), 7),
            ],
        )
        ensure_attribute(
            driver_4,
            code="TYPE_OF_EMPLOYER",
            label="Type of Employer",
            group_label="c) Type of Employer",
            weight_percent=Decimal("8.00"),
            options_data=[
                ("i) Parastatals & State Owned Enterprises", Decimal("1"), 1),
                ("ii) State - (Government departments and ministries)", Decimal("3"), 2),
                ("iii) Private Sector", Decimal("5"), 3),
                ("iv) Farmers & Other SMEs", Decimal("10"), 4),
                ("v) Self-Employed", Decimal("15"), 5),
            ],
        )
        ensure_attribute(
            driver_4,
            code="TIME_WITH_CURRENT_EMPLOYER",
            label="Time with current employer",
            group_label="b) Time with current employer",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("i) More than 5 Years", Decimal("1"), 1),
                ("ii) 3 - 5 Years", Decimal("3"), 2),
                ("iii) 1 - 3 Years", Decimal("5"), 3),
                ("iv) Less than 1 Year", Decimal("8"), 4),
            ],
        )
        ensure_attribute(
            driver_4,
            code="PROFESSIONAL_MOBILITY",
            label="Professional Mobility",
            group_label="d) Professional Mobility",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Low", Decimal("2"), 1),
                ("ii) Moderate", Decimal("5"), 2),
                ("iii) High", Decimal("7"), 3),
            ],
        )

        ensure_attribute(
            driver_5,
            code="TIME_WITH_BANK",
            label="Time with the bank",
            group_label="a) Time with the bank",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("i) More than 2 Years", Decimal("1"), 1),
                ("ii) 1 - 2 Years", Decimal("2"), 2),
                ("iii) 6 months - 1 Year", Decimal("3"), 3),
                ("iv) 1 - 6 months", Decimal("4"), 4),
                ("v) New account", Decimal("5"), 5),
            ],
        )
        ensure_attribute(
            driver_5,
            code="LOAN_AMOUNT",
            label="Loan Amount",
            group_label="b) Loan Amount",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("i) Below $1,000", Decimal("2"), 1),
                ("ii) $1,001 - $3,000", Decimal("4"), 2),
                ("iii) $3,001 - $5,000", Decimal("5"), 3),
                ("iv) $5,001 - $10,000", Decimal("8"), 4),
                ("v) Over $10,000", Decimal("10"), 5),
            ],
        )
        ensure_attribute(
            driver_5,
            code="LOAN_TENOR",
            label="Loan Tenor",
            group_label="c) Loan Tenor",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Below 12 months", Decimal("1"), 1),
                ("ii) 12 - 24 months", Decimal("3"), 2),
                ("iii) 24 - 36 months", Decimal("5"), 3),
                ("iv) 36 - 48 months", Decimal("10"), 4),
                ("v) More than 48 months", Decimal("15"), 5),
            ],
        )
        ensure_attribute(
            driver_5,
            code="LOAN_SECURITY",
            label="Loan security",
            group_label="d) Loan security",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) 100% Cash cover", Decimal("0"), 1),
                ("ii) Full tangible security", Decimal("3"), 2),
                ("iii) Partially secured", Decimal("5"), 3),
                (
                    "iv) Other forms of security - e.g. guarantor, shares, cession of insurance policies, etc",
                    Decimal("7"),
                    4,
                ),
                ("v) No security - below callateral threshold", Decimal("10"), 5),
                ("vi) Unsecured but insured against death and incapacitation", Decimal("15"), 6),
                ("vii) Unsecured but above collateral threshold", Decimal("20"), 7),
            ],
        )
        ensure_attribute(
            driver_5,
            code="DEFAULT_HISTORY",
            label="Default History (last 3 years)",
            group_label="e) Default History (last 3 years)",
            weight_percent=Decimal("10.00"),
            options_data=[
                ("i) No default hiostory", Decimal("5"), 1),
                ("ii) Past Default", Decimal("10"), 2),
                ("iii) Currently in default but not yet NPL", Decimal("20"), 3),
                ("iv) Currently in default and NPL", Decimal("30"), 4),
            ],
        )
        ensure_attribute(
            driver_5,
            code="OTHER_DEBTS",
            label="Other Debts",
            group_label="f) Other Debts",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("i) No other debts", Decimal("1"), 1),
                ("ii) Other debts exist but within debt ratio", Decimal("5"), 2),
                ("iii) Other debts exist with arrears", Decimal("10"), 3),
                ("iv) Other debts exist and are NPLs", Decimal("15"), 4),
            ],
        )

        ensure_attribute(
            driver_6,
            code="SALARY_DIVERSION",
            label="Salary Diversion - based on debt ratio",
            group_label="a) Salary Diversion - based on debt ratio",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) 0 - 30% debt ratio", Decimal("2"), 1),
                ("ii) 31 - 50% debt ratio", Decimal("10"), 2),
                ("iii) 51 - 65% debt ratio", Decimal("20"), 3),
                ("iv) 66 - 80% debt ratio", Decimal("25"), 4),
                ("v) Above 80% debt ratio", Decimal("30"), 5),
            ],
        )
        ensure_attribute(
            driver_6,
            code="SALARY_DELAYS",
            label="Salary Delays - based on employer sector",
            group_label="b) Salary Delays - based on employer sector",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) NGOs and Multi-National Companies", Decimal("1"), 1),
                ("ii) Indigenous Blue Chip Companies", Decimal("2"), 2),
                ("iii) Non-listed Local corporates (not blue chips)", Decimal("3"), 3),
                ("iv) State Owned Enterprises", Decimal("4"), 4),
                ("v) Locals Authorities", Decimal("5"), 5),
                ("vi) Parastatals", Decimal("6"), 6),
                (
                    "vii) Informal Bodies (clubs, sociaties, associations, co-ops, etc)",
                    Decimal("7"),
                    7,
                ),
                ("viii) SMEs and other small businesses", Decimal("8"), 8),
                ("ix) Salary Service Bureau", Decimal("9"), 9),
            ],
        )
        ensure_attribute(
            driver_6,
            code="SALARY_REDUCTION",
            label="Salary reduction/shrinkage - based on trends in the last 6 months",
            group_label="c) Salary reduction/shrinkage - based on trends in the last 6 months",
            weight_percent=Decimal("7.00"),
            options_data=[
                ("i) Salary consistent and no chnages", Decimal("2"), 1),
                (
                    "ii) Reduced once due to a once off commitment and reverted to normal",
                    Decimal("5"),
                    2,
                ),
                ("iii) Reduced on several occasions but now stabilised", Decimal("10"), 3),
                ("iv) Erratic salary accruing on client's account", Decimal("15"), 4),
                ("v) Reduced and insufficient to service loans", Decimal("20"), 5),
            ],
        )
        ensure_attribute(
            driver_6,
            code="LOSS_OF_EMPLOYMENT",
            label="Loss of employment Potential through",
            group_label="d) Loss of employment Potential through",
            weight_percent=Decimal("8.00"),
            options_data=[
                ("i) Retrenchment", Decimal("1"), 1),
                ("ii) Resignation", Decimal("3"), 2),
                ("iii) Dismissal", Decimal("5"), 3),
                ("iv) Company Closure/Bankruptcy", Decimal("8"), 4),
            ],
        )
        ensure_attribute(
            driver_6,
            code="DEATH_OR_INCAPACITATION",
            label="Death or incapacitation - based on age",
            group_label="e) Death or incapacitation - based on age",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("Below 25 Years", Decimal("3"), 1),
                ("26 - 35 Years", Decimal("5"), 2),
                ("36 - 40 Years", Decimal("7"), 3),
                ("41 - 55 Years", Decimal("9"), 4),
                ("56 - 64 Years", Decimal("10"), 5),
                ("Over 64 Years", Decimal("15"), 6),
            ],
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Sections, risk drivers, attributes, and options ensured for "
                "IFRS9PD-CONSUMER-001"
            )
        )
