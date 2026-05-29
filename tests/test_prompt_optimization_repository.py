from common.prompt_optimization_repository import select_grok_prompt_fragments, strip_repository_keywords


class FixedRandom:
    def __init__(self, roll):
        self.roll = roll

    def random(self):
        return self.roll

    def choice(self, values):
        return values[0]


def test_groksfw_keyword_strips_and_prefers_matching_repository(tmp_path):
    root = tmp_path / "repositories"
    (root / "grokSfw").mkdir(parents=True)
    (root / "general").mkdir()
    (root / "grokSfw" / "lighting.txt").write_text("soft portrait light\n", encoding="utf-8")
    (root / "general" / "fallback.txt").write_text("wide cinematic frame\n", encoding="utf-8")

    result = select_grok_prompt_fragments(
        "grokSfw 生成一张人物写真",
        repositories_root=root,
        limit=1,
        rng=FixedRandom(0.1),
    )

    assert result["keyword"] == "grokSfw"
    assert result["cleaned_prompt"] == "生成一张人物写真"
    assert result["fragments"][0]["repository"] == "grokSfw"
    assert result["fragments"][0]["text"] == "soft portrait light"


def test_groksfw_keyword_uses_other_repository_for_ten_percent_branch(tmp_path):
    root = tmp_path / "repositories"
    (root / "grokSfw").mkdir(parents=True)
    (root / "general").mkdir()
    (root / "grokSfw" / "lighting.txt").write_text("soft portrait light\n", encoding="utf-8")
    (root / "general" / "fallback.txt").write_text("wide cinematic frame\n", encoding="utf-8")

    result = select_grok_prompt_fragments(
        "grokSfw 生成一张人物写真",
        repositories_root=root,
        limit=1,
        rng=FixedRandom(0.95),
    )

    assert result["fragments"][0]["repository"] == "general"
    assert result["fragments"][0]["text"] == "wide cinematic frame"


def test_strip_repository_keywords_ignores_partial_words():
    assert strip_repository_keywords("use grokSfw style", ["grokSfw"]) == "use style"
    assert strip_repository_keywords("use mygrokSfw style", ["grokSfw"]) == "use mygrokSfw style"
