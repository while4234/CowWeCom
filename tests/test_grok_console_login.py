# encoding:utf-8

import os
import json
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestGrokConsoleLogin(unittest.TestCase):
    def _fake_conf(self, values=None):
        data = {}
        if values:
            data.update(values)
        fake_conf = MagicMock()
        fake_conf.get.side_effect = lambda key, default=None: data.get(key, default)
        return fake_conf

    def test_grok_provider_visible_in_console_by_default(self):
        from channel.web.web_channel import ConfigHandler

        providers = ConfigHandler()._visible_provider_models(self._fake_conf())

        self.assertIn("grok", providers)
        self.assertIn("openai", providers)
        self.assertIn("deepseek", providers)

    def test_legacy_grok_page_redirects_to_console(self):
        import channel.web.web_channel as web_channel
        from channel.web.web_channel import ConfigHandler

        providers = ConfigHandler()._visible_provider_models(self._fake_conf())

        self.assertIn("grok", providers)
        with patch.object(web_channel, "_require_auth", return_value=None):
            web_channel.web.ctx.path = "/grok"
            web_channel.web.ctx.home = ""
            web_channel.web.ctx.headers = []
            try:
                with self.assertRaises(web_channel.web.HTTPError):
                    web_channel.GrokPageHandler().GET()
            finally:
                try:
                    del web_channel.web.ctx.path
                    del web_channel.web.ctx.home
                    del web_channel.web.ctx.headers
                except AttributeError:
                    pass

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
                patch.object(web_channel.web, "input", return_value={"account_id": ""}), \
                patch(
                    "integrations.hermes_xai.auth.get_xai_oauth_status",
                    return_value={
                        "logged_in": True,
                        "provider": "xai-oauth",
                        "base_url": "https://api.x.ai/v1",
                        "email": "",
                        "expires_at": 0,
                        "needs_reauth": False,
                        "accounts": [],
                    },
                ):
            status_payload = json.loads(web_channel.GrokStatusHandler().GET())

        self._assert_no_secret_fields(status_payload)

        with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "header", return_value=None), \
                patch.object(web_channel.web, "data", return_value=b'{}'), \
                patch(
                    "integrations.hermes_xai.auth.start_xai_oauth_login",
                    return_value={
                        "authorize_url": "https://auth.x.ai/oauth2/auth?<redacted>",
                        "state": "pending",
                        "redirect_uri": "http://127.0.0.1:56121/callback",
                        "manual_paste_supported": True,
                        "account_id": "default",
                        "account_name": "Default",
                    },
                ):
            start_payload = json.loads(web_channel.GrokLoginStartHandler().POST())

        self._assert_no_secret_fields(start_payload)
        self.assertEqual(start_payload["account_id"], "default")

        with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "header", return_value=None), \
                patch.object(web_channel.web, "data", return_value=b'{"account_id":"work"}'), \
                patch(
                    "integrations.hermes_xai.xai_http.resolve_xai_http_credentials",
                    return_value={
                        "api_key": "access_token_secret",
                        "provider": "xai-oauth",
                        "auth_mode": "oauth_pkce",
                        "base_url": "https://api.x.ai/v1",
                        "account_id": "work",
                        "account_name": "Work Grok",
                    },
                ) as resolver:
            test_payload = json.loads(web_channel.GrokTestHandler().POST())

        self._assert_no_secret_fields(test_payload)
        self.assertEqual(test_payload["account_id"], "work")
        resolver.assert_called_once_with(account_id="work")

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
                patch.object(web_channel.web, "input", return_value={"account_id": ""}), \
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

    def test_grok_web_config_exposes_console_account_flags(self):
        import channel.web.web_channel as web_channel

        self.assertIn("grok_proxy", web_channel.ConfigHandler.EDITABLE_KEYS)
        self.assertNotIn("grok_gray_enabled", web_channel.ConfigHandler.EDITABLE_KEYS)
        self.assertIn("grok_import_hermes_auth", web_channel.ConfigHandler.EDITABLE_KEYS)
        self.assertIn("grok_import_hermes_auth_overwrite", web_channel.ConfigHandler.EDITABLE_KEYS)

    def test_console_has_grok_account_login_controls(self):
        script = open(
            os.path.join(os.path.dirname(__file__), "..", "channel", "web", "static", "js", "console.js"),
            encoding="utf-8",
        ).read()

        self.assertIn("startGrokAccountLogin", script)
        self.assertIn("/api/grok/account/select", script)
        self.assertIn("cfg-grok-account-name", script)

    def test_console_grok_manual_input_mentions_authorization_code(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        script = open(
            os.path.join(root, "channel", "web", "static", "js", "console.js"),
            encoding="utf-8",
        ).read()
        markup = open(
            os.path.join(root, "channel", "web", "chat.html"),
            encoding="utf-8",
        ).read()

        self.assertIn("Callback URL / 授权码", script)
        self.assertIn("Callback URL / auth code", script)
        self.assertIn("Grok Build 授权码", markup)

    def test_console_grok_manual_error_recovers_successful_login_status(self):
        script = open(
            os.path.join(os.path.dirname(__file__), "..", "channel", "web", "static", "js", "console.js"),
            encoding="utf-8",
        ).read()

        self.assertIn("grokStatusMatchesCompletedLogin", script)
        self.assertIn("recoverCompletedGrokLoginAfterManualError", script)
        self.assertIn("showCompletedGrokLogin(input)", script)
        self.assertIn("if (await recoverCompletedGrokLoginAfterManualError(accountId, accountName, input)) return;", script)

    def test_console_has_backend_provider_test_controls(self):
        script = open(
            os.path.join(os.path.dirname(__file__), "..", "channel", "web", "static", "js", "console.js"),
            encoding="utf-8",
        ).read()
        markup = open(
            os.path.join(os.path.dirname(__file__), "..", "channel", "web", "chat.html"),
            encoding="utf-8",
        ).read()

        self.assertIn("testBackendProviderConfig", script)
        self.assertIn("/config/backend/test", script)
        self.assertIn("cfg-backend-provider-test", markup)

    def test_config_handler_accepts_backend_profile_payload(self):
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
            config_json = os.path.join(tmp, "config.json")
            with open(config_json, "w", encoding="utf-8-sig") as handle:
                json.dump({"llm_backend": fake_conf["llm_backend"]}, handle)

            provider = {
                "backend": "custom_fast",
                "model": "gpt-custom",
                "api_base": "https://custom.example/v1",
                "api_key": "TEST-KEY",
                "wire_api": "responses",
            }
            save_payload = {
                "llm_backend_provider": {
                    **provider,
                }
            }

            with patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel.web, "header", return_value=None), \
                    patch.object(web_channel.web, "data", return_value=json.dumps(save_payload).encode()), \
                    patch.object(web_channel, "conf", return_value=fake_conf), \
                    patch("config.conf", return_value=fake_conf), \
                    patch.object(web_channel, "__file__", fake_file):
                untested = json.loads(web_channel.ConfigHandler().POST())

            calls = []

            class FakeClient:
                def __init__(self, **_kwargs):
                    pass

                def responses(self, **kwargs):
                    calls.append(kwargs)
                    return iter([{"type": "response.created"}])

            with patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel.web, "header", return_value=None), \
                    patch.object(web_channel.web, "data", return_value=json.dumps({"provider": provider}).encode()), \
                    patch.object(web_channel, "conf", return_value=fake_conf), \
                    patch("config.conf", return_value=fake_conf), \
                    patch("models.openai.openai_http_client.OpenAIHTTPClient", FakeClient), \
                    patch.object(web_channel, "__file__", fake_file):
                tested = json.loads(web_channel.ConfigBackendProviderTestHandler().POST())

            provider_with_token = dict(provider)
            provider_with_token["test_token"] = tested["test_token"]
            changed_provider = dict(provider_with_token)
            changed_provider["model"] = "gpt-other"
            changed_save_payload = {"llm_backend_provider": changed_provider}
            with patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel.web, "header", return_value=None), \
                    patch.object(web_channel.web, "data", return_value=json.dumps(changed_save_payload).encode()), \
                    patch.object(web_channel, "conf", return_value=fake_conf), \
                    patch("config.conf", return_value=fake_conf), \
                    patch.object(web_channel, "__file__", fake_file):
                changed = json.loads(web_channel.ConfigHandler().POST())

            tested_save_payload = {"llm_backend_provider": provider_with_token}
            with patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel.web, "header", return_value=None), \
                    patch.object(web_channel.web, "data", return_value=json.dumps(tested_save_payload).encode()), \
                    patch.object(web_channel, "conf", return_value=fake_conf), \
                    patch("config.conf", return_value=fake_conf), \
                    patch.object(web_channel, "__file__", fake_file):
                result = json.loads(web_channel.ConfigHandler().POST())

            with open(config_json, encoding="utf-8") as handle:
                saved = json.load(handle)

        self.assertEqual(untested["status"], "error")
        self.assertIn("test required", untested["message"])
        self.assertEqual(tested["status"], "success")
        self.assertTrue(tested["tested"])
        self.assertEqual(calls[0]["api_base"], "https://custom.example/v1")
        self.assertEqual(calls[0]["api_key"], "TEST-KEY")
        self.assertNotIn("TEST-KEY", json.dumps(tested, ensure_ascii=False))
        self.assertEqual(changed["status"], "error")
        self.assertIn("settings changed", changed["message"])
        self.assertEqual(result["status"], "success")
        self.assertIn("llm_backend_provider", result["applied"])
        self.assertEqual(saved["llm_backend"]["providers"]["custom_fast"]["wire_api"], "responses")

    def test_backend_provider_test_redacts_connection_error_secrets(self):
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
            provider = {
                "backend": "custom_fast",
                "model": "gpt-custom",
                "api_base": "https://secret.example/v1",
                "api_key": "SECRET-KEY-1234",
                "wire_api": "responses",
            }

            class FakeClient:
                def __init__(self, **_kwargs):
                    pass

                def responses(self, **kwargs):
                    raise RuntimeError(f"bad {kwargs['api_key']} {kwargs['api_base']}")

            with patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel.web, "header", return_value=None), \
                    patch.object(web_channel.web, "data", return_value=json.dumps({"provider": provider}).encode()), \
                    patch.object(web_channel, "conf", return_value=fake_conf), \
                    patch("config.conf", return_value=fake_conf), \
                    patch("models.openai.openai_http_client.OpenAIHTTPClient", FakeClient), \
                    patch.object(web_channel, "__file__", fake_file):
                result = json.loads(web_channel.ConfigBackendProviderTestHandler().POST())

        self.assertEqual(result["status"], "error")
        self.assertNotIn("SECRET-KEY-1234", result["message"])
        self.assertNotIn("https://secret.example/v1", result["message"])

    def test_model_config_grok_selection_sets_admin_backend_not_global_bot_type(self):
        import channel.web.web_channel as web_channel

        with tempfile.TemporaryDirectory() as tmp:
            fake_conf = {
                "llm_backend": {
                    "current_backend": "capi",
                    "state_path": os.path.join(tmp, "state.json"),
                    "providers": {"grok": {"model": "grok-4.3", "auth": "account"}},
                }
            }
            fake_file = os.path.join(tmp, "channel", "web", "web_channel.py")
            payload = {"updates": {"bot_type": "grok", "model": "grok-4.3"}}

            with patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel.web, "header", return_value=None), \
                    patch.object(web_channel.web, "data", return_value=json.dumps(payload).encode()), \
                    patch.object(web_channel, "conf", return_value=fake_conf), \
                    patch("config.conf", return_value=fake_conf), \
                    patch.object(web_channel, "__file__", fake_file):
                result = json.loads(web_channel.ConfigHandler().POST())

            config_json = os.path.join(tmp, "config.json")
            with open(config_json, encoding="utf-8") as handle:
                saved = json.load(handle)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["applied"]["actor_backend"]["backend"], "grok")
        self.assertNotIn("bot_type", saved)
        self.assertNotIn("model", saved)
        self.assertEqual(saved["llm_backend"]["providers"]["grok"]["model"], "grok-4.3")


if __name__ == "__main__":
    unittest.main()
