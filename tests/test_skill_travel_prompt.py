from pathlib import Path

from agent.skills.manager import SkillManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AMAP_ENV_NAMES = ("AMAP_WEBSERVICE_KEY", "SKILL_AMAP_COWWECHAT_WEBSERVICE_KEY", "AMAP_KEY")


def _manager(tmp_path):
    return SkillManager(
        builtin_dir=str(PROJECT_ROOT / "skills"),
        custom_dir=str(tmp_path / "skills"),
    )


def test_travel_and_amap_are_available_together_when_amap_key_is_configured(monkeypatch, tmp_path):
    for name in AMAP_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AMAP_WEBSERVICE_KEY", "unit-test-key")

    prompt = _manager(tmp_path).build_skills_prompt(skill_filter=["amap-cowwechat", "travel-manager"])

    assert "<available_skills>" in prompt
    assert "<unavailable_skills>" not in prompt
    assert "<name>amap-cowwechat</name>" in prompt
    assert "<name>travel-manager</name>" in prompt
    assert "amap-cowwechat" in prompt
    assert "plugin-12306-ticket" in prompt
    assert "AMap Web Service" in prompt


def test_travel_stays_available_and_amap_gets_setup_hint_when_key_is_missing(monkeypatch, tmp_path):
    for name in AMAP_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    prompt = _manager(tmp_path).build_skills_prompt(skill_filter=["amap-cowwechat", "travel-manager"])

    assert "<name>travel-manager</name>" in prompt
    assert "<unavailable_skills>" in prompt
    assert "<name>amap-cowwechat</name>" in prompt
    for name in AMAP_ENV_NAMES:
        assert name in prompt


def test_travel_skill_docs_cross_reference_each_other():
    amap = (PROJECT_ROOT / "skills" / "amap-cowwechat" / "SKILL.md").read_text(encoding="utf-8")
    travel = (PROJECT_ROOT / "skills" / "travel-manager" / "SKILL.md").read_text(encoding="utf-8")

    assert "travel-manager" in amap
    assert "amap-cowwechat" in travel
    assert "ETA" in travel
    assert "traffic" in travel
    assert "交通方式" in travel
