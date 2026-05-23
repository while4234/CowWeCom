import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "clash_verge_rule_guard.py"


def load_rule_guard():
    spec = importlib.util.spec_from_file_location("clash_verge_rule_guard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rule_guard = load_rule_guard()


class ClashVergeRuleGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = self.root / "profiles" / "active.yaml"
        self.profile.parent.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_profile(self, rules):
        body = "\n".join(["mixed-port: 7890", "rules:"] + [f"    - {rule}" for rule in rules])
        self.profile.write_text(body + "\n", encoding="utf-8")

    def test_inserts_required_rules_at_top_of_rules_section(self):
        self.write_profile(["'DOMAIN,sub.example,DIRECT'", "'MATCH,Proxy'"])

        result = rule_guard.ensure_direct_rules(self.profile)

        self.assertTrue(result.changed)
        text = self.profile.read_text(encoding="utf-8")
        self.assertIn("    - 'DOMAIN-SUFFIX,xiaohongshu.com,DIRECT'\n", text)
        self.assertLess(
            text.index("DOMAIN-SUFFIX,xiaohongshu.com,DIRECT"),
            text.index("DOMAIN,sub.example,DIRECT"),
        )

    def test_existing_rules_are_not_duplicated(self):
        self.write_profile(["'DOMAIN-SUFFIX,xiaohongshu.com,DIRECT'", "DOMAIN-SUFFIX,douyin.com,DIRECT"])

        first = rule_guard.ensure_direct_rules(self.profile)
        second = rule_guard.ensure_direct_rules(self.profile)

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        text = self.profile.read_text(encoding="utf-8")
        self.assertEqual(text.count("DOMAIN-SUFFIX,xiaohongshu.com,DIRECT"), 1)
        self.assertEqual(text.count("DOMAIN-SUFFIX,douyin.com,DIRECT"), 1)

    def test_dry_run_does_not_write_profile(self):
        self.write_profile(["'MATCH,Proxy'"])
        before = self.profile.read_text(encoding="utf-8")

        result = rule_guard.ensure_direct_rules(self.profile, dry_run=True)

        self.assertTrue(result.changed)
        self.assertEqual(self.profile.read_text(encoding="utf-8"), before)

    def test_utf8_profile_is_not_rewritten_with_bom(self):
        self.write_profile(["'MATCH,Proxy'"])

        rule_guard.ensure_direct_rules(self.profile)

        self.assertFalse(self.profile.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_resolves_current_clash_verge_profile(self):
        (self.root / "profiles.yaml").write_text("current: active\n", encoding="utf-8")
        self.write_profile(["'MATCH,Proxy'"])

        resolved = rule_guard.resolve_current_profile(self.root)

        self.assertEqual(resolved, self.profile)

    def test_cli_json_output_avoids_proxy_contents(self):
        self.write_profile(["'MATCH,Proxy'"])

        with tempfile.TemporaryFile("w+", encoding="utf-8") as stdout:
            old_stdout = rule_guard.sys.stdout
            try:
                rule_guard.sys.stdout = stdout
                code = rule_guard.main(["--profile", str(self.profile), "--dry-run", "--json"])
            finally:
                rule_guard.sys.stdout = old_stdout
            stdout.seek(0)
            payload = json.loads(stdout.read())

        self.assertEqual(code, 0)
        self.assertEqual(payload["path"], str(self.profile))
        self.assertTrue(payload["changed"])
        self.assertNotIn("proxies", payload)


if __name__ == "__main__":
    unittest.main()
