"""Independent exact evidence and bounded dependency-context expectations."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402

source_context = load("scripts/source_context.py")


class SourceContext(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
        return path

    def retrieved(self, result, symbol):
        return next(item for item in result["results"] if item["symbol"] == symbol and item["status"] == "ok")

    def test_imported_function_globals_and_exact_related_test_in_one_call(self):
        self.write("service.py", "from units import millis\r\nRATE = 3\r\ndef timeout(value):\r\n    return millis(value) * RATE\r\n")
        self.write("units.py", "def millis(value):\r\n    return int(float(value) * 1000)\r\n")
        expected = "def test_timeout():\n    assert timeout(2) == 6000\n"
        self.write("tests/test_service.py", "from service import timeout\n" + expected)
        result = source_context.related("service.py", "timeout", self.root, references=["tests/test_service.py"])
        self.assertTrue(result["complete"], result["issues"])
        self.assertFalse(result["semantic_closure"])
        self.assertEqual(self.retrieved(result, "timeout")["source"], "def timeout(value):\r\n    return millis(value) * RATE\r\n")
        self.assertEqual(self.retrieved(result, "millis")["source"], "def millis(value):\r\n    return int(float(value) * 1000)\r\n")
        self.assertEqual(self.retrieved(result, "RATE")["source"], "RATE = 3\r\n")
        self.assertTrue(any(item.get("source") == expected for item in result["results"]))

    def test_decorator_defaults_and_annotations_remain_outside_parameter_shadowing(self):
        self.write("sample.py", "def deco(fn):\n    return fn\nDEFAULT = 4\n@deco\ndef target(deco: int = DEFAULT):\n    return deco\n")
        result = source_context.related("sample.py", "target", self.root)
        self.assertEqual(self.retrieved(result, "target")["source"], "@deco\ndef target(deco: int = DEFAULT):\n    return deco\n")
        self.assertEqual(self.retrieved(result, "deco")["source"], "def deco(fn):\n    return fn\n")
        self.assertEqual(self.retrieved(result, "DEFAULT")["source"], "DEFAULT = 4\n")

    def test_relative_module_alias_import_and_imported_constant(self):
        self.write("package/__init__.py", "")
        self.write("package/value.py", "RATE = 5\ndef millis(value):\n    return value * RATE\n")
        self.write("package/service.py", "from .value import millis as convert\nfrom .value import RATE\ndef target(value):\n    return convert(value) + RATE\n")
        result = source_context.related("package/service.py", "target", self.root, hops=2)
        self.assertEqual(self.retrieved(result, "millis")["source"], "def millis(value):\n    return value * RATE\n")
        self.assertEqual(self.retrieved(result, "RATE")["source"], "RATE = 5\n")

    def test_module_attribute_and_same_file_dependency(self):
        self.write("dep.py", "def convert(value):\n    return value + 1\n")
        self.write("service.py", "import dep as conversions\ndef local(value):\n    return value * 2\ndef target(value):\n    return conversions.convert(local(value))\n")
        result = source_context.related("service.py", "target", self.root)
        self.assertEqual(self.retrieved(result, "convert")["source"], "def convert(value):\n    return value + 1\n")
        self.assertEqual(self.retrieved(result, "local")["source"], "def local(value):\n    return value * 2\n")

    def test_nested_arguments_and_local_imports_do_not_hide_parent_global_dependencies(self):
        self.write("service.py", "RATE = 7\ndef target(value):\n    from math import floor\n    def local(RATE):\n        return floor(RATE)\n    return local(value) * RATE\n")
        result = source_context.related("service.py", "target", self.root)
        self.assertTrue(result["complete"], result["issues"])
        self.assertEqual(self.retrieved(result, "RATE")["source"], "RATE = 7\n")

    def test_comprehension_lambda_and_nested_global_declarations_keep_scope(self):
        self.write("service.py", "RATE = 9\nVALUES = [1, 2]\ndef target():\n    RATE = 2\n    def local():\n        global RATE\n        return RATE\n    return [RATE for RATE in VALUES], (lambda RATE: RATE)(3), local()\n")
        result = source_context.related("service.py", "target", self.root)
        self.assertTrue(result["complete"], result["issues"])
        self.assertEqual(self.retrieved(result, "RATE")["source"], "RATE = 9\n")
        self.assertEqual(self.retrieved(result, "VALUES")["source"], "VALUES = [1, 2]\n")

    def test_method_uses_module_globals_instead_of_class_attributes(self):
        self.write("service.py", "RATE = 11\nclass Handler:\n    RATE = 2\n    def result(self):\n        return RATE\n")
        result = source_context.related("service.py", "Handler", self.root)
        self.assertTrue(result["complete"], result["issues"])
        self.assertEqual(self.retrieved(result, "RATE")["source"], "RATE = 11\n")

    def test_rebound_imports_are_reported_instead_of_guessing_a_definition(self):
        self.write("one.py", "def convert(value): return value + 1\n")
        self.write("two.py", "def convert(value): return value + 2\n")
        for imports in ("from one import convert\nfrom two import convert\n", "from one import convert\nconvert = None\n"):
            with self.subTest(imports=imports):
                self.write("service.py", imports + "def target(value): return convert(value)\n")
                result = source_context.related("service.py", "target", self.root)
                self.assertFalse(result["complete"])
                self.assertEqual(result["issues"][0]["name"], "convert")
                self.assertIn("ambiguous", result["issues"][0]["reason"])
                self.assertFalse(any(item["path"].endswith(("one.py", "two.py")) for item in result["results"]))

    def test_missing_and_dynamic_dependencies_are_explicit_not_semantic_claims(self):
        self.write("service.py", "from nonexistent import convert\ndef target(value):\n    return convert(value) + dynamic(value)\n")
        result = source_context.related("service.py", "target", self.root)
        self.assertFalse(result["complete"])
        self.assertEqual({item["name"] for item in result["issues"]}, {"convert", "dynamic"})
        self.assertIn("def target", self.retrieved(result, "target")["source"])

    def test_versions_workspace_escape_and_ambiguous_symbols_are_refused(self):
        self.write("service.py", "def target():\n    return 1\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            source_context.related("service.py", "target", self.root, expected_hash="0" * 64)
        with self.assertRaises((ValueError, OSError)):
            source_context.related("../outside.py", "target", self.root)
        self.write("service.py", "def target(): return 1\ndef target(): return 2\n")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            source_context.related("service.py", "target", self.root)

    def test_small_budget_withholds_whole_body_and_reports_fanout(self):
        self.write("service.py", "VALUE = 1\ndef target():\n" + "    # exact context\n" * 150 + "    return VALUE\n")
        result = source_context.related("service.py", "target", self.root, fanout=1, budget=1024)
        self.assertFalse(result["complete"])
        self.assertEqual(result["results"][0]["status"], "output_budget_exceeded")
        self.assertNotIn("source", result["results"][0])
        self.assertGreater(result["omitted_candidates"], 0)
        self.assertLessEqual(len(source_context.serialized(result).encode()), 1024)

    def test_one_snapshot_per_file_and_no_execution(self):
        self.write("dep.py", "from pathlib import Path\nPath('executed').touch()\ndef convert(value): return value\nRATE = 3\n")
        self.write("service.py", "from dep import convert, RATE\ndef target(value): return convert(value) + RATE\n")
        reads, original = [], Path.open
        def track(path, *args, **kwargs):
            if args and args[0] == "rb":
                reads.append(str(path))
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", track):
            result = source_context.related("service.py", "target", self.root, references=["dep.py"])
        self.assertEqual(len(reads), 2)
        self.assertFalse((self.root / "executed").exists())
        self.assertEqual(self.retrieved(result, "RATE")["sha256"], hashlib.sha256((self.root / "dep.py").read_bytes()).hexdigest())

    def test_failed_reads_obey_input_file_limits_and_are_not_repeated(self):
        primary = "from dep import RATE\ndef target(): return RATE\n"
        self.write("service.py", primary)
        self.write("dep.py", "RATE = 3\n" + "# large\n" * 100)
        self.write("other.py", "def test_target(): return target()\n")
        reads, original = [], Path.open
        def track(path, *args, **kwargs):
            if args and args[0] == "rb":
                reads.append(path.name)
            return original(path, *args, **kwargs)
        with patch.object(source_context, "MAX_INPUT_BYTES", len(primary.encode()) + 10), \
             patch.object(source_context, "MAX_FILES", 2), patch.object(Path, "open", track):
            result = source_context.related("service.py", "target", self.root,
                                            references=["dep.py", "dep.py", "other.py"])
        self.assertFalse(result["complete"])
        self.assertEqual(reads, ["service.py", "dep.py"], "failed files count toward the file limit and are cached")
        self.assertEqual(result["input_bytes"], len(primary.encode()))
        self.assertTrue(any("file limit" in item["reason"] for item in result["issues"]))

    def test_cli_uses_explicit_workspace_and_utf8_json(self):
        self.write("service.py", "def target():\n    return '雪'\n")
        command = [sys.executable, "-I", "-B", str(ROOT / "scripts/source.py"), "service.py", "--related", "target", "--workspace", str(self.root)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("雪".encode(), result.stdout)
        self.assertNotIn(b"\\u96ea", result.stdout)


if __name__ == "__main__":
    unittest.main()
