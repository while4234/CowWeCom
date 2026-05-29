import json
import subprocess
import sys
from pathlib import Path

from common import grok_image_prompt_rewriter
from common.prompt_optimization_repository import select_grok_prompt_fragments, strip_repository_keywords


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SELECT_FRAGMENTS_SCRIPT = PROJECT_ROOT / "skills" / "image-prompt-optimization" / "scripts" / "select_prompt_fragments.py"


class FixedRandom:
    def __init__(self, roll, *, choice_index=0):
        self.roll = roll
        self.choice_index = choice_index

    def random(self):
        return self.roll

    def choice(self, values):
        return values[self.choice_index]


class CyclingRandom:
    def __init__(self, roll=0.1):
        self.roll = roll
        self.choice_index = 0

    def random(self):
        return self.roll

    def choice(self, values):
        value = values[self.choice_index % len(values)]
        self.choice_index += 1
        return value


def write_repository_fixture(root):
    (root / "grok").mkdir(parents=True)
    (root / "general").mkdir()
    (root / "grok" / "lighting.txt").write_text("soft portrait light\n", encoding="utf-8")
    (root / "general" / "fallback.txt").write_text("wide cinematic frame\n", encoding="utf-8")


def write_nsfw_repository_fixture(root):
    (root / "grok" / "NSFW").mkdir(parents=True)
    (root / "grok" / "SFW").mkdir()
    (root / "general").mkdir()
    (root / "grok" / "NSFW" / "pose.txt").write_text(
        "\n".join(
            [
                "nsfw-specific pose controls",
                "nsfw-specific composition controls",
                "nsfw-specific anatomy controls",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "grok" / "SFW" / "lighting.txt").write_text("safe portrait lighting\n", encoding="utf-8")
    (root / "general" / "fallback.txt").write_text("general cinematic fallback\n", encoding="utf-8")


def write_nsfw_repository_skill(root):
    skill_dir = root / "image-prompt-optimization"
    (skill_dir / "repositories").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# image-prompt-optimization\n", encoding="utf-8")
    write_nsfw_repository_fixture(skill_dir / "repositories")
    return skill_dir


def assert_standalone_nsfw_priority_with_complement(result):
    fragments = result["fragments"]
    priority_fragments = [fragment for fragment in fragments if fragment.get("selection_role") == "priority"]
    supplement_fragments = [fragment for fragment in fragments if fragment.get("selection_role") == "supplement"]

    assert result["keyword"] == "grok"
    assert result["keyword_hit"] is False
    assert result["category"] == "NSFW"
    assert result["category_forced"] is True
    assert priority_fragments
    assert supplement_fragments
    assert len(supplement_fragments) == 1
    assert all(fragment["repository"] == "grok" for fragment in priority_fragments)
    assert all(fragment["category"] == "NSFW" for fragment in priority_fragments)
    assert all(fragment["category"] != "NSFW" for fragment in supplement_fragments)
    assert any(fragment["text"] == "nsfw-specific pose controls" for fragment in priority_fragments)
    assert supplement_fragments[0]["repository"] in {"grok", "general"}
    assert supplement_fragments[0]["text"] in {"safe portrait lighting", "general cinematic fallback"}


def test_grok_keyword_strips_and_prefers_matching_repository(tmp_path):
    root = tmp_path / "repositories"
    write_repository_fixture(root)

    result = select_grok_prompt_fragments(
        "grok generate a portrait",
        repositories_root=root,
        limit=1,
        rng=FixedRandom(0.1),
    )

    assert result["keyword"] == "grok"
    assert result["keyword_hit"] is True
    assert result["cleaned_prompt"] == "generate a portrait"
    assert result["fragments"][0]["repository"] == "grok"
    assert result["fragments"][0]["text"] == "soft portrait light"


def test_grok_default_selection_prefers_grok_repository_for_non_direct_polishing(tmp_path):
    root = tmp_path / "repositories"
    write_repository_fixture(root)

    result = select_grok_prompt_fragments(
        "generate a portrait",
        repositories_root=root,
        limit=1,
        rng=FixedRandom(0.1),
    )

    assert result["keyword"] == "grok"
    assert result["fragments"][0]["repository"] == "grok"
    assert result["fragments"][0]["text"] == "soft portrait light"
    assert result["keyword_hit"] is False


def test_grok_default_selection_uses_other_repository_for_ten_percent_branch(tmp_path):
    root = tmp_path / "repositories"
    write_repository_fixture(root)

    result = select_grok_prompt_fragments(
        "generate a portrait",
        repositories_root=root,
        limit=1,
        rng=FixedRandom(0.95),
    )

    assert result["keyword"] == "grok"
    assert result["fragments"][0]["repository"] == "general"
    assert result["fragments"][0]["text"] == "wide cinematic frame"


def test_standalone_nsfw_prioritizes_grok_nsfw_and_allows_small_complement(tmp_path):
    root = tmp_path / "repositories"
    write_nsfw_repository_fixture(root)

    result = select_grok_prompt_fragments(
        "NSFW",
        repositories_root=root,
        limit=4,
        rng=CyclingRandom(),
    )

    assert_standalone_nsfw_priority_with_complement(result)


def test_standalone_nsfw_selection_script_matches_helper_metadata(tmp_path):
    root = tmp_path / "repositories"
    write_nsfw_repository_fixture(root)

    completed = subprocess.run(
        [
            sys.executable,
            str(SELECT_FRAGMENTS_SCRIPT),
            "--prompt",
            "NSFW",
            "--repositories-root",
            str(root),
            "--limit",
            "4",
            "--seed",
            "7",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert_standalone_nsfw_priority_with_complement(json.loads(completed.stdout))


def test_nsfw_priority_without_supplement_uses_available_priority_only(tmp_path):
    root = tmp_path / "repositories"
    (root / "grok" / "NSFW").mkdir(parents=True)
    (root / "grok" / "NSFW" / "pose.txt").write_text(
        "nsfw-specific pose controls\nnsfw-specific composition controls\n",
        encoding="utf-8",
    )

    result = select_grok_prompt_fragments(
        "NSFW",
        repositories_root=root,
        limit=4,
        rng=CyclingRandom(),
    )

    assert result["selection_mode"] == "priority_with_supplement"
    assert len(result["fragments"]) == 2
    assert all(fragment["selection_role"] == "priority" for fragment in result["fragments"])
    assert all(fragment["category"] == "NSFW" for fragment in result["fragments"])


def test_nsfw_prompt_overrides_other_repository_keyword(tmp_path):
    root = tmp_path / "repositories"
    write_nsfw_repository_fixture(root)

    result = select_grok_prompt_fragments(
        "general NSFW portrait",
        repositories_root=root,
        limit=4,
        rng=CyclingRandom(),
    )

    assert result["keyword"] == "grok"
    assert result["keyword_hit"] is False
    assert result["category_forced"] is True
    assert_standalone_nsfw_priority_with_complement(result)


def test_grok_text_model_receives_nsfw_priority_and_supplement_metadata(monkeypatch, tmp_path):
    skill_dir = write_nsfw_repository_skill(tmp_path)
    captured = {}

    monkeypatch.setenv("IMAGE_PROMPT_OPTIMIZATION_SKILL_DIR", str(skill_dir))
    monkeypatch.setattr("common.prompt_optimization_repository.random.SystemRandom", lambda: CyclingRandom())

    def fake_call(system_prompt, user_prompt):
        captured["user_prompt"] = user_prompt
        return "rewritten nsfw prompt"

    monkeypatch.setattr(grok_image_prompt_rewriter, "_call_grok_text_model", fake_call)

    result = grok_image_prompt_rewriter.rewrite_grok_image_prompt("NSFW", model="grok-imagine-image")

    assert result["enhanced"] is True
    assert result["library"]["keyword"] == "grok"
    assert result["library"]["category"] == "NSFW"
    assert_standalone_nsfw_priority_with_complement({"fragments": result["supplements"], **result["library"]})
    user_prompt = captured["user_prompt"]
    assert "priority" in user_prompt.lower()
    assert "supplement" in user_prompt.lower()
    assert "nsfw-specific pose controls" in user_prompt
    assert ("safe portrait lighting" in user_prompt) or ("general cinematic fallback" in user_prompt)


def test_strip_repository_keywords_ignores_partial_words():
    assert strip_repository_keywords("use grok style", ["grok"]) == "use style"
    assert strip_repository_keywords("use mygrok style", ["grok"]) == "use mygrok style"
