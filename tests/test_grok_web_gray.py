# encoding:utf-8

import os
import json
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestGrokWebGray(unittest.TestCase):
    def _fake_conf(self, values=None):
        data = {"grok_gray_enabled": False}
        if values:
            data.update(values)
        fake_conf = MagicMock()
        fake_conf.get.side_effect = lambda key, default=None: data.get(key, default)
        return fake_conf

    def test_grok_provider_hidden_by_default(self):
        from channel.web.web_channel import ConfigHandler

        providers = ConfigHandler()._visible_provider_models(self._fake_conf())

        self.assertNotIn("grok", providers)
        self.assertIn("openai", providers)
        self.assertIn("deepseek", providers)

    def test_grok_provider_visible_when_gray_enabled(self):
        from channel.web.web_channel import ConfigHandler

        providers = ConfigHandler()._visible_provider_models(
            self._fake_conf({"grok_gray_enabled": True})
        )

        self.assertIn("grok", providers)

    def _assert_no_secret_fields(self, payload):
        serialized = json.dumps(payload, ensure_ascii=False)
        forbidden = (
            "access_token",
            "refresh_token",
            "authorization_code",
            "secret-code",
            "code_verifier",
            "Authorization",
            "Bearer ",
        )
        for marker in forbidden:
            self.assertNotIn(marker, serialized)

    def test_grok_status_start_and_test_do_not_return_tokens_or_codes(self):
        import channel.web.web_channel as web_channel

        with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "header", return_value=None), \
                patch(
                    "integrations.hermes_xai.auth.get_xai_oauth_status",
                    return_value={
                        "logged_in": True,
                        "provider": "xai-oauth",
                        "base_url": "https://api.x.ai/v1",
                        "email": "",
                        "expires_at": 0,
                        "needs_reauth": False,
                    },
                ):
            status_payload = json.loads(web_channel.GrokStatusHandler().GET())

        self._assert_no_secret_fields(status_payload)

        with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "header", return_value=None), \
                patch(
                    "integrations.hermes_xai.auth.start_xai_oauth_login",
                    return_value={
                        "authorize_url": "https://auth.x.ai/oauth2/auth?<redacted>",
                        "state": "pending",
                        "redirect_uri": "http://127.0.0.1:56121/callback",
                        "manual_paste_supported": True,
                    },
                ):
            start_payload = json.loads(web_channel.GrokLoginStartHandler().POST())

        self._assert_no_secret_fields(start_payload)

        with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "header", return_value=None), \
                patch(
                    "integrations.hermes_xai.xai_http.resolve_xai_http_credentials",
                    return_value={
                        "api_key": "access_token_secret",
                        "provider": "xai-oauth",
                        "auth_mode": "oauth_pkce",
                        "base_url": "https://api.x.ai/v1",
                    },
                ):
            test_payload = json.loads(web_channel.GrokTestHandler().POST())

        self._assert_no_secret_fields(test_payload)

    def test_grok_manual_handler_can_finish_pending_loopback_callback(self):
        import channel.web.web_channel as web_channel
        from integrations.hermes_xai import auth

        pending_auth = {
            "logged_in": True,
            "provider": "xai-oauth",
            "base_url": "https://api.x.ai/v1",
            "email": "",
            "expires_at": 0,
            "needs_reauth": False,
        }
        missing_state = auth.AuthError(
            "Grok manual login requires the full callback URL or a query string with code and state.",
            code="xai_state_missing",
        )

        with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "header", return_value=None), \
                patch.object(web_channel.web, "data", return_value=b'{"callback_url":"?code=manual-only"}'), \
                patch(
                    "integrations.hermes_xai.auth.complete_xai_oauth_with_callback_url",
                    side_effect=missing_state,
                ) as manual_complete, \
                patch(
                    "integrations.hermes_xai.auth.complete_xai_oauth_with_pending_callback",
                    return_value=pending_auth,
                ) as pending_complete:
            payload = json.loads(web_channel.GrokLoginManualHandler().POST())

        self.assertEqual(payload["status"], "complete")
        self.assertTrue(payload["logged_in"])
        manual_complete.assert_called_once_with("?code=manual-only")
        pending_complete.assert_called_once_with()
        self._assert_no_secret_fields(payload)

    def test_grok_api_errors_redact_sensitive_details(self):
        import channel.web.web_channel as web_channel

        secret_message = (
            "failed Authorization: Bearer secret-bearer access_token=secret-access "
            "refresh_token=secret-refresh api_key=secret-api "
            "http://127.0.0.1:56121/callback?code=secret-code&state=secret-state"
        )

        with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "header", return_value=None), \
                patch(
                    "integrations.hermes_xai.auth.get_xai_oauth_status",
                    side_effect=RuntimeError(secret_message),
                ):
            payload = json.loads(web_channel.GrokStatusHandler().GET())

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn("redacted", serialized.lower())
        for secret in (
            "secret-bearer",
            "secret-access",
            "secret-refresh",
            "secret-api",
            "secret-code",
            "secret-state",
        ):
            self.assertNotIn(secret, serialized)

    def test_grok_web_config_can_toggle_gray_and_import_flags(self):
        import channel.web.web_channel as web_channel

        self.assertIn("grok_gray_enabled", web_channel.ConfigHandler.EDITABLE_KEYS)
        self.assertIn("grok_import_hermes_auth", web_channel.ConfigHandler.EDITABLE_KEYS)
        self.assertIn("grok_import_hermes_auth_overwrite", web_channel.ConfigHandler.EDITABLE_KEYS)

    def test_backend_profile_id_accepts_custom_backend_names(self):
        from channel.web.web_channel import ConfigHandler

        handler = ConfigHandler()

        self.assertEqual(handler._backend_profile_id("custom_fast"), "custom_fast")
        self.assertEqual(handler._backend_profile_id("grok"), "grok")
        self.assertEqual(handler._backend_profile_id("gpt"), "")

    def test_backend_profile_save_does_not_require_updates_payload(self):
        import channel.web.web_channel as web_channel

        with tempfile.TemporaryDirectory() as tmp:
            fake_conf = {
                "llm_backend": {
                    "current_backend": "capi",
                    "state_path": os.path.join(tmp, "state.json"),
                    "providers": {},
                }
            }
            fake_file = os.path.join(tmp, "channel", "web", "web_channel.py")
            payload = {
                "llm_backend_provider": {
                    "backend": "custom_fast",
                    "model": "gpt-custom",
                    "api_base": "https://custom.example/v1",
                    "wire_api": "responses",
                }
            }

            with patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel.web, "header", return_value=None), \
                    patch.object(web_channel.web, "data", return_value=json.dumps(payload).encode()), \
                    patch.object(web_channel, "conf", return_value=fake_conf), \
                    patch.object(web_channel, "__file__", fake_file):
                result = json.loads(web_channel.ConfigHandler().POST())

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["applied"]["llm_backend_provider"], "custom_fast")
        self.assertEqual(
            fake_conf["llm_backend"]["providers"]["custom_fast"]["api_base"],
            "https://custom.example/v1",
        )


if __name__ == "__main__":
    unittest.main()
