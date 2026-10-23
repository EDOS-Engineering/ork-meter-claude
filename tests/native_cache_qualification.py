"""Native cache-prefix invariants and paired request-text diagnostics, no billing claims."""
import copy
import hashlib
import importlib.metadata
import json
import os
import re
from pathlib import Path
import shlex
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import host_retry  # noqa: E402
import native_output_qualification as native_output


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Tokenizer:
    """Test-only dependency; never import a separate benchmark checkout."""
    def __init__(self, name):
        import token_gate
        version = importlib.metadata.version("tiktoken")
        if version != "0.14.0" or name != "tiktoken:o200k_base":
            raise ValueError("native token screen requires tiktoken==0.14.0 / o200k_base")
        # The plugin's own bundled, hash-checked vocabulary: tiktoken.get_encoding
        # downloads it from openaipublic.blob.core.windows.net on a cold cache,
        # which failed a macOS runner on DNS (#261's CI). Same encoding: the
        # fingerprint below is unchanged.
        self.encoder = token_gate.tiktoken_encoder()
        fingerprint = hashlib.sha256(canonical({"pattern": self.encoder._pat_str,
            "special": self.encoder._special_tokens,
            "ranks": sorted((key.hex(), rank) for key, rank in self.encoder._mergeable_ranks.items())}).encode()).hexdigest()
        if fingerprint != "a3c0b7579201641caa996ee76e448c1c1ea266c8c6a3ab88a82535284e549020":
            raise ValueError("native screen encoding fingerprint changed")
        self.identity = {"status": "available", "name": name, "package_version": version,
            "encoding_sha256": fingerprint, "provider_equivalence": "surrogate; unverified for target model"}

    def count(self, texts):
        return sum(len(self.encoder.encode_ordinary(text)) for text in texts)


def source_hashes():
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for directory in ("hooks", "scripts", "skills", "agents", "tests", ".claude-plugin")
            for path in sorted((ROOT / directory).rglob("*"))
            if path.is_file() and path.suffix in {".py", ".sh", ".md", ".json"}}


def history(messages, canonical_text=True):
    """Remove only movable provider annotations, never tool/source data keys."""
    result = copy.deepcopy(messages)
    for message in result:
        if canonical_text and isinstance(message.get("content"), str):
            message["content"] = [{"type": "text", "text": message["content"]}]
        if isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    return result


def controls(value):
    if isinstance(value, dict):
        found = [value["cache_control"]] if "cache_control" in value else []
        return found + [control for key, item in value.items() if key != "cache_control" for control in controls(item)]
    if isinstance(value, list):
        return [control for item in value for control in controls(item)]
    return []


def normalize_paths(value, base):
    if isinstance(value, str):
        for path in (str(base), str(base).replace("\\", "/")):
            value = value.replace(path, "<fixture-root>")
        # Native Grep persists oversized output under a random session UUID and
        # encoded workspace directory. Normalize only that generated locator;
        # preserve the filename and every byte of the returned evidence.
        value = re.sub(
            r"(?m)(^Output too large \([^\n]*\)\. Full output saved to: <fixture-root>[/\\]config[/\\]projects[/\\])"
            r"[^/\\\r\n]+[/\\][0-9a-f-]{36}([/\\]tool-results[/\\][^\r\n]+)$",
            r"\1<workspace>/<session>\2", value)
        return value
    if isinstance(value, dict):
        return {key: normalize_paths(item, base) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_paths(item, base) for item in value]
    return value


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "installed host + localhost opt-in")
@host_retry.retry_once
class NativeCache(unittest.TestCase):
    def test_new_reductions_preserve_cache_prefix_and_delivered_history(self):
        tokenizer = Tokenizer("tiktoken:o200k_base")
        self.assertIsNotNone(tokenizer.encoder, "token counts must be available to qualify savings")
        before = source_hashes()
        results = {}
        captures = {}
        stock_initial_request = None
        for mode in ("stock", "off", "reduce", "shadow"):
            runner = native_output.NativeOutputQualification("test_native_baseline_reduction_and_shadow_capture")
            runner.qualify(mode)
            bodies = copy.deepcopy(native_output.Fixture.requests)
            captures[mode] = bodies
            if os.environ.get("NATIVE_CACHE_REPORT"):
                capture_path = Path(os.environ["NATIVE_CACHE_REPORT"]).resolve().with_suffix(".captures.json")
                capture_path.relative_to(ROOT)
                capture_path.parent.mkdir(parents=True, exist_ok=True)
                capture_path.write_text(json.dumps(captures, indent=2) + "\n", encoding="utf-8")
            self.assertEqual(len(bodies), 4)
            script = shlex.split(native_output.Fixture.calls[0][1]["command"])[1]
            base = Path(script).parent.parent
            stable = {key: bodies[0].get(key) for key in ("system", "tools", "tool_choice", "cache_control")}
            initial = normalize_paths({key: bodies[0][key] for key in (
                "model", "system", "tools", "tool_choice", "cache_control", "messages", "thinking", "output_config"
            ) if key in bodies[0]}, base)
            if mode == "stock":
                stock_initial_request = initial
            else:
                self.assertEqual(initial, stock_initial_request,
                                 "plugin discovery must preserve the stock initial prompt and cache configuration")
            count = 0
            wire_content_stable = True
            for index, body in enumerate(bodies):
                self.assertEqual({key: body.get(key) for key in stable}, stable,
                                 "plugin/host must not alter tools, system, tool choice or cache policy mid-session")
                cache_controls = controls({key: body[key] for key in ("system", "tools", "messages", "cache_control") if key in body})
                self.assertTrue(cache_controls, "a stable prefix without enabled cache controls is insufficient")
                self.assertTrue(all(isinstance(control, dict) and control.get("type") == "ephemeral" for control in cache_controls))
                if index:
                    prior = history(bodies[index - 1]["messages"])
                    current = history(body["messages"])
                    self.assertGreater(len(current), len(prior), "fresh tool work must append to history")
                    self.assertEqual(current[:len(prior)], prior, "prior roles and content must remain identical after text-container canonicalization")
                    raw_prior = history(bodies[index - 1]["messages"], canonical_text=False)
                    wire_content_stable = wire_content_stable and history(body["messages"], canonical_text=False)[:len(raw_prior)] == raw_prior
                texts = [canonical(normalize_paths(body.get(key), base)) for key in ("system", "tools", "messages")]
                measured = tokenizer.count(texts)
                count = count + measured if count is not None and measured is not None else None
            results[mode] = {"requests": len(bodies), "normalized_request_text_surrogate_tokens": count,
                             "stable_prefix_sha256": hashlib.sha256(canonical(stable).encode()).hexdigest(),
                             "normalized_initial_request_sha256": hashlib.sha256(canonical(initial).encode()).hexdigest(),
                             "initial_prompt_matches_stock": True,
                             "history_content_append_only": True, "wire_history_append_only_except_cache_controls": wire_content_stable,
                             "cache_controls_present": True}
        if tokenizer.encoder is not None:
            self.assertLess(results["reduce"]["normalized_request_text_surrogate_tokens"],
                            results["stock"]["normalized_request_text_surrogate_tokens"])
            self.assertEqual(results["off"]["normalized_request_text_surrogate_tokens"],
                             results["stock"]["normalized_request_text_surrogate_tokens"])
            self.assertEqual(results["shadow"]["normalized_request_text_surrogate_tokens"],
                             results["stock"]["normalized_request_text_surrogate_tokens"])
        self.assertEqual(before, source_hashes(), "source changed during native qualification")
        evidence = {"schema_version": 1, "inference": "localhost-synthetic", "tokenizer": tokenizer.identity,
                    "source_hashes": before, "source_unchanged": True,
                    "scope": "four predetermined tool continuations; not an agent task or provider token count",
                    "normalization": "cross-arm generated locators only; history content canonicalizes string versus single text-block containers and movable cache controls; wire equality reported separately",
                    "measured_provider_cache_hits": False, "economic_claim_eligible": False, "arms": results}
        report = os.environ.get("NATIVE_CACHE_REPORT")
        if report:
            path = Path(report).resolve()
            path.relative_to(ROOT)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
            path.with_suffix(".captures.json").write_text(json.dumps(captures, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"inference": evidence["inference"], "arms": results,
                          "tokenizer": tokenizer.identity["name"], "source_unchanged": True,
                          "measured_provider_cache_hits": False, "economic_claim_eligible": False,
                          "report": report}))


if __name__ == "__main__":
    unittest.main()
