from django import template

from Users.runtime import get_system_settings


register = template.Library()


def _format_lockout_duration(minutes):
    minutes = max(int(minutes or 0), 0)
    if minutes == 1:
        return "1 minute"
    if minutes < 60:
        return f"{minutes} minutes"
    hours, remainder = divmod(minutes, 60)
    hour_label = "hour" if hours == 1 else "hours"
    if remainder == 0:
        return f"{hours} {hour_label}"
    minute_label = "minute" if remainder == 1 else "minutes"
    return f"{hours} {hour_label} {remainder} {minute_label}"


@register.simple_tag
def lockout_duration_minutes():
    settings = get_system_settings()
    return int(getattr(settings, "lockout_duration_minutes", 60) or 60)


@register.simple_tag
def lockout_duration_label():
    settings = get_system_settings()
    return _format_lockout_duration(getattr(settings, "lockout_duration_minutes", 60))


@register.simple_tag
def failed_login_limit():
    settings = get_system_settings()
    return int(getattr(settings, "failed_login_limit", 3) or 3)
