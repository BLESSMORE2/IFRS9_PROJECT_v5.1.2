from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
EXPIRY_FILE = PACKAGE_DIR / ".scorecard_expiry_date"
WARNING_THRESHOLD_DAYS = 30


def get_expiry_time():
    """Return the raw expiry text and parsed datetime from .scorecard_expiry_date."""
    expiry_text = EXPIRY_FILE.read_text(encoding="utf-8").strip()
    expiry_date = datetime.strptime(expiry_text, "%Y-%m-%d %H:%M:%S")
    return expiry_text, expiry_date


def check_expiry():
    """
    Return a notice message only when the scorecard license needs attention.

    The home dashboard should stay quiet while the license is healthy, and only
    surface a notice once the expiry date is within the warning window.
    """
    try:
        expiry_text, expiry_date = get_expiry_time()
    except FileNotFoundError:
        return "Your scorecard license file is missing. Please contact support."
    except ValueError:
        return "Your scorecard license file is invalid. Please contact support."
    except OSError:
        return "Your scorecard license could not be read. Please contact support."

    now = datetime.now()
    if expiry_date <= now:
        return f"Your scorecard license expired on {expiry_text}. Please contact support."

    remaining_seconds = max(1, (expiry_date - now).total_seconds())
    remaining_days = max(1, math.ceil(remaining_seconds / 86400))
    if remaining_days <= WARNING_THRESHOLD_DAYS:
        return (
            f"Your scorecard license is valid until {expiry_text}. "
            f"({remaining_days} day(s) remaining)."
        )

    return None
