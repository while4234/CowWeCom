import json
import random
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from config import conf
from common.grok_real_mode_prompt_assets import (
    ASSET_FIELDS,
    GrokRealModePromptError,
    compose_real_mode_prompt,
    load_catalog,
    material_choices,
    sync_workbook_to_cache,
)


@contextmanager
def patch_config(values):
    old_values = {key: conf().get(key) for key in values}
    conf().update(values)
    try:
        yield
    finally:
        for key, old_value in old_values.items():
            if old_value is None:
                conf().pop(key, None)
            else:
                conf()[key] = old_value


def _single_item_catalog():
    return {field: [{"key": "one", "prompt": f"{field} prompt"}] for field in ASSET_FIELDS}


class GrokRealModePromptAssetsTest(unittest.TestCase):
    def test_sync_creates_default_workbook_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "assets.xlsx"
            cache = root / "assets.json"

            first = sync_workbook_to_cache(workbook_path=workbook, cache_path=cache)
            second = sync_workbook_to_cache(workbook_path=workbook, cache_path=cache)

            self.assertEqual(first.status, "updated")
            self.assertEqual(second.status, "unchanged")
            self.assertTrue(workbook.exists())
            self.assertTrue(cache.exists())
            payload = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(set(payload["categories"]), set(ASSET_FIELDS))

    def test_load_catalog_uses_default_when_cache_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = load_catalog(cache_path=Path(tmp) / "missing.json")

        self.assertIn("camera_angle", catalog)
        self.assertTrue(catalog["camera_angle"])

    def test_compose_text_to_image_prompt_uses_custom_materials(self):
        prompt = compose_real_mode_prompt(
            media_type="image",
            image_count=0,
            selections={
                "camera_angle": "custom:under a chair",
                "scene": "custom:attic room",
                "time": "custom:midnight",
                "light_source": "custom:phone",
                "color_tone": "custom:violet",
                "nationality": "custom:French",
                "action": "custom:looking back",
                "clothing": "custom:a wool coat",
                "lower_state": "custom:with black trousers",
                "tattoo": "custom:a tiny star tattoo",
                "expression": "custom:soft expression",
            },
            catalog=_single_item_catalog(),
        )

        self.assertIn("One imaginary 20-year-old French woman", prompt)
        self.assertLess(prompt.index("a tiny star tattoo"), prompt.index("soft expression"))
        self.assertIn("background with attic room", prompt)

    def test_tattoo_is_omitted_by_default(self):
        prompt = compose_real_mode_prompt(
            media_type="image",
            image_count=0,
            selections={field: "one" for field in ASSET_FIELDS if field != "tattoo"},
            catalog=_single_item_catalog(),
        )

        self.assertNotIn("tattoo prompt", prompt)
        self.assertIn("expression prompt", prompt)

    def test_tattoo_random_option_uses_catalog_material(self):
        prompt = compose_real_mode_prompt(
            media_type="image",
            image_count=0,
            selections={**{field: "one" for field in ASSET_FIELDS if field != "tattoo"}, "tattoo": "random"},
            catalog=_single_item_catalog(),
            rng=random.Random(0),
        )

        self.assertIn("tattoo prompt", prompt)
        self.assertLess(prompt.index("tattoo prompt"), prompt.index("expression prompt"))

    def test_compose_image_to_image_prompt_inserts_extra_reference_roles(self):
        prompt = compose_real_mode_prompt(
            media_type="image",
            image_count=3,
            selections={field: "one" for field in ASSET_FIELDS},
            extra_prompts={2: "the city background", 3: "the color palette"},
            catalog=_single_item_catalog(),
        )

        self.assertNotIn("One imaginary 20-year-old", prompt)
        self.assertIn("Reference image 2 is the city background", prompt)
        self.assertIn("Reference image 3 is the color palette", prompt)
        self.assertLess(prompt.index("Reference image 2"), prompt.index("background with scene prompt"))

    def test_compose_video_prompt_uses_image_markers_for_extra_references(self):
        prompt = compose_real_mode_prompt(
            media_type="video",
            image_count=2,
            selections={field: "one" for field in ASSET_FIELDS},
            extra_prompts={2: "the motion background"},
            catalog=_single_item_catalog(),
        )

        self.assertIn("<IMAGE_2> is the motion background", prompt)

    def test_compose_requires_extra_prompt_for_each_extra_reference(self):
        with self.assertRaises(GrokRealModePromptError):
            compose_real_mode_prompt(
                media_type="image",
                image_count=2,
                selections={field: "one" for field in ASSET_FIELDS},
                extra_prompts={},
                catalog=_single_item_catalog(),
            )

    def test_compose_rejects_unknown_material_key(self):
        with self.assertRaises(GrokRealModePromptError):
            compose_real_mode_prompt(
                media_type="image",
                image_count=0,
                selections={"camera_angle": "missing"},
                catalog=_single_item_catalog(),
            )

    def test_empty_selection_randomly_uses_catalog_material(self):
        prompt = compose_real_mode_prompt(
            media_type="image",
            image_count=0,
            selections={},
            catalog=_single_item_catalog(),
            rng=random.Random(0),
        )

        self.assertIn("camera_angle prompt", prompt)
        self.assertIn("scene prompt", prompt)
        self.assertNotIn("tattoo prompt", prompt)

    def test_tattoo_choices_include_random_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "assets.json"
            payload = {
                "categories": {
                    field: [{"key": "one", "prompt": f"{field} prompt"}]
                    for field in ASSET_FIELDS
                }
            }
            cache.write_text(json.dumps(payload), encoding="utf-8")

            with patch_config({"grok_real_mode_assets_cache": str(cache)}):
                choices = material_choices("tattoo", "")
                random_prefix_choices = material_choices("tattoo", "ran")
                unrelated_choices = material_choices("tattoo", "dom")

        self.assertEqual(choices[:2], ["random", "one"])
        self.assertEqual(random_prefix_choices, ["random"])
        self.assertNotIn("random", unrelated_choices)


if __name__ == "__main__":
    unittest.main()
