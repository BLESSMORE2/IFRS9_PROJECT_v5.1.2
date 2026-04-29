from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    Group,
    Permission,
    PermissionsMixin,
)
from django.core.cache import cache
from django.db import models
from django.utils import timezone
from django.conf import settings

class CustomUserManager(BaseUserManager):
    def create_user(self, email, surname, **extra_fields):
        if not email:
            raise ValueError('The Email must be set')
        if not surname:
            raise ValueError('The Surname must be set')
        
        extra_fields.setdefault('gender', 'male')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        from .security import build_policy_compliant_temporary_password, record_password_history
        user.set_password(build_policy_compliant_temporary_password(surname or email))
        user.password_changed_at = timezone.now()
        user.must_change_password = not extra_fields.get('is_superuser', False)
        user.save(using=self._db)
        record_password_history(user)
        return user

    def create_superuser(self, email, surname, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, surname, **extra_fields)

class CustomUser(AbstractBaseUser, PermissionsMixin):
    name = models.CharField(max_length=30)
    surname = models.CharField(max_length=30)
    phone_number = models.CharField(max_length=15, unique=True, null=True, blank=True)
    address = models.CharField(max_length=255)
    department = models.CharField(max_length=55)
    email = models.EmailField(unique=True)
    GENDER_CHOICES = [
        ('male', 'Male'),
        ('female', 'Female'),
    ]
    gender = models.CharField(max_length=6, choices=GENDER_CHOICES, default='male')
    
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    password_changed_at = models.DateTimeField(default=timezone.now)
    must_change_password = models.BooleanField(default=False)
    microsoft_authenticator_secret = models.CharField(max_length=64, blank=True, default="")
    microsoft_authenticator_enabled = models.BooleanField(default=False)
    microsoft_authenticator_confirmed_at = models.DateTimeField(blank=True, null=True)

    groups = models.ManyToManyField(
        Group,
        related_name='customuser_groups',
        blank=True,
        help_text='The groups this user belongs to.',
        verbose_name='groups',
    )
    user_permissions = models.ManyToManyField(
        Permission,
        related_name='customuser_permissions',
        blank=True,
        help_text='Specific permissions for this user.',
        verbose_name='user permissions',
    )

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['surname']

    def __str__(self):
        return self.name

    def get_accessible_branches(self):
        from scorecard.models import BankBranch

        queryset = BankBranch.objects.all().order_by("bank_name", "branch_name")
        if self.is_superuser:
            return queryset

        explicit_branch_access = queryset.filter(
            scorecard_user_access_entries__user_id=self.id
        ).distinct()
        if explicit_branch_access.exists():
            return explicit_branch_access

        relation_name = None
        for relation in BankBranch._meta.related_objects:
            if relation.related_model is self.__class__:
                relation_name = relation.get_accessor_name()
                break

        if relation_name:
            return queryset.filter(**{f"{relation_name}__id": self.id}).distinct()

        return queryset.none()

    def has_branch_access(self, branch_code):
        cleaned_branch_code = (branch_code or "").strip()
        if not cleaned_branch_code:
            return False
        return self.get_accessible_branches().filter(branch_code=cleaned_branch_code).exists()

    class Meta:
        permissions = [
            ('can_view_settings_user_roles', 'Can view settings user roles'),
            ('can_assign_settings_roles', 'Can assign settings user roles'),
            ('can_view_settings_database', 'Can view settings database configuration'),
            ('can_manage_settings_database', 'Can manage settings database configuration'),
        ]


class AuditTrail(models.Model):
    ACTION_CHOICES = [
        ('create', 'Create'),
        ('update', 'Update'),
        ('delete', 'Delete'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    model_name = models.CharField(max_length=100)
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    object_id = models.CharField(max_length=50, null=True, blank=True)
    change_description = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} {self.action} {self.model_name} on {self.timestamp}"

    class Meta:
        db_table = "audit_trail"
        verbose_name = "Audit Trail"
        verbose_name_plural = "Audit Trails"
        ordering = ["-timestamp"]


class SystemModule(models.Model):
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=100)
    route_name = models.CharField(max_length=120)
    icon_class = models.CharField(max_length=120, blank=True, default="")
    accent_class = models.CharField(max_length=40, blank=True, default="")
    description = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "name"]
        verbose_name = "System Module"
        verbose_name_plural = "System Modules"

    def __str__(self):
        return self.name


class RoleModuleAccess(models.Model):
    group = models.ForeignKey(
        Group,
        on_delete=models.CASCADE,
        related_name="module_access_entries",
    )
    module = models.ForeignKey(
        SystemModule,
        on_delete=models.CASCADE,
        related_name="role_access_entries",
    )
    can_view = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("group", "module")
        verbose_name = "Role Module Access"
        verbose_name_plural = "Role Module Access"

    def __str__(self):
        return f"{self.group.name} -> {self.module.name}"


class UserModuleAccess(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="module_access_entries",
    )
    module = models.ForeignKey(
        SystemModule,
        on_delete=models.CASCADE,
        related_name="user_access_entries",
    )
    can_view = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "module")
        verbose_name = "User Module Access"
        verbose_name_plural = "User Module Access"

    def __str__(self):
        return f"{self.user.email} -> {self.module.name}"


class SystemSetting(models.Model):
    LANDING_RULE_LAUNCHER = "launcher"
    LANDING_RULE_DIRECT = "direct_first_module"
    LANDING_RULE_CHOICES = [
        (LANDING_RULE_LAUNCHER, "Always show module launcher"),
        (LANDING_RULE_DIRECT, "Open directly when only one module is available"),
    ]
    MICROSOFT_AUTH_MODE_SIMULATED = "simulated"
    MICROSOFT_AUTH_MODE_CHOICES = [
        (MICROSOFT_AUTH_MODE_SIMULATED, "Microsoft Authenticator app (QR code)"),
    ]
    PASSWORD_POLICY_BASIC = "basic"
    PASSWORD_POLICY_MINIMUM = "minimum"
    PASSWORD_POLICY_STANDARD = "standard"
    PASSWORD_POLICY_STRONG = "strong"
    PASSWORD_POLICY_ENTERPRISE = "enterprise"
    PASSWORD_POLICY_CHOICES = [
        (PASSWORD_POLICY_BASIC, "Basic: no restrictions"),
        (PASSWORD_POLICY_MINIMUM, "Minimum: at least 8 characters"),
        (PASSWORD_POLICY_STANDARD, "Standard: upper, lower, and number"),
        (PASSWORD_POLICY_STRONG, "Strong: 12+ chars with upper, lower, number, and special"),
        (PASSWORD_POLICY_ENTERPRISE, "Enterprise: 14+ chars with upper, lower, number, and special"),
    ]

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    idle_timeout_minutes = models.PositiveSmallIntegerField(default=15)
    absolute_session_timeout_minutes = models.PositiveSmallIntegerField(default=480)
    default_landing_rule = models.CharField(
        max_length=32,
        choices=LANDING_RULE_CHOICES,
        default=LANDING_RULE_LAUNCHER,
    )
    failed_login_limit = models.PositiveSmallIntegerField(default=3)
    lockout_duration_minutes = models.PositiveSmallIntegerField(default=60)
    enable_self_profile_edit = models.BooleanField(default=True)
    enable_self_password_change = models.BooleanField(default=True)
    password_expiry_days = models.PositiveSmallIntegerField(default=90)
    password_history_count = models.PositiveSmallIntegerField(default=5)
    password_policy = models.CharField(
        max_length=20,
        choices=PASSWORD_POLICY_CHOICES,
        default=PASSWORD_POLICY_STANDARD,
    )
    enable_microsoft_authentication = models.BooleanField(default=False)
    microsoft_auth_mode = models.CharField(
        max_length=24,
        choices=MICROSOFT_AUTH_MODE_CHOICES,
        default=MICROSOFT_AUTH_MODE_SIMULATED,
    )
    microsoft_auth_on_login = models.BooleanField(default=True)
    microsoft_auth_on_password_change = models.BooleanField(default=False)
    microsoft_auth_enforce_periodically = models.BooleanField(default=False)
    microsoft_auth_recheck_days = models.PositiveSmallIntegerField(default=30)
    microsoft_tenant_id = models.CharField(max_length=120, blank=True, default="")
    microsoft_client_id = models.CharField(max_length=120, blank=True, default="")
    microsoft_redirect_uri = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    CACHE_KEY = "users_system_settings_singleton"

    class Meta:
        verbose_name = "System Setting"
        verbose_name_plural = "System Settings"

    def __str__(self):
        return "System Settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete(self.CACHE_KEY)

    @classmethod
    def load(cls):
        cached = cache.get(cls.CACHE_KEY)
        if cached is not None:
            return cached

        obj, _ = cls.objects.get_or_create(pk=1)
        cache.set(cls.CACHE_KEY, obj, 300)
        return obj


class PasswordHistory(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="password_history_entries",
    )
    password_hash = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Password History"
        verbose_name_plural = "Password History"

    def __str__(self):
        return f"{self.user_id} password at {self.created_at}"
