from datetime import timedelta

from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .middleware import RuntimeSessionControlMiddleware
from .models import CustomUser


class LoginRedirectTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.factory = RequestFactory()
        self.user = CustomUser.objects.create_user(
            email="redirect-user@example.com",
            surname="User",
            name="Redirect",
            address="HQ",
            department="Risk",
            phone_number="27888888881",
        )
        self.user.set_password("Secret123!")
        self.user.must_change_password = False
        self.user.save(update_fields=["password", "must_change_password"])

    def test_login_honors_safe_next_target(self):
        response = self.client.post(
            reverse("login"),
            {
                "login_identifier": self.user.email,
                "password": "Secret123!",
                "next": "/ifrs9/ecl-summary-report/?page=2",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/ifrs9/ecl-summary-report/?page=2")

    def test_session_timeout_redirects_to_login_with_next_target(self):
        middleware = RuntimeSessionControlMiddleware(lambda request: None)
        request = self.factory.get("/ifrs9/ecl-summary-report/?page=2")
        request.user = self.user

        session_middleware = SessionMiddleware(lambda req: None)
        session_middleware.process_request(request)
        request.session.save()
        setattr(request, "_messages", FallbackStorage(request))
        request.session[RuntimeSessionControlMiddleware.SESSION_STARTED_AT_KEY] = (
            timezone.now() - timedelta(minutes=30)
        ).isoformat()
        request.session[RuntimeSessionControlMiddleware.LAST_ACTIVITY_AT_KEY] = (
            timezone.now() - timedelta(minutes=30)
        ).isoformat()

        response = middleware(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('login')}?next=%2Fifrs9%2Fecl-summary-report%2F%3Fpage%3D2",
        )
