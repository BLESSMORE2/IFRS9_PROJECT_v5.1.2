from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.timesince import timesince



class Branch(models.Model):
    name = models.CharField(max_length=100, unique=True)
    address = models.CharField(max_length=255)
    city = models.ForeignKey('stock_management.City', on_delete=models.SET_NULL, null=True, blank=True)
    
    def __str__(self):
        return self.name   
class CustomUserManager(BaseUserManager):
    def create_user(self, phone_number, surname, **extra_fields):
        if not phone_number:
            raise ValueError('The Phone Number must be set')
        if not surname:
            raise ValueError('The Surname must be set')
        
        extra_fields.setdefault('email', 'b@gmail.com')
        extra_fields.setdefault('gender', 'male')
        extra_fields.setdefault('marital_status', 'single')
        extra_fields.setdefault('id_number', 'default_id')
        extra_fields.setdefault('hire_date', timezone.now())

        user = self.model(phone_number=phone_number, **extra_fields)
        user.set_password(surname.lower())
        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, surname, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        return self.create_user(phone_number, surname, **extra_fields)

class CustomUser(AbstractBaseUser, PermissionsMixin):
    name = models.CharField(max_length=30)
    surname = models.CharField(max_length=30)
    phone_number = models.CharField(max_length=15, unique=True)
    address = models.CharField(max_length=255)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='users')
    department = models.ForeignKey('Department', on_delete=models.SET_NULL, null=True, blank=True)
    city = models.ForeignKey('stock_management.City', on_delete=models.SET_NULL, null=True, blank=True)
    country = models.ForeignKey('stock_management.Country', on_delete=models.SET_NULL, null=True, blank=True)
    email = models.EmailField(default='b@gmail.com')
    date_of_birth = models.DateField(null=True, blank=True)
    GENDER_CHOICES = [
        ('male', 'Male'),
        ('female', 'Female'),
    ]
    gender = models.CharField(max_length=6, choices=GENDER_CHOICES, default='male')
    MARITAL_STATUS_CHOICES = [
        ('single', 'Single'),
        ('married', 'Married'),
        ('divorced', 'Divorced'),
        ('widowed', 'Widowed'),
    ]
    marital_status = models.CharField(max_length=8, choices=MARITAL_STATUS_CHOICES, default='single')
    id_number = models.CharField(max_length=20, unique=True, default='default_id')
    inventory_access = models.BooleanField(default=False)
    hire_date = models.DateField(default=timezone.now)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = CustomUserManager()

    USERNAME_FIELD = 'phone_number'
    REQUIRED_FIELDS = ['surname']

    def __str__(self):
        return self.name
    
    
class Notification(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=255)
    message = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    is_read = models.BooleanField(default=False)

    def __str__(self):
        return f"Notification for {self.user.phone_number} - {self.title}"

    class Meta:
        ordering = ['-created_at']
    
    def time_since_created(self):
        return timesince(self.created_at)
    

  
class Department(models.Model):
    department_id = models.CharField(max_length=10, primary_key=True)
    department_name = models.CharField(max_length=100)
    manager = models.CharField(max_length=100, blank=True, null=True)
    location = models.CharField(max_length=100, blank=True, null=True)
    number_of_employees = models.PositiveIntegerField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.department_name
    

    