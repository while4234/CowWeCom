# encoding:utf-8

from unittest.mock import MagicMock, patch

from integrations.hermes_xai import proxy


def _fake_conf(values):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: values.get(key, default)
    return fake_conf


def test_grok_proxy_normalizes_host_port():
    assert proxy.normalize_proxy_url("127.0.0.1:7897") == "http://127.0.0.1:7897"
    assert proxy.normalize_proxy_url("socks5://127.0.0.1:7890") == "socks5://127.0.0.1:7890"
    assert proxy.normalize_proxy_url("") == ""


def test_xai_proxy_prefers_grok_proxy_then_discord_proxy():
    env = {}
    with patch("integrations.hermes_xai.proxy.conf", return_value=_fake_conf({
        "grok_proxy": "127.0.0.1:7897",
        "discord_proxy": "127.0.0.1:7898",
        "proxy": "",
    })):
        assert proxy.resolve_xai_proxy_url(env) == "http://127.0.0.1:7897"

    with patch("integrations.hermes_xai.proxy.conf", return_value=_fake_conf({
        "grok_proxy": "",
        "discord_proxy": "127.0.0.1:7898",
        "proxy": "",
    })):
        assert proxy.resolve_xai_proxy_url(env) == "http://127.0.0.1:7898"


def test_apply_xai_proxy_env_exports_for_skill_subprocess():
    env = {}
    with patch("integrations.hermes_xai.proxy.conf", return_value=_fake_conf({
        "grok_proxy": "",
        "discord_proxy": "http://127.0.0.1:7897",
        "proxy": "",
    })):
        proxy_url = proxy.apply_xai_proxy_env(env)

    assert proxy_url == "http://127.0.0.1:7897"
    assert env["GROK_PROXY"] == "http://127.0.0.1:7897"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert env["HTTP_PROXY"] == "http://127.0.0.1:7897"


def test_grok_bot_http_client_uses_xai_proxy():
    from models.grok.grok_bot import GrokBot

    with patch("models.grok.grok_bot.resolve_xai_proxy_url", return_value="http://127.0.0.1:7897"):
        client = GrokBot.__new__(GrokBot)._get_http_client()

    assert client.proxies == {
        "http": "http://127.0.0.1:7897",
        "https": "http://127.0.0.1:7897",
    }
