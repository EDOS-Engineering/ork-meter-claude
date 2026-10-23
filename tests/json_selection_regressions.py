"""Independent nested evidence, strict numeric fidelity and bounded CLI recovery."""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402
import artifacts
import json_selection as selection
import ledger
import session

reference = load("tests/fixtures/json_selection_reference.py")


def selected(text, spec, budget=24576):
    document = {"text": text, "tool": "Bash", "completeness": {"received_response": "complete", "before_hook": "unknown"}}
    raw = selection.serialized(selection.select(document, json.dumps(spec), budget))
    return json.loads(raw, parse_float=Decimal), raw


def outcome(module, document, request, budget):
    try:
        return module.serialized(module.select(document, request, budget))
    except ValueError as exc:
        return "ValueError: %s" % exc


def random_value(rng, depth=0):
    roll = rng.random()
    if depth < 3 and roll < 0.25:
        return {rng.choice(["a", "b", "雪", "", "k%d" % rng.randrange(9)]) + str(i): random_value(rng, depth + 1)
                for i in range(rng.randrange(0, 5))}
    if depth < 3 and roll < 0.45:
        return [random_value(rng, depth + 1) for _ in range(rng.randrange(0, 6))]
    return rng.choice([None, True, False, 0, -7, 1.5, "x" * rng.randrange(0, 900), "ü€😀" * rng.randrange(0, 60),
                       {"status": rng.choice(["ok", "failed"])}])


class SameSelections(unittest.TestCase):
    """#190: the running-size admission selects exactly what re-serializing did."""

    def test_varied_corpus_matches_the_reference(self):
        rng = random.Random(190)
        for n in range(150):
            rows = [{"id": i, "status": rng.choice(["ok", "failed", 1, None]), "body": random_value(rng)}
                    for i in range(rng.randrange(0, 60))]
            text = json.dumps({"rows": rows, "meta": random_value(rng), "n": 1e999 if n % 7 == 0 else n},
                              ensure_ascii=rng.random() < 0.5).replace("Infinity", "1e999")
            document = {"text": text, "tool": "Bash"}
            pointers = rng.sample(["", "/rows", "/meta", "/n", "/rows/0", "/rows/1/body", "/missing", "/rows/99",
                                   "/meta/a0", "/rows/2/status"], rng.randrange(0, 8))
            spec = {"pointers": pointers} if pointers else {}
            if not pointers or rng.random() < 0.6:
                spec["filter"] = {"pointer": "/rows", "where": "/status", "equals": rng.choice(["ok", "failed", 1, None]),
                                  "start": rng.randrange(1, 4), "limit": rng.randrange(1, 40)}
            request = json.dumps(spec)
            for budget in (1024, 2048, 6000, 24 * 1024, 200 * 1024):
                with self.subTest(case=n, budget=budget):
                    self.assertEqual(outcome(selection, document, request, budget),
                                     outcome(reference, document, request, budget))

    def test_large_filter_page_is_linear(self):
        rows = [{"id": i, "status": "failed", "body": "y" * 900} for i in range(1000)]
        document = {"text": json.dumps(rows), "tool": "Bash"}
        request = json.dumps({"filter": {"pointer": "", "where": "/status", "equals": "failed", "limit": 1000}})
        started = time.monotonic()
        result = selection.select(document, request, 1024 * 1024)
        self.assertLess(time.monotonic() - started, 2.0)   # about 9 s when quadratic
        self.assertEqual(result["filter"]["returned_matches"], 1000)


class JSONSelection(unittest.TestCase):
    def test_nested_pointer_escaping_empty_keys_and_unicode_are_exact(self):
        text = '{"a/b":{"m~n":{"~1": ["雪",null]}}, "":false}'
        result, raw = selected(text, {"pointers": ["/a~1b/m~0n/~01/0", "/", "/a~1b/m~0n/~01/1"]})
        self.assertTrue(result["complete"])
        self.assertEqual([item["value"] for item in result["projections"]], ["雪", False, None])
        self.assertIn("雪".encode(), raw)
        self.assertEqual(result["source_sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(result["completeness"]["before_hook"], "unknown")

    def test_numeric_lexemes_are_not_rounded_or_converted_to_strings(self):
        text = '{"decimal":0.10000000000000000000001,"huge":1e999,"negative_zero":-0,"integer":9007199254740993}'
        result, raw = selected(text, {"pointers": ["/decimal", "/huge", "/negative_zero", "/integer"]})
        self.assertEqual(result["projections"][0]["value"], Decimal("0.10000000000000000000001"))
        for literal in (b'0.10000000000000000000001', b'1e999', b'"value":-0', b'9007199254740993'):
            self.assertIn(literal, raw)

    def test_array_filter_paginates_matches_and_keeps_original_indices(self):
        value = {"tests": [{"id": i, "status": {"kind": "failed" if i % 2 else "passed"}} for i in range(9)]}
        result, _ = selected(json.dumps(value), {"filter": {"pointer": "/tests", "where": "/status/kind",
                                                             "equals": "failed", "start": 2, "limit": 2}})
        filtered = result["filter"]
        self.assertEqual([item["index"] for item in filtered["items"]], [3, 5])
        self.assertEqual([item["value"] for item in filtered["items"]], [value["tests"][3], value["tests"][5]])
        self.assertEqual((filtered["scanned_items"], filtered["total_matches"], filtered["omitted_matches"]), (9, 4, 2))
        self.assertTrue(filtered["page_complete"])
        self.assertFalse(result["full_document"])

    def test_numeric_equality_separates_bool_null_missing_and_precise_decimal(self):
        text = '[{"v":true},{"v":1},{"v":1.0},{"v":null},{},{"v":0.10000000000000000000001}]'
        for expected, indices in ((True, [0]), (1, [1, 2]), (None, [3]), ("1", [])):
            result, _ = selected(text, {"filter": {"pointer": "", "where": "/v", "equals": expected}})
            self.assertEqual([item["index"] for item in result["filter"]["items"]], indices)
            self.assertEqual(result["filter"]["missing_where"], 1)
        doc = {"text": text, "tool": "Bash"}
        request = '{"filter":{"pointer":"","where":"/v","equals":0.10000000000000000000001}}'
        result = selection.select(doc, request)
        self.assertEqual([item["index"] for item in result["filter"]["items"]], [5])

    def test_missing_and_noncanonical_array_indices_are_visible(self):
        result, _ = selected('{"items":[5],"01":"object key"}', {"pointers": ["/items/01", "/items/-", "/missing", "/01"]})
        self.assertFalse(result["complete"])
        self.assertEqual([item["status"] for item in result["projections"]], ["missing", "missing", "missing", "ok"])
        self.assertEqual(result["projections"][3]["value"], "object key")

    def test_budget_withholds_whole_values_but_still_returns_small_evidence(self):
        value = {"large": "x" * 20000, "small": {"cause": "credentials missing"}}
        result, raw = selected(json.dumps(value), {"pointers": ["/large", "/small"]}, budget=1024)
        self.assertLessEqual(len(raw), 1024)
        self.assertFalse(result["complete"])
        self.assertEqual(result["projections"][0]["status"], "output_budget_exceeded")
        self.assertNotIn("value", result["projections"][0])
        self.assertEqual(result["projections"][1]["value"], value["small"])

    def test_filtered_page_omissions_include_budget_refusals(self):
        value = [{"state": "failed", "detail": "x" * 3000}, {"state": "failed", "detail": "small"}]
        result, raw = selected(json.dumps(value), {"filter": {"pointer": "", "where": "/state", "equals": "failed"}}, 1024)
        self.assertLessEqual(len(raw), 1024)
        self.assertFalse(result["complete"])
        self.assertFalse(result["filter"]["page_complete"])
        self.assertEqual(result["filter"]["returned_matches"], 1)
        self.assertEqual(result["filter"]["omitted_matches"], 1)
        self.assertEqual(result["filter"]["items"][1]["index"], 1)

    def test_ambiguous_nonfinite_malformed_and_unbounded_structures_are_refused(self):
        for text in ('{"key":1,"key":2}', '{"n":NaN}', '{"n":Infinity}', 'not json', '[' * 70 + '0' + ']' * 70):
            with self.subTest(text=text[:20]), self.assertRaises(ValueError):
                selected(text, {"pointers": [""]})
        for spec in ({"pointers": ["/bad~2escape"]}, {"pointers": ["/a", "/a"]},
                     {"filter": {"pointer": "", "where": "", "equals": {}, "limit": 1}},
                     {"filter": {"pointer": "", "where": "", "equals": 1, "limit": True}}):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                selected('[1]', spec)

    def test_cli_emits_utf8_and_counts_exact_returned_bytes_without_changing_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary) / "scratch"
            store = session.Store.for_scratchpad(str(scratch))
            text = '{"failures":[{"reason":"雪"}],"number":0.10000000000000000000001}'
            response = {"stdout": text, "stderr": "", "exitCode": 7}
            path = artifacts.save(store, "Bash", response, text)
            before = Path(session.fs_path(path)).read_bytes()
            command = [sys.executable, "-I", "-B", str(ROOT / "scripts/artifact.py"), "--scratchpad", str(scratch),
                       "--id", Path(path).stem, "--select-json", '{"pointers":["/failures/0","/number"]}', "--budget", "2048"]
            result = subprocess.run(command, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLessEqual(len(result.stdout), 2048)
            self.assertIn("雪".encode(), result.stdout)
            self.assertIn(b'0.10000000000000000000001', result.stdout)
            self.assertEqual(ledger.load(store)[-1]["bytes_after"], len(result.stdout))
            self.assertEqual(ledger.load(store)[-1]["est_tokens_avoided"], 0)
            self.assertEqual(Path(session.fs_path(path)).read_bytes(), before)
            self.assertEqual(artifacts.load(store, Path(path).stem)["response"], response)
            failed = subprocess.run(command[:-3] + ['{"pointers":["/bad~escape"]}'], capture_output=True, timeout=10)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(len(ledger.load(store)), 1)


if __name__ == "__main__":
    unittest.main()
