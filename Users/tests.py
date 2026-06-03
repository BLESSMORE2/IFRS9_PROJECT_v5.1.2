from datetime import timedelta

from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.sessions.models import Session
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .access_logs import begin_user_session_log
from .middleware import RuntimeSessionControlMiddleware
from .models import CustomUser, UserAccessLog


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

    def test_login_and_manual_logout_write_access_log(self):
        login_response = self.client.post(
            reverse("login"),
            {
                "login_identifier": self.user.email,
                "password": "Secret123!",
            },
        )

        self.assertEqual(login_response.status_code, 302)
        access_log = UserAccessLog.objects.get(user=self.user)
        self.assertEqual(access_log.end_reason, UserAccessLog.END_REASON_ACTIVE)
        self.assertIsNone(access_log.logout_time)

        logout_response = self.client.get(reverse("logout"))

        self.assertEqual(logout_response.status_code, 302)
        access_log.refresh_from_db()
        self.assertEqual(access_log.end_reason, UserAccessLog.END_REASON_MANUAL_LOGOUT)
        self.assertIsNotNone(access_log.logout_time)
        self.assertIsNotNone(access_log.session_duration_seconds)
        self.assertGreaterEqual(access_log.session_duration_seconds, 0)

    def test_session_timeout_closes_access_log_with_timeout_reason(self):
        session_middleware = SessionMiddleware(lambda req: None)
        request = self.factory.get("/ifrs9/ecl-summary-report/?page=2")
        request.user = self.user
        session_middleware.process_request(request)
        request.session.save()
        setattr(request, "_messages", FallbackStorage(request))

        access_log = UserAccessLog.objects.create(
            user=self.user,
            session_key=request.session.session_key,
            login_time=timezone.now() - timedelta(minutes=30),
        )
        request.session["users_access_log_id"] = access_log.pk
        request.session[RuntimeSessionControlMiddleware.SESSION_STARTED_AT_KEY] = (
            timezone.now() - timedelta(minutes=30)
        ).isoformat()
        request.session[RuntimeSessionControlMiddleware.LAST_ACTIVITY_AT_KEY] = (
            timezone.now() - timedelta(minutes=30)
        ).isoformat()

        middleware = RuntimeSessionControlMiddleware(lambda inner_request: None)
        response = middleware(request)

        self.assertEqual(response.status_code, 302)
        access_log.refresh_from_db()
        self.assertEqual(access_log.end_reason, UserAccessLog.END_REASON_IDLE_TIMEOUT)
        self.assertIsNotNone(access_log.logout_time)
        self.assertIsNotNone(access_log.session_duration_seconds)

    def test_begin_user_session_log_prefers_forwarded_ip_and_closes_stale_rows(self):
        stale_log = UserAccessLog.objects.create(
            user=self.user,
            session_key="stale-session",
            login_time=timezone.now() - timedelta(hours=6),
            ip_address="127.0.0.1",
        )

        request = self.factory.get(
            "/login/popup/",
            HTTP_X_FORWARDED_FOR="192.168.4.17, 127.0.0.1",
            REMOTE_ADDR="127.0.0.1",
        )
        request.user = self.user
        session_middleware = SessionMiddleware(lambda req: None)
        session_middleware.process_request(request)
        request.session.save()

        Session.objects.filter(session_key="stale-session").delete()

        access_log = begin_user_session_log(request, self.user)

        self.assertIsNotNone(access_log)
        self.assertEqual(access_log.ip_address, "192.168.4.17")

        stale_log.refresh_from_db()
        self.assertEqual(stale_log.end_reason, UserAccessLog.END_REASON_IDLE_TIMEOUT)
        self.assertIsNotNone(stale_log.logout_time)
