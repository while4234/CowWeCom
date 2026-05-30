import json
import subprocess
import sys
from pathlib import Path

from common import grok_image_prompt_rewriter
from common.prompt_optimization_repository import (
    select_grok_prompt_fragments,
    strip_control_keywords,
    strip_repository_keywords,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SELECT_FRAGMENTS_SCRIPT = PROJECT_ROOT / "skills" / "grok-image-prompt-optimization" / "scripts" / "select_prompt_fragments.py"


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
    (root / "grok" / "Background").mkdir()
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
    (root / "grok" / "Background" / "lighting.txt").write_text("safe portrait lighting\n", encoding="utf-8")
    (root / "general" / "fallback.txt").write_text("general cinematic fallback\n", encoding="utf-8")


def write_korean_repository_fixture(root):
    (root / "grok" / "States").mkdir(parents=True)
    (root / "grok" / "Background").mkdir()
    (root / "grok" / "Styling").mkdir()
    (root / "general").mkdir()
    (root / "grok" / "States" / "Nationality-Race.txt").write_text("korean\n", encoding="utf-8")
    (root / "grok" / "Background" / "scene.txt").write_text("soft Seoul evening background\n", encoding="utf-8")
    (root / "grok" / "Styling" / "portrait.txt").write_text("Nordic blonde runway styling\n", encoding="utf-8")
    (root / "general" / "fallback.txt").write_text("wide cinematic frame\n", encoding="utf-8")


def write_nsfw_repository_skill(root):
    skill_dir = root / "grok-image-prompt-optimization"
    (skill_dir / "repositories").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# grok-image-prompt-optimization\n", encoding="utf-8")
    write_nsfw_repository_fixture(skill_dir / "repositories")
    return skill_dir


def assert_standalone_nsfw_priority_with_complement(
    result,
    *,
    allowed_supplement_texts=frozenset({"safe portrait lighting", "general cinematic fallback"}),
):
    fragments = [
        fragment
        for fragment in result["fragments"]
        if str(fragment.get("selection_role") or "").lower() != "constraint"
    ]
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
    assert supplement_fragments[0]["text"] in allowed_supplement_texts


def assert_korean_nationality_constraint(result):
    constraints = result.get("constraints") or [
        fragment
        for fragment in result.get("fragments", [])
        if str(fragment.get("selection_role") or "").lower() == "constraint"
    ]
    assert len(constraints) == 1

    constraint = constraints[0]
    text = constraint["text"]

    assert constraint["selection_role"] == "constraint"
    assert constraint["constraint_type"] == "nationality"
    assert constraint["repository"] == "grok"
    assert constraint["file"] == "States/Nationality-Race.txt"
    assert constraint["source_text"] == "korean"
    assert "mandatory nationality/ethnicity constraint: korean" in text
    assert "Korean/East Asian facial features" in text
    assert "do not add conflicting identity traits from random fragments" in text


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


def test_korean_prompt_adds_locked_nationality_constraint_and_filters_conflicts(tmp_path):
    root = tmp_path / "repositories"
    write_korean_repository_fixture(root)

    result = select_grok_prompt_fragments(
        "random Korean female portrait",
        repositories_root=root,
        limit=1,
        rng=FixedRandom(0.1),
    )

    assert_korean_nationality_constraint(result)
    selected_texts = [fragment["text"] for fragment in result["fragments"]]
    assert "soft Seoul evening background" in selected_texts
    assert "Nordic blonde runway styling" not in selected_texts


def test_korea_and_chinese_korea_terms_share_same_stable_constraint(tmp_path):
    root = tmp_path / "repositories"
    write_korean_repository_fixture(root)

    english_result = select_grok_prompt_fragments(
        "random Korea female portrait",
        repositories_root=root,
        limit=1,
        rng=FixedRandom(0.1),
    )
    chinese_result = select_grok_prompt_fragments(
        "random 韩国 female portrait",
        repositories_root=root,
        limit=1,
        rng=FixedRandom(0.1),
    )

    assert_korean_nationality_constraint(english_result)
    assert_korean_nationality_constraint(chinese_result)
    assert english_result["constraints"] == chinese_result["constraints"]


def test_combined_nsfw_korean_prompt_keeps_nsfw_priority_and_nationality_constraint(tmp_path):
    root = tmp_path / "repositories"
    write_nsfw_repository_fixture(root)
    (root / "grok" / "States").mkdir()
    (root / "grok" / "States" / "Nationality-Race.txt").write_text("korean\n", encoding="utf-8")
    (root / "grok" / "Background" / "conflict.txt").write_text("Nordic blonde runway styling\n", encoding="utf-8")

    result = select_grok_prompt_fragments(
        "random NSFW Korean female",
        repositories_root=root,
        limit=4,
        rng=CyclingRandom(),
    )

    assert_standalone_nsfw_priority_with_complement(
        result,
        allowed_supplement_texts=frozenset({"safe portrait lighting"}),
    )
    assert_korean_nationality_constraint(result)
    assert result["fragments"][-1]["selection_role"] == "supplement"
    assert result["fragments"][-1]["text"] == "safe portrait lighting"


def test_reference_image_uses_identity_lock_instead_of_nationality_appearance(tmp_path):
    root = tmp_path / "repositories"
    write_korean_repository_fixture(root)

    result = select_grok_prompt_fragments(
        "NSFW Korean female portrait",
        repositories_root=root,
        limit=4,
        reference_image=True,
        rng=CyclingRandom(),
    )

    constraints = result["constraints"]
    assert len(constraints) == 1
    assert constraints[0]["constraint_type"] == "reference_image_identity"
    assert "preserve the reference subject's exact face" in constraints[0]["text"]
    assert all(fragment.get("constraint_type") != "nationality" for fragment in result["fragments"])
    assert result["cleaned_prompt"] == "Korean female portrait"


def test_grok_text_model_receives_korean_constraint_that_overrides_random_fragments(monkeypatch, tmp_path):
    skill_dir = tmp_path / "grok-image-prompt-optimization"
    (skill_dir / "repositories").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# grok-image-prompt-optimization\n", encoding="utf-8")
    write_korean_repository_fixture(skill_dir / "repositories")
    captured = {}

    monkeypatch.setenv("GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_DIR", str(skill_dir))
    monkeypatch.setattr("common.prompt_optimization_repository.random.SystemRandom", lambda: FixedRandom(0.1))

    def fake_call(system_prompt, user_prompt):
        captured["user_prompt"] = user_prompt
        return "rewritten Korean portrait"

    monkeypatch.setattr(grok_image_prompt_rewriter, "_call_grok_text_model", fake_call)

    result = grok_image_prompt_rewriter.rewrite_grok_image_prompt(
        "random Korean female portrait",
        model="grok-imagine-image",
    )

    assert result["enhanced"] is True
    assert_korean_nationality_constraint({"fragments": result["supplements"]})
    user_prompt = captured["user_prompt"]
    assert "Stable user constraints" in user_prompt
    assert "random fragments must not override" in user_prompt
    assert "mandatory nationality/ethnicity constraint: korean" in user_prompt
    assert "Korean/East Asian facial features" in user_prompt
    assert "soft Seoul evening background" in user_prompt
    assert "Nordic blonde runway styling" not in user_prompt


def test_grok_reference_rewrite_locks_reference_identity_and_final_prompt(monkeypatch, tmp_path):
    skill_dir = tmp_path / "grok-image-prompt-optimization"
    (skill_dir / "repositories").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# grok-image-prompt-optimization\n", encoding="utf-8")
    write_korean_repository_fixture(skill_dir / "repositories")
    captured = {}

    monkeypatch.setenv("GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_DIR", str(skill_dir))
    monkeypatch.setattr("common.prompt_optimization_repository.random.SystemRandom", lambda: FixedRandom(0.1))

    def fake_call(system_prompt, user_prompt):
        captured["user_prompt"] = user_prompt
        return "rewrite the outfit and background"

    monkeypatch.setattr(grok_image_prompt_rewriter, "_call_grok_text_model", fake_call)

    result = grok_image_prompt_rewriter.rewrite_grok_image_prompt(
        "NSFW Korean female portrait",
        model="grok-imagine-image",
        image_url="C:\\tmp\\ref.png",
    )

    assert "Reference image identity lock:" in result["enhanced_prompt"]
    assert "NSFW" not in result["source_prompt"]
    user_prompt = captured["user_prompt"]
    assert "reference_image: provided" in user_prompt
    assert "mandatory reference image identity constraint" in user_prompt
    assert "mandatory nationality/ethnicity constraint" not in user_prompt


def test_nsfw_control_keyword_is_case_insensitive_and_stripped_from_source_prompt(tmp_path):
    root = tmp_path / "repositories"
    write_nsfw_repository_fixture(root)

    lower_result = select_grok_prompt_fragments(
        "nsfw Korean woman portrait",
        repositories_root=root,
        limit=4,
        rng=CyclingRandom(),
    )
    upper_result = select_grok_prompt_fragments(
        "NSFW Korean woman portrait",
        repositories_root=root,
        limit=4,
        rng=CyclingRandom(),
    )

    assert lower_result["category"] == "NSFW"
    assert upper_result["category"] == "NSFW"
    assert lower_result["cleaned_prompt"] == "Korean woman portrait"
    assert upper_result["cleaned_prompt"] == "Korean woman portrait"


def test_grok_text_model_receives_nsfw_priority_and_supplement_metadata(monkeypatch, tmp_path):
    skill_dir = write_nsfw_repository_skill(tmp_path)
    captured = {}

    monkeypatch.setenv("GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_DIR", str(skill_dir))
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


def test_grok_rewrite_strips_nsfw_control_token_from_final_prompt(monkeypatch, tmp_path):
    skill_dir = write_nsfw_repository_skill(tmp_path)

    monkeypatch.setenv("GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_DIR", str(skill_dir))
    monkeypatch.setattr("common.prompt_optimization_repository.random.SystemRandom", lambda: CyclingRandom())
    monkeypatch.setattr(
        grok_image_prompt_rewriter,
        "_call_grok_text_model",
        lambda system_prompt, user_prompt: "Final prompt: NSFW cinematic portrait",
    )

    result = grok_image_prompt_rewriter.rewrite_grok_image_prompt("nsfw portrait", model="grok-imagine-image")

    assert result["enhanced"] is True
    assert result["enhanced_prompt"] == "cinematic portrait"
    assert result["source_prompt"] == "portrait"


def test_random_prompt_request_defaults_to_image_to_image_contract(monkeypatch, tmp_path):
    skill_dir = write_nsfw_repository_skill(tmp_path)
    captured = {}

    monkeypatch.setenv("GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_DIR", str(skill_dir))
    monkeypatch.setattr("common.prompt_optimization_repository.random.SystemRandom", lambda: CyclingRandom())

    def fake_call(system_prompt, user_prompt):
        if "Translate Grok image generation prompts" in system_prompt:
            return "中文提示词"
        captured["user_prompt"] = user_prompt
        return (
            "Final prompt: NSFW subject, soft cinematic lighting, highly detailed, realistic skin texture, "
            "sensual atmosphere, 8k, black hair, blue eyes, biting her lower lip"
        )

    monkeypatch.setattr(grok_image_prompt_rewriter, "_call_grok_text_model", fake_call)

    result = grok_image_prompt_rewriter.build_grok_random_image_prompt("随机给我个NSFW图生图提示词")
    response = grok_image_prompt_rewriter.format_grok_random_image_prompt_response(result)

    assert result["prompt_mode"] == grok_image_prompt_rewriter.RANDOM_PROMPT_MODE_IMAGE_TO_IMAGE
    assert result["library"]["keyword"] == "grok"
    assert result["library"]["category"] == "NSFW"
    assert result["source_prompt"] == "image-to-image visual concept"
    assert result["enhanced_prompt"] == "subject"
    assert result["chinese_prompt"] == "中文提示词"
    assert "prompt_mode: image-to-image prompt text" in captured["user_prompt"]
    assert "Hair is appearance" in captured["user_prompt"]
    assert "Preserve the reference subject's original facial expression" in captured["user_prompt"]
    assert "nsfw-specific pose controls" in captured["user_prompt"]
    assert response.startswith("随机图生图提示词：")
    assert "English Prompt:" in response
    assert "中文翻译：" in response


def test_random_prompt_request_honors_explicit_text_to_image_mode(monkeypatch, tmp_path):
    skill_dir = write_nsfw_repository_skill(tmp_path)

    monkeypatch.setenv("GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_DIR", str(skill_dir))
    monkeypatch.setattr("common.prompt_optimization_repository.random.SystemRandom", lambda: CyclingRandom())
    monkeypatch.setattr(
        grok_image_prompt_rewriter,
        "_call_grok_text_model",
        lambda system_prompt, user_prompt: "中文提示词"
        if "Translate Grok image generation prompts" in system_prompt
        else "Final prompt: black hair, cinematic subject prompt.",
    )

    result = grok_image_prompt_rewriter.build_grok_random_image_prompt("随机给我个NSFW文生图提示词")

    assert result["prompt_mode"] == grok_image_prompt_rewriter.RANDOM_PROMPT_MODE_TEXT_TO_IMAGE
    assert result["source_prompt"] == "image concept"
    assert result["enhanced_prompt"] == "black hair, cinematic subject prompt."


def test_grok_image_rewrite_sanitizes_quality_boosters_and_reference_expression(monkeypatch, tmp_path):
    skill_dir = write_nsfw_repository_skill(tmp_path)

    monkeypatch.setenv("GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_DIR", str(skill_dir))
    monkeypatch.setattr(
        grok_image_prompt_rewriter,
        "_call_grok_text_model",
        lambda system_prompt, user_prompt: (
            "Final prompt: portrait, soft cinematic lighting, highly detailed, realistic skin texture, "
            "sensual atmosphere, 8k, smiling"
        ),
    )

    result = grok_image_prompt_rewriter.rewrite_grok_image_prompt(
        "make it noir",
        model="grok-imagine-image",
        image_url="C:\\tmp\\ref.png",
    )

    assert result["enhanced_prompt"] == (
        "portrait\n\nReference image identity lock: preserve the reference subject's exact face, facial structure, "
        "original expression, gaze direction, skin texture/tone, hair, distinctive features, and general body "
        "proportions; only change the requested style, clothing, objects, pose, or environment; do not invent "
        "a new person or add new ethnicity, eye color, hair color, age, body type, expression, or facial traits."
    )


def test_strip_repository_keywords_ignores_partial_words():
    assert strip_repository_keywords("use grok style", ["grok"]) == "use style"
    assert strip_repository_keywords("use mygrok style", ["grok"]) == "use mygrok style"
    assert strip_control_keywords("use nsfw style") == "use style"
    assert strip_control_keywords("use mynsfw style") == "use mynsfw style"
