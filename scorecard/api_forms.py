from django import forms
from django.forms import modelformset_factory

from scorecard.models import (
    ApiConfiguration,
    ApiConfigurationParameter,
    ApiEndpoint,
    ApiImportSchedule,
    ApiMainSyncConfiguration,
    ApiSchedulerServiceStatus,
)


class ApiConfigurationForm(forms.ModelForm):
    base_url = forms.URLField(
        assume_scheme="https",
        max_length=500,
        help_text="Main API host, for example http://192.168.4.48:8090",
        widget=forms.URLInput(attrs={"placeholder": "http://192.168.4.48:8090"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        has_saved_instance = bool(getattr(self.instance, "pk", None))
        secret_field = self.fields["auth_secret_key"]
        if has_saved_instance:
            secret_field.required = False
            secret_field.help_text = "Leave this blank to keep the currently saved secret key."
            secret_field.widget.attrs["placeholder"] = "Leave blank to keep current secret"
        else:
            secret_field.help_text = "This is the value sent with the header above when calling the API."
            secret_field.widget.attrs["placeholder"] = "Enter API secret key"

    def clean_auth_secret_key(self):
        value = (self.cleaned_data.get("auth_secret_key") or "").strip()
        if value:
            return value
        if getattr(self.instance, "pk", None):
            return self.instance.auth_secret_key
        return value

    class Meta:
        model = ApiConfiguration
        fields = [
            "name",
            "base_url",
            "auth_header_name",
            "auth_secret_key",
            "timeout_seconds",
            "test_timeout_seconds",
            "browser_timeout_seconds",
            "is_active",
        ]
        labels = {
            "timeout_seconds": "Request timeout (seconds)",
            "test_timeout_seconds": "Endpoint test timeout (seconds)",
            "browser_timeout_seconds": "Browser wait timeout (seconds)",
            "auth_secret_key": "Secret key / API key",
            "auth_header_name": "Header name",
        }
        help_texts = {
            "timeout_seconds": "How many seconds the system should wait for the API before treating the request as failed.",
            "test_timeout_seconds": "How many seconds the server should spend testing an endpoint before stopping the test.",
            "browser_timeout_seconds": "How many seconds the page should wait before stopping a test request in the browser.",
            "auth_secret_key": "This is the value sent with the header above when calling the API.",
            "base_url": "Main API host, for example http://192.168.4.48:8090",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Primary API Configuration"}),
            "auth_header_name": forms.TextInput(attrs={"placeholder": "X-API-KEY"}),
            "auth_secret_key": forms.PasswordInput(render_value=False, attrs={"placeholder": "Enter API secret key", "autocomplete": "new-password"}),
            "timeout_seconds": forms.NumberInput(attrs={"min": 5}),
            "test_timeout_seconds": forms.NumberInput(attrs={"min": 1}),
            "browser_timeout_seconds": forms.NumberInput(attrs={"min": 1}),
        }


class ApiConfigurationParameterForm(forms.ModelForm):
    def clean_name(self):
        value = (self.cleaned_data.get("name") or "").strip().lower()
        if not value:
            return value
        normalized = "_".join(value.replace("-", " ").split())
        while "__" in normalized:
            normalized = normalized.replace("__", "_")
        return normalized

    class Meta:
        model = ApiConfigurationParameter
        fields = [
            "name",
            "default_value",
            "display_order",
            "is_required",
            "use_for_testing",
            "use_for_retrieval",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "reporting_date"}),
            "default_value": forms.TextInput(attrs={"placeholder": "2026-03-30"}),
            "display_order": forms.NumberInput(attrs={"min": 1}),
        }


ApiConfigurationParameterFormSet = modelformset_factory(
    ApiConfigurationParameter,
    form=ApiConfigurationParameterForm,
    can_delete=True,
    extra=0,
)


class ApiEndpointForm(forms.ModelForm):
    class Meta:
        model = ApiEndpoint
        fields = ["name", "path", "target_table", "description", "parameters", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Corporate Clients"}),
            "path": forms.TextInput(attrs={"placeholder": "/api/ecl/corporate-clients/"}),
            "target_table": forms.Select(),
            "description": forms.Textarea(attrs={"rows": 3, "placeholder": "Short description of this endpoint"}),
            "parameters": forms.CheckboxSelectMultiple(),
        }

    def __init__(self, *args, **kwargs):
        configuration = kwargs.pop("configuration", None)
        super().__init__(*args, **kwargs)
        self.configuration = configuration
        self.path_warning = None
        queryset = ApiConfigurationParameter.objects.none()
        if configuration is not None:
            queryset = ApiConfigurationParameter.objects.filter(configuration=configuration).order_by("display_order", "name")
        elif self.instance.pk:
            queryset = self.instance.parameters.order_by("display_order", "name")
        self.fields["parameters"].queryset = queryset
        self.fields["parameters"].required = False
        self.fields["parameters"].help_text = "Choose only the parameters this endpoint should use during testing and retrieval."
        self.fields["target_table"].help_text = "Choose where this endpoint should load data during import."
        if not self.is_bound and not self.instance.pk and configuration is not None:
            default_parameters = list(
                queryset.filter(name__in=["reporting_date", "branch_code"]).values_list("pk", flat=True)
            )
            if default_parameters:
                self.initial.setdefault("parameters", default_parameters)

    def clean_path(self):
        path = (self.cleaned_data.get("path") or "").strip()
        cleaned_path = path.lstrip("- ").strip()
        if "?" in cleaned_path:
            endpoint_path, query_string = cleaned_path.split("?", 1)
            self.path_warning = (
                "Query parameters were detected in the path. "
                "The path has been saved without them. "
                f"Use the retrieve page for parameters such as: {query_string}"
            )
            return endpoint_path.strip()
        return cleaned_path

    def clean(self):
        cleaned_data = super().clean()
        path = cleaned_data.get("path")
        if not path:
            return cleaned_data

        normalized_path = path.strip().rstrip("/") or "/"
        configuration = getattr(self.instance, "configuration", None) or getattr(self, "configuration", None)

        duplicate_queryset = ApiEndpoint.objects.all()
        if configuration is not None:
            duplicate_queryset = duplicate_queryset.filter(configuration=configuration)
        duplicate_queryset = duplicate_queryset.exclude(pk=self.instance.pk)

        duplicate_endpoint = None
        for existing_endpoint in duplicate_queryset.only("id", "name", "path", "target_table"):
            if (existing_endpoint.path or "").strip().rstrip("/") == normalized_path:
                duplicate_endpoint = existing_endpoint
                break

        if duplicate_endpoint is not None:
            target_label = duplicate_endpoint.get_target_table_display() or "No target table"
            self.add_error(
                "path",
                (
                    f"This path is already used by endpoint '{duplicate_endpoint.name}' "
                    f"with target table '{target_label}'. Use a different path or edit the existing endpoint."
                ),
            )

        return cleaned_data


class ApiRetrieveForm(forms.Form):
    endpoint = forms.ModelChoiceField(
        queryset=ApiEndpoint.objects.none(),
        empty_label="Select saved endpoint",
    )
    extra_query_string = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "page=1&page_size=1000",
            }
        ),
        help_text="Optional extra parameters in simple query-string format, for example page=1&page_size=1000",
    )

    def __init__(self, *args, **kwargs):
        endpoint_queryset = kwargs.pop("endpoint_queryset", ApiEndpoint.objects.none())
        parameter_definitions = kwargs.pop("parameter_definitions", [])
        super().__init__(*args, **kwargs)
        self.fields["endpoint"].queryset = endpoint_queryset
        self.parameter_fields: list[tuple[str, ApiConfigurationParameter]] = []
        for parameter in parameter_definitions:
            field_name = f"param_{parameter.pk}"
            widget = forms.TextInput()
            if "date" in parameter.name.lower():
                widget = forms.DateInput(
                    attrs={
                        "type": "date",
                    },
                    format="%Y-%m-%d",
                )
            self.fields[field_name] = forms.CharField(
                required=parameter.is_required and parameter.use_for_retrieval and parameter.is_active,
                initial=parameter.default_value,
                label=parameter.name.replace("_", " ").title(),
                help_text=f"Saved parameter: {parameter.name}",
                widget=widget,
            )
            self.fields[field_name].widget.attrs["data-parameter-name"] = parameter.name
            self.parameter_fields.append((field_name, parameter))


class ApiImportForm(forms.Form):
    endpoint = forms.ModelChoiceField(
        queryset=ApiEndpoint.objects.none(),
        empty_label="Select saved endpoint",
    )
    extra_query_string = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "page_size=1000",
            }
        ),
        help_text="Optional extra parameters in simple query-string format, for example page_size=1000",
    )

    def __init__(self, *args, **kwargs):
        endpoint_queryset = kwargs.pop("endpoint_queryset", ApiEndpoint.objects.none())
        parameter_definitions = kwargs.pop("parameter_definitions", [])
        super().__init__(*args, **kwargs)
        self.fields["endpoint"].queryset = endpoint_queryset
        self.parameter_fields: list[tuple[str, ApiConfigurationParameter]] = []
        for parameter in parameter_definitions:
            field_name = f"param_{parameter.pk}"
            widget = forms.TextInput()
            if "date" in parameter.name.lower():
                widget = forms.DateInput(
                    attrs={
                        "type": "date",
                    },
                    format="%Y-%m-%d",
                )
            self.fields[field_name] = forms.CharField(
                required=parameter.is_required and parameter.use_for_retrieval and parameter.is_active,
                initial="",
                label=parameter.name.replace("_", " ").title(),
                help_text=f"Saved parameter: {parameter.name}",
                widget=widget,
            )
            self.fields[field_name].widget.attrs["data-parameter-name"] = parameter.name
            self.parameter_fields.append((field_name, parameter))


class ApiImportScheduleForm(forms.ModelForm):
    class Meta:
        model = ApiImportSchedule
        fields = [
            "name",
            "endpoint",
            "frequency",
            "run_time",
            "weekday",
            "day_of_month",
            "reporting_date_mode",
            "fixed_reporting_date",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Daily individual import"}),
            "endpoint": forms.Select(),
            "frequency": forms.Select(),
            "run_time": forms.TimeInput(attrs={"type": "time"}),
            "weekday": forms.Select(),
            "day_of_month": forms.NumberInput(attrs={"min": 1, "max": 31}),
            "reporting_date_mode": forms.Select(),
            "fixed_reporting_date": forms.DateInput(attrs={"type": "date"}),
        }
        help_texts = {
            "endpoint": "Choose the saved endpoint this schedule should import automatically.",
            "run_time": "Time of day when the import should run.",
            "weekday": "Required when frequency is Weekly.",
            "day_of_month": "Required when frequency is Monthly. Use 1 to 31.",
            "reporting_date_mode": "Controls how the reporting_date parameter is filled during scheduled runs.",
            "fixed_reporting_date": "Only used when reporting date mode is Fixed date.",
        }

    def __init__(self, *args, **kwargs):
        endpoint_queryset = kwargs.pop("endpoint_queryset", ApiEndpoint.objects.none())
        super().__init__(*args, **kwargs)
        self.fields["endpoint"].queryset = endpoint_queryset

    def clean(self):
        cleaned_data = super().clean()
        endpoint = cleaned_data.get("endpoint")
        frequency = cleaned_data.get("frequency")
        weekday = cleaned_data.get("weekday")
        day_of_month = cleaned_data.get("day_of_month")
        reporting_date_mode = cleaned_data.get("reporting_date_mode")
        fixed_reporting_date = cleaned_data.get("fixed_reporting_date")

        if endpoint is not None:
            duplicate_schedule = (
                ApiImportSchedule.objects
                .filter(endpoint=endpoint)
                .exclude(pk=self.instance.pk)
                .select_related("endpoint")
                .first()
            )
            if duplicate_schedule:
                self.add_error(
                    "endpoint",
                    (
                        f"This endpoint is already linked to schedule '{duplicate_schedule.name}' "
                        f"({duplicate_schedule.get_frequency_display()}). Edit that schedule instead of creating another one."
                    ),
                )

        if frequency == ApiImportSchedule.FREQUENCY_WEEKLY and weekday in (None, ""):
            self.add_error("weekday", "Choose the weekday for weekly schedules.")

        if frequency == ApiImportSchedule.FREQUENCY_MONTHLY:
            if day_of_month in (None, ""):
                self.add_error("day_of_month", "Choose the day of the month for monthly schedules.")
            elif not 1 <= day_of_month <= 31:
                self.add_error("day_of_month", "Day of month must be between 1 and 31.")

        if reporting_date_mode == ApiImportSchedule.REPORTING_DATE_FIXED and not fixed_reporting_date:
            self.add_error("fixed_reporting_date", "Choose the fixed reporting date for this schedule.")

        return cleaned_data


class ApiSchedulerControlForm(forms.ModelForm):
    class Meta:
        model = ApiSchedulerServiceStatus
        fields = [
            "scheduler_enabled",
            "check_interval_seconds",
            "run_import_schedules",
            "run_main_customer_sync",
            "run_historical_score_capture",
            "retry_limit",
            "retry_delay_seconds",
        ]
        widgets = {
            "check_interval_seconds": forms.NumberInput(attrs={"min": 10, "max": 3600}),
            "retry_limit": forms.NumberInput(attrs={"min": 0, "max": 10}),
            "retry_delay_seconds": forms.NumberInput(attrs={"min": 5, "max": 3600}),
        }
        labels = {
            "scheduler_enabled": "Scheduler enabled",
            "check_interval_seconds": "Check interval (seconds)",
            "run_import_schedules": "Run saved endpoint imports",
            "run_main_customer_sync": "Run main customer auto-sync",
            "run_historical_score_capture": "Run historical score capture",
            "retry_limit": "Cycle retry limit",
            "retry_delay_seconds": "Retry delay (seconds)",
        }
        help_texts = {
            "scheduler_enabled": "Pause or resume all automatic scheduler work without restarting Django.",
            "check_interval_seconds": "How often the worker checks for due work. Recommended production range: 30 to 60 seconds.",
            "run_import_schedules": "Allow active endpoint schedules to run when due.",
            "run_main_customer_sync": "Allow ready reporting dates to sync into MainCustomer.",
            "run_historical_score_capture": "Allow due month-end score snapshots to be captured.",
            "retry_limit": "How many consecutive failed scheduler cycles may retry before entering a longer recovery wait.",
            "retry_delay_seconds": "Base delay used between failed scheduler-cycle retries.",
        }

    def clean_check_interval_seconds(self):
        value = self.cleaned_data["check_interval_seconds"]
        if not 10 <= value <= 3600:
            raise forms.ValidationError("Check interval must be between 10 and 3600 seconds.")
        return value

    def clean_retry_limit(self):
        value = self.cleaned_data["retry_limit"]
        if value > 10:
            raise forms.ValidationError("Retry limit cannot exceed 10.")
        return value

    def clean_retry_delay_seconds(self):
        value = self.cleaned_data["retry_delay_seconds"]
        if not 5 <= value <= 3600:
            raise forms.ValidationError("Retry delay must be between 5 and 3600 seconds.")
        return value


class ApiMainSyncConfigurationForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].disabled = True
        self.fields["name"].widget.attrs["readonly"] = True
        self.fields["corporate_endpoint"].queryset = ApiEndpoint.objects.filter(
            target_table=ApiEndpoint.TARGET_CUSTOMER_CORPORATE,
            is_active=True,
        ).order_by("name", "code")
        self.fields["individual_endpoint"].queryset = ApiEndpoint.objects.filter(
            target_table=ApiEndpoint.TARGET_CUSTOMER_INDIVIDUAL,
            is_active=True,
        ).order_by("name", "code")

    class Meta:
        model = ApiMainSyncConfiguration
        fields = ["name", "corporate_endpoint", "individual_endpoint", "delay_minutes", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Main Customer Auto Sync"}),
            "delay_minutes": forms.NumberInput(attrs={"min": 0}),
        }
        help_texts = {
            "corporate_endpoint": "Choose the saved CustomerCorporate endpoint that should drive main customer sync readiness.",
            "individual_endpoint": "Choose the saved CustomerIndividual endpoint that should drive main customer sync readiness.",
            "delay_minutes": "How many minutes the system should wait after the required customer source imports are ready. Use 60 for one hour.",
            "name": "This is the built-in main customer auto sync configuration.",
            "is_active": "Turn automatic main customer syncing on or off.",
        }

    def clean_corporate_endpoint(self):
        endpoint = self.cleaned_data.get("corporate_endpoint")
        if endpoint and endpoint.target_table != ApiEndpoint.TARGET_CUSTOMER_CORPORATE:
            raise forms.ValidationError("Choose a CustomerCorporate endpoint.")
        return endpoint

    def clean_individual_endpoint(self):
        endpoint = self.cleaned_data.get("individual_endpoint")
        if endpoint and endpoint.target_table != ApiEndpoint.TARGET_CUSTOMER_INDIVIDUAL:
            raise forms.ValidationError("Choose a CustomerIndividual endpoint.")
        return endpoint

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.name = instance.name or "Main Customer Auto Sync"
        if commit:
            instance.save()
        return instance
