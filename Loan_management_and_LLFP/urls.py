"""
URL configuration for Loan_management_and_LLFP project.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_yasg import openapi
from drf_yasg.views import get_schema_view
from rest_framework import permissions

from Loan_management_and_LLFP.package_runtime import (
    get_ifrs9_package_status,
    get_scorecard_package_status,
)


schema_view = get_schema_view(
    openapi.Info(
        title="IFRS 9 API Documentation",
        default_version='v1',
        description="API documentation for IFRS 9 Compliance and Risk Management System",
        terms_of_service="https://www.example.com/terms/",
        contact=openapi.Contact(email="contact@example.com"),
        license=openapi.License(name="Your License"),
    ),
    public=True,
    permission_classes=(permissions.IsAuthenticated,),
)

admin.site.site_title = "IFRS 9 Compliance and Risk Management"
admin.site.site_header = "IFRS 9 Compliance and Risk Management Administration"
admin.site.index_title = "Welcome to the Administration Panel"

ifrs9_status = get_ifrs9_package_status()
scorecard_status = get_scorecard_package_status()

urlpatterns = [
    path('ifrs-admin/', admin.site.urls),
    path('', include('Users.urls')),
    path('swagger<format>/', schema_view.without_ui(cache_timeout=0), name='schema-json'),
    path('swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
    path('redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
]

if ifrs9_status['usable']:
    from IFRS9.Functions_view.ead_and_cashflows import rule_execution_guide

    urlpatterns.extend([
        path('ifrs9/cashflow-projections/rule-execution-guide/', rule_execution_guide, name='rule_execution_guide'),
        path('ifrs9/', include('IFRS9.urls')),
    ])

if scorecard_status['usable']:
    urlpatterns.append(
        path('scorecard/', include(('scorecard.urls', 'scorecard'), namespace='scorecard'))
    )

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
