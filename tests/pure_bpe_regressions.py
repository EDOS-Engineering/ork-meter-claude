"""The pure-Python o200k_base encoder (hooks/lib/pure_bpe.py, #115).

PurePythonTests need no tiktoken: token ids captured from tiktoken 0.14.0,
the vocabulary and Unicode table checks, and the two merge strategies against
each other. DifferentialTests compare a pure_bpe Encoder with tiktoken's
encode_ordinary over the same pinned encoding on a large corpus; they are
skipped, saying why, only where tiktoken can't load. A mismatch fails with a
minimal reproducer.
"""
import functools
import json
import os
from pathlib import Path
import random
import re
import sys
import unicodedata
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  scrubs ORK_METER_* when this file runs on its own

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/lib"))
sys.path.insert(0, str(ROOT / "tests"))
import pure_bpe
import pure_bpe_tables
import token_gate


@functools.lru_cache(maxsize=1)
def encoder():
    """One pure_bpe Encoder for the whole module: each load costs about 50 ms."""
    return pure_bpe.encoder()


def encode(text):
    return encoder().encode_ordinary(text)


def count(text):
    return len(encode(text))


def tiktoken_encoding():
    """tiktoken itself, never the fallback, or None where it can't load."""
    load = getattr(token_gate, "tiktoken_encoder", None) or token_gate.encoding
    try:
        return load()
    except Exception:
        return None


# Captured from tiktoken 0.14.0 encode_ordinary over the pinned encoding.
EXPECTED = {
    'hello world': [24912, 2375],
    '': [],
    '    def count(self):\n        return 42\r\n': [271, 1056, 3605, 2402, 1883, 309, 622, 220, 4689, 370],
    "He'S here, they'LL go; I'd've": [2066, 31233, 2105, 11, 1023, 6, 7454, 810, 26, 18754, 7341],
    '\u6570\u636e\u5e93\u8fde\u63a5\u5931\u8d25: \u043e\u0448\u0438\u0431\u043a\u0430 \u0441\u043e\u0435\u0434\u0438\u043d\u0435\u043d\u0438\u044f': [108009, 72838, 33161, 25, 194534, 75938, 3746],
    '\U0001f469\U0001f3fd\u200d\U0001f4bb e\u0301 \u05e9\u05dc\u05d5\u05dd': [28823, 102, 52622, 121, 2524, 31446, 119, 319, 13430, 173283],
    '1234567 -- ----- \t\t\n\n\n   x': [7633, 19354, 22, 2230, 77201, 28075, 2499, 256, 1215],
    'a\u2028b\x85c\x1cd\u3000e': [64, 51008, 65, 126, 227, 66, 216, 67, 1397, 68],
}


def mismatch(enc, text):
    return enc.encode_ordinary(text) != encode(text)


def shrink(enc, text):
    """A short substring-of-edits of text on which the encoders still disagree."""
    chunk = max(1, len(text) // 2)
    while chunk >= 1:
        i, shrunk = 0, False
        while i < len(text):
            candidate = text[:i] + text[i + chunk:]
            if candidate and mismatch(enc, candidate):
                text, shrunk = candidate, True
            else:
                i += chunk
        if not shrunk:
            chunk //= 2
    return text


# Corpus generators: deterministic, seeded.

def repository_texts():
    suffixes = {".py", ".md", ".json", ".sh", ".txt", ".yml", ".yaml", ".toml", ".jsonl"}
    skip = {".git", "runtime", "__pycache__", "node_modules"}
    for directory, names, files in os.walk(ROOT):
        names[:] = sorted(n for n in names if n not in skip)
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix in suffixes and path.stat().st_size < 4 << 20:
                try:
                    yield str(path.relative_to(ROOT)), path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue


def fixture_payload_texts():
    """The fixtures' tool outputs as the gate sees them: JSON in both layouts."""
    for path in sorted((ROOT / "tests/fixtures").glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        response = value.get("tool_response", value) if isinstance(value, dict) else value
        for indent in (None, 2):
            yield "%s indent=%s" % (path.name, indent), json.dumps(response, ensure_ascii=False, indent=indent)


def synthetic_logs():
    rng = random.Random(115)
    words = ["error", "warning", "Error:", "FAILED", "PASS", "ok", "panic", "undefined", "TypeError",
             "expected", "received", "src/lib.rs", "node_modules", "AssertionError", "Traceback"]

    def py():
        out = ["============================= test session starts ==============================",
               "platform darwin -- Python 3.15.0, pytest-8.4.1, pluggy-1.6.0",
               "rootdir: /Users/dev/project", "collected %d items" % rng.randrange(900), ""]
        for _ in range(60):
            out.append("tests/test_%s.py::test_%s_%d %s [%3d%%]" % (
                rng.choice(words).lower().strip(":"), rng.choice(words).lower(), rng.randrange(99),
                rng.choice(["PASSED", "FAILED", "SKIPPED", "XFAIL"]), rng.randrange(101)))
        out += ["", "    def test_it():", ">       assert foo(%d) == %d" % (rng.randrange(9), rng.randrange(9)),
                "E       AssertionError: assert 3 == 4", "E        +  where 3 = foo(1)", "",
                "tests/test_x.py:%d: AssertionError" % rng.randrange(999),
                "=========================== short test summary info ============================",
                "FAILED tests/test_x.py::test_it - AssertionError: assert 3 == 4",
                "========================= 1 failed, 41 passed in 3.21s ========================="]
        return "\n".join(out)

    def jest():
        out = []
        for _ in range(40):
            out.append(" %s src/components/%s.test.tsx (%.3f s)" % (
                rng.choice(["PASS", "FAIL"]), rng.choice(["Button", "Modal", "useAuth"]), rng.random() * 9))
        out += ["  ● Modal › renders the title", "", "    expect(received).toBe(expected) // Object.is equality",
                "", "    Expected: \"Hello\"", "    Received: undefined", "",
                "      12 |   it('renders', () => {", "    > 13 |     expect(x).toBe('Hello');",
                "         |               ^", "", "Tests:       1 failed, 87 passed, 88 total",
                "Snapshots:   0 total", "Time:        4.512 s", "Ran all test suites."]
        return "\n".join(out)

    def go():
        out = []
        for _ in range(40):
            out.append("=== RUN   Test%s/case_%d" % (rng.choice(["Parse", "Lexer", "Server"]), rng.randrange(50)))
            out.append("--- %s: Test%s (%.2fs)" % (rng.choice(["PASS", "FAIL"]), "Parse", rng.random()))
        out += ["    parser_test.go:42: got \"a\", want \"b\"", "FAIL",
                "FAIL\tgithub.com/acme/thing/parser\t0.123s", "ok  \tgithub.com/acme/thing/lexer\t(cached)"]
        return "\n".join(out)

    def rust():
        return "\n".join([
            "   Compiling serde v1.0.219", "   Compiling ork v0.1.0 (/Users/dev/ork)",
            "error[E0308]: mismatched types", "  --> src/main.rs:12:18", "   |",
            "12 |     let x: u32 = \"five\";", "   |            ---   ^^^^^^ expected `u32`, found `&str`",
            "   |            |", "   |            expected due to this", "",
            "warning: unused variable: `y`", " --> src/lib.rs:3:9",
            "test tests::it_works ... ok", "test tests::it_fails ... FAILED",
            "thread 'tests::it_fails' panicked at src/lib.rs:20:5:",
            "assertion `left == right` failed", "  left: 4", " right: 5",
            "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s"] * 6)

    def gcc():
        out = []
        for _ in range(30):
            out.append("src/%s.c:%d:%d: %s: implicit declaration of function '%s' [-Wimplicit-function-declaration]"
                       % (rng.choice(["main", "util", "net"]), rng.randrange(900), rng.randrange(80),
                          rng.choice(["warning", "error", "note"]), rng.choice(["foo", "bar_baz", "qux2"])))
            out.append("  %d |     int y = foo(x);" % rng.randrange(900))
            out.append("      |             ^~~")
        out.append("make: *** [Makefile:23: build/main.o] Error 1")
        return "\n".join(out)

    def ci():
        out = []
        for n in range(80):
            out.append("2026-10-18T19:%02d:%02d.%07dZ ##[group]Run actions/checkout@v4" % (
                n % 60, rng.randrange(60), rng.randrange(10 ** 7)))
            out.append("2026-10-18T19:%02d:%02d.%07dZ \x1b[36;1mnpm ci\x1b[0m" % (
                n % 60, rng.randrange(60), rng.randrange(10 ** 7)))
        out.append("##[error]Process completed with exit code 1.")
        return "\r\n".join(out)

    for name, make in [("pytest", py), ("jest", jest), ("go", go), ("cargo", rust), ("gcc", gcc), ("ci", ci)]:
        for n in range(3):
            yield "%s log %d" % (name, n), make()


def unicode_texts():
    """Every general category and the scripts the gate is likely to see."""
    by_category = {}
    for point in range(0x110000):
        by_category.setdefault(unicodedata.category(chr(point)), []).append(chr(point))
    rng = random.Random(16)
    for category, chars in sorted(by_category.items()):
        if category == "Cs":
            continue
        sample = rng.sample(chars, min(len(chars), 400))
        yield "category %s" % category, "".join(sample)
        yield "category %s spaced" % category, " ".join(sample)
        yield "category %s in words" % category, " ".join("a%sb %sX'S" % (c, c) for c in sample[:100])
    yield "cjk", "数据库连接失败，请检查配置文件。日本語のテキストと한국어 텍스트도 있습니다。"
    yield "emoji", "👩🏽‍💻 👨‍👩‍👧‍👦 🏳️‍🌈 👍🏿👍🏻 ✋🏼 🇺🇸🇯🇵 ❤️‍🔥 1️⃣ #️⃣"
    yield "combining", "é ạ̈ Z̶̷̸ क्ष กิ่ ́̂abc"
    yield "rtl", "שלום עולם مرحبا بالعالم ‏abc‎ 123"
    yield "line ends", "a\r\nb\rc\n\rd\x85e f g\x0bh\x0ci\r\n\r\n\n  \r\n\t\r\n"
    yield "tabs", "\tdef f():\n\t\treturn 1\n\t \t x\t\t\n \t"
    yield "odd spaces", "a\xa0b c　d e f g\x1ch\x1d\x1e\x1fi ​j"
    yield "lone surrogate", "a\ud800b \udfff c"
    yield "contractions", ("He'S she'LL we'Re you'VE I'M they'D it'T don't WON'T 's 'Ll ' 's 'S"
                           " x'ſ X'K ſ'ſ DON'T'S rock'n'roll ''s 'sss 'lll 'dd 's'")
    yield "numbers", " ".join(str(10 ** n + n) for n in range(60)) + " 3.14159 1e10 0x1F -42 ١٢٣ Ⅲ½"


def long_runs():
    for char in [" ", "-", "=", "0", "9", "a", "A", "\n", "\t", "\r\n", ".", "*", "#", "一", "́",
                 "\xa0", "é", "😀", "/", "'", "_"]:
        for size in (1, 2, 3, 7, 64, 127, 128, 129, 1000, 4097):
            yield "run %r x %d" % (char, size), char * size
            yield "run %r x %d framed" % (char, size), "x" + char * size + "y"
    yield "spaces then newline", " " * 3000 + "\n" + " " * 3000 + "x"
    yield "digits", "".join(str(i % 10) for i in range(5000))


def random_strings():
    rng = random.Random(0xB9E)
    for n in range(300):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 300)))
        yield "random bytes %d" % n, data.decode("utf-8", "replace")
    for n in range(300):
        yield "random code points %d" % n, "".join(
            chr(rng.choice([rng.randrange(0x80), rng.randrange(0x800), rng.randrange(0x10000), rng.randrange(0x110000)]))
            for _ in range(rng.randrange(1, 200))).encode("utf-8", "replace").decode("utf-8")


ALPHABET = (list("aeiouxyzAEIOUXYZsStTlLdDmMrReEvV0123456789 \t\n\r'\"/\\.,;:!?-_=+*#@()[]{}<>|&%$^~`")
            + ["'s", "'S", "'ll", "'LL", "'re", "'Ve", "'d", "'M", "\r\n", "  ", "ſ", "K", "\x85",
               " ", "\xa0", "　", "\x1c", "é", "é", "́", "ß", "ǅ", "ʰ",
               "中", "文", "あ", "가", "א", "ب", "क्", "١", "Ⅲ",
               "½", "😀", "👍🏽", "‍", "️", "\U0001f1fa", "\U00010400", "\U0001d400", "—", "§",
               "�", "\x00", "\x7f", "​"])


def fuzz_cases(seed, count, longest):
    rng = random.Random(seed)
    for n in range(count):
        size = rng.randrange(1, longest)
        weights = [rng.random() ** 3 for _ in ALPHABET]
        yield "fuzz seed %d case %d" % (seed, n), "".join(rng.choices(ALPHABET, weights, k=size))


class PurePythonTests(unittest.TestCase):
    def test_token_ids_captured_from_tiktoken(self):
        for text, ids in EXPECTED.items():
            with self.subTest(text=text):
                self.assertEqual(encode(text), ids)
                self.assertEqual(count(text), len(ids))

    def test_ranks_are_line_numbers(self):
        # token_gate.load_ranks relies on it rather than parsing the rank column.
        import gzip
        raw = gzip.decompress(Path(token_gate.VOCABULARY).read_bytes())
        ranks = [int(line.split()[1]) for line in raw.splitlines()]
        self.assertEqual(ranks, list(range(len(ranks))))

    def test_every_byte_is_a_token(self):
        ranks = encoder()._ranks
        self.assertTrue(all(bytes([b]) in ranks for b in range(256)))

    def test_category_table_covers_every_code_point_once(self):
        self.assertEqual(pure_bpe._category(0x10FFFF), ".")
        self.assertEqual(pure_bpe._category(ord("A")), "U")
        self.assertEqual(pure_bpe._category(0x4E00), "o")
        self.assertEqual(pure_bpe._category(0x0301), "m")
        self.assertEqual(pure_bpe._category(0x0661), "n")

    def test_category_table_is_unicode_16(self):
        if unicodedata.unidata_version != pure_bpe.UNICODE_VERSION:
            self.skipTest("this Python has Unicode %s; the table is checked against %s by Python 3.14"
                          % (unicodedata.unidata_version, pure_bpe.UNICODE_VERSION))
        self.assertEqual(pure_bpe._CATEGORIES, pure_bpe_tables.categories())

    def test_white_space_is_the_unicode_property(self):
        # Python's str.isspace adds the four information separators.
        spaces = {c for c in map(chr, range(0x110000)) if c.isspace()}
        self.assertEqual(spaces - pure_bpe._WHITE_SPACE, set("\x1c\x1d\x1e\x1f"))
        self.assertEqual(pure_bpe._WHITE_SPACE - spaces, set())

    def test_scan_and_heap_merges_agree(self):
        enc = encoder()
        rng = random.Random(7)
        pieces = [b" " * n for n in (2, 3, 50, 129, 500)] + [b"-" * 300, b"ab" * 200, b"0123456789" * 40]
        pieces += [bytes(rng.choice(b"aaabbcde ") for _ in range(rng.randrange(2, 300))) for _ in range(300)]
        pieces += ["".join(rng.choice("éaß中😀") for _ in range(rng.randrange(1, 80))).encode() for _ in range(200)]
        for piece in pieces:
            self.assertEqual(enc._scan_merge(piece), enc._heap_merge(piece), piece)

    def test_long_piece_is_refused(self):
        with self.assertRaises(ValueError):
            count("x" + "a" * (pure_bpe.MAX_PIECE_BYTES + 1))
        self.assertGreater(count("a " * pure_bpe.MAX_PIECE_BYTES), 0)

    def test_many_long_pieces_are_refused(self):
        run = pure_bpe.MAX_MERGE_BYTES // 3
        with self.assertRaises(ValueError):
            count("".join("x" + c * run for c in "a-=_"))
        # Each distinct long piece is merged once; repeats come from the cache.
        self.assertGreater(count(("x" + "-" * run) * 8), 0)

    def test_corrupt_vocabulary_is_refused(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "vocab.gz"
            import gzip
            bad.write_bytes(gzip.compress(b"YQ== 0\n"))
            with patch.object(token_gate, "VOCABULARY", bad):
                with self.assertRaises(ValueError):
                    pure_bpe.encoder()


def exhaustive():
    """The slow sweeps run only with ORK_SLOW_TESTS=1: in CI, ci.yml's
    unit (ubuntu-latest) job sets it, so the sweep runs once per ci run
    (#140), and so do the release workflows' verify jobs."""
    return os.environ.get("ORK_SLOW_TESTS") == "1"


def sample_code_points():
    """Every code point when exhaustive(); otherwise every 64th, ASCII and
    Latin-1, the white-space set, and both ends of every run in the category
    table, so each class and each class change is still compared."""
    if exhaustive():
        return range(0x110000)
    points = set(range(0, 0x110000, 64)) | set(range(0x100)) | {ord(c) for c in pure_bpe._WHITE_SPACE}
    start = 0
    for length in re.findall(r"[Ulomn.]([0-9a-f]+)", "".join(pure_bpe._CATEGORIES)):
        end = start + int(length, 16)
        points |= {start, end - 1}
        start = end
    assert start == 0x110000, "category table must cover every code point"
    return sorted(points)


@unittest.skipIf(tiktoken_encoding() is None,
                 "tiktoken %s is not installed for this Python; the differential test needs it" % token_gate.VERSION)
class DifferentialTests(unittest.TestCase):
    cases = 0
    characters = 0
    mismatches = 0

    @classmethod
    def setUpClass(cls):
        cls.tiktoken = tiktoken_encoding()

    @classmethod
    def tearDownClass(cls):
        print("\npure_bpe differential: %d cases, %d characters, %d mismatches" % (
            DifferentialTests.cases, DifferentialTests.characters, DifferentialTests.mismatches), file=sys.stderr)

    def compare(self, corpus):
        for name, text in corpus:
            DifferentialTests.cases += 1
            DifferentialTests.characters += len(text)
            expected = self.tiktoken.encode_ordinary(text)
            actual = encode(text)
            if actual != expected:
                DifferentialTests.mismatches += 1
                small = shrink(self.tiktoken, text)
                self.fail("%s: pure_bpe disagrees with tiktoken; minimal reproducer %r: tiktoken %r, pure_bpe %r"
                          % (name, small, self.tiktoken.encode_ordinary(small), encode(small)))

    def test_repository_files(self):
        self.compare(repository_texts())

    def test_fixture_payloads(self):
        self.compare(fixture_payload_texts())

    def test_synthetic_logs(self):
        self.compare(synthetic_logs())

    def test_unicode(self):
        self.compare(unicode_texts())

    def test_every_code_point_in_context(self):
        # Each code point in contexts that tell its class apart, a batch at a
        # time. The full sweep (about 28 s) runs with ORK_SLOW_TESTS=1, which
        # one CI job sets; other runs check a sample that keeps every class
        # boundary.
        points = [p for p in sample_code_points() if not 0xD800 <= p < 0xE000]
        for start in range(0, len(points), 2048):
            batch = map(chr, points[start:start + 2048])
            self.compare([("code points from U+%04X" % points[start], "".join(
                "a%sb %sx A%sa %s%s1 1%s2 %s's\t%sA %s\n" % ((c,) * 9) for c in batch))])

    def test_long_runs(self):
        self.compare(long_runs())

    def test_random_strings(self):
        self.compare(random_strings())

    def test_seeded_fuzz(self):
        for seed in range(4):
            self.compare(fuzz_cases(seed, 1000, 120))
        self.compare(fuzz_cases(99, 40, 20000))


if __name__ == "__main__":
    unittest.main()
