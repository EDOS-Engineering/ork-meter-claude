"""Cache screening must preserve actual evidence when normalizing host locators."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  scrubs ORK_METER_* when this file runs on its own
import native_cache_qualification as screen


class CacheScreen(unittest.TestCase):
    def test_text_container_equivalence_keeps_role_text_and_metadata(self):
        plain = [{"role": "user", "content": "same"}]
        blocks = [{"role": "user", "content": [{"type": "text", "text": "same"}]}]
        self.assertEqual(screen.history(plain), screen.history(blocks))
        self.assertNotEqual(screen.history(plain, False), screen.history(blocks, False))
        blocks[0]["content"][0]["text"] = "different"
        self.assertNotEqual(screen.history(plain), screen.history(blocks))
        blocks[0]["content"][0]["text"] = "same"
        blocks[0]["content"][0]["citations"] = [{"source": "keep this metadata"}]
        self.assertNotEqual(screen.history(plain), screen.history(blocks))

    def test_only_generated_persistence_locator_is_normalized(self):
        base = Path("/fixture")
        first = "Output too large (24KB). Full output saved to: /fixture/config/projects/work-a/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/tool-results/native_2.txt\nERROR: a"
        second = first.replace("work-a", "work-b").replace("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        self.assertEqual(screen.normalize_paths(first, base), screen.normalize_paths(second, base))
        self.assertNotEqual(screen.normalize_paths(first, base), screen.normalize_paths(second.replace("ERROR: a", "ERROR: b"), base))
        self.assertNotEqual(screen.normalize_paths(first, base), screen.normalize_paths(second.replace("native_2.txt", "native_3.txt"), base))

    def test_history_keeps_cache_control_keys_inside_tool_evidence(self):
        messages = [{"role": "user", "content": [{"type": "tool_result", "cache_control": {"type": "ephemeral"},
                      "content": {"cache_control": "application data", "value": 3}}]}]
        normalized = screen.history(messages)
        self.assertNotIn("cache_control", normalized[0]["content"][0])
        self.assertEqual(normalized[0]["content"][0]["content"], {"cache_control": "application data", "value": 3})
        self.assertIn("cache_control", messages[0]["content"][0])

    def test_read_history_check_does_not_strip_application_cache_control_data(self):
        from native_qualification import content_only
        original = [{"role": "user", "content": [{"type": "tool_result", "content": {
            "cache_control": "source setting", "value": 3}}]}]
        changed = [{"role": "user", "content": [{"type": "tool_result", "content": {
            "cache_control": "changed setting", "value": 3}}]}]
        self.assertNotEqual(content_only(original), content_only(changed))


if __name__ == "__main__":
    unittest.main()
