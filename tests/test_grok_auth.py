# encoding:utf-8

import base64
import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from integrations.hermes_xai import auth


def _jwt_with_exp(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode("utf-8")).decode("ascii").rstrip("=")
    return f"header.{payload}.sig"


def _jwt_with_payload(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"header.{encoded}.sig"


class TestGrokAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth_file = os.path.join(self.tmp.name, "grok_auth.json")
        self.fake_conf = MagicMock()
        self.fake_conf.get.side_effect = lambda key, default=None: {
            "grok_auth_file": self.auth_file,
            "grok_api_base": "https://api.x.ai/v1",
            "grok_oauth_accept_bare_code": False,
            "grok_import_hermes_auth": False,
            "grok_import_hermes_auth_overwrite": False,
        }.get(key, default)
        self.conf_patch = patch("integrations.hermes_xai.auth.conf", return_value=self.fake_conf)
        self.conf_patch.start()
        self.addCleanup(self.conf_patch.stop)
        self.addCleanup(self.tmp.cleanup)
        auth._active_login = None

    def tearDown(self):
        auth._active_login = None

    def test_state_mismatch_is_rejected(self):
        auth._active_login = auth._LoginSession(
            state="expected",
            nonce="nonce",
            code_verifier="verifier",
            code_challenge="challenge",
            redirect_uri=auth._default_redirect_uri(),
            discovery={
                "issuer": auth.XAI_OAUTH_ISSUER,
                "authorization_endpoint": "https://auth.x.ai/oauth2/auth",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            },
            authorize_url="https://auth.x.ai/oauth2/auth",
            created_at=time.time(),
        )

        with self.assertRaises(auth.AuthError) as ctx:
            auth.complete_xai_oauth_with_callback_url(
                "http://127.0.0.1:56121/callback?code=secret-code&state=wrong"
            )

        self.assertEqual(ctx.exception.code, "xai_state_mismatch")

    def test_callback_host_path_and_port_are_rejected(self):
        invalid_urls = [
            "http://localhost:56121/callback?code=x&state=s",
            "http://127.0.0.1:56121/not-callback?code=x&state=s",
            "http://127.0.0.1:56122/callback?code=x&state=s",
            "https://127.0.0.1:56121/callback?code=x&state=s",
        ]
        for callback_url in invalid_urls:
            with self.subTest(callback_url=callback_url):
                with self.assertRaises(auth.AuthError):
                    auth._parse_callback_url(callback_url)

    def _start_fake_login(self, state="expected-state"):
        auth._active_login = auth._LoginSession(
            state=state,
            nonce="nonce",
            code_verifier="verifier",
            code_challenge="challenge",
            redirect_uri=auth._default_redirect_uri(),
            discovery={
                "issuer": auth.XAI_OAUTH_ISSUER,
                "authorization_endpoint": "https://auth.x.ai/oauth2/auth",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            },
            authorize_url="https://auth.x.ai/oauth2/auth",
            created_at=time.time(),
        )

    def _fake_token_response(self):
        access_token = _jwt_with_exp(time.time() + 3600)
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "access_token": access_token,
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
        }
        return fake_response

    def _assert_no_secret_fields(self, payload):
        serialized = json.dumps(payload, ensure_ascii=False)
        forbidden = (
            "access_token",
            "refresh_token",
            "id_token",
            "refresh-secret",
            "loopback-code",
            "secret-code",
            "code_verifier",
            "Authorization",
            "Bearer ",
        )
        for marker in forbidden:
            self.assertNotIn(marker, serialized)

    def test_start_login_payload_is_token_and_verifier_free(self):
        fake_discovery = MagicMock()
        fake_discovery.status_code = 200
        fake_discovery.json.return_value = {
            "issuer": auth.XAI_OAUTH_ISSUER,
            "authorization_endpoint": "https://auth.x.ai/oauth2/auth",
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        }

        with patch("integrations.hermes_xai.auth.requests.get", return_value=fake_discovery), \
                patch(
                    "integrations.hermes_xai.auth._start_callback_server",
                    return_value=(None, None, auth._default_redirect_uri()),
                ):
            payload = auth.start_xai_oauth_login()

        self.assertEqual(payload["state"], "pending")
        self.assertNotIn("code_verifier", payload)
        self.assertNotIn(auth._active_login.code_verifier, json.dumps(payload, ensure_ascii=False))
        self._assert_no_secret_fields(payload)

    def test_manual_authorization_code_without_state_is_rejected_by_default(self):
        self._start_fake_login()

        with self.assertRaises(auth.AuthError) as ctx:
            auth.complete_xai_oauth_with_callback_url("manual-code-from-grok-build")

        self.assertEqual(ctx.exception.code, "xai_state_missing")

    def test_manual_authorization_code_uses_active_session_state_when_enabled(self):
        self.fake_conf.get.side_effect = lambda key, default=None: {
            "grok_auth_file": self.auth_file,
            "grok_api_base": "https://api.x.ai/v1",
            "grok_oauth_accept_bare_code": True,
            "grok_import_hermes_auth": False,
            "grok_import_hermes_auth_overwrite": False,
        }.get(key, default)
        self._start_fake_login()
        fake_response = self._fake_token_response()

        with patch("integrations.hermes_xai.auth.requests.post", return_value=fake_response) as post:
            status = auth.complete_xai_oauth_with_callback_url("manual-code-from-grok-build")

        self.assertTrue(status["logged_in"])
        request_data = post.call_args.kwargs["data"]
        self.assertEqual(request_data["code"], "manual-code-from-grok-build")
        self.assertEqual(request_data["redirect_uri"], auth._default_redirect_uri())
        self.assertEqual(request_data["code_verifier"], "verifier")

    def test_manual_callback_query_is_accepted(self):
        parsed = auth._parse_manual_callback_input("?code=secret-code&state=state-1")

        self.assertEqual(parsed["code"], "secret-code")
        self.assertEqual(parsed["state"], "state-1")

    def test_manual_callback_query_without_state_is_rejected(self):
        self._start_fake_login()

        with self.assertRaises(auth.AuthError) as ctx:
            auth.complete_xai_oauth_with_callback_url("?code=secret-code")

        self.assertEqual(ctx.exception.code, "xai_state_missing")

    def test_full_callback_url_state_match_succeeds(self):
        self._start_fake_login(state="state-1")
        fake_response = self._fake_token_response()

        with patch("integrations.hermes_xai.auth.requests.post", return_value=fake_response) as post:
            status = auth.complete_xai_oauth_with_callback_url(
                "http://127.0.0.1:56121/callback?code=secret-code&state=state-1"
            )

        self.assertTrue(status["logged_in"])
        request_data = post.call_args.kwargs["data"]
        self.assertEqual(request_data["code"], "secret-code")
        self.assertEqual(request_data["code_verifier"], "verifier")

    def test_poll_login_completes_pending_loopback_callback_without_manual_paste(self):
        self._start_fake_login(state="state-1")
        auth._active_login.callback = {
            "code": "loopback-code",
            "state": "state-1",
            "error": None,
            "error_description": None,
        }
        fake_response = self._fake_token_response()

        with patch("integrations.hermes_xai.auth.requests.post", return_value=fake_response) as post:
            payload = auth.poll_xai_oauth_login()

        self.assertEqual(payload["status"], "complete")
        self.assertTrue(payload["auth"]["logged_in"])
        request_data = post.call_args.kwargs["data"]
        self.assertEqual(request_data["code"], "loopback-code")
        self.assertEqual(request_data["redirect_uri"], auth._default_redirect_uri())
        self.assertEqual(request_data["code_verifier"], "verifier")
        self._assert_no_secret_fields(payload)

    def test_discovery_endpoint_must_be_https_xai_origin(self):
        bad_discoveries = [
            {
                "issuer": "https://auth.x.ai",
                "authorization_endpoint": "http://auth.x.ai/oauth2/auth",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            },
            {
                "issuer": "https://auth.x.ai",
                "authorization_endpoint": "https://evil.example/oauth2/auth",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            },
        ]
        for discovery in bad_discoveries:
            with self.subTest(discovery=discovery):
                with self.assertRaises(auth.AuthError):
                    auth._sanitize_discovery(discovery)

    def test_token_store_status_does_not_expose_secrets(self):
        auth.save_xai_oauth_tokens(
            {
                "access_token": _jwt_with_exp(time.time() + 3600),
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
            },
            discovery={
                "issuer": auth.XAI_OAUTH_ISSUER,
                "authorization_endpoint": "https://auth.x.ai/oauth2/auth",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            },
            redirect_uri=auth._default_redirect_uri(),
        )

        status = auth.get_xai_oauth_status()
        serialized = json.dumps(status, ensure_ascii=False)
        self.assertTrue(status["logged_in"])
        self.assertNotIn("access_token", serialized)
        self.assertNotIn("refresh_token", serialized)
        self.assertNotIn("refresh-secret", serialized)
        self.assertNotIn("code", serialized)

    def test_multiple_named_accounts_are_listed_and_selectable(self):
        discovery = {
            "issuer": auth.XAI_OAUTH_ISSUER,
            "authorization_endpoint": "https://auth.x.ai/oauth2/auth",
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        }
        auth.save_xai_oauth_tokens(
            {
                "access_token": _jwt_with_exp(time.time() + 3600),
                "refresh_token": "work-refresh-secret",
                "id_token": _jwt_with_payload({"email": "work@example.com"}),
                "expires_in": 3600,
            },
            discovery=discovery,
            redirect_uri=auth._default_redirect_uri(),
            account_id="work",
            account_name="Work Grok",
        )
        auth.save_xai_oauth_tokens(
            {
                "access_token": _jwt_with_exp(time.time() + 3600),
                "refresh_token": "personal-refresh-secret",
                "id_token": _jwt_with_payload({"email": "personal@example.com"}),
                "expires_in": 3600,
            },
            discovery=discovery,
            redirect_uri=auth._default_redirect_uri(),
            account_id="personal",
            account_name="Personal Grok",
        )

        status = auth.get_xai_oauth_status()
        self.assertEqual(status["account_id"], "personal")
        self.assertEqual(status["email"], "personal@example.com")
        accounts = {item["account_id"]: item for item in status["accounts"]}
        self.assertEqual(accounts["work"]["account_name"], "Work Grok")
        self.assertEqual(accounts["personal"]["account_name"], "Personal Grok")

        selected = auth.select_xai_oauth_account("work")
        self.assertEqual(selected["account_id"], "work")
        self.assertEqual(selected["email"], "work@example.com")
        creds = auth.resolve_xai_oauth_runtime_credentials()
        self.assertEqual(creds["account_id"], "work")
        self.assertEqual(creds["account_name"], "Work Grok")
        serialized = json.dumps(selected, ensure_ascii=False)
        self.assertNotIn("work-refresh-secret", serialized)
        self.assertNotIn("personal-refresh-secret", serialized)

    def test_import_hermes_auth_copies_only_xai_provider_without_returning_tokens(self):
        hermes_home = os.path.join(self.tmp.name, "hermes")
        os.makedirs(hermes_home)
        hermes_auth_path = os.path.join(hermes_home, "auth.json")
        hermes_store = {
            "version": 1,
            "active_provider": "other",
            "providers": {
                "xai-oauth": {
                    "provider": "xai-oauth",
                    "auth_mode": "oauth_pkce",
                    "redirect_uri": auth._default_redirect_uri(),
                    "tokens": {
                        "access_token": _jwt_with_exp(time.time() + 3600),
                        "refresh_token": "hermes-refresh-secret",
                        "id_token": _jwt_with_payload({"email": "hermes@example.com"}),
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    },
                    "discovery": {
                        "issuer": auth.XAI_OAUTH_ISSUER,
                        "authorization_endpoint": "https://auth.x.ai/oauth2/auth",
                        "token_endpoint": "https://auth.x.ai/oauth2/token",
                    },
                },
                "openrouter": {
                    "tokens": {"access_token": "must-not-copy"},
                },
            },
        }
        with open(hermes_auth_path, "w", encoding="utf-8") as handle:
            json.dump(hermes_store, handle)
        self.fake_conf.get.side_effect = lambda key, default=None: {
            "grok_auth_file": self.auth_file,
            "grok_api_base": "https://api.x.ai/v1",
            "grok_oauth_accept_bare_code": False,
            "grok_import_hermes_auth": True,
            "grok_import_hermes_auth_overwrite": False,
        }.get(key, default)

        with patch.dict(os.environ, {"HERMES_HOME": hermes_home}, clear=False):
            result = auth.import_hermes_xai_auth_if_available()

        self.assertTrue(result["imported"])
        self._assert_no_secret_fields(result)
        status = auth.get_xai_oauth_status()
        self.assertTrue(status["logged_in"])
        self.assertEqual(status["email"], "hermes@example.com")
        stored = json.loads(open(self.auth_file, encoding="utf-8").read())
        self.assertIn("xai-oauth", stored["providers"])
        self.assertNotIn("openrouter", stored["providers"])
        self.assertEqual(json.loads(open(hermes_auth_path, encoding="utf-8").read()), hermes_store)

    def test_import_hermes_auth_does_not_overwrite_existing_cowwecom_provider(self):
        auth.save_xai_oauth_tokens(
            {
                "access_token": _jwt_with_exp(time.time() + 3600),
                "refresh_token": "cow-refresh-secret",
                "id_token": _jwt_with_payload({"email": "cow@example.com"}),
                "expires_in": 3600,
            },
            discovery={
                "issuer": auth.XAI_OAUTH_ISSUER,
                "authorization_endpoint": "https://auth.x.ai/oauth2/auth",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            },
            redirect_uri=auth._default_redirect_uri(),
        )
        hermes_home = os.path.join(self.tmp.name, "hermes-existing")
        os.makedirs(hermes_home)
        with open(os.path.join(hermes_home, "auth.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "providers": {
                        "xai-oauth": {
                            "tokens": {
                                "access_token": _jwt_with_exp(time.time() + 7200),
                                "refresh_token": "hermes-refresh-secret",
                            }
                        }
                    }
                },
                handle,
            )
        self.fake_conf.get.side_effect = lambda key, default=None: {
            "grok_auth_file": self.auth_file,
            "grok_api_base": "https://api.x.ai/v1",
            "grok_oauth_accept_bare_code": False,
            "grok_import_hermes_auth": True,
            "grok_import_hermes_auth_overwrite": False,
        }.get(key, default)

        with patch.dict(os.environ, {"HERMES_HOME": hermes_home}, clear=False):
            result = auth.import_hermes_xai_auth_if_available()

        self.assertFalse(result["imported"])
        self.assertEqual(result["reason"], "cowwecom_auth_store_exists")
        self.assertEqual(auth.get_xai_oauth_status()["email"], "cow@example.com")

    def test_logout_does_not_immediately_reimport_hermes_auth(self):
        auth.save_xai_oauth_tokens(
            {
                "access_token": _jwt_with_exp(time.time() + 3600),
                "refresh_token": "cow-refresh-secret",
                "expires_in": 3600,
            },
            discovery={
                "issuer": auth.XAI_OAUTH_ISSUER,
                "authorization_endpoint": "https://auth.x.ai/oauth2/auth",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            },
            redirect_uri=auth._default_redirect_uri(),
        )
        hermes_home = os.path.join(self.tmp.name, "hermes-logout")
        os.makedirs(hermes_home)
        with open(os.path.join(hermes_home, "auth.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "providers": {
                        "xai-oauth": {
                            "tokens": {
                                "access_token": _jwt_with_exp(time.time() + 7200),
                                "refresh_token": "hermes-refresh-secret",
                            }
                        }
                    }
                },
                handle,
            )
        self.fake_conf.get.side_effect = lambda key, default=None: {
            "grok_auth_file": self.auth_file,
            "grok_api_base": "https://api.x.ai/v1",
            "grok_oauth_accept_bare_code": False,
            "grok_import_hermes_auth": True,
            "grok_import_hermes_auth_overwrite": False,
        }.get(key, default)

        with patch.dict(os.environ, {"HERMES_HOME": hermes_home}, clear=False):
            status = auth.logout_xai_oauth()

        self.assertFalse(status["logged_in"])
        stored = json.loads(open(self.auth_file, encoding="utf-8").read())
        self.assertNotIn("xai-oauth", stored["providers"])

    def test_status_includes_email_from_id_token_when_available(self):
        auth.save_xai_oauth_tokens(
            {
                "access_token": _jwt_with_exp(time.time() + 3600),
                "refresh_token": "refresh-secret",
                "id_token": _jwt_with_payload({"email": "user@example.com"}),
                "expires_in": 3600,
            },
            discovery={
                "issuer": auth.XAI_OAUTH_ISSUER,
                "authorization_endpoint": "https://auth.x.ai/oauth2/auth",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            },
            redirect_uri=auth._default_redirect_uri(),
        )

        status = auth.get_xai_oauth_status()

        self.assertEqual(status["email"], "user@example.com")
        serialized = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("id_token", serialized)
        self.assertNotIn("refresh-secret", serialized)

    def test_redact_sensitive_text_masks_token_values(self):
        redacted = auth.redact_sensitive_text(
            "Authorization: Bearer secret-token access_token=secret-access "
            "refresh_token=secret-refresh code_verifier=secret-verifier"
        )

        self.assertNotIn("secret-token", redacted)
        self.assertNotIn("secret-access", redacted)
        self.assertNotIn("secret-refresh", redacted)
        self.assertNotIn("secret-verifier", redacted)
        self.assertIn("***", redacted)

    def test_resolve_credentials_refreshes_expiring_token_and_writes_back(self):
        auth.save_xai_oauth_tokens(
            {
                "access_token": _jwt_with_exp(time.time() - 10),
                "refresh_token": "refresh-secret",
                "expires_at": int(time.time() - 10),
            },
            discovery={
                "issuer": auth.XAI_OAUTH_ISSUER,
                "authorization_endpoint": "https://auth.x.ai/oauth2/auth",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            },
            redirect_uri=auth._default_redirect_uri(),
        )
        refreshed_token = _jwt_with_exp(time.time() + 7200)
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "access_token": refreshed_token,
            "refresh_token": "refresh-secret-2",
            "expires_in": 7200,
        }

        with patch("integrations.hermes_xai.auth.requests.post", return_value=fake_response) as post:
            creds = auth.resolve_xai_oauth_runtime_credentials()

        self.assertEqual(creds["api_key"], refreshed_token)
        stored = auth.read_xai_oauth_tokens()["tokens"]
        self.assertEqual(stored["refresh_token"], "refresh-secret-2")
        post.assert_called_once()

    def test_default_auth_file_is_project_root_relative_not_cwd_relative(self):
        self.fake_conf.get.side_effect = lambda key, default=None: {
            "grok_auth_file": "",
            "grok_api_base": "https://api.x.ai/v1",
            "grok_oauth_accept_bare_code": False,
            "grok_import_hermes_auth": False,
            "grok_import_hermes_auth_overwrite": False,
        }.get(key, default)

        with tempfile.TemporaryDirectory() as cwd, patch(
            "integrations.hermes_xai.auth.get_root",
            return_value=self.tmp.name,
        ):
            old_cwd = os.getcwd()
            try:
                os.chdir(cwd)
                path = auth._auth_file_path()
            finally:
                os.chdir(old_cwd)

        self.assertEqual(path, os.path.join(self.tmp.name, "data", "auth", "grok_auth.json"))

    def test_relative_auth_file_is_project_root_relative_not_cwd_relative(self):
        self.fake_conf.get.side_effect = lambda key, default=None: {
            "grok_auth_file": "custom/grok_auth.json",
            "grok_api_base": "https://api.x.ai/v1",
            "grok_oauth_accept_bare_code": False,
            "grok_import_hermes_auth": False,
            "grok_import_hermes_auth_overwrite": False,
        }.get(key, default)

        with tempfile.TemporaryDirectory() as cwd, patch(
            "integrations.hermes_xai.auth.get_root",
            return_value=self.tmp.name,
        ):
            old_cwd = os.getcwd()
            try:
                os.chdir(cwd)
                path = auth._auth_file_path()
            finally:
                os.chdir(old_cwd)

        self.assertEqual(path, os.path.join(self.tmp.name, "custom", "grok_auth.json"))

    def test_id_token_nonce_mismatch_is_rejected(self):
        with self.assertRaises(auth.AuthError) as ctx:
            auth._validate_id_token_nonce(_jwt_with_payload({"nonce": "wrong"}), "expected")

        self.assertEqual(ctx.exception.code, "xai_nonce_mismatch")

    def test_loopback_request_must_use_127_host(self):
        self.assertTrue(auth._is_loopback_http_request("127.0.0.1", "127.0.0.1:56121"))
        self.assertFalse(auth._is_loopback_http_request("127.0.0.1", "localhost:56121"))
        self.assertFalse(auth._is_loopback_http_request("192.168.1.2", "127.0.0.1:56121"))


if __name__ == "__main__":
    unittest.main()
