def get_axes_username(request, credentials=None):
    credentials = credentials or {}

    if request is not None:
        posted_value = (
            request.POST.get("login_identifier")
            or request.POST.get("email")
            or request.POST.get("username")
            or request.POST.get("login")
        )
        if posted_value:
            return str(posted_value).strip().lower()

    for key in ("login_identifier", "email", "username"):
        value = credentials.get(key)
        if value:
            return str(value).strip().lower()

    return ""
