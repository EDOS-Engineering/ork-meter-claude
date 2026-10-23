"""Shared offline behavioral fixtures against one checkout's production code.

This checker is a test dependency, never a plugin component or model tool.
Artifact formats remain adapter-specific. Reports qualify only listed cases.
"""
import argparse
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expand(parts):
    values = []
    for part in parts:
        repeat = part.get("repeat", 1)
        if type(repeat) is not int or not 1 <= repeat <= 10000:
            raise ValueError("invalid fixture repeat")
        if "base64" in part:
            values.append(base64.b64decode(part["base64"], validate=True) * repeat)
        else:
            for index in range(repeat):
                text = part["text"].replace("{i}", str(index)) if part.get("indexed") else part["text"]
                values.append(text.encode("utf-8"))
    data = b"".join(values)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("fixture input exceeds 2 MiB")
    return data


class Claude:
    namespace = "claude-structured-response-v1"

    def __init__(self, root):
        sys.path.insert(0, str(root / "hooks/lib"))
        import artifacts, hook, ledger, session, trimmer
        self.artifacts, self.hook, self.ledger, self.session, self.trimmer = artifacts, hook, ledger, session, trimmer
        self.source = load_file("contract_claude_source", root / "scripts/source.py")

    def reduce(self, data, kind):
        text = data.decode("utf-8")
        if kind == "test":
            text, _ = self.trimmer.filter_test_output(text)
        output, _ = self.trimmer.trim_text(text, 1024)
        return output.encode("utf-8")

    def symbol(self, workspace, case):
        result = self.source.retrieve(workspace / "sample.py", symbol=case["symbol"], expected_hash=case.get("expected_hash"))
        return result["source"], result["sha256"], result["start"], result["end"]

    def artifact(self, directory, data):
        store = self.session.Store.for_payload({"scratchpad_dir": str(directory), "session_id": directory.name})
        try:
            text, encoding = data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            text, encoding = base64.b64encode(data).decode("ascii"), "base64"
        response = {"stdout": text, "stderr": "", "exitCode": 7, "stream_encodings": {"stdout": encoding}}
        identity = Path(self.artifacts.save(store, "Bash", response, text)).stem
        def recover():
            value = self.artifacts.load(store, identity)["response"]
            return (base64.b64decode(value["stdout"], validate=True) if value["stream_encodings"]["stdout"] == "base64"
                    else value["stdout"].encode("utf-8"))
        return identity, recover, Path(store.tool_output_path(identity, ".json")), lambda: self.artifacts.save(store, "Bash", response, text)

    def delivery(self, directory, case):
        payload = {"hook_event_name": "PostToolUse", "session_id": directory.name,
                   "scratchpad_dir": str(directory), "tool_name": "Bash", "tool_use_id": "contract-call",
                   "tool_response": {"stdout": "x" * case["before"], "stderr": "", "exitCode": 0, "isImage": False}}
        def handler(ctx):
            ctx.record(self.ledger.trimmed("Bash", case["before"], case["after"]))
            if case["phase"] == "abandoned":
                return None
            return self.hook.updated_tool_output("PostToolUse", dict(payload["tool_response"], stdout="x" * case["after"]))
        sink = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), patch.object(sys, "stdout", sink):
            if case["phase"] == "write_failed":
                with patch.object(sink, "write", side_effect=OSError("synthetic write failure")):
                    self.hook.run(("PostToolUse",), handler)
            elif case["phase"] == "flush_failed":
                with patch.object(sink, "flush", side_effect=OSError("synthetic flush failure")):
                    self.hook.run(("PostToolUse",), handler)
            else:
                self.hook.run(("PostToolUse",), handler)
        store = self.session.Store.for_scratchpad(str(directory))
        if case["phase"] == "emitted" and case["recovery"]:
            self.ledger.append(store, self.ledger.stamp(self.ledger.recovered("Bash", case["recovery"]), None))
        return self.ledger.summarise(self.ledger.load(store))["totals"]["net_bytes_avoided"]


class Codex:
    namespace = "codex-raw-bytes-v1"

    def __init__(self, root):
        sys.path.insert(0, str(root))
        from ork_codex.reduce import compact
        from ork_codex.policy import Policy
        from ork_codex.retrieval import Sources
        from ork_codex.storage import Store
        from ork_codex.delivery import DeliveryInfo, RecoveryReceipt
        self.compact, self.policy, self.sources, self.store = compact, Policy, Sources, Store
        self.delivery_info, self.recovery_receipt = DeliveryInfo, RecoveryReceipt

    def reduce(self, data, kind):
        return self.compact(data, kind, self.policy(budget=1024, threshold=2048))[0]

    def symbol(self, workspace, case):
        result = self.sources(workspace).symbol("sample.py", case["symbol"], expected_sha256=case.get("expected_hash"))
        return result["content"], result["sha256"], result["start_line"], result["end_line"]

    def artifact(self, directory, data):
        store = self.store(directory / "state", run_id="contract-run")
        identity = store.put(data)
        return identity, lambda: store.get(identity), store.path(identity), lambda: store.put(data)

    def delivery(self, directory, case):
        store = self.store(directory / "state", run_id="contract-run")
        store.event("log", before=case["before"], after=case["after"], mode="compact",
                    intervention="contract-call", policy_id="fixture-v1", delivery_state="prepared")
        receipt = self.delivery_info({"intervention": "contract-call"}, store)
        if case["phase"] == "emitted":
            receipt.confirm(case["after"])
            if case["recovery"]:
                handle = store.put(b"fixture")
                context = {"mode": "compact", "run_id": "contract-run", "policy_id": "fixture-v1", "origin_intervention": "contract-call"}
                self.recovery_receipt(store, handle, context).confirm(case["recovery"])
        else:
            receipt.abandon(case["phase"])
        return sum(value["net_bytes_avoided"] for value in store.report()["modes"].values())


def verify(adapter, case, test):
    if case["type"] == "output":
        data = expand(case["parts"])
        output = adapter.reduce(data, case["kind"])
        if case["expect"] == "passthrough":
            test.assertEqual(output, data, "unknown/small formats must remain byte-identical")
        else:
            test.assertLess(len(output), len(data))
        required = case.get("required", []) + expand(case.get("required_parts", [])).decode("utf-8").splitlines()
        for evidence in required:
            test.assertIn(evidence.encode("utf-8"), output)
        test.assertGreaterEqual(len(output), case.get("minimum_output_bytes", 0))
        test.assertEqual(adapter.reduce(data, case["kind"]), output, "pure reduction must be deterministic")
        return
    with tempfile.TemporaryDirectory(prefix="ork-contract-") as temporary:
        directory = Path(temporary)
        if case["type"] in {"source", "source_error"}:
            data = case["text"].encode("utf-8")
            (directory / "sample.py").write_bytes(data)
            if case["type"] == "source_error":
                with test.assertRaisesRegex(ValueError, case["error"]):
                    adapter.symbol(directory, case)
            else:
                text, digest, start, end = adapter.symbol(directory, case)
                test.assertEqual(text, case["expected"])
                test.assertEqual(digest, hashlib.sha256(data).hexdigest())
                test.assertEqual((start, end), (case["start"], case["end"]))
        elif case["type"] == "artifact":
            data = expand(case["parts"])
            identity, recover, path, save_again = adapter.artifact(directory, data)
            test.assertRegex(identity, r"^[a-f0-9]{64}$")
            test.assertEqual(recover(), data)
            before = path.read_bytes()
            save_again()
            test.assertEqual(path.read_bytes(), before, "same original must not overwrite the immutable artifact")
            if adapter.namespace == "codex-raw-bytes-v1":
                test.assertEqual(identity, hashlib.sha256(data).hexdigest())
            if case.get("corrupt"):
                path.write_bytes(b"tampered")
                with test.assertRaises((OSError, ValueError)):
                    recover()
        elif case["type"] == "delivery":
            test.assertEqual(adapter.delivery(directory, case), case["expected_net"])
        else:
            raise ValueError("unknown fixture type")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, choices=("claude", "codex"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    corpus_path = Path(__file__).parent / "v1/fixtures.json"
    raw = corpus_path.read_bytes()
    corpus = json.loads(raw)
    manifest = json.loads((corpus_path.parent / "manifest.json").read_bytes())
    digest = hashlib.sha256(raw).hexdigest()
    if corpus["contract"] != "ork-evidence-behavior" or corpus["version"] != 1 or manifest["fixtures_sha256"] != digest:
        raise SystemExit("fixture contract/version/digest mismatch")
    shared = manifest["shared_modules"]["evidence_formats"]
    shared_path = args.root / shared[args.adapter]
    if hashlib.sha256(shared_path.read_bytes()).hexdigest() != shared["sha256"]:
        raise SystemExit("shared production format guard differs from contract pin")
    cases = corpus["cases"]
    if len({case["id"] for case in cases}) != len(cases):
        raise SystemExit("fixture IDs must be unique")
    adapter = (Claude if args.adapter == "claude" else Codex)(args.root.resolve())
    suite = unittest.TestSuite()
    for case in cases:
        def check(test, fixture=case):
            verify(adapter, fixture, test)
        cls = type("Contract_" + case["id"].replace("-", "_"), (unittest.TestCase,), {"runTest": check})
        suite.addTest(cls())
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    qualified = result.wasSuccessful() and not result.skipped and result.testsRun == len(cases)
    report = {"contract": corpus["contract"], "version": 1, "adapter": args.adapter,
              "artifact_namespace": adapter.namespace, "fixtures_sha256": digest,
              "shared_format_sha256": shared["sha256"],
              "inference": "none", "cases": [case["id"] for case in cases], "tests_run": result.testsRun,
              "failures": [str(test) for test, _ in result.failures], "errors": [str(test) for test, _ in result.errors],
              "qualified": qualified, "scope": "offline listed behaviors; not native integration, shared storage format or economics"}
    print(json.dumps(report, sort_keys=True))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if qualified else 1


if __name__ == "__main__":
    sys.exit(main())
