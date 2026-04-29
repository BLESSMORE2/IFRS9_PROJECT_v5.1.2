from django.urls import path
from . import views

urlpatterns = [
    path('', views.modules_home_view, name='modules_home'),
    path('modules/', views.modules_home_view, name='modules_home_alias'),
    path('settings/', views.user_settings_view, name='user_settings'),
    path('settings/add-users/', views.user_settings_add_user_view, name='user_settings_add_user'),
    path('settings/authenticator/', views.user_settings_authenticator_view, name='user_settings_authenticator'),
    path('settings/user-roles/', views.user_settings_roles_view, name='user_settings_roles'),
    path('settings/system/', views.user_settings_system_view, name='user_settings_system'),
    path('login/', views.login_view, name='login'),
    path('login/microsoft/', views.microsoft_auth_start_view, name='microsoft_auth_start'),
    path('logout/', views.custom_logout_view, name='logout'),
    path('update-profile/', views.update_profile, name='update_profile'),
    path('change-password/', views.change_password, name='change_password'),
    path('password-change-done/', views.password_change_done, name='password_change_done'),
]
