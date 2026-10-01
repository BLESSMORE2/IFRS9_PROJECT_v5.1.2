from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.sessions.models import Session
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .access_logs import begin_user_session_log, get_live_access_log_user_ids, reconcile_stale_user_session_logs, sweep_idle_user_sessions
from .backends import CaseInsensitiveEmailOrAliasBackend
from .middleware import RuntimeSessionControlMiddleware
from .forms import SystemSettingsForm
from .inactivity_locks import sweep_inactive_user_accounts
from .models import AuditTrail, CustomUser, UserAccessLog
from .session_sweeper import _should_start_sweeper, run_user_security_sweep_cycle
from .views import _release_user_lockout_by_admin


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

    def test_live_user_ids_ignore_open_access_logs_without_live_session(self):
        UserAccessLog.objects.create(
            user=self.user,
            session_key="missing-session",
            login_time=timezone.now() - timedelta(hours=3),
            ip_address="192.168.4.17",
        )

        self.assertNotIn(self.user.pk, get_live_access_log_user_ids())

        live_session = SessionStore()
        live_session["user_marker"] = self.user.pk
        live_session.set_expiry(3600)
        live_session.save()
        UserAccessLog.objects.create(
            user=self.user,
            session_key=live_session.session_key,
            login_time=timezone.now(),
            ip_address="192.168.4.17",
        )

        self.assertIn(self.user.pk, get_live_access_log_user_ids())

    def test_reconcile_stale_session_logs_closes_missing_session(self):
        stale_log = UserAccessLog.objects.create(
            user=self.user,
            session_key="expired-session",
            login_time=timezone.now() - timedelta(hours=6),
            ip_address="192.168.4.17",
        )
        Session.objects.filter(session_key="expired-session").delete()

        closed_count = reconcile_stale_user_session_logs(ended_at=timezone.now())

        self.assertEqual(closed_count, 1)
        stale_log.refresh_from_db()
        self.assertEqual(stale_log.end_reason, UserAccessLog.END_REASON_IDLE_TIMEOUT)
        self.assertIsNotNone(stale_log.logout_time)
        self.assertIsNotNone(stale_log.session_duration_seconds)

    def test_sweep_idle_user_sessions_closes_idle_live_session(self):
        now = timezone.now()
        live_session = SessionStore()
        live_session["users_session_started_at"] = (now - timedelta(minutes=5)).isoformat()
        live_session["users_last_activity_at"] = (now - timedelta(minutes=5)).isoformat()
        live_session.set_expiry(3600)
        live_session.save()
        access_log = UserAccessLog.objects.create(
            user=self.user,
            session_key=live_session.session_key,
            login_time=now - timedelta(minutes=5),
            ip_address="192.168.4.17",
        )

        closed_count = sweep_idle_user_sessions(ended_at=now, idle_timeout_minutes=1)

        self.assertEqual(closed_count, 1)
        self.assertFalse(Session.objects.filter(session_key=live_session.session_key).exists())
        access_log.refresh_from_db()
        self.assertEqual(access_log.end_reason, UserAccessLog.END_REASON_IDLE_TIMEOUT)
        self.assertIsNotNone(access_log.logout_time)


class InactiveAccountLockTests(TestCase):
    def _create_user(self, email, phone_number, *, superuser=False):
        factory = CustomUser.objects.create_superuser if superuser else CustomUser.objects.create_user
        user = factory(
            email=email,
            surname="User",
            name="Inactive Policy",
            address="HQ",
            department="Risk",
            phone_number=phone_number,
        )
        user.set_password("Secret123!")
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        return user

    @staticmethod
    def _policy(enabled=True, days=30):
        return SimpleNamespace(
            enable_inactivity_lock=enabled,
            inactivity_lock_days=days,
        )

    def test_sweep_locks_stale_users_and_never_logged_accounts_only(self):
        now = timezone.now()
        stale_login = self._create_user("stale-login@example.com", "27888000001")
        never_logged = self._create_user("never-logged@example.com", "27888000002")
        recent_user = self._create_user("recent-user@example.com", "27888000003")
        superuser = self._create_user(
            "recovery-admin@example.com",
            "27888000004",
            superuser=True,
        )
        CustomUser.objects.filter(pk=stale_login.pk).update(
            last_login=now - timedelta(days=31)
        )
        CustomUser.objects.filter(pk=never_logged.pk).update(
            last_login=None,
            date_joined=now - timedelta(days=31),
        )
        CustomUser.objects.filter(pk=recent_user.pk).update(
            last_login=now - timedelta(days=29)
        )
        CustomUser.objects.filter(pk=superuser.pk).update(
            last_login=now - timedelta(days=365)
        )

        live_session = SessionStore()
        live_session.set_expiry(3600)
        live_session.save()
        access_log = UserAccessLog.objects.create(
            user=stale_login,
            session_key=live_session.session_key,
            login_time=now - timedelta(hours=1),
        )

        locked_count = sweep_inactive_user_accounts(
            checked_at=now,
            runtime_settings=self._policy(),
        )

        self.assertEqual(locked_count, 2)
        for user in (stale_login, never_logged):
            user.refresh_from_db()
            self.assertEqual(user.inactivity_locked_at, now)
        recent_user.refresh_from_db()
        superuser.refresh_from_db()
        self.assertIsNone(recent_user.inactivity_locked_at)
        self.assertIsNone(superuser.inactivity_locked_at)
        self.assertFalse(Session.objects.filter(session_key=live_session.session_key).exists())
        access_log.refresh_from_db()
        self.assertEqual(access_log.end_reason, UserAccessLog.END_REASON_INACTIVITY_LOCK)
        self.assertEqual(
            AuditTrail.objects.filter(
                model_name="CustomUser",
                change_description__icontains="Automatically locked inactive account",
            ).count(),
            2,
        )

    def test_disabled_policy_does_not_lock_stale_user(self):
        now = timezone.now()
        user = self._create_user("disabled-policy@example.com", "27888000005")
        CustomUser.objects.filter(pk=user.pk).update(
            last_login=now - timedelta(days=365)
        )

        locked_count = sweep_inactive_user_accounts(
            checked_at=now,
            runtime_settings=self._policy(enabled=False),
        )

        user.refresh_from_db()
        self.assertEqual(locked_count, 0)
        self.assertIsNone(user.inactivity_locked_at)

    def test_admin_reset_starts_new_inactivity_grace_period(self):
        now = timezone.now()
        user = self._create_user("reset-grace@example.com", "27888000006")
        CustomUser.objects.filter(pk=user.pk).update(
            last_login=now - timedelta(days=90),
            inactivity_locked_at=now,
            lock_immediately_on_next_failure=True,
        )
        user.refresh_from_db()

        released_inactivity_lock = _release_user_lockout_by_admin(user)

        user.refresh_from_db()
        self.assertTrue(released_inactivity_lock)
        self.assertIsNone(user.inactivity_locked_at)
        self.assertFalse(user.lock_immediately_on_next_failure)
        self.assertIsNotNone(user.inactivity_lock_reset_at)
        self.assertEqual(
            sweep_inactive_user_accounts(
                checked_at=user.inactivity_lock_reset_at + timedelta(days=29),
                runtime_settings=self._policy(),
            ),
            0,
        )
        self.assertEqual(
            sweep_inactive_user_accounts(
                checked_at=user.inactivity_lock_reset_at + timedelta(days=30),
                runtime_settings=self._policy(),
            ),
            1,
        )

    def test_inactivity_locked_user_sees_specific_lock_screen(self):
        user = self._create_user("locked-screen@example.com", "27888000007")
        CustomUser.objects.filter(pk=user.pk).update(inactivity_locked_at=timezone.now())

        response = self.client.post(
            reverse("login_popup"),
            {
                "login_identifier": user.email,
                "password": "Secret123!",
            },
        )

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Account Locked Due to Inactivity", status_code=429)
        self.assertContains(
            response,
            "administrator must release the inactivity lock",
            status_code=429,
        )

    def test_authentication_backend_rejects_inactivity_locked_user(self):
        user = self._create_user("backend-locked@example.com", "27888000008")
        user.inactivity_locked_at = timezone.now()
        user.save(update_fields=["inactivity_locked_at"])

        authenticated_user = CaseInsensitiveEmailOrAliasBackend().authenticate(
            None,
            username=user.email,
            password="Secret123!",
        )

        self.assertIsNone(authenticated_user)

    def test_security_form_exposes_inactivity_policy_controls(self):
        form = SystemSettingsForm()

        self.assertIn("enable_inactivity_lock", form.fields)
        self.assertIn("inactivity_lock_days", form.fields)
        self.assertEqual(form.fields["inactivity_lock_days"].widget.attrs["min"], 1)

    def test_users_scheduler_runs_session_and_inactivity_checks(self):
        checked_at = timezone.now()
        with (
            patch("Users.access_logs.sweep_idle_user_sessions", return_value=2) as idle_sweep,
            patch("Users.inactivity_locks.maybe_sweep_inactive_user_accounts", return_value=3) as inactivity_sweep,
        ):
            result = run_user_security_sweep_cycle(checked_at=checked_at)

        self.assertEqual(result, {"closed_sessions": 2, "locked_users": 3})
        idle_sweep.assert_called_once_with(ended_at=checked_at)
        inactivity_sweep.assert_called_once_with(checked_at=checked_at)

    def test_inactivity_check_continues_when_session_sweep_fails(self):
        checked_at = timezone.now()
        with (
            patch("Users.access_logs.sweep_idle_user_sessions", side_effect=RuntimeError("session failure")),
            patch("Users.inactivity_locks.maybe_sweep_inactive_user_accounts", return_value=3) as inactivity_sweep,
            self.assertLogs("Users.session_sweeper", level="ERROR"),
        ):
            result = run_user_security_sweep_cycle(checked_at=checked_at)

        self.assertEqual(result, {"closed_sessions": 0, "locked_users": 3})
        inactivity_sweep.assert_called_once_with(checked_at=checked_at)

    def test_session_check_continues_when_inactivity_sweep_fails(self):
        checked_at = timezone.now()
        with (
            patch("Users.access_logs.sweep_idle_user_sessions", return_value=2) as idle_sweep,
            patch(
                "Users.inactivity_locks.maybe_sweep_inactive_user_accounts",
                side_effect=RuntimeError("inactivity failure"),
            ),
            self.assertLogs("Users.session_sweeper", level="ERROR"),
        ):
            result = run_user_security_sweep_cycle(checked_at=checked_at)

        self.assertEqual(result, {"closed_sessions": 2, "locked_users": 0})
        idle_sweep.assert_called_once_with(ended_at=checked_at)

    def test_runserver_reloader_parent_does_not_start_duplicate_scheduler(self):
        with (
            patch("Users.session_sweeper.sys.argv", ["manage.py", "runserver"]),
            patch.dict("Users.session_sweeper.os.environ", {}, clear=True),
        ):
            self.assertFalse(_should_start_sweeper())

        with (
            patch("Users.session_sweeper.sys.argv", ["manage.py", "runserver"]),
            patch.dict("Users.session_sweeper.os.environ", {"RUN_MAIN": "true"}, clear=True),
        ):
            self.assertTrue(_should_start_sweeper())
