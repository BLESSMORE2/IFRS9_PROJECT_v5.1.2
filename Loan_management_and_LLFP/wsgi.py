"""
WSGI config for Loan_management_and_LLFP project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Loan_management_and_LLFP.settings')
# Marks this as a web-serving process so the scorecard scheduler can auto-start.
# A process lock ensures only one web worker becomes the scheduler owner.
os.environ.setdefault('SCORECARD_WEB_PROCESS', '1')

application = get_wsgi_application()
