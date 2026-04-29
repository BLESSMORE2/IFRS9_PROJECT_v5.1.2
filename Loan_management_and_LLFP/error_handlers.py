from urllib.parse import urlsplit

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse
from django.utils.http import url_has_allowed_host_and_scheme


def _safe_reverse(name, fallback):
    try:
        return reverse(name)
    except NoReverseMatch:
        return fallback


def _safe_redirect_target(request):
    referer = request.META.get('HTTP_REFERER', '')
    if referer and url_has_allowed_host_and_scheme(
        url=referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        referer_parts = urlsplit(referer)
        current_parts = urlsplit(request.build_absolute_uri())
        if referer_parts.path and referer_parts.path != current_parts.path:
            target = referer_parts.path
            if referer_parts.query:
                target = f"{target}?{referer_parts.query}"
            return target

    default_target = _safe_reverse('modules_home', reverse('login') if not request.user.is_authenticated else '/')

    if request.path.startswith('/scorecard/'):
        return _safe_reverse('scorecard:scorecard_dashboard', default_target)
    if request.path.startswith('/ifrs9/') or request.path.startswith('/dashboard/'):
        return _safe_reverse('dashboard', default_target)
    if request.user.is_authenticated:
        return _safe_reverse('modules_home', reverse('login'))
    return reverse('login')


def csrf_failure(request, reason='', template_name=None):
    messages.error(
        request,
        'Your form session expired or the page was submitted twice. Please reopen the page and try again.',
    )
    return redirect(_safe_redirect_target(request))
