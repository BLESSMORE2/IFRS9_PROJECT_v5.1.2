import logging

from django.db import DatabaseError, connection

from scorecard.models import ScorecardWorkflowApprovalSetting


logger = logging.getLogger(__name__)
_workflow_settings_schema_checked = False


WORKFLOW_AUTO_APPROVAL_FIELD_MAP = {
    "basel_scores": {
        "superuser_field": "basel_score_superuser_auto_approve",
        "admin_field": "basel_score_admin_auto_approve",
        "reviewer_field": "basel_score_reviewer_auto_approve",
        "admin_permission": "scorecard.reopen_basel_scores",
    },
    "ifrs9_scores": {
        "superuser_field": "ifrs9_score_superuser_auto_approve",
        "admin_field": "ifrs9_score_admin_auto_approve",
        "reviewer_field": "ifrs9_score_reviewer_auto_approve",
        "admin_permission": "scorecard.reopen_ifrs9_scores",
    },
    "basel_templates": {
        "superuser_field": "basel_template_superuser_auto_approve",
        "admin_field": "basel_template_admin_auto_approve",
        "reviewer_field": "basel_template_reviewer_auto_approve",
        "admin_permission": "scorecard.manage_basel_templates",
    },
    "ifrs9_templates": {
        "superuser_field": "ifrs9_template_superuser_auto_approve",
        "admin_field": "ifrs9_template_admin_auto_approve",
        "reviewer_field": "ifrs9_template_reviewer_auto_approve",
        "admin_permission": "scorecard.manage_ifrs9_templates",
    },
}


def ensure_scorecard_workflow_approval_settings_schema():
    """Repair late-added workflow setting columns before ORM queries select them."""
    global _workflow_settings_schema_checked

    if _workflow_settings_schema_checked:
        return

    model = ScorecardWorkflowApprovalSetting
    table_name = model._meta.db_table
    field_names = [
        "without_score_list_include_loans",
        "without_score_list_include_overdrafts",
        "single_branch_submission_reviewer_notifications",
        "single_branch_without_score_maker_notifications",
        "auto_refresh_autofilled_scores_enabled",
        "auto_refresh_autofilled_scores_frequency",
        "auto_refresh_autofilled_scores_time",
        "auto_refresh_autofilled_scores_weekday",
        "auto_refresh_autofilled_scores_month_day",
        "auto_refresh_autofilled_scores_last_run_at",
    ]

    try:
        with connection.cursor() as cursor:
            existing_tables = {
                table.lower()
                for table in connection.introspection.table_names(cursor)
            }
            if table_name.lower() not in existing_tables:
                _workflow_settings_schema_checked = True
                return

            existing_columns = {
                column.name.lower()
                for column in connection.introspection.get_table_description(cursor, table_name)
            }
            missing_fields = [
                model._meta.get_field(field_name)
                for field_name in field_names
                if model._meta.get_field(field_name).column.lower() not in existing_columns
            ]
            if not missing_fields:
                _workflow_settings_schema_checked = True
                return

        with connection.schema_editor() as schema_editor:
            for field in missing_fields:
                schema_editor.add_field(model, field)

        _workflow_settings_schema_checked = True
    except DatabaseError:
        _workflow_settings_schema_checked = False
        logger.exception("Unable to repair scorecard workflow approval settings schema.")
        raise


def get_scorecard_workflow_approval_settings() -> ScorecardWorkflowApprovalSetting:
    ensure_scorecard_workflow_approval_settings_schema()
    settings_obj, _ = ScorecardWorkflowApprovalSetting.objects.get_or_create(pk=1)
    return settings_obj


def should_auto_approve_scorecard_workflow(user, workflow_key: str, reviewer_permission: str) -> bool:
    settings_obj = get_scorecard_workflow_approval_settings()
    workflow_meta = WORKFLOW_AUTO_APPROVAL_FIELD_MAP[workflow_key]

    if user.is_superuser:
        return bool(getattr(settings_obj, workflow_meta["superuser_field"], False))

    # Score admin auto-approval must use admin-only permissions. Maker/checker
    # permissions can overlap with manage_* permissions, so the field map uses
    # reopen_* for score workflows to keep admin and checker switches separate.
    if user.has_perm(workflow_meta["admin_permission"]):
        return bool(getattr(settings_obj, workflow_meta["admin_field"], False))

    if user.has_perm(reviewer_permission):
        return bool(getattr(settings_obj, workflow_meta["reviewer_field"], False))

    return False


def can_user_self_review_score_submission(user, workflow_key: str, reviewer_permission: str) -> bool:
    """Return True only when the active workflow settings intentionally allow self-approval."""
    return should_auto_approve_scorecard_workflow(user, workflow_key, reviewer_permission)


def can_user_self_review_template_submission(user, workflow_key: str, reviewer_permission: str) -> bool:
    """Return True only when the active workflow settings intentionally allow template self-approval."""
    return should_auto_approve_scorecard_workflow(user, workflow_key, reviewer_permission)


def is_counterpart_completion_enforced() -> bool:
    settings_obj = get_scorecard_workflow_approval_settings()
    return bool(getattr(settings_obj, "enforce_score_counterpart_completion", True))



def is_cross_branch_duplicate_scoring_prevented() -> bool:
    settings_obj = get_scorecard_workflow_approval_settings()
    return bool(getattr(settings_obj, "prevent_cross_branch_duplicate_scoring", False))


def get_without_score_list_customer_sources() -> dict[str, bool]:
    settings_obj = get_scorecard_workflow_approval_settings()
    return {
        "include_loans": bool(getattr(settings_obj, "without_score_list_include_loans", True)),
        "include_overdrafts": bool(getattr(settings_obj, "without_score_list_include_overdrafts", True)),
    }


def should_limit_submission_notifications_to_single_branch_reviewers() -> bool:
    settings_obj = get_scorecard_workflow_approval_settings()
    return bool(getattr(settings_obj, "single_branch_submission_reviewer_notifications", False))


def should_limit_without_score_notifications_to_single_branch_makers() -> bool:
    settings_obj = get_scorecard_workflow_approval_settings()
    return bool(getattr(settings_obj, "single_branch_without_score_maker_notifications", False))
