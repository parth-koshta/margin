"""Which files the leak guard refuses outright, with made-up contents."""
import tests  # noqa: F401  (temporary data folder first)

import json
import unittest

from margin import privacy

HOOKS = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "guard.py"}]}]}}


def refused(name: str, content: str) -> bool:
    return any("must never be committed" in p for p in privacy.scan([(name, content.encode())], None))


class ClaudeSettings(unittest.TestCase):
    def test_claude_codes_settings_are_code(self):
        self.assertFalse(refused(".claude/settings.json", json.dumps(HOOKS)))
        self.assertFalse(refused(".claude/settings.json", json.dumps({**HOOKS, "permissions": {"deny": []}})))

    def test_margins_settings_at_that_path_are_refused(self):
        self.assertTrue(refused(".claude/settings.json", json.dumps({"currency": "USD", "self_names": ["DEMO NAME"]})))
        self.assertTrue(refused(".claude/settings.json", json.dumps({**HOOKS, "self_names": ["DEMO NAME"]})))

    def test_anything_else_at_that_path_is_refused(self):
        for content in ("not json", "[]", "{}", '"hooks"'):
            with self.subTest(content=content):
                self.assertTrue(refused(".claude/settings.json", content))

    def test_every_other_settings_json_is_refused(self):
        for name in ("settings.json", "data/settings.json", "x/.claude/settings.json"):
            with self.subTest(name=name):
                self.assertTrue(refused(name, json.dumps(HOOKS)))


if __name__ == "__main__":
    unittest.main()
