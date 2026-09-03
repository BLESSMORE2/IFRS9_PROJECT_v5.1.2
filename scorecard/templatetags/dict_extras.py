from django import template
from decimal import Decimal
import json

register = template.Library()


@register.filter
def get_item(mapping, key):
    """
    Simple template helper to read dictionaries by key.
    Usage:
        {{ my_dict|get_item:some_id }}
    """

    if mapping is None:
        return None
    if hasattr(mapping, "get"):
        return mapping.get(key)
    try:
        return mapping[key]
    except (KeyError, TypeError, IndexError, AttributeError):
        return None


@register.filter
def response_option_id(response):
    """Return the selected option id for either dict-backed or model-backed responses."""
    if response is None:
        return None
    if hasattr(response, "get"):
        option_id = response.get("option_id")
        if option_id is not None:
            return option_id
        option = response.get("option")
        return getattr(option, "id", None)
    option_id = getattr(response, "option_id", None)
    if option_id is not None:
        return option_id
    option = getattr(response, "option", None)
    return getattr(option, "id", None)


@register.filter
def response_option_ids(response):
    """Return all selected option ids for either dict-backed or model-backed responses."""
    if response is None:
        return []

    if hasattr(response, "get"):
        raw_value = response.get("raw_value")
    else:
        raw_value = getattr(response, "raw_value", None)

    values = []
    if raw_value not in (None, ""):
        if isinstance(raw_value, (list, tuple)):
            values = [str(value) for value in raw_value if str(value)]
        elif isinstance(raw_value, str):
            try:
                parsed = json.loads(raw_value)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                values = [str(value) for value in parsed if str(value)]

    if values:
        return list(dict.fromkeys(values))

    selected = response_option_id(response)
    return [str(selected)] if selected not in (None, "") else []


@register.filter
def response_has_option(response, option_id):
    return str(option_id) in response_option_ids(response)


@register.filter
def sum_scores(options):
    """Get the total allocated score from a queryset of options."""
    if not options:
        return Decimal("0")
    try:
        total = sum((Decimal(str(opt.allocated_score or 0)) for opt in options), Decimal("0"))
        return total
    except (ValueError, TypeError):
        return Decimal("0")


@register.filter
def max_score(options):
    """Get the maximum allocated score from a queryset of options."""
    if not options:
        return Decimal("0")
    try:
        max_val = max((opt.allocated_score for opt in options), default=Decimal("0"))
        return max_val
    except (ValueError, TypeError):
        return Decimal("0")


@register.filter
def isnotequal(value, arg):
    """Check if value is not equal to arg."""
    return value != arg


@register.filter
def email_localpart(value):
    if value in (None, ""):
        return ""
    return str(value).split("@", 1)[0]


@register.simple_tag
def weighted_score(actual_score, max_score, weight_percent):
    """Return (actual / max) * weight using decimal math for display."""
    try:
        actual = Decimal(str(actual_score or 0))
        maximum = Decimal(str(max_score or 0))
        weight = Decimal(str(weight_percent or 0))
    except (ArithmeticError, ValueError, TypeError):
        return Decimal("0")

    if maximum <= 0:
        return Decimal("0")

    return (actual / maximum) * weight
