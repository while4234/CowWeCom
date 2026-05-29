from common.prompt_optimization_repository import select_grok_prompt_fragments, strip_repository_keywords


class FixedRandom:
    def __init__(self, roll, *, choice_index=0):
        self.roll = roll
        self.choice_index = choice_index

    def random(self):
        return self.roll

    def choice(self, values):
        return values[self.choice_index]


def write_repository_fixture(root):
    (root / "grok").mkdir(parents=True)
    (root / "general").mkdir()
    (root / "grok" / "lighting.txt").write_text("soft portrait light\n", encoding="utf-8")
    (root / "general" / "fallback.txt").write_text("wide cinematic frame\n", encoding="utf-8")


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


def test_nsfw_prompt_forces_grok_nsfw_repository_path(tmp_path):
    root = tmp_path / "repositories"
    (root / "grok" / "NSFW").mkdir(parents=True)
    (root / "grok" / "SFW").mkdir()
    (root / "general").mkdir()
    (root / "grok" / "NSFW" / "pose.txt").write_text("nsfw-specific pose controls\n", encoding="utf-8")
    (root / "grok" / "SFW" / "lighting.txt").write_text("safe portrait lighting\n", encoding="utf-8")
    (root / "general" / "fallback.txt").write_text("general fallback\n", encoding="utf-8")

    result = select_grok_prompt_fragments(
        "generate an nsFw portrait",
        repositories_root=root,
        limit=1,
        rng=FixedRandom(0.95, choice_index=-1),
    )

    fragment = result["fragments"][0]
    assert result["keyword"] == "grok"
    assert result["category"] == "NSFW"
    assert result["category_forced"] is True
    assert result["preferred_probability"] == 1.0
    assert fragment["repository"] == "grok"
    assert fragment["category"] == "NSFW"
    assert fragment["file"].startswith("NSFW/")
    assert fragment["text"] == "nsfw-specific pose controls"


def test_strip_repository_keywords_ignores_partial_words():
    assert strip_repository_keywords("use grok style", ["grok"]) == "use style"
    assert strip_repository_keywords("use mygrok style", ["grok"]) == "use mygrok style"
