from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from scorecard.models import (
    BaselScoreSheetTemplate,
    CreditEvaluation,
    IFRS9Evaluation,
    IFRS9ScoreSheetTemplate,
)


def _bump_workflow_badges(*, customer_counts=False):
    # Import lazily so app startup does not pull the full context processor graph.
    from scorecard.context_processors import (
        bump_checker_pending_counts_version,
        bump_customer_queue_counts_version,
        bump_maker_queue_counts_version,
    )

    bump_checker_pending_counts_version()
    bump_maker_queue_counts_version()
    if customer_counts:
        bump_customer_queue_counts_version()


@receiver(post_save, sender=CreditEvaluation, dispatch_uid="scorecard_badges_credit_save")
@receiver(post_delete, sender=CreditEvaluation, dispatch_uid="scorecard_badges_credit_delete")
@receiver(post_save, sender=IFRS9Evaluation, dispatch_uid="scorecard_badges_ifrs9_save")
@receiver(post_delete, sender=IFRS9Evaluation, dispatch_uid="scorecard_badges_ifrs9_delete")
def invalidate_score_badges(**kwargs):
    from scorecard.functions_view.customers import bump_customer_list_summary_version

    bump_customer_list_summary_version()
    _bump_workflow_badges()


@receiver(post_save, sender=BaselScoreSheetTemplate, dispatch_uid="scorecard_badges_basel_template_save")
@receiver(post_delete, sender=BaselScoreSheetTemplate, dispatch_uid="scorecard_badges_basel_template_delete")
@receiver(post_save, sender=IFRS9ScoreSheetTemplate, dispatch_uid="scorecard_badges_ifrs9_template_save")
@receiver(post_delete, sender=IFRS9ScoreSheetTemplate, dispatch_uid="scorecard_badges_ifrs9_template_delete")
def invalidate_template_badges(**kwargs):
    _bump_workflow_badges()
