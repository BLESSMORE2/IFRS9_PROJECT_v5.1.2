PASSWORD_EXPIRY_REMINDER_SESSION_KEY = "users_password_expiry_reminder"
WORKSPACE_POPUP_SESSION_KEY = "users_workspace_popup_mode"
WORKSPACE_POPUP_WINDOW_NAME = "nexaWorkspaceWindow"


def password_expiry_reminder(request):
    reminder = request.session.pop(PASSWORD_EXPIRY_REMINDER_SESSION_KEY, None)
    if not isinstance(reminder, dict):
        return {}

    days_left = int(reminder.get("days_left", 0) or 0)
    if days_left <= 0:
        return {}

    return {
        "password_expiry_reminder": {
            "days_left": days_left,
            "can_change_password": bool(reminder.get("can_change_password", True)),
            "message": (
                f"Your password will expire in {days_left} "
                f"day{'s' if days_left != 1 else ''}. This is only a reminder, so you can continue working and change it before the expiry date."
            ),
        }
    }


def workspace_popup(request):
    return {
        "workspace_popup_mode": bool(request.session.get(WORKSPACE_POPUP_SESSION_KEY)),
        "workspace_popup_window_name": WORKSPACE_POPUP_WINDOW_NAME,
        "workspace_launcher_path": "/login/",
    }
