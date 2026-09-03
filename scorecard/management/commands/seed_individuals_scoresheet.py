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
        "Seed the Basel Individual/Consumer Credit Scoresheet template "
        "(AICSC1-17) with sections, risk drivers, attributes, options, and grade bands."
    )

    def handle(self, *args, **options):
        template, created = BaselScoreSheetTemplate.objects.get_or_create(
            code="AICSC1-17",
            defaults={
                "name": "Individual/Consumer Credit Scoresheet",
                "description": "Basel II individual/consumer credit scoresheet as per AICSC1-17.",
                "is_active": True,
            },
        )
        if created:
            self.stdout.write(
                self.style.SUCCESS("Created BaselScoreSheetTemplate AICSC1-17")
            )
        else:
            self.stdout.write(
                self.style.WARNING("BaselScoreSheetTemplate AICSC1-17 already exists")
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
            help_text: str = "",
        ) -> None:
            attribute, _ = Attribute.objects.get_or_create(
                risk_driver=driver,
                code=code,
                defaults={
                    "label": label,
                    "help_text": help_text,
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

        section_1 = get_section("1", "Personal Demographics", 1)
        section_2 = get_section("2", "Residential Demographics", 2)
        section_3 = get_section("3", "Income", 3)
        section_4 = get_section("4", "Employment History", 4)
        section_5 = get_section("5", "Banking History", 5)
        section_6 = get_section("6", "Credit History", 6)

        driver_1a = create_risk_driver(section_1, "1A", "Gender", Decimal("1.00"), Decimal("2"), 1)
        driver_1b = create_risk_driver(section_1, "1B", "Age", Decimal("3.00"), Decimal("8"), 2)
        driver_1c = create_risk_driver(section_1, "1C", "Marital Status", Decimal("2.00"), Decimal("10"), 3)
        driver_1d = create_risk_driver(section_1, "1D", "No. of dependants", Decimal("4.00"), Decimal("5"), 4)
        driver_1e = create_risk_driver(
            section_1,
            "1E",
            "Spouse Employment and income details",
            Decimal("3.00"),
            Decimal("4"),
            5,
        )
        driver_1f = create_risk_driver(
            section_1, "1F", "Educational Level attained", Decimal("2.00"), Decimal("5"), 6
        )

        driver_2a = create_risk_driver(section_2, "2A", "Residence Status", Decimal("1.00"), Decimal("2"), 1)
        driver_2b = create_risk_driver(section_2, "2B", "Residential Status", Decimal("3.00"), Decimal("5"), 2)
        driver_2c = create_risk_driver(section_2, "2C", "Location of residence", Decimal("1.00"), Decimal("5"), 3)
        driver_2d = create_risk_driver(section_2, "2D", "Time at Current Address", Decimal("5.00"), Decimal("5"), 4)

        driver_3a = create_risk_driver(section_3, "3A", "Source of Income", Decimal("10.00"), Decimal("25"), 1)
        driver_3b = create_risk_driver(section_3, "3B", "Net Monthly Income", Decimal("15.00"), Decimal("20"), 2)

        driver_4a = create_risk_driver(
            section_4, "4A", "Occupational/Professional Status", Decimal("5.00"), Decimal("7"), 1
        )
        driver_4b = create_risk_driver(section_4, "4B", "Employer Economic Sector", Decimal("7.00"), Decimal("15"), 2)
        driver_4c = create_risk_driver(
            section_4, "4C", "Length of service with current employer", Decimal("3.00"), Decimal("3"), 3
        )
        driver_4d = create_risk_driver(
            section_4, "4D", "Length of service with previous employer", Decimal("1.00"), Decimal("3"), 4
        )

        driver_5a = create_risk_driver(section_5, "5A", "Time with the bank", Decimal("4.00"), Decimal("7"), 1)

        driver_6a = create_risk_driver(section_6, "6A", "Loan Amount", Decimal("4.00"), Decimal("5"), 1)
        driver_6b = create_risk_driver(section_6, "6B", "Loan Tenor", Decimal("3.00"), Decimal("10"), 2)
        driver_6c = create_risk_driver(section_6, "6C", "Loan security", Decimal("7.00"), Decimal("15"), 3)
        driver_6d = create_risk_driver(section_6, "6D", "Repeat borrowing", Decimal("2.00"), Decimal("10"), 4)
        driver_6e = create_risk_driver(
            section_6, "6E", "Repayment History - arrears", Decimal("10.00"), Decimal("10"), 5
        )
        driver_6f = create_risk_driver(section_6, "6F", "CRB Search Results", Decimal("3.00"), Decimal("5"), 6)
        driver_6g = create_risk_driver(section_6, "6G", "Other Debts", Decimal("1.00"), Decimal("3"), 7)

        ensure_attribute(
            driver_1a,
            code="GENDER",
            label="Gender",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Female", Decimal("2"), 1),
                ("ii) Male", Decimal("1"), 2),
            ],
        )
        ensure_attribute(
            driver_1b,
            code="AGE",
            label="Age",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("i) 31 - 40 Years", Decimal("8"), 1),
                ("ii) 26 - 30 Years", Decimal("6"), 2),
                ("iii) 41 - 60 Years", Decimal("5"), 3),
                ("iv) Above 60 Years", Decimal("3"), 4),
                ("v) 18 - 25 Years", Decimal("1"), 5),
            ],
        )
        ensure_attribute(
            driver_1c,
            code="MARITAL_STATUS",
            label="Marital Status",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Married", Decimal("10"), 1),
                ("ii) Widowed", Decimal("8"), 2),
                ("iii) Engaged", Decimal("7"), 3),
                ("iv) Divorced", Decimal("5"), 4),
                ("v) Separated", Decimal("3"), 5),
                ("vi) Single", Decimal("1"), 6),
            ],
        )
        ensure_attribute(
            driver_1d,
            code="DEPENDANTS",
            label="No. of dependants",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("i) None", Decimal("5"), 1),
                ("ii) 1 Person", Decimal("4"), 2),
                ("iii) 2 - 3 People", Decimal("3"), 3),
                ("iv) 4 - 6 People", Decimal("2"), 4),
                ("v) 6 or more people", Decimal("1"), 5),
            ],
        )
        ensure_attribute(
            driver_1e,
            code="SPOUSE_EMPLOYMENT",
            label="Spouse Employment and income details",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("i) Spouse gainfully employed with regular income", Decimal("4"), 1),
                (
                    "ii) Not gainfully employed but receives fixed income from long term projects",
                    Decimal("3"),
                    2,
                ),
                (
                    "iii) Not gainfully employed but runs short term personal projects with regular income",
                    Decimal("2"),
                    3,
                ),
                ("iv) Spouse unemployed with no income", Decimal("0"), 4),
            ],
        )
        ensure_attribute(
            driver_1f,
            code="EDUCATION_LEVEL",
            label="Educational Level attained",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) Degreed", Decimal("5"), 1),
                ("ii) Diploma", Decimal("4"), 2),
                ("iii) Certificate", Decimal("3"), 3),
                ("iv) A or O Level Education", Decimal("2"), 4),
                ("v) Primary Education", Decimal("1"), 5),
                ("vi) None", Decimal("0"), 6),
            ],
        )

        ensure_attribute(
            driver_2a,
            code="RESIDENCE_STATUS",
            label="Residence Status",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Permanent Residence", Decimal("2"), 1),
                ("ii) Temporary Residence", Decimal("1"), 2),
                ("iii) Non-Resident", Decimal("0"), 3),
            ],
        )
        ensure_attribute(
            driver_2b,
            code="RESIDENTIAL_STATUS",
            label="Residential Status",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("i) Owner Freehold", Decimal("5"), 1),
                ("ii) Owner Mortgaged", Decimal("4"), 2),
                ("iii) Rent to buy", Decimal("3"), 3),
                ("iv) Company House", Decimal("2"), 4),
                ("v) Rented", Decimal("1"), 5),
                ("vi) Living with Parents or other arrangements", Decimal("0"), 6),
            ],
        )
        ensure_attribute(
            driver_2c,
            code="RESIDENCE_LOCATION",
            label="Location of residence",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) Low Density", Decimal("5"), 1),
                ("ii) Peri-Urban", Decimal("4"), 2),
                ("iii) Medium Density", Decimal("3"), 3),
                ("iv) High Density", Decimal("2"), 4),
                ("v) Farming Land", Decimal("1"), 5),
                ("vi) Other", Decimal("0"), 6),
            ],
        )
        ensure_attribute(
            driver_2d,
            code="TIME_AT_CURRENT_ADDRESS",
            label="Time at Current Address",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("i) More than 5 Years", Decimal("5"), 1),
                ("ii) 4 - 5 Years", Decimal("4"), 2),
                ("iii) 3 - 4 Years", Decimal("3"), 3),
                ("iv) 2 - 3 Years", Decimal("2"), 4),
                ("v) 1 - 2 Years", Decimal("1"), 5),
                ("vi) Below 1 Year", Decimal("0"), 6),
            ],
        )

        ensure_attribute(
            driver_3a,
            code="SOURCE_OF_INCOME",
            label="Source of Income",
            weight_percent=Decimal("10.00"),
            options_data=[
                ("i) Salary + other incomes", Decimal("25"), 1),
                ("ii) Salary income only", Decimal("20"), 2),
                ("iii) Non salary income - e.g. investments, rental income etc", Decimal("10"), 3),
                ("iv) Pension only", Decimal("5"), 4),
                ("v) Alimony, grants, social welfare grants, allowances etc", Decimal("1"), 5),
                ("vi) Salary ceased - less than 6 months ago", Decimal("0"), 6),
                (
                    "vii) Salary ceased - less than 12 months but more than 6 months",
                    Decimal("-10"),
                    7,
                ),
                ("viii) Salary ceased - more than 12 months", Decimal("-25"), 8),
            ],
        )
        ensure_attribute(
            driver_3b,
            code="NET_MONTHLY_INCOME",
            label="Net Monthly Income",
            weight_percent=Decimal("15.00"),
            options_data=[
                ("i) Above $5,000 per month", Decimal("20"), 1),
                ("ii) $3,001 - $5,000 per month", Decimal("15"), 2),
                ("iii) $1,001 - $3,000 per month", Decimal("10"), 3),
                ("iv) $501 - $1,000 per month", Decimal("7"), 4),
                ("v) $251 - $500 per month", Decimal("5"), 5),
                ("vi) Below $251 per month", Decimal("3"), 6),
                ("vii) Salary ceased - less than 6 months ago", Decimal("0"), 7),
                (
                    "viii) Salary ceased - less than 12 months but more than 6 months",
                    Decimal("-5"),
                    8,
                ),
                ("ix) Salary ceased - more than 12 months", Decimal("-20"), 9),
            ],
        )

        ensure_attribute(
            driver_4a,
            code="OCCUPATIONAL_STATUS",
            label="Occupational/Professional Status",
            weight_percent=Decimal("5.00"),
            options_data=[
                ("i) Salaried employee", Decimal("7"), 1),
                ("ii) Career Investor with regular monthly returns", Decimal("6"), 2),
                ("iii) Businessman/Proprietor", Decimal("5"), 3),
                ("iv) Other professional engagements e.g. lawyers, doctors", Decimal("4"), 4),
                ("v) Consultant", Decimal("3"), 5),
                ("vi) Commission Worker", Decimal("2"), 6),
                ("vii) Informal trader", Decimal("1"), 7),
                ("viii) Pensioner/Retired", Decimal("0"), 8),
                ("ix) Unemployed", Decimal("-3"), 9),
            ],
        )
        ensure_attribute(
            driver_4b,
            code="EMPLOYER_ECONOMIC_SECTOR",
            label="Employer Economic Sector",
            weight_percent=Decimal("7.00"),
            options_data=[
                ("i) Tourism and Hospitality", Decimal("15"), 1),
                ("ii) Telecommunications", Decimal("13"), 2),
                ("iii) Information and Communications Technology", Decimal("12"), 3),
                ("iv) Finance & Insurance", Decimal("10"), 4),
                ("v) Power & Energy", Decimal("8"), 5),
                ("vi) Processing & Value Addition", Decimal("7"), 6),
                ("vii) Distribution & Services", Decimal("6"), 7),
                ("viii) State and Parastatals", Decimal("5"), 8),
                ("ix) Mining", Decimal("4"), 9),
                ("x) Manufacturing", Decimal("5"), 10),
                ("xi) Agriculture", Decimal("2"), 11),
                ("xii) Unemployed", Decimal("0"), 12),
            ],
        )
        ensure_attribute(
            driver_4c,
            code="CURRENT_EMPLOYER_SERVICE_LENGTH",
            label="Length of service with current employer",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("i) More than 5 Years", Decimal("3"), 1),
                ("ii) 3 - 5 Years", Decimal("2"), 2),
                ("iii) 1 - 3 Years", Decimal("1"), 3),
                ("iv) Less than 1 Year", Decimal("0"), 4),
                ("v) Out of employment", Decimal("-2"), 5),
            ],
        )
        ensure_attribute(
            driver_4d,
            code="PREVIOUS_EMPLOYER_SERVICE_LENGTH",
            label="Length of service with previous employer",
            weight_percent=Decimal("1.00"),
            options_data=[
                ("i) More than 5 Years", Decimal("3"), 1),
                ("ii) 3 - 5 Years", Decimal("1"), 2),
                ("iii) 1 - 3 Years", Decimal("0"), 3),
                ("iv) Less than 1 Year or N/A", Decimal("-2"), 4),
            ],
        )

        ensure_attribute(
            driver_5a,
            code="TIME_WITH_BANK",
            label="Time with the bank",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("i) More than 5 Years", Decimal("7"), 1),
                ("ii) 4 - 5 Years", Decimal("6"), 2),
                ("iii) 3 - 4 Years", Decimal("5"), 3),
                ("iv) 2 - 3 Years", Decimal("4"), 4),
                ("v) 1 - 2 Years", Decimal("3"), 5),
                ("vi) Less than 1 Year but more than 1 month", Decimal("2"), 6),
                ("vii) New account or account now inactive", Decimal("1"), 7),
            ],
        )

        ensure_attribute(
            driver_6a,
            code="LOAN_AMOUNT",
            label="Loan Amount",
            weight_percent=Decimal("4.00"),
            options_data=[
                ("i) Below $1,000", Decimal("5"), 1),
                ("ii) $1,000 - $5,000", Decimal("4"), 2),
                ("iii) $5,000 - $10,000", Decimal("3"), 3),
                ("iv) Above $10,000", Decimal("2"), 4),
                ("v) Existing loan is in arrears", Decimal("0"), 5),
            ],
        )
        ensure_attribute(
            driver_6b,
            code="LOAN_TENOR",
            label="Loan Tenor",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("i) 0 - 6 months", Decimal("10"), 1),
                ("ii) 6 - 12 months", Decimal("7"), 2),
                ("iii) 12 - 24 months", Decimal("5"), 3),
                ("iv) 24 - 36 months", Decimal("4"), 4),
                ("v) 36 - 48 months", Decimal("3"), 5),
                ("vi) More than 48 months", Decimal("1"), 6),
                ("vii) Existing loan is in arrears", Decimal("0"), 7),
            ],
        )
        ensure_attribute(
            driver_6c,
            code="LOAN_SECURITY",
            label="Loan security",
            weight_percent=Decimal("7.00"),
            options_data=[
                ("i) Adequately secured by tangible security/cash cover", Decimal("15"), 1),
                (
                    "ii) Secured only against death/incapacitation through insurance",
                    Decimal("13"),
                    2,
                ),
                ("iii) No security - below collateral threshold", Decimal("10"), 3),
                ("iv) Partially secured by tangible security", Decimal("5"), 4),
                ("v) Unsecured but above collateral threshold", Decimal("0"), 5),
            ],
        )
        ensure_attribute(
            driver_6d,
            code="REPEAT_BORROWING",
            label="Repeat borrowing",
            help_text="Has borrowed and fully repaid all loans with the bank in the last:",
            weight_percent=Decimal("2.00"),
            options_data=[
                ("i) 5 Years", Decimal("10"), 1),
                ("ii) 4 Years", Decimal("9"), 2),
                ("iii) 3 Years", Decimal("8"), 3),
                ("iv) 2 Years", Decimal("6"), 4),
                ("v) 1 Year", Decimal("4"), 5),
                ("vi) New Borrower", Decimal("1"), 6),
            ],
        )
        ensure_attribute(
            driver_6e,
            code="REPAYMENT_HISTORY_ARREARS",
            label="Repayment History - arrears",
            weight_percent=Decimal("10.00"),
            options_data=[
                ("i) All loans current and up to date or new borrower", Decimal("10"), 1),
                ("ii) 30 days default", Decimal("8"), 2),
                ("iii) 60 days default", Decimal("4"), 3),
                ("iv) 90 days default", Decimal("0"), 4),
                ("v) More than 90 days default", Decimal("-5"), 5),
            ],
        )
        ensure_attribute(
            driver_6f,
            code="CRB_SEARCH_RESULTS",
            label="CRB Search Results",
            weight_percent=Decimal("3.00"),
            options_data=[
                ("i) Clean CRB report", Decimal("5"), 1),
                ("ii) Returned paper/items", Decimal("4"), 2),
                ("iii) Has current default records on CRB", Decimal("3"), 3),
                ("iv) Compulsory closure", Decimal("2"), 4),
                ("v) Civil judgements", Decimal("0"), 5),
            ],
        )
        # Assumption: the sheet splits Other Debts into Bank and Non-Bank checks
        # under a combined 1% total weight, so we split the weight equally.
        ensure_attribute(
            driver_6g,
            code="OTHER_DEBTS_BANK",
            label="Bank",
            weight_percent=Decimal("0.50"),
            options_data=[
                ("i) No", Decimal("2"), 1),
                ("ii) Yes", Decimal("1"), 2),
            ],
        )
        ensure_attribute(
            driver_6g,
            code="OTHER_DEBTS_NON_BANK",
            label="Non-Bank",
            weight_percent=Decimal("0.50"),
            options_data=[
                ("iii) No", Decimal("1"), 1),
                ("iv) Yes", Decimal("-1"), 2),
            ],
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Sections, risk drivers, attributes, and options ensured for AICSC1-17"
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

        self.stdout.write(self.style.SUCCESS("Grade bands ensured for AICSC1-17"))
