# encoding:utf-8

import os
import unittest
from unittest.mock import MagicMock, patch

from integrations.hermes_xai import xai_http
from integrations.hermes_xai.auth import AuthError


class TestGrokHttpCredentials(unittest.TestCase):
    def _fake_conf(self, values=None):
        data = {
            "grok_auth_prefer_oauth": True,
            "grok_api_key": "",
            "grok_api_base": "https://api.x.ai/v1",
        }
        if values:
            data.update(values)
        fake_conf = MagicMock()
        fake_conf.get.side_effect = lambda key, default=None: data.get(key, default)
        return fake_conf

    def test_resolve_prefers_oauth_over_api_key(self):
        oauth_creds = {
            "provider": "xai-oauth",
            "auth_mode": "oauth_pkce",
            "api_key": "oauth-access-token",
            "base_url": "https://api.x.ai/v1",
            "account_id": "work",
            "account_name": "Work Grok",
        }
        with patch("integrations.hermes_xai.xai_http.conf", return_value=self._fake_conf({"grok_api_key": "fallback"})):
            with patch("integrations.hermes_xai.xai_http.resolve_xai_oauth_runtime_credentials", return_value=oauth_creds) as resolver:
                creds = xai_http.resolve_xai_http_credentials(account_id="work")

        self.assertEqual(creds["provider"], "xai-oauth")
        self.assertEqual(creds["auth_mode"], "oauth_pkce")
        self.assertEqual(creds["api_key"], "oauth-access-token")
        self.assertEqual(creds["account_id"], "work")
        self.assertEqual(creds["account_name"], "Work Grok")
        resolver.assert_called_once_with(force_refresh=False, account_id="work")

    def test_resolve_falls_back_to_env_api_key(self):
        with patch.dict(os.environ, {"XAI_API_KEY": "env-key"}, clear=False):
            with patch("integrations.hermes_xai.xai_http.conf", return_value=self._fake_conf()):
                with patch(
                    "integrations.hermes_xai.xai_http.resolve_xai_oauth_runtime_credentials",
                    side_effect=AuthError("missing", code="xai_auth_missing"),
                ):
                    creds = xai_http.resolve_xai_http_credentials()

        self.assertEqual(creds["provider"], "xai")
        self.assertEqual(creds["auth_mode"], "api_key")
        self.assertEqual(creds["api_key"], "env-key")

    def test_missing_oauth_and_api_key_raises_clear_auth_error(self):
        with patch.dict(os.environ, {"XAI_API_KEY": ""}, clear=False):
            with patch("integrations.hermes_xai.xai_http.conf", return_value=self._fake_conf()):
                with patch(
                    "integrations.hermes_xai.xai_http.resolve_xai_oauth_runtime_credentials",
                    side_effect=AuthError("missing", code="xai_auth_missing"),
                ):
                    with self.assertRaises(AuthError) as ctx:
                        xai_http.resolve_xai_http_credentials()

        self.assertEqual(ctx.exception.code, "xai_auth_missing")
        self.assertTrue(ctx.exception.relogin_required)


if __name__ == "__main__":
    unittest.main()
