import http.server
import importlib.util
import json
import sys
import tempfile
import threading
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

    def write_active_root(self, active_rules, runtime_rules, runtime_header=None):
        (self.root / "profiles.yaml").write_text("current: active\n", encoding="utf-8")
        self.write_profile(active_rules)
        header = runtime_header or ["external-controller: 127.0.0.1:1"]
        runtime = self.root / "clash-verge.yaml"
        body = "\n".join(header + ["rules:"] + [f"    - {rule}" for rule in runtime_rules])
        runtime.write_text(body + "\n", encoding="utf-8")
        return runtime

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

    def test_normalizes_required_rules_to_existing_top_level_indent(self):
        body = "\n".join(
            [
                "mixed-port: 7890",
                "rules:",
                "  - 'DOMAIN-SUFFIX,xiaohongshu.com,DIRECT'",
                "- DOMAIN,sub.example,DIRECT",
                "- MATCH,Proxy",
                "",
            ]
        )
        self.profile.write_text(body, encoding="utf-8")

        result = rule_guard.ensure_direct_rules(self.profile)

        self.assertTrue(result.changed)
        text = self.profile.read_text(encoding="utf-8")
        self.assertIn("\n- 'DOMAIN-SUFFIX,xiaohongshu.com,DIRECT'\n", text)
        self.assertNotIn("  - 'DOMAIN-SUFFIX,xiaohongshu.com,DIRECT'", text)

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
                code = rule_guard.main(["--root", str(self.root), "--profile", str(self.profile), "--dry-run", "--json"])
            finally:
                rule_guard.sys.stdout = old_stdout
            stdout.seek(0)
            payload = json.loads(stdout.read())

        self.assertEqual(code, 0)
        self.assertEqual(payload["patches"][0]["path"], str(self.profile))
        self.assertTrue(payload["patches"][0]["changed"])
        self.assertNotIn("proxies", payload)

    def test_run_guard_patches_active_profile_and_runtime_config(self):
        runtime = self.write_active_root(["'MATCH,Proxy'"], ["'MATCH,Proxy'"])

        result = rule_guard.run_guard(root=self.root, reload_core=False)

        self.assertEqual(len(result.patches), 2)
        self.assertTrue(all(patch.changed for patch in result.patches))
        self.assertIn("DOMAIN-SUFFIX,xiaohongshu.com,DIRECT", self.profile.read_text(encoding="utf-8"))
        self.assertIn("DOMAIN-SUFFIX,xiaohongshu.com,DIRECT", runtime.read_text(encoding="utf-8"))
        self.assertEqual(result.reload.message, "reload disabled")

    def test_explicit_profile_does_not_patch_runtime_by_default(self):
        runtime = self.write_active_root(["'MATCH,Proxy'"], ["'MATCH,Proxy'"])

        result = rule_guard.run_guard(root=self.root, profile_path=self.profile, reload_core=False)

        self.assertEqual(len(result.patches), 1)
        self.assertTrue(result.patches[0].changed)
        self.assertNotIn("DOMAIN-SUFFIX,xiaohongshu.com,DIRECT", runtime.read_text(encoding="utf-8"))

    def test_dry_run_skips_runtime_reload(self):
        with ReloadServer() as server:
            runtime = self.write_active_root(
                ["'MATCH,Proxy'"],
                ["'MATCH,Proxy'"],
                runtime_header=[f"external-controller: 127.0.0.1:{server.port}"],
            )
            before = runtime.read_text(encoding="utf-8")

            result = rule_guard.run_guard(root=self.root, dry_run=True)

        self.assertTrue(result.reload.ok)
        self.assertEqual(result.reload.message, "dry-run; reload skipped")
        self.assertEqual(runtime.read_text(encoding="utf-8"), before)
        self.assertEqual(server.requests, [])

    def test_runtime_change_triggers_one_reload_request(self):
        with ReloadServer() as server:
            runtime = self.write_active_root(
                ["'DOMAIN-SUFFIX,xiaohongshu.com,DIRECT'", "'DOMAIN-SUFFIX,douyin.com,DIRECT'"],
                ["'MATCH,Proxy'"],
                runtime_header=[f"external-controller: 127.0.0.1:{server.port}", "secret: 'local-secret'"],
            )

            result = rule_guard.run_guard(root=self.root)

        self.assertTrue(result.reload.ok)
        self.assertEqual(result.reload.message, "core reloaded")
        self.assertEqual(len(server.requests), 1)
        request = server.requests[0]
        self.assertEqual(request["path"], "/configs?force=true")
        self.assertEqual(json.loads(request["body"]), {"path": str(runtime)})
        self.assertEqual(request["authorization"], "Bearer local-secret")

    def test_reload_always_reloads_when_rules_already_exist(self):
        with ReloadServer() as server:
            complete_rules = [f"'{rule}'" for rule in rule_guard.DEFAULT_RULES]
            self.write_active_root(
                complete_rules,
                complete_rules,
                runtime_header=[f"external-controller: 127.0.0.1:{server.port}"],
            )

            result = rule_guard.run_guard(root=self.root, reload_always=True)

        self.assertTrue(result.reload.ok)
        self.assertEqual(len(server.requests), 1)


class ReloadServer:
    def __enter__(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), ReloadRequestHandler)
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def requests(self):
        return self.server.requests


class ReloadRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        self.server.requests.append(
            {
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
            }
        )
        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    unittest.main()
