WORKSPACE_POPUP_SESSION_KEY = "users_workspace_popup_mode"
WORKSPACE_POPUP_WINDOW_NAME = "nexaWorkspaceWindow"


def workspace_popup(request):
    return {
        "workspace_popup_mode": bool(request.session.get(WORKSPACE_POPUP_SESSION_KEY)),
        "workspace_popup_window_name": WORKSPACE_POPUP_WINDOW_NAME,
        "workspace_launcher_path": "/login/",
    }
