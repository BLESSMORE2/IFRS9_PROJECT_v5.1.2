import logging

from django.core.management import call_command


logger = logging.getLogger(__name__)


BASELINE_TEMPLATE_SEEDS = (
    {
        "command": "seed_corporate_scoresheet",
        "model": "basel",
        "code": "ACCSC1-17",
    },
    {
        "command": "seed_farmers_scoresheet",
        "model": "basel",
        "code": "AFCSC1-19",
    },
    {
        "command": "seed_individuals_scoresheet",
        "model": "basel",
        "code": "AICSC1-17",
    },
    {
        "command": "seed_retail_scoresheet",
        "model": "basel",
        "code": "ARCSC1-17",
    },
    {
        "command": "seed_ifrs9pd_consumer_loans_scorecard",
        "model": "ifrs9",
        "code": "IFRS9PD-CONSUMER-001",
    },
    {
        "command": "seed_ifrs9pd_corporate_scorecard",
        "model": "ifrs9",
        "code": "IFRS9PD-CORPORATE-001",
    },
    {
        "command": "seed_ifrs9pd_farming_scorecard",
        "model": "ifrs9",
        "code": "IFRS9PD-FARMING-001",
    },
    {
        "command": "seed_ifrs9pd_local_authorities_scorecard",
        "model": "ifrs9",
        "code": "IFRS9PD-LOCALAUTHORITIES-001",
    },
    {
        "command": "seed_ifrs9pd_mfinance_scorecard",
        "model": "ifrs9",
        "code": "IFRS9PD-MFINANCE-001",
    },
    {
        "command": "seed_ifrs9pd_retail_scorecard",
        "model": "ifrs9",
        "code": "IFRS9PD-RETAIL-001",
    },
    {
        "command": "seed_ifrs9pd_schools_scorecard",
        "model": "ifrs9",
        "code": "IFRS9PD-SCHOOLS-001",
    },
    {
        "command": "seed_ifrs9pd_tertiary_institutions_scorecard",
        "model": "ifrs9",
        "code": "IFRS9PD-TERTIARY-001",
    },
)


def run_template_seed_seeder(sender, **kwargs):
    from scorecard.models import BaselScoreSheetTemplate, IFRS9ScoreSheetTemplate

    model_map = {
        "basel": BaselScoreSheetTemplate,
        "ifrs9": IFRS9ScoreSheetTemplate,
    }

    for definition in BASELINE_TEMPLATE_SEEDS:
        model = model_map[definition["model"]]
        if model.objects.filter(code=definition["code"]).exists():
            continue

        logger.info(
            "Auto-seeding missing scorecard template %s via %s",
            definition["code"],
            definition["command"],
        )
        call_command(definition["command"], verbosity=0)
