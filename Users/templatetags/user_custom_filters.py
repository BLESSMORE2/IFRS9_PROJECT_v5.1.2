import os
import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

try:
    import markdown as markdown_lib
except ImportError:  # pragma: no cover - optional package fallback
    markdown_lib = None


register = template.Library()


@register.filter(name="user_initials")
def user_initials(user):
    if not user:
        return "?"
    if getattr(user, "name", None) and getattr(user, "surname", None):
        return (str(user.name)[0] + str(user.surname)[0]).upper()
    email = getattr(user, "email", None) or getattr(user, "phone_number", None) or ""
    email = str(email).strip()
    if not email:
        return "?"
    if "@" in email:
        local, _, domain = email.partition("@")
        first = (local[0] if local else "").upper()
        second = (domain[0] if domain else "").upper()
        return (first + second) if (first or second) else email[:2].upper()
    return email[:2].upper()


@register.filter(name="format_ai_narrative")
def format_ai_narrative(text):
    if not text:
        return ""
    escaped = escape(text)
    bold = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped, flags=re.DOTALL)
    return mark_safe(bold.replace("\n", "<br>"))


@register.filter(name="format_ai_sections")
def format_ai_sections(text):
    if not text:
        return ""
    escaped = escape(text)
    bold = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped, flags=re.DOTALL)
    parts = []
    for block in re.split(r"\n\s*\n", bold):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        if len(lines) == 1 and lines[0].strip().startswith("<strong>") and lines[0].strip().endswith("</strong>"):
            inner = lines[0].strip()[len("<strong>"):-len("</strong>")].strip()
            parts.append(f'<h6 class="ai-section-head mt-4 mb-2 text-dark font-weight-bold">{inner}</h6>')
        else:
            paragraph = block.replace("\n", "<br>")
            parts.append(f'<p class="ai-narrative-p mb-3">{paragraph}</p>')
    return mark_safe("\n".join(parts))


@register.filter
def get_attribute(value, attr_name):
    return getattr(value, attr_name, None)


@register.filter
def get_item(dictionary, key):
    if dictionary is None:
        return None
    try:
        if hasattr(dictionary, "get"):
            return dictionary.get(key)
        if hasattr(dictionary, key):
            return getattr(dictionary, key)
        if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
            return dictionary[int(key)]
        return dictionary[key]
    except (KeyError, IndexError, AttributeError, TypeError):
        return None


@register.filter
def divide_by_60(value):
    if value is None:
        return None
    try:
        return round(float(value) / 60, 2)
    except (TypeError, ValueError):
        return None


@register.filter(name="markdown")
def markdown_filter(text):
    if not text:
        return ""
    if markdown_lib is None:
        return mark_safe(escape(text).replace("\n", "<br>"))
    return mark_safe(markdown_lib.markdown(text))


@register.filter
def get_item_for_stage(summary_list, stage):
    if not summary_list or not isinstance(summary_list, list):
        return None
    for item in summary_list:
        if item.get("stage") == stage:
            return item
    return None


@register.filter
def format_number(value):
    try:
        return f"{float(value):,.2f}"
    except (ValueError, TypeError):
        return value


@register.filter(name="div")
def div(value, arg):
    try:
        return float(value) / float(arg)
    except (ValueError, TypeError, ZeroDivisionError):
        return 0


@register.filter(name="get_range")
def get_range(value):
    try:
        return range(int(value))
    except (TypeError, ValueError):
        return range(0)


@register.filter(name="replace_underscores")
def replace_underscores(value):
    if value is None:
        return None
    return str(value).replace("_", " ")


@register.filter(name="file_extension")
def file_extension(value):
    if not value:
        return ""
    _, ext = os.path.splitext(str(value))
    return ext.lstrip(".").lower()
