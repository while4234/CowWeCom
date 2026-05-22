import unittest
from unittest.mock import patch

from agent.tools.bash.bash import Bash


class TestBashTool(unittest.TestCase):
    def test_normalize_subprocess_env_skips_none_and_stringifies_values(self):
        env = Bash._normalize_subprocess_env({
            "TEXT": "value",
            "NUMBER": 3,
            "NONE": None,
            None: "ignored",
        })

        self.assertEqual(env["TEXT"], "value")
        self.assertEqual(env["NUMBER"], "3")
        self.assertNotIn("NONE", env)
        self.assertNotIn(None, env)

    def test_prepend_current_python_dir_adds_interpreter_directory(self):
        env = {"PATH": r"C:\Windows\System32"}

        with patch("agent.tools.bash.bash.sys.executable", r"D:\CowAgent\.venv312\Scripts\python.exe"):
            Bash._prepend_current_python_dir(env)

        self.assertTrue(env["PATH"].startswith(r"D:\CowAgent\.venv312\Scripts"))


if __name__ == "__main__":
    unittest.main()
