# encoding:utf-8

from types import SimpleNamespace

import pytest

from common import reasoning_effort_policy as policy


@pytest.fixture(autouse=True)
def enable_policy(monkeypatch, tmp_path):
    settings = {
        "reasoning_effort_policy_enabled": True,
        "reasoning_effort_policy_admin_only": False,
        "reasoning_effort_policy_default_effort": "medium",
        "reasoning_effort_policy_quality_effort": "xhigh",
        "reasoning_effort_policy_low_effort": "low",
        "reasoning_effort_policy_audit_enabled": False,
        "agent_workspace": str(tmp_path),
    }
    monkeypatch.setattr(policy, "conf", lambda: settings)
    monkeypatch.setattr(policy, "get_current_backend", lambda: "capi")
    monkeypatch.setattr(policy, "get_effective_model", lambda: "gpt-test")


def test_text_input_keeps_existing_medium_behavior():
    effort, rule = policy.classify_local_task("你好", "xhigh", "medium", input_is_voice=False)

    assert effort == "medium"
    assert rule == "greeting"


def test_voice_short_simple_task_gets_low():
    model = SimpleNamespace(input_is_voice=True, channel_type="wecom_bot", session_id="s1")

    decision = policy.resolve_reasoning_effort_for_task("你好", model)

    assert decision.selected_effort == "low"
    assert decision.decision_source == "local"
    assert decision.local_rule.startswith("low_")
    assert decision.input_is_voice is True


def test_voice_complex_task_stays_xhigh():
    model = SimpleNamespace(input_is_voice=True, channel_type="wecom_bot", session_id="s1")

    decision = policy.resolve_reasoning_effort_for_task("帮我实现一个 Python 脚本并提交 git", model)

    assert decision.selected_effort == "xhigh"
    assert decision.local_rule in {"coding", "repo_work"}


def test_reasoning_decision_event_payload_has_safe_voice_fields():
    from agent.protocol.agent_stream import AgentStreamExecutor

    model = SimpleNamespace(channel_type="wecom_bot", session_id="session-1")
    executor = object.__new__(AgentStreamExecutor)
    executor.model = model
    executor._reasoning_effort_decision = policy.ReasoningEffortDecision(
        task_id="task",
        selected_effort="low",
        decision_source="local",
        reason="low_greeting",
        active_backend="capi",
        main_model="gpt-test",
        local_rule="low_greeting",
        input_is_voice=True,
    )

    payload = executor._reasoning_effort_event_payload()

    assert payload == {
        "selected_effort": "low",
        "source": "local",
        "local_rule": "low_greeting",
        "input_is_voice": True,
        "channel": "wecom_bot",
        "session_id": "session-1",
    }
    assert "token" not in repr(payload).lower()
