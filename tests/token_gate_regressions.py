"""Offline admission checks: tokenizer, footer costs and unchanged history."""
import binascii
import contextlib
import gzip
import io
import json
import random
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  scrubs ORK_METER_* when this file runs on its own

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/lib"))
import hook
import ledger
import pure_bpe
import session
import token_gate


class CountingEncoder:
    """Wraps the real encoding and records the length of every text it tokenizes."""

    def __init__(self, real):
        self.real, self.lengths = real, []

    def encode_ordinary(self, text):
        self.lengths.append(len(text))
        return self.real.encode_ordinary(text)


def old_permits(before, after, context=""):
    """The gate before #130: four full tokenizations, any failure refuses."""
    try:
        originals = [token_gate.count(json.dumps(before, ensure_ascii=False, indent=i)) for i in (None, 2)]
        replacements = [token_gate.count(json.dumps(after, ensure_ascii=False, indent=i)) for i in (None, 2)]
        return min(originals) - max(replacements) - token_gate.count(context) >= 512
    except Exception:
        return False


def layouts_permits(before, after, context=""):
    """The gate before #190: both original layouts built, the smaller bound taken."""
    try:
        exact = token_gate.available()
        measure = token_gate.count if exact else token_gate.upper_bound
        needed = (max(measure(text) for text in token_gate._layouts(after))
                  + (measure(context) if context else 0) + token_gate.RESERVE)
        originals = token_gate._layouts(before)
        if min(map(token_gate.lower_bound, originals)) >= needed:
            return True
        if not exact:
            return False
        return all(token_gate.count(text) >= needed for text in sorted(originals, key=len))
    except Exception:
        return False


def bash_output(size):
    rng = random.Random(size)
    lines, total = [], 0
    while total < size:
        line = "%s src/pkg_%d/file_%d.go:%d: object v%d %08x" % (
            rng.choice(["compile", "link", "copy"]), rng.randrange(99), rng.randrange(999),
            rng.randrange(5000), rng.randrange(99), rng.getrandbits(32))
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


@contextlib.contextmanager
def unimportable(*names):
    token_gate.encoding.cache_clear()
    try:
        with patch.dict(sys.modules, dict.fromkeys(names)):
            yield
    finally:
        token_gate.encoding.cache_clear()


def no_tiktoken():
    """tiktoken unimportable, as in a hooks' Python without requirements-hooks.txt."""
    return unimportable("tiktoken")


def no_exact_encoder():
    """Neither tiktoken nor the pure-Python encoder loads: the byte-count gate."""
    return unimportable("tiktoken", "pure_bpe")


def tiktoken_encoding():
    try:
        return token_gate.tiktoken_encoder()
    except Exception:
        return None


class PurePythonFallbackTests(unittest.TestCase):
    """#115: without tiktoken, pure_bpe counts exactly and trims are delivered."""

    def test_tiktoken_missing_counts_exactly_with_pure_bpe(self):
        texts = ["hello world", bash_output(20000), "数据库 👩🏽‍💻 é he'LL", " " * 3000 + "x"]
        with no_tiktoken():
            self.assertTrue(token_gate.available())
            self.assertIs(type(token_gate.encoding()), pure_bpe.Encoder)
            counts = [token_gate.count(text) for text in texts]
        pure = pure_bpe.encoder()
        self.assertEqual(counts, [len(pure.encode_ordinary(text)) for text in texts])
        reference = tiktoken_encoding()
        if reference is not None:
            self.assertEqual(counts, [len(reference.encode_ordinary(text)) for text in texts])

    def test_tiktoken_missing_still_admits_a_borderline_trim(self):
        # The byte-count gate refuses this (ByteCountFallbackTests); the exact
        # count proves thousands of tokens saved.
        after, context = "kept head and tail", "Full output: /tmp/x.txt"
        with no_tiktoken():
            self.assertTrue(token_gate.permits(bash_output(60000), after, context))
            self.assertFalse(token_gate.permits(bash_output(600), after, context))

    def test_wrong_tiktoken_version_uses_pure_bpe(self):
        token_gate.encoding.cache_clear()
        try:
            with patch("importlib.metadata.version", return_value="0.9.0"):
                self.assertIs(type(token_gate.encoding()), pure_bpe.Encoder)
        finally:
            token_gate.encoding.cache_clear()

    def test_pure_bpe_failing_falls_back_to_the_byte_gate(self):
        def broken():
            raise ValueError("token vocabulary integrity check failed")
        after = "kept head and tail"
        with no_tiktoken(), patch.object(pure_bpe, "encoder", broken):
            self.assertFalse(token_gate.available())
            self.assertFalse(token_gate.permits(bash_output(60000), after))
            self.assertTrue(token_gate.permits("x" * (token_gate.MAX_TOKEN_BYTES * fallback_needed(after)), after))

    def test_each_loader_is_tried_once_per_process(self):
        calls = []
        def failing():
            calls.append(1)
            raise ImportError("no such encoder")
        token_gate.encoding.cache_clear()
        try:
            with patch.object(token_gate, "EXACT_ENCODERS", (failing, failing)):
                for _ in range(5):
                    self.assertFalse(token_gate.available())
                    self.assertFalse(token_gate.permits("x" * 2000, "short"))
            self.assertEqual(len(calls), 2)
        finally:
            token_gate.encoding.cache_clear()


def fallback_needed(after, context=""):
    """What the byte-count gate requires of the original's size bound."""
    return (max(len(t.encode("utf-8")) for t in token_gate._layouts(after))
            + (len(context.encode("utf-8")) if context else 0) + token_gate.RESERVE)


class ByteCountFallbackTests(unittest.TestCase):
    """#115: without the tokenizer, a sound byte-count gate replaces the exact one."""

    def test_no_exact_encoder_disables_the_exact_gate(self):
        with no_exact_encoder():
            self.assertFalse(token_gate.available())
            with self.assertRaises(ImportError):
                token_gate.count("ork")

    def test_encoder_chain_uses_the_first_that_loads_then_the_byte_bound(self):
        class Exact:
            def encode_ordinary(self, text):
                return list(text.encode("utf-8"))   # one token per byte: a stand-in
        def unavailable():
            raise ImportError("no such encoder")
        before, after = "x" * 70000, "short"
        token_gate.encoding.cache_clear()
        try:
            with patch.object(token_gate, "EXACT_ENCODERS", (unavailable, Exact)):
                self.assertTrue(token_gate.available())
                self.assertEqual(token_gate.count("abc"), 3)
                # The exact path counts a short original the size bound can't decide.
                self.assertTrue(token_gate.permits("x" * 2000, after))
            token_gate.encoding.cache_clear()
            with patch.object(token_gate, "EXACT_ENCODERS", (unavailable,)):
                self.assertFalse(token_gate.available())
                self.assertTrue(token_gate.permits(before, after))
                self.assertFalse(token_gate.permits("x" * 2000, after))
        finally:
            token_gate.encoding.cache_clear()

    def test_large_trim_is_admitted_without_tiktoken(self):
        out = bash_output(3 * 1024 * 1024)
        before = {"stdout": out, "stderr": "", "interrupted": False, "isImage": False}
        after = {"stdout": out[:6000] + "\n[trimmed]\n" + out[-6000:], "stderr": "",
                 "interrupted": False, "isImage": False}
        with no_exact_encoder():
            self.assertTrue(token_gate.permits(before, after, "Full output: /tmp/x.txt"))

    def test_borderline_trim_is_refused_without_tiktoken(self):
        after, context = "kept head and tail", "Full output: /tmp/x.txt"
        needed = fallback_needed(after, context)
        # A JSON string's layout is its bytes plus two quotes.
        at = "x" * (token_gate.MAX_TOKEN_BYTES * needed - 2)
        short = "x" * (token_gate.MAX_TOKEN_BYTES * (needed - 1) - 2)
        with no_exact_encoder():
            self.assertTrue(token_gate.permits(at, after, context))
            self.assertFalse(token_gate.permits(short, after, context))
            # Far past the 512-token reserve by any real count, yet unproven by bytes.
            self.assertFalse(token_gate.permits(bash_output(60000), after, context))
        try:
            token_gate.encoding()
        except ImportError:
            return
        # The exact gate admits what the fallback refused: it is conservative.
        self.assertTrue(token_gate.permits(short, after, context))
        self.assertTrue(token_gate.permits(bash_output(60000), after, context))

    def test_fallback_applies_the_exact_gates_input_limits(self):
        huge = "data " * 2_000_000
        with no_exact_encoder():
            self.assertTrue(token_gate.permits(huge, "short"))
            self.assertFalse(token_gate.permits(huge, "x" * (token_gate.MAX_CHARS + 1)))
            self.assertFalse(token_gate.permits(huge, "short", " " * (token_gate.MAX_WHITESPACE_RUN + 1)))

    def test_every_single_byte_is_a_vocabulary_token(self):
        # The replacement's upper bound (tokens <= bytes) rests on this; it
        # needs only the bundled vocabulary, not tiktoken.
        raw = gzip.decompress(Path(token_gate.VOCABULARY).read_bytes())
        tokens = {binascii.a2b_base64(line.split()[0]) for line in raw.splitlines()}
        self.assertTrue(all(bytes([b]) in tokens for b in range(256)))
        self.assertEqual(max(map(len, tokens)), token_gate.MAX_TOKEN_BYTES)


def corpus_texts(rng):
    alphabets = ["abc xyz\n", "確認済み、テスト。", "🙂🚀 é", " \t\n\r", "{}[]\":,0123456789",
                 "".join(map(chr, range(0x20, 0x7f))), "\u0300\u0301aé\u200d"]
    for _ in range(300):
        alphabet = rng.choice(alphabets) + rng.choice(alphabets)
        yield "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 400)))
    for size in (0, 1, 127, 128, 129, 4096):
        yield "x" * size
        yield " " * size
        yield bash_output(size)


class FallbackSoundnessTests(unittest.TestCase):
    """The fallback never admits what the exact gate refuses (needs tiktoken)."""

    def setUp(self):
        try:
            token_gate.encoding()
        except ImportError:
            self.skipTest("offline tokenizer not installed")

    def test_byte_bounds_bracket_the_exact_count(self):
        for text in corpus_texts(random.Random(115)):
            exact = token_gate.count(text)
            self.assertLessEqual(token_gate.lower_bound(text), exact, repr(text[:40]))
            self.assertGreaterEqual(token_gate.upper_bound(text), exact, repr(text[:40]))

    def test_fallback_admission_implies_exact_admission(self):
        rng = random.Random(1150)
        texts = list(corpus_texts(rng))
        cases = []
        for _ in range(400):
            after = rng.choice(texts)
            if rng.random() < 0.5:
                after = {"stdout": after, "stderr": rng.choice(texts)[:50]}
            context = rng.choice(["", "Full output: /tmp/x.txt", rng.choice(texts)])
            needed = fallback_needed(after, context)
            # Originals sized around the fallback's threshold, in varied content.
            size = token_gate.MAX_TOKEN_BYTES * needed + rng.randrange(-3000, 3000)
            original = rng.choice(texts) or "x"
            original = (original * (size // max(1, len(original.encode("utf-8"))) + 1))[:max(0, size)]
            if rng.random() < 0.3:
                original = {"stdout": original, "stderr": ""}
            cases.append((original, after, context))
        verdicts = []
        with no_exact_encoder():
            for case in cases:
                verdicts.append(token_gate.permits(*case))
        admitted = 0
        for case, fallback in zip(cases, verdicts):
            exact = token_gate.permits(*case)
            if fallback:
                admitted += 1
                self.assertTrue(exact, (str(case[0])[:40], case[1], case[2]))
        # Non-vacuous: the corpus exercises both verdicts.
        self.assertGreater(admitted, 20)
        self.assertLess(admitted, len(cases) - 20)


class TokenGateTests(unittest.TestCase):
    def tokenizer(self):
        try:
            return token_gate.encoding()
        except ImportError:
            self.skipTest("offline tokenizer not installed")

    def counting(self):
        counter = CountingEncoder(self.tokenizer())
        return counter, patch.object(token_gate, "encoding", return_value=counter)

    def test_large_original_is_admitted_without_tokenizing_it(self):
        # #130: the original was tokenized twice, so the largest outputs ran out
        # of the hook's budget (or hit the 2M-character bound) and were never
        # trimmed. Their size alone proves the saving.
        out = bash_output(3 * 1024 * 1024)
        before = {"stdout": out, "stderr": "", "interrupted": False, "isImage": False}
        after = {"stdout": out[:6000] + "\n[trimmed]\n" + out[-6000:], "stderr": "",
                 "interrupted": False, "isImage": False}
        counter, patched = self.counting()
        with patched:
            self.assertTrue(token_gate.permits(before, after, "Full output: /tmp/x.txt"))
        self.assertLess(max(counter.lengths), 20000)
        self.assertLessEqual(len(counter.lengths), 3)

    def test_each_distinct_serialization_is_tokenized_at_most_once(self):
        before = "\n".join("row %d : 確認済み" % i for i in range(800))
        counter, patched = self.counting()
        with patched:
            self.assertTrue(token_gate.permits(before, "small", "note"))
        # A string has one JSON layout: original, replacement and context once each.
        self.assertEqual(len(counter.lengths), 3)
        # A short original stops at the first layout that falls short; no
        # context costs no tokenization. Two replacement layouts, one original.
        counter, patched = self.counting()
        with patched:
            self.assertFalse(token_gate.permits({"stdout": "a b " * 100}, {"stdout": "a"}))
        self.assertEqual(len(counter.lengths), 3)

    def test_decisions_match_the_four_pass_gate(self):
        self.tokenizer()
        rng = random.Random(130)
        cases = [(" " * 4000, "?a " * 600, ""), ("x" * 20000, "short", ""),
                 ({"stdout": "\n".join("row %d : 確認済み" % i for i in range(1000))}, {"stdout": "small"}, "")]
        for size in (2000, 3000, 4000, 6000, 12000, 50000, 200000):
            out = bash_output(size)
            for keep in (100, 1000, 3000):
                cases.append(({"stdout": out, "stderr": ""}, {"stdout": out[:keep], "stderr": ""},
                              "note " * rng.randrange(0, 200)))
                cases.append((out, out[-keep:], ""))
        for before, after, context in cases:
            self.assertEqual(token_gate.permits(before, after, context),
                             old_permits(before, after, context), (str(before)[:40], len(str(before))))

    def test_decisions_match_the_two_layout_gate(self):
        # #190: only the compact original is serialized for the size bound;
        # the indented one only when it must be counted. Same decisions, with
        # the exact encoder and with the byte-count fallback.
        self.tokenizer()
        rng = random.Random(190)
        cases = [("x" * 20000, "short", ""), ([], [], ""), ({}, "", "note"), (None, "x", ""), (12, "x", "")]
        for size in (1500, 3000, 8000, 40000, 300000):
            out = bash_output(size)
            nested = {"stdout": out.split("\n"), "meta": {"rows": [{"i": i, "v": [i, None, True]} for i in range(size // 200)]},
                      "empty": [], "obj": {}}
            for keep in (50, 500, 5000):
                context = "note " * rng.randrange(0, 300)
                cases += [(nested, {"stdout": out[:keep]}, context), ({"stdout": out}, {"stdout": out[:keep]}, context),
                          (out.split("\n"), out[:keep].split("\n"), ""), (out, out[-keep:], context)]
        for encoders in (None, no_exact_encoder):
            with (encoders() if encoders else contextlib.nullcontext()):
                for before, after, context in cases:
                    self.assertEqual(token_gate.permits(before, after, context),
                                     layouts_permits(before, after, context), (str(before)[:40], len(str(before))))

    def test_size_bound_never_exceeds_the_exact_count(self):
        self.tokenizer()
        raw = gzip.decompress(Path(token_gate.VOCABULARY).read_bytes())
        longest = max(len(binascii.a2b_base64(line.split()[0])) for line in raw.splitlines())
        self.assertEqual(token_gate.MAX_TOKEN_BYTES, longest)
        for text in ("", "a", "x" * 5000, "確認済み" * 900, bash_output(40000), " " * 70000):
            self.assertLessEqual(token_gate.lower_bound(text), token_gate.count(text))

    def test_long_single_piece_is_counted(self):
        # The pinned tokenizer merges long pieces in near-linear time, so the
        # per-piece scan that refused them (and cost 4x the encoding) is gone.
        self.tokenizer()
        self.assertGreater(token_gate.count("x" * (1024 * 1024)), 0)

    def test_byte_saving_whitespace_can_cost_more_tokens(self):
        before = " " * 4000
        after = "?a " * 600
        self.assertLess(len(after), len(before))
        self.assertFalse(token_gate.permits(before, after))

    def test_footer_and_unicode_serialization_are_charged(self):
        before = {"stdout": "\n".join("row %d : 確認済み" % i for i in range(1000))}
        self.assertTrue(token_gate.permits(before, {"stdout": "small"}))
        self.assertFalse(token_gate.permits(before, {"stdout": "small"}, before["stdout"]))
        self.assertFalse(token_gate.permits("small " * 30, "tiny"))

    def test_missing_or_corrupt_tokenizer_and_pathological_input_pass_through(self):
        with patch.object(token_gate, "encoding", side_effect=ValueError("unavailable")):
            self.assertFalse(token_gate.permits("data " * 2000, "short"))
        self.assertFalse(token_gate.permits("data " * 2000, "x" * (token_gate.MAX_CHARS + 1)))

    def test_huge_whitespace_run_is_refused_not_a_crash(self):
        # Review of #162: about a million spaces overflow the pinned tiktoken's
        # regex stack and raise a Rust panic (a BaseException) when counted.
        original = " " * 1_000_600 + "end"
        with patch.object(token_gate, "lower_bound", return_value=0):   # force the exact count
            self.assertFalse(token_gate.permits(original, "short"))
        with self.assertRaises(ValueError):
            token_gate.count(" " * (token_gate.MAX_WHITESPACE_RUN + 1))

    def test_a_tokenizer_panic_refuses_instead_of_escaping(self):
        class PanicException(BaseException):
            pass
        with patch.object(token_gate, "count", side_effect=PanicException("stack overflow")):
            self.assertFalse(token_gate.permits("data " * 2000, "short"))
        with patch.object(token_gate, "count", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                token_gate.permits("data " * 2000, "short")

    def test_bulk_vocabulary_load_matches_validated_decode(self):
        import base64
        import gzip
        raw = gzip.decompress(Path(token_gate.VOCABULARY).read_bytes())
        expected = {base64.b64decode(token, validate=True): int(rank)
                    for token, rank in (line.split() for line in raw.splitlines())}
        try:
            self.assertEqual(token_gate.encoding()._mergeable_ranks, expected)
        except ImportError:
            self.skipTest("offline tokenizer not installed")

    def test_malformed_vocabulary_disables_reduction(self):
        import gzip
        import hashlib
        for raw in (b"YQ== 0\nYg==\n", b"YQ== 0\nYQ== 1\n"):
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "vocab.gz"
                path.write_bytes(gzip.compress(raw))
                # Both exact encoders read it through token_gate.load_ranks.
                with patch.object(token_gate, "VOCABULARY", path), \
                        patch.object(token_gate, "VOCAB_SHA256", hashlib.sha256(raw).hexdigest()):
                    token_gate.encoding.cache_clear()
                    try:
                        with self.assertRaises((ValueError, ImportError)):
                            token_gate.encoding()
                        self.assertFalse(token_gate.permits("data " * 2000, "short"))
                    finally:
                        token_gate.encoding.cache_clear()

    def test_rejected_reply_has_no_savings_credit(self):
        with tempfile.TemporaryDirectory() as d:
            payload = {"hook_event_name": "PostToolUse", "session_id": "gate-test",
                       "scratchpad_dir": d, "tool_name": "Bash", "tool_response": {"stdout": " " * 4000}}
            def handler(ctx):
                ctx.record(ledger.trimmed("Bash", 4000, 1800))
                return hook.updated_tool_output("PostToolUse", {"stdout": "?a " * 600})
            sink = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", sink):
                hook.run(("PostToolUse",), handler)
            self.assertEqual(sink.getvalue(), "")
            self.assertEqual(ledger.load(session.Store.for_scratchpad(d)), [])


if __name__ == "__main__":
    unittest.main()
