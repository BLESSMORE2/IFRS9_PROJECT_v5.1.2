# from django.contrib import admin

# from scorecard.models import (
#     ApiConfiguration,
#     ApiConfigurationParameter,
#     ApiEndpoint,
#     ApiImportSchedule,
#     CustomerCorporate,
#     CustomerIndividual,
#     MainCustomer,
# )


# @admin.register(MainCustomer)
# class MainCustomerAdmin(admin.ModelAdmin):
#     list_display = (
#         "reporting_date",
#         "customer_ref_code",
#         "customer_name",
#         "branch_code",
#         "branch_name",
#         "customer_type",
#         "loan_count",
#         "overdraft_count",
#         "is_active_for_scoring",
#     )
#     search_fields = ("customer_ref_code", "customer_name", "branch_code", "branch_name", "email", "mobile")
#     list_filter = ("reporting_date", "customer_type", "branch_name", "is_active_for_scoring")


# @admin.register(CustomerCorporate)
# class CustomerCorporateAdmin(admin.ModelAdmin):
#     list_display = ("client_code", "client_name", "industry_code", "country_code", "years_in_business")
#     search_fields = ("client_code", "client_name", "registration_number")
#     list_filter = ("country_code", "resident_status", "organization_qualifier")


# @admin.register(CustomerIndividual)
# class CustomerIndividualAdmin(admin.ModelAdmin):
#     list_display = ("client_code", "first_name", "surname", "gender", "nationality_code", "mobile_number")
#     search_fields = ("client_code", "first_name", "surname", "mobile_number", "email_primary")
#     list_filter = ("gender", "resident_status", "nationality_code", "marital_status")


# @admin.register(ApiConfiguration)
# class ApiConfigurationAdmin(admin.ModelAdmin):
#     list_display = ("name", "base_url", "auth_header_name", "timeout_seconds", "is_active", "updated_at")
#     search_fields = ("name", "base_url", "auth_header_name")
#     list_filter = ("is_active",)


# @admin.register(ApiEndpoint)
# class ApiEndpointAdmin(admin.ModelAdmin):
#     list_display = ("name", "code", "path", "target_table", "http_method", "is_active", "last_test_status", "last_tested_at")
#     search_fields = ("name", "code", "path")
#     list_filter = ("target_table", "is_active", "http_method", "last_test_status")


# @admin.register(ApiConfigurationParameter)
# class ApiConfigurationParameterAdmin(admin.ModelAdmin):
#     list_display = (
#         "name",
#         "configuration",
#         "default_value",
#         "display_order",
#         "is_required",
#         "use_for_testing",
#         "use_for_retrieval",
#         "is_active",
#     )
#     search_fields = ("name", "default_value", "configuration__name")
#     list_filter = ("is_required", "use_for_testing", "use_for_retrieval", "is_active")


# @admin.register(ApiImportSchedule)
# class ApiImportScheduleAdmin(admin.ModelAdmin):
#     list_display = (
#         "name",
#         "endpoint",
#         "frequency",
#         "run_time",
#         "is_active",
#         "last_status",
#         "last_run_at",
#     )
#     search_fields = ("name", "endpoint__name", "endpoint__code")
#     list_filter = ("frequency", "is_active", "last_status", "reporting_date_mode")

