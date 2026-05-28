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

    def test_manual_authorization_code_uses_active_session_state(self):
        auth._active_login = auth._LoginSession(
            state="expected-state",
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
        access_token = _jwt_with_exp(time.time() + 3600)
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "access_token": access_token,
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
        }

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
