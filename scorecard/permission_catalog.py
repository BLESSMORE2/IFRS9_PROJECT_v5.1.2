SCORECARD_PERMISSION_DEFINITIONS = [
    {
        "codename": "access_scorecard_dashboard",
        "label": "Can access the scorecard dashboard",
        "description": "Open the scorecard landing workspace and switch active branches.",
    },
    {
        "codename": "view_full_scorecard_dashboard",
        "label": "Can view all scorecard dashboard content",
        "description": "View every dashboard statistic, chart, and activity panel without granting access to the underlying workspaces.",
    },
    {
        "codename": "view_scorecard_api",
        "label": "Can view scorecard API workspaces",
        "description": "Open API monitoring, retrieval, health, import history, and sync status pages.",
    },
    {
        "codename": "manage_scorecard_api_settings",
        "label": "Can manage scorecard API settings",
        "description": "Maintain API configuration and auto-sync settings.",
    },
    {
        "codename": "manage_scorecard_api_endpoints",
        "label": "Can manage scorecard API endpoints",
        "description": "Create, edit, and delete API endpoint definitions.",
    },
    {
        "codename": "manage_scorecard_api_schedules",
        "label": "Can manage scorecard API schedules",
        "description": "Create, edit, and delete scheduler automation entries.",
    },
    {
        "codename": "run_scorecard_api_operations",
        "label": "Can run scorecard API operations",
        "description": "Run endpoint tests, manual imports, retries, and main customer syncs.",
    },
    {
        "codename": "use_scorecard_uploads",
        "label": "Can use scorecard external imports",
        "description": "Upload, map, preview, and import external Basel and IFRS9 workbooks.",
    },
    {
        "codename": "view_basel_templates",
        "label": "Can view Basel template configuration",
        "description": "Review Basel scorecard templates and their structure.",
    },
    {
        "codename": "manage_basel_templates",
        "label": "Can manage Basel template configuration",
        "description": "Create and maintain Basel templates, including the builder, sections, drivers, attributes, options, and grade bands.",
    },
    {
        "codename": "review_basel_templates",
        "label": "Can review Basel templates",
        "description": "Review, approve, return, and reopen Basel template workflow items.",
    },
    {
        "codename": "view_ifrs9_templates",
        "label": "Can view IFRS9 template configuration",
        "description": "Review IFRS9 scorecard templates and their structure.",
    },
    {
        "codename": "manage_ifrs9_templates",
        "label": "Can manage IFRS9 template configuration",
        "description": "Create and maintain IFRS9 templates, including the builder, sections, drivers, attributes, and options.",
    },
    {
        "codename": "review_ifrs9_templates",
        "label": "Can review IFRS9 templates",
        "description": "Review, approve, return, and reopen IFRS9 template workflow items.",
    },
    {
        "codename": "view_basel_scores",
        "label": "Can view Basel score evaluations",
        "description": "Review Basel score submissions, history, comparisons, and exports.",
    },
    {
        "codename": "manage_basel_scores",
        "label": "Can manage Basel score evaluations",
        "description": "Create, edit, assign templates, delete, and submit Basel score evaluations.",
    },
    {
        "codename": "review_basel_scores",
        "label": "Can review Basel score evaluations",
        "description": "Review, approve, and return Basel score evaluations in maker-checker flow.",
    },
    {
        "codename": "reopen_basel_scores",
        "label": "Can reopen Basel score evaluations",
        "description": "Reopen approved Basel evaluations for further work.",
    },
    {
        "codename": "view_ifrs9_scores",
        "label": "Can view IFRS9 score evaluations",
        "description": "Review IFRS9 score submissions, history, comparisons, and exports.",
    },
    {
        "codename": "manage_ifrs9_scores",
        "label": "Can manage IFRS9 score evaluations",
        "description": "Create, edit, assign templates, delete, and submit IFRS9 score forms.",
    },
    {
        "codename": "review_ifrs9_scores",
        "label": "Can review IFRS9 score evaluations",
        "description": "Review, approve, and return IFRS9 score evaluations in maker-checker flow.",
    },
    {
        "codename": "reopen_ifrs9_scores",
        "label": "Can reopen IFRS9 score evaluations",
        "description": "Reopen approved IFRS9 evaluations for further work.",
    },
    {
        "codename": "manage_ifrs9_supporting_data",
        "label": "Can manage IFRS9 supporting data",
        "description": "Maintain branch-scoped collateral and payment schedule staging records for active IFRS9 loans.",
    },
    {
        "codename": "view_ifrs9_results",
        "label": "Can view IFRS9 results",
        "description": "Open branch-scoped IFRS9 results extract and ECL summary report workspaces.",
    },
    {
        "codename": "view_scorecard_historical_scores",
        "label": "Can view scorecard historical scores",
        "description": "Open branch-scoped historical score snapshots and download filtered historical score extracts.",
    },
    {
        "codename": "manage_scorecard_historical_scores",
        "label": "Can manage scorecard historical scores",
        "description": "Refresh and bulk-delete branch-scoped historical score snapshots without granting API administration rights.",
    },
    {
        "codename": "view_scorecard_customers",
        "label": "Can view scorecard customer workspaces",
        "description": "Open customer directories, quality lists, and customer exports.",
    },
    {
        "codename": "manage_scorecard_customers",
        "label": "Can manage scorecard customers",
        "description": "Create and maintain customer records used by scorecard workflows.",
    },
    {
        "codename": "view_scorecard_branches",
        "label": "Can view scorecard branch workspaces",
        "description": "Reserved for branch-scoped read-only access when applicable.",
    },
    {
        "codename": "manage_scorecard_branches",
        "label": "Can manage scorecard branch workspaces",
        "description": "Maintain branch master data.",
    },
    {
        "codename": "view_scorecard_notifications",
        "label": "Can view scorecard notifications",
        "description": "Open and read scorecard notifications.",
    },
    {
        "codename": "view_scorecard_documents",
        "label": "Can view scorecard documents",
        "description": "Open questionnaire and scorecard document libraries and downloads.",
    },
    {
        "codename": "manage_scorecard_documents",
        "label": "Can manage scorecard documents",
        "description": "Upload and maintain scorecard document records.",
    },
    {
        "codename": "view_scorecard_email",
        "label": "Can view scorecard email administration",
        "description": "Open scorecard email configuration, templates, and delivery history pages.",
    },
    {
        "codename": "view_scorecard_audit_trail",
        "label": "Can view scorecard audit trail",
        "description": "Open the standalone scorecard audit trail page and review scorecard-wide activity history.",
    },
    {
        "codename": "manage_scorecard_email",
        "label": "Can manage scorecard email administration",
        "description": "Maintain scorecard email configuration, templates, and retry actions.",
    },
    {
        "codename": "view_scorecard_permissions",
        "label": "Can view scorecard permission settings",
        "description": "Open the scorecard permission dashboard, role list, and permission matrix.",
    },
    {
        "codename": "assign_scorecard_permissions",
        "label": "Can assign scorecard permission roles",
        "description": "Assign scorecard permission roles and branch access.",
    },
    {
        "codename": "view_scorecard_workflow",
        "label": "Can view scorecard workflow rules",
        "description": "Open standalone scorecard workflow rules in read-only mode.",
    },
    {
        "codename": "manage_scorecard_workflow",
        "label": "Can manage scorecard workflow rules",
        "description": "Edit scorecard workflow rules for approvals, scoring locks, notification routing, and without-score sources.",
    },
]


DASHBOARD_ROUTES = [
    "switch_branch",
    "scorecard_dashboard",
    "scorecard_dashboard_content",
]

API_VIEW_ROUTES = [
    "api_dashboard",
    "api_settings",
    "api_scheduler",
    "api_scheduler_status",
    "api_retrieve",
    "api_import_progress",
    "api_main_sync",
    "api_endpoints",
    "api_health",
]

API_SETTINGS_ROUTES = []

API_ENDPOINT_ROUTES = [
    "api_endpoint_save",
    "api_endpoint_delete",
]

API_SCHEDULE_ROUTES = [
    "api_scheduler_start",
    "api_scheduler_controls",
    "api_scheduler_toggle",
    "api_scheduler_run_now",
    "api_scheduler_delete",
]

API_OPERATOR_ROUTES = [
    "api_endpoint_test",
    "api_import",
    "api_import_run",
    "api_import_stop",
    "api_import_retry",
    "api_main_sync_run",
]

UPLOAD_ROUTES = [
    "upload_home",
    "upload_start",
    "upload_mapping",
    "upload_preview",
    "upload_preview_errors_download",
    "upload_result",
    "upload_run",
    "upload_reset",
]

BASEL_TEMPLATE_VIEW_ROUTES = [
    "basel_template_list",
    "basel_template_detail",
    "farmers_credit_scoresheet",
]

BASEL_TEMPLATE_MANAGE_ROUTES = [
    "basel_template_create",
    "basel_template_builder",
    "basel_template_edit",
    "basel_template_delete",
    "option_edit",
    "grade_band_list",
    "grade_band_create",
    "grade_band_edit",
    "grade_band_delete",
    "section_list",
    "section_create",
    "section_edit",
    "section_delete",
    "risk_driver_list",
    "risk_driver_create",
    "risk_driver_edit",
    "risk_driver_delete",
    "attribute_list",
    "attribute_create",
    "attribute_edit",
    "attribute_delete",
    "option_list",
    "option_create",
    "option_edit_new",
    "option_delete",
]

IFRS9_TEMPLATE_VIEW_ROUTES = [
    "ifrs9_template_list",
    "ifrs9_template_detail",
]

IFRS9_TEMPLATE_MANAGE_ROUTES = [
    "ifrs9_template_create",
    "ifrs9_template_builder",
    "ifrs9_template_edit",
    "ifrs9_template_delete",
    "ifrs9_grade_band_list",
    "ifrs9_grade_band_create",
    "ifrs9_grade_band_edit",
    "ifrs9_grade_band_delete",
    "ifrs9_section_list",
    "ifrs9_section_create",
    "ifrs9_section_edit",
    "ifrs9_section_delete",
    "ifrs9_risk_driver_list",
    "ifrs9_risk_driver_create",
    "ifrs9_risk_driver_edit",
    "ifrs9_risk_driver_delete",
    "ifrs9_attribute_list",
    "ifrs9_attribute_create",
    "ifrs9_attribute_edit",
    "ifrs9_attribute_delete",
    "ifrs9_option_list",
    "ifrs9_option_create",
    "ifrs9_option_edit",
    "ifrs9_option_delete",
]

BASEL_SCORE_VIEW_ROUTES = [
    "basel_scores_submitted_list",
    "basel_scores_view_detail",
    "basel_scores_compare_versions",
    "basel_scores_customer_versions_list",
    "basel_scores_customer_evaluation_versions",
    "basel_scores_customer_version_detail",
    "basel_scores_export_excel",
    "basel_scores_export_csv",
]

BASEL_SCORE_MANAGE_ROUTES = [
    "basel_scores_template_select",
    "basel_scores_customer_search",
    "basel_scores_autofill",
    "basel_scores_form",
    "basel_scores_draft_list",
    "basel_scores_edit",
    "basel_scores_template_assignment",
    "basel_scores_delete",
    "basel_scores_check_existing",
    "maker_draft_list",
    "maker_submitted_list",
    "maker_basel_scores_view",
    "submit_for_review",
    "withdraw_submission",
]

BASEL_SCORE_REVIEW_ROUTES = [
    "checker_pending_list",
    "checker_review",
    "approve_evaluation",
    "return_evaluation",
]

BASEL_SCORE_REOPEN_ROUTES = [
    "admin_reopen",
]

IFRS9_SCORE_VIEW_ROUTES = [
    "ifrs9_scores_submitted_list",
    "ifrs9_scores_view_detail",
    "ifrs9_scores_compare_versions",
    "ifrs9_customer_versions_list",
    "ifrs9_customer_evaluation_versions",
    "ifrs9_customer_version_detail",
    "ifrs9_scores_export_excel",
    "ifrs9_scores_export_csv",
]

IFRS9_SCORE_MANAGE_ROUTES = [
    "ifrs9_scores_template_select",
    "ifrs9_scores_customer_search",
    "ifrs9_scores_autofill",
    "ifrs9_scores_form",
    "ifrs9_scores_draft_list",
    "ifrs9_scores_edit",
    "ifrs9_scores_template_assignment",
    "ifrs9_scores_delete",
    "ifrs9_scores_check_existing",
    "maker_ifrs9_scores_draft_list",
    "maker_ifrs9_scores_submitted_list",
    "maker_ifrs9_scores_view",
    "submit_ifrs9_scores_for_review",
    "withdraw_ifrs9_scores_submission",
]

IFRS9_SCORE_REVIEW_ROUTES = [
    "checker_ifrs9_scores_pending_list",
    "checker_ifrs9_scores_review",
    "ifrs9_scores_approve_evaluation",
    "ifrs9_scores_return_evaluation",
]

IFRS9_SCORE_REOPEN_ROUTES = [
    "admin_reopen_ifrs9_scores",
]

IFRS9_SUPPORTING_DATA_ROUTES = [
    "ifrs9_supporting_data",
    "ifrs9_supporting_data_collateral",
    "ifrs9_supporting_data_collateral_add",
    "ifrs9_supporting_data_collateral_edit",
    "ifrs9_supporting_data_collateral_delete",
    "ifrs9_supporting_data_collateral_template",
    "ifrs9_supporting_data_payment_schedules",
    "ifrs9_supporting_data_payment_schedules_add",
    "ifrs9_supporting_data_payment_schedules_edit",
    "ifrs9_supporting_data_payment_schedules_delete",
    "ifrs9_supporting_data_payment_schedules_template",
]

SCORECARD_HISTORICAL_SCORE_ROUTES = [
    "historical_scores_list",
    "historical_scores_download",
]

SCORECARD_HISTORICAL_SCORE_MANAGE_ROUTES = [
    "historical_scores_refresh",
    "historical_scores_bulk_delete",
]

IFRS9_RESULTS_ROUTES = [
    "ifrs9_results_home",
    "ifrs9_results_extract",
    "ifrs9_results_extract_download",
    "ifrs9_results_ecl_summary",
    "ifrs9_results_ecl_summary_download_excel",
    "ifrs9_results_ecl_summary_download_pdf",
]

BASEL_TEMPLATE_WORKFLOW_MANAGE_ROUTES = [
    "template_maker_draft_list",
    "template_maker_submitted_list",
    "template_maker_view",
    "template_submit",
    "template_withdraw",
]

BASEL_TEMPLATE_WORKFLOW_REVIEW_ROUTES = [
    "template_checker_pending_list",
    "template_checker_review",
    "template_approve",
    "template_return",
]

IFRS9_TEMPLATE_WORKFLOW_MANAGE_ROUTES = [
    "ifrs9_template_maker_draft_list",
    "ifrs9_template_maker_submitted_list",
    "ifrs9_template_maker_view",
    "ifrs9_template_submit",
    "ifrs9_template_withdraw",
]

IFRS9_TEMPLATE_WORKFLOW_REVIEW_ROUTES = [
    "ifrs9_template_checker_pending_list",
    "ifrs9_template_checker_review",
    "ifrs9_template_approve",
    "ifrs9_template_return",
]

CHECKER_APPROVALS_ROUTES = [
    "checker_my_approvals",
    "checker_my_approvals_basel_score_view",
    "checker_my_approvals_ifrs9_score_view",
    "checker_my_approvals_basel_template_view",
    "checker_my_approvals_ifrs9_template_view",
]

CUSTOMER_VIEW_ROUTES = [
    "customer_list",
    "customer_detail",
    "customer_export_excel",
    "customer_export_csv",
    "customer_without_questionnaire_list",
    "customer_without_questionnaire_export_excel",
    "customer_without_questionnaire_export_csv",
    "customer_without_ifrs9_list",
    "customer_without_ifrs9_export_excel",
    "customer_without_ifrs9_export_csv",
]

CUSTOMER_MANAGE_ROUTES = [
    "add_customer",
    "manual_overdraft_customer_list",
]

BRANCH_VIEW_ROUTES = [
    "branch_master",
]

BRANCH_MANAGE_ROUTES = [
    "branch_master_delete",
]

NOTIFICATION_ROUTES = [
    "notifications",
    "notification_mark_read",
    "notification_detail",
    "notification_mark_all_read",
]
NOTIFICATION_ACCESS_PERMISSIONS = (
    "scorecard.view_scorecard_notifications",
    "scorecard.reopen_basel_scores",
    "scorecard.reopen_ifrs9_scores",
)

DOCUMENT_VIEW_ROUTES = [
    "document_list",
    "document_detail",
    "document_ifrs9_detail",
    "scorecard_document_list",
    "scorecard_document_detail",
    "scorecard_document_download",
]

DOCUMENT_MANAGE_ROUTES = [
    "scorecard_document_upload",
    "document_delete",
    "document_ifrs9_delete",
    "scorecard_document_delete",
]

EMAIL_VIEW_ROUTES = [
    "email_configuration",
    "email_templates",
    "email_delivery",
]

AUDIT_TRAIL_VIEW_ROUTES = [
    "settings_audit_trail",
]

SETTINGS_VIEW_ROUTES = [
    "settings_permissions",
    "settings_user_roles",
    "settings_user_access_summary",
    "settings_permission_matrix",
    "settings_user_role_assignment",
]

WORKFLOW_VIEW_ROUTES = [
    "settings_workflow_approvals",
]


ROUTE_PERMISSION_MAP = {}
ROUTE_PERMISSION_MAP.update({name: "scorecard.access_scorecard_dashboard" for name in DASHBOARD_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_scorecard_api" for name in API_VIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_scorecard_api_settings" for name in API_SETTINGS_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_scorecard_api_endpoints" for name in API_ENDPOINT_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_scorecard_api_schedules" for name in API_SCHEDULE_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.run_scorecard_api_operations" for name in API_OPERATOR_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.use_scorecard_uploads" for name in UPLOAD_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_basel_templates" for name in BASEL_TEMPLATE_VIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_basel_templates" for name in BASEL_TEMPLATE_MANAGE_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_ifrs9_templates" for name in IFRS9_TEMPLATE_VIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_ifrs9_templates" for name in IFRS9_TEMPLATE_MANAGE_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_basel_scores" for name in BASEL_SCORE_VIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_basel_scores" for name in BASEL_SCORE_MANAGE_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.review_basel_scores" for name in BASEL_SCORE_REVIEW_ROUTES})
ROUTE_PERMISSION_MAP["basel_scores_override_grade"] = (
    "scorecard.manage_basel_scores",
    "scorecard.review_basel_scores",
)
ROUTE_PERMISSION_MAP.update({name: "scorecard.reopen_basel_scores" for name in BASEL_SCORE_REOPEN_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_ifrs9_scores" for name in IFRS9_SCORE_VIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_ifrs9_scores" for name in IFRS9_SCORE_MANAGE_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.review_ifrs9_scores" for name in IFRS9_SCORE_REVIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.reopen_ifrs9_scores" for name in IFRS9_SCORE_REOPEN_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_ifrs9_supporting_data" for name in IFRS9_SUPPORTING_DATA_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_scorecard_historical_scores" for name in SCORECARD_HISTORICAL_SCORE_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_scorecard_historical_scores" for name in SCORECARD_HISTORICAL_SCORE_MANAGE_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_ifrs9_results" for name in IFRS9_RESULTS_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_basel_templates" for name in BASEL_TEMPLATE_WORKFLOW_MANAGE_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.review_basel_templates" for name in BASEL_TEMPLATE_WORKFLOW_REVIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_ifrs9_templates" for name in IFRS9_TEMPLATE_WORKFLOW_MANAGE_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.review_ifrs9_templates" for name in IFRS9_TEMPLATE_WORKFLOW_REVIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({
    name: (
        "scorecard.review_basel_scores",
        "scorecard.review_ifrs9_scores",
        "scorecard.review_basel_templates",
        "scorecard.review_ifrs9_templates",
    )
    for name in CHECKER_APPROVALS_ROUTES
})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_scorecard_customers" for name in CUSTOMER_VIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_scorecard_customers" for name in CUSTOMER_MANAGE_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_scorecard_branches" for name in BRANCH_VIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_scorecard_branches" for name in BRANCH_MANAGE_ROUTES})
ROUTE_PERMISSION_MAP.update({name: NOTIFICATION_ACCESS_PERMISSIONS for name in NOTIFICATION_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_scorecard_documents" for name in DOCUMENT_VIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.manage_scorecard_documents" for name in DOCUMENT_MANAGE_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_scorecard_email" for name in EMAIL_VIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_scorecard_audit_trail" for name in AUDIT_TRAIL_VIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_scorecard_permissions" for name in SETTINGS_VIEW_ROUTES})
ROUTE_PERMISSION_MAP.update({name: "scorecard.view_scorecard_workflow" for name in WORKFLOW_VIEW_ROUTES})


MODULE_ACCESS_REGISTRY = [
    {
        "key": "workspace",
        "label": "Scorecard Workspace",
        "views_file": "scorecard/functions_view/dashboard.py",
        "template_folder": "scorecard/templates/credit_scoreshifts",
        "notes": "Controls access to the dashboard workspace and active branch switching.",
        "permission_codes": [
            "access_scorecard_dashboard",
            "view_full_scorecard_dashboard",
        ],
        "route_names": DASHBOARD_ROUTES,
    },
    {
        "key": "api",
        "label": "Scorecard API",
        "views_file": "scorecard/functions_view/api.py",
        "template_folder": "scorecard/templates/api",
        "notes": "Covers API monitoring, settings, endpoint maintenance, scheduler automation, manual imports, and main customer sync.",
        "permission_codes": [
            "view_scorecard_api",
            "manage_scorecard_api_settings",
            "manage_scorecard_api_endpoints",
            "manage_scorecard_api_schedules",
            "run_scorecard_api_operations",
        ],
        "route_names": API_VIEW_ROUTES + API_SETTINGS_ROUTES + API_ENDPOINT_ROUTES + API_SCHEDULE_ROUTES + API_OPERATOR_ROUTES,
    },
    {
        "key": "uploads",
        "label": "External Imports",
        "views_file": "scorecard/functions_view/uploads.py",
        "template_folder": "scorecard/templates/uploads",
        "notes": "Covers the external file upload, mapping, preview, and import workflow.",
        "permission_codes": ["use_scorecard_uploads"],
        "route_names": UPLOAD_ROUTES,
    },
    {
        "key": "basel_templates",
        "label": "Basel Templates",
        "views_file": "scorecard/functions_view/credit_scoreshits.py + creditscorecreate.py + template_maker_checker.py",
        "template_folder": "scorecard/templates/credit_scoreshifts",
        "notes": "Controls Basel template setup through the template builder, template maintenance pages, and the Basel template maker-checker workflow.",
        "permission_codes": [
            "view_basel_templates",
            "manage_basel_templates",
            "review_basel_templates",
        ],
        "route_names": BASEL_TEMPLATE_VIEW_ROUTES + BASEL_TEMPLATE_MANAGE_ROUTES + BASEL_TEMPLATE_WORKFLOW_MANAGE_ROUTES + BASEL_TEMPLATE_WORKFLOW_REVIEW_ROUTES,
    },
    {
        "key": "ifrs9_templates",
        "label": "IFRS9 Templates",
        "views_file": "scorecard/functions_view/ifrs9_score_config.py + template_maker_checker.py",
        "template_folder": "scorecard/templates/ifrs9_score_config",
        "notes": "Controls IFRS9 template setup through the template builder, template maintenance pages, and the IFRS9 template maker-checker workflow.",
        "permission_codes": [
            "view_ifrs9_templates",
            "manage_ifrs9_templates",
            "review_ifrs9_templates",
        ],
        "route_names": IFRS9_TEMPLATE_VIEW_ROUTES + IFRS9_TEMPLATE_MANAGE_ROUTES + IFRS9_TEMPLATE_WORKFLOW_MANAGE_ROUTES + IFRS9_TEMPLATE_WORKFLOW_REVIEW_ROUTES,
    },
    {
        "key": "basel_scores",
        "label": "Basel Scores",
        "views_file": "scorecard/functions_view/basel_scores_form.py + maker_checker.py",
        "template_folder": "scorecard/templates/credit_scoreshifts/basel_scores_form",
        "notes": "Controls Basel score creation, review, history, exports, and reopen actions.",
        "permission_codes": [
            "view_basel_scores",
            "manage_basel_scores",
            "review_basel_scores",
            "reopen_basel_scores",
        ],
        "route_names": BASEL_SCORE_VIEW_ROUTES + BASEL_SCORE_MANAGE_ROUTES + BASEL_SCORE_REVIEW_ROUTES + BASEL_SCORE_REOPEN_ROUTES,
    },
    {
        "key": "ifrs9_scores",
        "label": "IFRS9 Scores",
        "views_file": "scorecard/functions_view/ifrs9_scores_form.py + maker_checker.py",
        "template_folder": "scorecard/templates/ifrs9_score_config/ifrs9_form",
        "notes": "Controls IFRS9 score creation, review, history, exports, and reopen actions.",
        "permission_codes": [
            "view_ifrs9_scores",
            "manage_ifrs9_scores",
            "review_ifrs9_scores",
            "reopen_ifrs9_scores",
        ],
        "route_names": IFRS9_SCORE_VIEW_ROUTES + IFRS9_SCORE_MANAGE_ROUTES + IFRS9_SCORE_REVIEW_ROUTES + IFRS9_SCORE_REOPEN_ROUTES,
    },
    {
        "key": "ifrs9_supporting_data",
        "label": "IFRS9 Supporting Data",
        "views_file": "scorecard/functions_view/ifrs9_supporting_data.py",
        "template_folder": "scorecard/templates/credit_scoreshifts/ifrs9_supporting_data.html",
        "notes": "Lets authorized users maintain branch-scoped collateral and payment schedule staging data for active IFRS9 loans.",
        "permission_codes": [
            "manage_ifrs9_supporting_data",
        ],
        "route_names": IFRS9_SUPPORTING_DATA_ROUTES,
    },
    {
        "key": "ifrs9_results",
        "label": "IFRS9 Results",
        "views_file": "scorecard/functions_view/ifrs9_results.py",
        "template_folder": "scorecard/templates/credit_scoreshifts",
        "notes": "Lets authorized users review branch-scoped IFRS9 results extracts and ECL summary reports using the latest run key per reporting date.",
        "permission_codes": [
            "view_ifrs9_results",
        ],
        "route_names": IFRS9_RESULTS_ROUTES,
    },
    {
        "key": "historical_scores",
        "label": "Historical Scores",
        "views_file": "scorecard/functions_view/scorecard_historical_scores.py",
        "template_folder": "scorecard/templates/scorecard_historical_scores",
        "notes": "Shows branch-scoped scheduled score snapshots from SCORECARD_HISTORICAL_SCORES.",
        "permission_codes": [
            "view_scorecard_historical_scores",
            "manage_scorecard_historical_scores",
        ],
        "route_names": SCORECARD_HISTORICAL_SCORE_ROUTES + SCORECARD_HISTORICAL_SCORE_MANAGE_ROUTES,
    },
    {
        "key": "customers",
        "label": "Customers",
        "views_file": "scorecard/functions_view/customers.py",
        "template_folder": "scorecard/templates/credit_scoreshifts/customers",
        "notes": "Controls customer listing, exports, gap analysis lists, and customer maintenance.",
        "permission_codes": [
            "view_scorecard_customers",
            "manage_scorecard_customers",
        ],
        "route_names": CUSTOMER_VIEW_ROUTES + CUSTOMER_MANAGE_ROUTES,
    },
    {
        "key": "branches",
        "label": "Branches",
        "views_file": "scorecard/functions_view/customers.py",
        "template_folder": "scorecard/templates/credit_scoreshifts/branches",
        "notes": "Branch Viewer opens Branch Master in read-only mode, while Branch Manager can maintain branch master data.",
        "permission_codes": [
            "view_scorecard_branches",
            "manage_scorecard_branches",
        ],
        "route_names": BRANCH_VIEW_ROUTES + BRANCH_MANAGE_ROUTES,
    },
    {
        "key": "documents",
        "label": "Documents",
        "views_file": "scorecard/functions_view/media.py",
        "template_folder": "scorecard/templates/media",
        "notes": "Controls questionnaire and scorecard document libraries, uploads, and downloads.",
        "permission_codes": [
            "view_scorecard_documents",
            "manage_scorecard_documents",
        ],
        "route_names": DOCUMENT_VIEW_ROUTES + DOCUMENT_MANAGE_ROUTES,
    },
    {
        "key": "notifications",
        "label": "Notifications",
        "views_file": "scorecard/functions_view/notifications.py",
        "template_folder": "scorecard/templates/notifications",
        "notes": "Controls access to the scorecard notification centre.",
        "permission_codes": ["view_scorecard_notifications"],
        "route_names": NOTIFICATION_ROUTES,
    },
    {
        "key": "email",
        "label": "Email Administration",
        "views_file": "scorecard/functions_view/email.py",
        "template_folder": "scorecard/templates/email",
        "notes": "Controls scorecard email configuration, template editing, and delivery activity.",
        "permission_codes": [
            "view_scorecard_email",
            "manage_scorecard_email",
        ],
        "route_names": EMAIL_VIEW_ROUTES,
    },
    {
        "key": "audit",
        "label": "Audit Trail",
        "views_file": "scorecard/functions_view/settings.py",
        "template_folder": "scorecard/templates/settings",
        "notes": "Controls access to the standalone scorecard audit trail and export of scorecard-wide activity history.",
        "permission_codes": [
            "view_scorecard_audit_trail",
        ],
        "route_names": AUDIT_TRAIL_VIEW_ROUTES,
    },
    {
        "key": "settings",
        "label": "Permission Settings",
        "views_file": "scorecard/functions_view/settings.py",
        "template_folder": "scorecard/templates/settings",
        "notes": "Controls permission dashboards, scorecard role assignment, and permission matrix visibility.",
        "permission_codes": [
            "view_scorecard_permissions",
            "assign_scorecard_permissions",
        ],
        "route_names": SETTINGS_VIEW_ROUTES,
    },
    {
        "key": "workflow",
        "label": "Workflow Rules",
        "views_file": "scorecard/functions_view/settings.py",
        "template_folder": "scorecard/templates/settings",
        "notes": "Controls standalone workflow rules for approvals, scoring locks, notification routing, and without-score list sources.",
        "permission_codes": [
            "view_scorecard_workflow",
            "manage_scorecard_workflow",
        ],
        "route_names": WORKFLOW_VIEW_ROUTES,
    },
]


DEFAULT_ROLE_DEFINITIONS = [
    {
        "name": "Workspace Access",
        "legacy_names": ["Scorecard Workspace - Access"],
        "label": "Workspace Access",
        "module_key": "workspace",
        "module_label": "Scorecard Workspace",
        "level_key": "view",
        "level_label": "View",
        "description": "Open the scorecard workspace and use branch switching.",
        "access_badges": ["View Only"],
        "action_summary": ["Open the dashboard", "Switch active branches"],
        "permissions": ["scorecard.access_scorecard_dashboard"],
    },
    {
        "name": "Full Dashboard Viewer",
        "legacy_names": ["Scorecard Dashboard - Full Viewer"],
        "label": "Full Dashboard Viewer",
        "module_key": "workspace",
        "module_label": "Scorecard Workspace",
        "level_key": "view",
        "level_label": "View",
        "description": "Open the dashboard and view every dashboard statistic, chart, and activity panel without opening the underlying workspaces.",
        "access_badges": ["View Only"],
        "action_summary": ["Open the dashboard", "View all dashboard content", "Switch assigned branches"],
        "permissions": [
            "scorecard.access_scorecard_dashboard",
            "scorecard.view_full_scorecard_dashboard",
        ],
    },
    {
        "name": "Notification Viewer",
        "legacy_names": ["Scorecard Notifications - Viewer"],
        "label": "Notification Viewer",
        "module_key": "notifications",
        "module_label": "Notifications",
        "level_key": "view",
        "level_label": "View",
        "description": "Read scorecard alerts and open notification details.",
        "access_badges": ["View Only"],
        "action_summary": ["Open notifications", "Mark alerts as read"],
        "permissions": ["scorecard.view_scorecard_notifications"],
    },
    {
        "name": "API Viewer",
        "legacy_names": ["Scorecard API - Viewer"],
        "label": "API Viewer",
        "module_key": "api",
        "module_label": "Scorecard API",
        "level_key": "view",
        "level_label": "View",
        "description": "Review API health, import history, and sync monitoring pages.",
        "access_badges": ["View Only"],
        "action_summary": ["Open API dashboards", "Review health and history"],
        "permissions": ["scorecard.view_scorecard_api"],
    },
    {
        "name": "API Settings Manager",
        "legacy_names": ["Scorecard API - Settings Manager"],
        "label": "API Settings Manager",
        "module_key": "api",
        "module_label": "Scorecard API",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Maintain API configuration and main customer auto-sync settings.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Maintain API settings", "Update sync configuration"],
        "permissions": ["scorecard.view_scorecard_api", "scorecard.manage_scorecard_api_settings"],
    },
    {
        "name": "API Endpoint Manager",
        "legacy_names": ["Scorecard API - Endpoints Manager"],
        "label": "API Endpoint Manager",
        "module_key": "api",
        "module_label": "Scorecard API",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Create, edit, and retire API endpoint definitions.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Create endpoints", "Edit and retire endpoints"],
        "permissions": ["scorecard.view_scorecard_api", "scorecard.manage_scorecard_api_endpoints"],
    },
    {
        "name": "API Scheduler Manager",
        "legacy_names": ["Scorecard API - Scheduler Manager"],
        "label": "API Scheduler Manager",
        "module_key": "api",
        "module_label": "Scorecard API",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Maintain API schedules and automation timing.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Create schedules", "Edit or delete automation entries"],
        "permissions": ["scorecard.view_scorecard_api", "scorecard.manage_scorecard_api_schedules"],
    },
    {
        "name": "API Operator",
        "legacy_names": ["Scorecard API - Operator"],
        "label": "API Operator",
        "module_key": "api",
        "module_label": "Scorecard API",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Run API tests, manual imports, retries, and main customer sync operations.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Run manual imports", "Run tests and syncs"],
        "permissions": ["scorecard.view_scorecard_api", "scorecard.run_scorecard_api_operations"],
    },
    {
        "name": "API Administrator",
        "legacy_names": ["Scorecard API - Admin"],
        "label": "API Administrator",
        "module_key": "api",
        "module_label": "Scorecard API",
        "level_key": "admin",
        "level_label": "Admin",
        "description": "Full API control across settings, endpoints, schedules, and operations.",
        "access_badges": ["Can View", "Can Work", "Admin"],
        "action_summary": ["Manage all API settings", "Run all API operations"],
        "permissions": [
            "scorecard.view_scorecard_api",
            "scorecard.manage_scorecard_api_settings",
            "scorecard.manage_scorecard_api_endpoints",
            "scorecard.manage_scorecard_api_schedules",
            "scorecard.run_scorecard_api_operations",
        ],
    },
    {
        "name": "External Import Operator",
        "legacy_names": ["External Imports - Operator"],
        "label": "External Import Operator",
        "module_key": "uploads",
        "module_label": "External Imports",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Upload, map, validate, and import external Basel, IFRS9, and historical score files.",
        "access_badges": ["Can Work"],
        "action_summary": ["Upload files", "Preview mapping errors", "Run Basel, IFRS9, and historical imports"],
        "permissions": ["scorecard.use_scorecard_uploads"],
    },
    {
        "name": "Basel Template Viewer",
        "legacy_names": ["Basel Templates - Viewer"],
        "label": "Basel Template Viewer",
        "module_key": "basel_templates",
        "module_label": "Basel Templates",
        "level_key": "view",
        "level_label": "View",
        "description": "Review Basel scorecard templates, builder structure, and grade bands without editing them.",
        "access_badges": ["View Only"],
        "action_summary": ["Open template library", "Review builder structure"],
        "permissions": ["scorecard.view_basel_templates"],
    },
    {
        "name": "Basel Template Manager",
        "legacy_names": ["Basel Templates - Manager"],
        "label": "Basel Template Manager",
        "module_key": "basel_templates",
        "module_label": "Basel Templates",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Create and maintain Basel templates, including the builder flow, sections, drivers, attributes, options, and grade bands.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Create templates", "Build and maintain template hierarchy", "Maintain grade bands"],
        "permissions": ["scorecard.view_basel_templates", "scorecard.manage_basel_templates"],
    },
    {
        "name": "Basel Template Checker",
        "legacy_names": ["Basel Templates - Reviewer"],
        "label": "Basel Template Checker",
        "module_key": "basel_templates",
        "module_label": "Basel Templates",
        "level_key": "approve",
        "level_label": "Can Approve",
        "description": "Review, approve, return, and reopen Basel template workflow items.",
        "access_badges": ["Can Approve"],
        "action_summary": ["Approve templates", "Review builder changes", "Return templates for rework"],
        "permissions": ["scorecard.review_basel_templates"],
    },
    {
        "name": "Basel Template Administrator",
        "legacy_names": ["Basel Templates - Admin"],
        "label": "Basel Template Administrator",
        "module_key": "basel_templates",
        "module_label": "Basel Templates",
        "level_key": "admin",
        "level_label": "Admin",
        "description": "Full Basel template control across the builder, setup pages, and approval workflow.",
        "access_badges": ["Can View", "Can Work", "Can Approve", "Admin"],
        "action_summary": ["Manage templates", "Control builder structure", "Approve template workflow"],
        "permissions": [
            "scorecard.view_basel_templates",
            "scorecard.manage_basel_templates",
            "scorecard.review_basel_templates",
        ],
    },
    {
        "name": "IFRS9 Template Viewer",
        "legacy_names": ["IFRS9 Templates - Viewer"],
        "label": "IFRS9 Template Viewer",
        "module_key": "ifrs9_templates",
        "module_label": "IFRS9 Templates",
        "level_key": "view",
        "level_label": "View",
        "description": "Review IFRS9 template structures and builder layout without changing them.",
        "access_badges": ["View Only"],
        "action_summary": ["Open IFRS9 template library", "Review builder structure"],
        "permissions": ["scorecard.view_ifrs9_templates"],
    },
    {
        "name": "IFRS9 Template Manager",
        "legacy_names": ["IFRS9 Templates - Manager"],
        "label": "IFRS9 Template Manager",
        "module_key": "ifrs9_templates",
        "module_label": "IFRS9 Templates",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Create and maintain IFRS9 templates, including the builder flow, sections, drivers, attributes, and options.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Create templates", "Build and maintain template hierarchy"],
        "permissions": ["scorecard.view_ifrs9_templates", "scorecard.manage_ifrs9_templates"],
    },
    {
        "name": "IFRS9 Template Checker",
        "legacy_names": ["IFRS9 Templates - Reviewer"],
        "label": "IFRS9 Template Checker",
        "module_key": "ifrs9_templates",
        "module_label": "IFRS9 Templates",
        "level_key": "approve",
        "level_label": "Can Approve",
        "description": "Review, approve, return, and reopen IFRS9 template workflow items.",
        "access_badges": ["Can Approve"],
        "action_summary": ["Approve templates", "Review builder changes", "Return templates for rework"],
        "permissions": ["scorecard.review_ifrs9_templates"],
    },
    {
        "name": "IFRS9 Template Administrator",
        "legacy_names": ["IFRS9 Templates - Admin"],
        "label": "IFRS9 Template Administrator",
        "module_key": "ifrs9_templates",
        "module_label": "IFRS9 Templates",
        "level_key": "admin",
        "level_label": "Admin",
        "description": "Full IFRS9 template control across the builder, setup pages, and approval workflow.",
        "access_badges": ["Can View", "Can Work", "Can Approve", "Admin"],
        "action_summary": ["Manage templates", "Control builder structure", "Approve template workflow"],
        "permissions": [
            "scorecard.view_ifrs9_templates",
            "scorecard.manage_ifrs9_templates",
            "scorecard.review_ifrs9_templates",
        ],
    },
    {
        "name": "Basel Score Viewer",
        "legacy_names": ["Basel Scores - Viewer"],
        "label": "Basel Score Viewer",
        "module_key": "basel_scores",
        "module_label": "Basel Scores",
        "level_key": "view",
        "level_label": "View",
        "description": "Review Basel score submissions, comparisons, history, and exports.",
        "access_badges": ["View Only"],
        "action_summary": ["Open submitted scores", "Review history and exports"],
        "permissions": ["scorecard.view_basel_scores"],
    },
    {
        "name": "Basel Score Maker",
        "legacy_names": ["Basel Scores - Maker"],
        "label": "Basel Score Maker",
        "module_key": "basel_scores",
        "module_label": "Basel Scores",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Create, edit, submit, and manage Basel score evaluations.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Create scores", "Submit for review", "Assign templates"],
        "permissions": ["scorecard.view_basel_scores", "scorecard.manage_basel_scores"],
    },
    {
        "name": "Basel Score Checker",
        "legacy_names": ["Basel Scores - Checker"],
        "label": "Basel Score Checker",
        "module_key": "basel_scores",
        "module_label": "Basel Scores",
        "level_key": "approve",
        "level_label": "Can Approve",
        "description": "Review, approve, and return Basel score evaluations.",
        "access_badges": ["Can Approve"],
        "action_summary": ["Approve scores", "Return scores for changes"],
        "permissions": ["scorecard.review_basel_scores"],
    },
    {
        "name": "Basel Score Reopen Administrator",
        "legacy_names": ["Basel Scores - Reopen Admin"],
        "label": "Basel Score Reopen Administrator",
        "module_key": "basel_scores",
        "module_label": "Basel Scores",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Reopen approved Basel evaluations for correction or rework.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Reopen approved Basel scores"],
        "permissions": ["scorecard.view_basel_scores", "scorecard.reopen_basel_scores"],
    },
    {
        "name": "Basel Score Administrator",
        "legacy_names": ["Basel Scores - Admin"],
        "label": "Basel Score Administrator",
        "module_key": "basel_scores",
        "module_label": "Basel Scores",
        "level_key": "admin",
        "level_label": "Admin",
        "description": "Full Basel score control across creation, review, and reopen actions.",
        "access_badges": ["Can View", "Can Work", "Can Approve", "Admin"],
        "action_summary": ["Manage scores", "Approve workflow", "Reopen approved scores"],
        "permissions": [
            "scorecard.view_basel_scores",
            "scorecard.manage_basel_scores",
            "scorecard.review_basel_scores",
            "scorecard.reopen_basel_scores",
        ],
    },
    {
        "name": "IFRS9 Score Viewer",
        "legacy_names": ["IFRS9 Scores - Viewer"],
        "label": "IFRS9 Score Viewer",
        "module_key": "ifrs9_scores",
        "module_label": "IFRS9 Scores",
        "level_key": "view",
        "level_label": "View",
        "description": "Review IFRS9 score submissions, comparisons, history, and exports.",
        "access_badges": ["View Only"],
        "action_summary": ["Open submitted scores", "Review history and exports"],
        "permissions": ["scorecard.view_ifrs9_scores"],
    },
    {
        "name": "IFRS9 Score Maker",
        "legacy_names": ["IFRS9 Scores - Maker"],
        "label": "IFRS9 Score Maker",
        "module_key": "ifrs9_scores",
        "module_label": "IFRS9 Scores",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Create, edit, submit, and manage IFRS9 score forms.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Create score forms", "Submit for review", "Assign templates"],
        "permissions": ["scorecard.view_ifrs9_scores", "scorecard.manage_ifrs9_scores"],
    },
    {
        "name": "IFRS9 Score Checker",
        "legacy_names": ["IFRS9 Scores - Checker"],
        "label": "IFRS9 Score Checker",
        "module_key": "ifrs9_scores",
        "module_label": "IFRS9 Scores",
        "level_key": "approve",
        "level_label": "Can Approve",
        "description": "Review, approve, and return IFRS9 score evaluations.",
        "access_badges": ["Can Approve"],
        "action_summary": ["Approve score forms", "Return score forms for changes"],
        "permissions": ["scorecard.review_ifrs9_scores"],
    },
    {
        "name": "IFRS9 Score Reopen Administrator",
        "legacy_names": ["IFRS9 Scores - Reopen Admin"],
        "label": "IFRS9 Score Reopen Administrator",
        "module_key": "ifrs9_scores",
        "module_label": "IFRS9 Scores",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Reopen approved IFRS9 score forms for correction or rework.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Reopen approved IFRS9 score forms"],
        "permissions": ["scorecard.view_ifrs9_scores", "scorecard.reopen_ifrs9_scores"],
    },
    {
        "name": "IFRS9 Score Administrator",
        "legacy_names": ["IFRS9 Scores - Admin"],
        "label": "IFRS9 Score Administrator",
        "module_key": "ifrs9_scores",
        "module_label": "IFRS9 Scores",
        "level_key": "admin",
        "level_label": "Admin",
        "description": "Full IFRS9 score control across creation, review, and reopen actions.",
        "access_badges": ["Can View", "Can Work", "Can Approve", "Admin"],
        "action_summary": ["Manage score forms", "Approve workflow", "Reopen approved forms"],
        "permissions": [
            "scorecard.view_ifrs9_scores",
            "scorecard.manage_ifrs9_scores",
            "scorecard.review_ifrs9_scores",
            "scorecard.reopen_ifrs9_scores",
        ],
    },
    {
        "name": "IFRS9 Supporting Data Manager",
        "legacy_names": ["IFRS9 Supporting Data - Manager"],
        "label": "IFRS9 Supporting Data Manager",
        "module_key": "ifrs9_supporting_data",
        "module_label": "IFRS9 Supporting Data",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Maintain branch-scoped collateral and payment schedule staging records for active IFRS9 loans.",
        "access_badges": ["Can Work"],
        "action_summary": ["Add or update payment schedules", "Add or update collateral records"],
        "permissions": ["scorecard.manage_ifrs9_supporting_data"],
    },
    {
        "name": "IFRS9 Results Viewer",
        "legacy_names": ["IFRS9 Results - Viewer"],
        "label": "IFRS9 Results Viewer",
        "module_key": "ifrs9_results",
        "module_label": "IFRS9 Results",
        "level_key": "view",
        "level_label": "View",
        "description": "Open branch-scoped IFRS9 results extracts and ECL summary reporting workspaces.",
        "access_badges": ["View Only"],
        "action_summary": ["Open results extract", "Open ECL summary report"],
        "permissions": ["scorecard.view_ifrs9_results"],
    },
    {
        "name": "Historical Score Viewer",
        "legacy_names": ["Historical Scores - Viewer"],
        "label": "Historical Score Viewer",
        "module_key": "historical_scores",
        "module_label": "Historical Scores",
        "level_key": "view",
        "level_label": "View",
        "description": "Review branch-scoped historical score snapshots and download filtered historical score extracts.",
        "access_badges": ["View Only"],
        "action_summary": ["Open historical scores", "Download filtered historical scores"],
        "permissions": ["scorecard.view_scorecard_historical_scores"],
    },
    {
        "name": "Historical Score Administrator",
        "legacy_names": ["Historical Scores - Admin"],
        "label": "Historical Score Administrator",
        "module_key": "historical_scores",
        "module_label": "Historical Scores",
        "level_key": "admin",
        "level_label": "Admin",
        "description": "Refresh and bulk-delete historical score snapshots without granting API administration.",
        "access_badges": ["Can View", "Admin"],
        "action_summary": ["Open historical scores", "Refresh selected reporting dates", "Delete filtered historical rows"],
        "permissions": [
            "scorecard.view_scorecard_historical_scores",
            "scorecard.manage_scorecard_historical_scores",
        ],
    },
    {
        "name": "Customer Viewer",
        "legacy_names": ["Customers - Viewer"],
        "label": "Customer Viewer",
        "module_key": "customers",
        "module_label": "Customers",
        "level_key": "view",
        "level_label": "View",
        "description": "Review customer directories, exports, and score gap lists.",
        "access_badges": ["View Only"],
        "action_summary": ["Open customers", "Review gap lists", "Export customer data"],
        "permissions": ["scorecard.view_scorecard_customers"],
    },
    {
        "name": "Customer Manager",
        "legacy_names": ["Customers - Manager"],
        "label": "Customer Manager",
        "module_key": "customers",
        "module_label": "Customers",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Create and maintain scorecard customer records.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Add customers", "Maintain customer records"],
        "permissions": ["scorecard.view_scorecard_customers", "scorecard.manage_scorecard_customers"],
    },
    {
        "name": "Branch Viewer",
        "legacy_names": ["Branches - Viewer"],
        "label": "Branch Viewer",
        "module_key": "branches",
        "module_label": "Branches",
        "level_key": "view",
        "level_label": "View",
        "description": "Open the branch master workspace in read-only mode.",
        "access_badges": ["View Only"],
        "action_summary": ["Open branch master (read-only)"],
        "permissions": ["scorecard.view_scorecard_branches"],
    },
    {
        "name": "Branch Manager",
        "legacy_names": ["Branches - Manager"],
        "label": "Branch Manager",
        "module_key": "branches",
        "module_label": "Branches",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Maintain branch master data.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Maintain branch master data"],
        "permissions": ["scorecard.view_scorecard_branches", "scorecard.manage_scorecard_branches"],
    },
    {
        "name": "Document Viewer",
        "legacy_names": ["Documents - Viewer"],
        "label": "Document Viewer",
        "module_key": "documents",
        "module_label": "Documents",
        "level_key": "view",
        "level_label": "View",
        "description": "Open questionnaire and scorecard document libraries.",
        "access_badges": ["View Only"],
        "action_summary": ["Open document libraries", "Download scorecard documents"],
        "permissions": ["scorecard.view_scorecard_documents"],
    },
    {
        "name": "Document Manager",
        "legacy_names": ["Documents - Manager"],
        "label": "Document Manager",
        "module_key": "documents",
        "module_label": "Documents",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Upload and maintain scorecard document records.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Upload documents", "Maintain document records"],
        "permissions": ["scorecard.view_scorecard_documents", "scorecard.manage_scorecard_documents"],
    },
    {
        "name": "Email Viewer",
        "legacy_names": ["Email Administration - Viewer"],
        "label": "Email Viewer",
        "module_key": "email",
        "module_label": "Email Administration",
        "level_key": "view",
        "level_label": "View",
        "description": "Open email configuration, templates, and delivery history in read-only mode.",
        "access_badges": ["View Only"],
        "action_summary": ["Review email settings", "Review delivery history"],
        "permissions": ["scorecard.view_scorecard_email"],
    },
    {
        "name": "Email Manager",
        "legacy_names": ["Email Administration - Manager"],
        "label": "Email Manager",
        "module_key": "email",
        "module_label": "Email Administration",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Maintain email configuration, templates, and retry actions.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Edit email templates", "Retry failed emails", "Maintain configuration"],
        "permissions": ["scorecard.view_scorecard_email", "scorecard.manage_scorecard_email"],
    },
    {
        "name": "Audit Trail Viewer",
        "legacy_names": ["Scorecard Audit Trail - Viewer"],
        "label": "Audit Trail Viewer",
        "module_key": "audit",
        "module_label": "Audit Trail",
        "level_key": "view",
        "level_label": "View",
        "description": "Open the standalone scorecard audit trail page and review scorecard-wide activity history.",
        "access_badges": ["View Only"],
        "action_summary": ["Open audit trail", "Review scorecard activity history", "Export audit history"],
        "permissions": ["scorecard.view_scorecard_audit_trail"],
    },
    {
        "name": "Permission Viewer",
        "legacy_names": ["Permission Settings - Viewer"],
        "label": "Permission Viewer",
        "module_key": "settings",
        "module_label": "Permission Settings",
        "level_key": "view",
        "level_label": "View",
        "description": "Review the permission dashboard, user access summary, and matrix.",
        "access_badges": ["View Only"],
        "action_summary": ["Open permission dashboard", "Review user roles", "Review permission matrix"],
        "permissions": ["scorecard.view_scorecard_permissions"],
    },
    {
        "name": "Permission Manager",
        "legacy_names": ["Permission Settings - Manager"],
        "label": "Permission Manager",
        "module_key": "settings",
        "module_label": "Permission Settings",
        "level_key": "work",
        "level_label": "Can Assign",
        "description": "Assign scorecard permission roles and branch access.",
        "access_badges": ["Can View", "Can Assign"],
        "action_summary": ["Assign scorecard roles", "Maintain branch access", "Maintain permission ownership"],
        "permissions": [
            "scorecard.view_scorecard_permissions",
            "scorecard.assign_scorecard_permissions",
        ],
    },
    {
        "name": "Workflow Viewer",
        "legacy_names": ["Workflow Rules - Viewer"],
        "label": "Workflow Viewer",
        "module_key": "workflow",
        "module_label": "Workflow Rules",
        "level_key": "view",
        "level_label": "View",
        "description": "Open workflow rules in read-only mode without permission-administration access.",
        "access_badges": ["View Only"],
        "action_summary": ["Open workflow rules", "Review saved workflow controls"],
        "permissions": ["scorecard.view_scorecard_workflow"],
    },
    {
        "name": "Workflow Manager",
        "legacy_names": ["Workflow Rules - Manager"],
        "label": "Workflow Manager",
        "module_key": "workflow",
        "module_label": "Workflow Rules",
        "level_key": "work",
        "level_label": "Can Work",
        "description": "Edit workflow rules without granting user-role or permission-matrix administration.",
        "access_badges": ["Can View", "Can Work"],
        "action_summary": ["Open workflow rules", "Edit workflow controls"],
        "permissions": [
            "scorecard.view_scorecard_workflow",
            "scorecard.manage_scorecard_workflow",
        ],
    },
    {
        "name": "Scorecard Administrator",
        "legacy_names": ["Scorecard - Full Admin"],
        "label": "Scorecard Administrator",
        "module_key": "platform",
        "module_label": "All Scorecard Areas",
        "level_key": "admin",
        "level_label": "Admin",
        "description": "Full scorecard access across workspace, API, templates, scores, customers, documents, email, workflow, and permission settings.",
        "access_badges": ["Can View", "Can Work", "Can Approve", "Admin"],
        "action_summary": ["Manage all scorecard areas", "Approve workflow items", "Assign permissions"],
        "permissions": [f"scorecard.{item['codename']}" for item in SCORECARD_PERMISSION_DEFINITIONS],
    },
]
