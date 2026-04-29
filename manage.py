#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT_DIR)

def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Loan_management_and_LLFP.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc


    sys.path.append(os.path.join(os.path.dirname(__file__)))
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
