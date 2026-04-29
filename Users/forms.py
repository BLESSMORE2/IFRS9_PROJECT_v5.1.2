from django import forms
from django.contrib.auth.forms import AdminPasswordChangeForm, ReadOnlyPasswordHashField, UserChangeForm
from django.contrib.auth.models import Group
from django.utils import timezone

from .models import CustomUser, SystemModule, SystemSetting
from .security import (
    build_policy_compliant_temporary_password,
    mark_password_changed,
    password_policy_help_text,
    record_password_history,
    validate_password_against_policy,
    validate_password_history,
)


class UserEmailChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.email



class CustomUserChangeForm(UserChangeForm):
    gender = forms.ChoiceField(choices=CustomUser.GENDER_CHOICES, widget=forms.RadioSelect)

    class Meta:
        model = CustomUser
        fields = ['name', 'surname', 'phone_number',  'department']


class CustomPasswordChangeForm(forms.Form):
    old_password = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control'}), label="Old Password")
    new_password1 = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control'}), label="New Password")
    new_password2 = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control'}), label="Confirm New Password")

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        self.fields["new_password1"].help_text = password_policy_help_text()

    def clean_old_password(self):
        old_password = self.cleaned_data.get("old_password")
        if not self.user.check_password(old_password):
            raise forms.ValidationError("Old password is incorrect.")
        return old_password

    def clean(self):
        cleaned_data = super().clean()
        new_password1 = cleaned_data.get("new_password1")
        new_password2 = cleaned_data.get("new_password2")

        if new_password1 and new_password2 and new_password1 != new_password2:
            raise forms.ValidationError("The new passwords do not match.")
        if new_password1:
            policy_errors = validate_password_against_policy(new_password1)
            history_errors = validate_password_history(self.user, new_password1)
            errors = policy_errors + history_errors
            if errors:
                raise forms.ValidationError(errors)
        
        return cleaned_data

    def save(self, commit=True):
        new_password = self.cleaned_data.get("new_password1")
        self.user.set_password(new_password)
        if commit:
            mark_password_changed(self.user, force_change_next_login=False, save=True)
        return self.user


class CustomUserAdminCreationForm(forms.ModelForm):
    password1 = forms.CharField(widget=forms.PasswordInput(attrs={"class": "vTextField"}), label="Password")
    password2 = forms.CharField(widget=forms.PasswordInput(attrs={"class": "vTextField"}), label="Confirm password")

    class Meta:
        model = CustomUser
        fields = ("email", "name", "surname", "phone_number", "address", "department", "gender")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].help_text = password_policy_help_text()

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise forms.ValidationError("The two password fields must match.")
        if password1:
            policy_errors = validate_password_against_policy(password1)
            if policy_errors:
                raise forms.ValidationError(policy_errors)
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        user.password_changed_at = timezone.now()
        user.must_change_password = True
        if commit:
            user.save()
            self.save_m2m()
            record_password_history(user)
        return user


class UserWorkspaceCreationForm(CustomUserAdminCreationForm):
    gender = forms.ChoiceField(
        choices=CustomUser.GENDER_CHOICES,
        widget=forms.Select(attrs={"class": "form-control form-control-sm"}),
    )
    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.order_by("name"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta(CustomUserAdminCreationForm.Meta):
        fields = CustomUserAdminCreationForm.Meta.fields

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].widget = forms.EmailInput(
            attrs={"class": "form-control form-control-sm", "autocomplete": "off", "autocapitalize": "none"}
        )
        self.fields["name"].widget = forms.TextInput(
            attrs={"class": "form-control form-control-sm", "autocomplete": "off"}
        )
        self.fields["surname"].widget = forms.TextInput(
            attrs={"class": "form-control form-control-sm", "autocomplete": "off"}
        )
        self.fields["phone_number"].widget = forms.TextInput(
            attrs={"class": "form-control form-control-sm", "autocomplete": "off"}
        )
        self.fields["address"].widget = forms.TextInput(
            attrs={"class": "form-control form-control-sm", "autocomplete": "off"}
        )
        self.fields["department"].widget = forms.TextInput(
            attrs={"class": "form-control form-control-sm", "autocomplete": "off"}
        )
        self.fields["password1"].widget.attrs.update(
            {"class": "form-control form-control-sm", "autocomplete": "new-password"}
        )
        self.fields["password2"].widget.attrs.update(
            {"class": "form-control form-control-sm", "autocomplete": "new-password"}
        )
        if not self.is_bound:
            default_password = build_policy_compliant_temporary_password()
            self.fields["password1"].initial = default_password
            self.fields["password2"].initial = default_password

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            user.groups.set(self.cleaned_data.get("groups", []))
        else:
            self._pending_groups = self.cleaned_data.get("groups", [])
        return user


class CustomUserAdminChangeForm(UserChangeForm):
    password = ReadOnlyPasswordHashField(
        help_text="Raw passwords are not stored, so there is no way to see this user's password."
    )

    class Meta:
        model = CustomUser
        fields = "__all__"


class CustomAdminPasswordChangeForm(AdminPasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "password1" in self.fields:
            self.fields["password1"].help_text = password_policy_help_text()

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        if password1:
            policy_errors = validate_password_against_policy(password1)
            history_errors = validate_password_history(self.user, password1)
            errors = policy_errors + history_errors
            if errors:
                raise forms.ValidationError(errors)
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        if commit:
            mark_password_changed(user, force_change_next_login=True, save=True)
        return user


class UserRoleAssignmentForm(forms.Form):
    user = UserEmailChoiceField(
        queryset=CustomUser.objects.order_by("name", "surname", "email"),
        widget=forms.Select(attrs={"class": "form-control form-control-sm"}),
    )
    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.order_by("name"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )


class UserModuleAccessForm(forms.Form):
    user = UserEmailChoiceField(
        queryset=CustomUser.objects.order_by("email"),
        widget=forms.Select(attrs={"class": "form-control form-control-sm"}),
    )
    modules = forms.ModelMultipleChoiceField(
        queryset=SystemModule.objects.filter(is_active=True).order_by("display_order", "name"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )


class AuthenticatorResetForm(forms.Form):
    user = UserEmailChoiceField(
        queryset=CustomUser.objects.order_by("email"),
        widget=forms.Select(attrs={"class": "form-control form-control-sm"}),
    )


class SystemSettingsForm(forms.ModelForm):
    class Meta:
        model = SystemSetting
        fields = [
            "idle_timeout_minutes",
            "absolute_session_timeout_minutes",
            "default_landing_rule",
            "failed_login_limit",
            "lockout_duration_minutes",
            "enable_self_profile_edit",
            "enable_self_password_change",
            "password_expiry_days",
            "password_history_count",
            "password_policy",
            "enable_microsoft_authentication",
            "microsoft_auth_on_login",
            "microsoft_auth_on_password_change",
            "microsoft_auth_enforce_periodically",
            "microsoft_auth_recheck_days",
        ]
        widgets = {
            "idle_timeout_minutes": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "absolute_session_timeout_minutes": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "default_landing_rule": forms.Select(attrs={"class": "form-control form-control-sm"}),
            "failed_login_limit": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 1}),
            "lockout_duration_minutes": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 1}),
            "password_expiry_days": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "password_history_count": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0}),
            "password_policy": forms.Select(attrs={"class": "form-control form-control-sm"}),
            "microsoft_auth_recheck_days": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 1}),
        }
        help_texts = {
            "idle_timeout_minutes": "Minutes of inactivity before the user is logged out automatically. Use 0 to disable.",
            "absolute_session_timeout_minutes": "Maximum signed-in duration in minutes even if the user stays active. Use 0 to disable.",
            "default_landing_rule": "Choose whether login opens the launcher or jumps straight into the only available module.",
            "failed_login_limit": "How many failed sign-in attempts are allowed before the account is locked.",
            "lockout_duration_minutes": "How long a sign-in lockout lasts after too many failed attempts.",
            "enable_self_profile_edit": "Allow users to maintain their own profile details.",
            "enable_self_password_change": "Allow users to change their own passwords.",
            "password_expiry_days": "Number of days before a password expires. Use 0 to disable password expiry.",
            "password_history_count": "Number of previous passwords that cannot be reused. Use 0 to disable password history checks.",
            "password_policy": "Choose the password strength policy enforced for users and also in the admin panel when passwords are created or reset.",
            "enable_microsoft_authentication": "Turn on Microsoft Authenticator policy controls for the shared workspace.",
            "microsoft_auth_on_login": "Require or offer Microsoft authentication from the sign-in flow.",
            "microsoft_auth_on_password_change": "Prompt Microsoft authentication before a password change is completed.",
            "microsoft_auth_enforce_periodically": "Require Microsoft authentication again after a configured number of days.",
            "microsoft_auth_recheck_days": "Number of days before Microsoft authentication should be requested again.",
        }
