"""Offline, pinned payload token gate; never a provider-usage measurement.

The vocabulary ships as public package data. Loading it does not fetch data,
change environment variables, or resolve a model name.

Tokens are counted by the first encoder in ``EXACT_ENCODERS`` that loads: the
pinned ``tiktoken``, else the pure-Python ``pure_bpe``, which gives the same
tokens without any dependency, more slowly. When neither loads (in practice, a
vocabulary that fails its check) the gate does not refuse everything: it falls back to a byte-count gate that admits a
replacement only when bounds on the pinned ``o200k_base`` count prove the
saving (#115). It is sound because:

* Lower bound on the original. No vocabulary token is longer than
  ``MAX_TOKEN_BYTES`` (128) bytes, and the tokens of a text concatenate to its
  UTF-8 bytes, so n bytes are at least ceil(n / 128) tokens.
* Upper bound on the replacement and context. ``encode_ordinary`` splits the
  text into pre-tokenization pieces and byte-pair-encodes each one: a piece
  starts as one part per byte and merges only join parts, so it never ends with
  more tokens than it has bytes. That needs every single byte to be a token,
  which holds for the pinned vocabulary (all 256 are; a test checks it, and the
  SHA-256 pin fixes it). So n bytes are at most n tokens.

The fallback admits only if the smallest original lower bound is at least the
largest replacement upper bound plus the context's upper bound plus
``RESERVE``. Each upper bound is at least the exact count, so the exact gate
would then admit through its own size-bound path: the fallback never admits a
reply the exact gate refuses. It applies the same input limits as ``count``,
and refuses where they would. It admits far fewer replies: an original must be
roughly 128 times the replacement and its note, so a 12 KiB Bash preview needs
about 1.6 MB of output.
"""
import json
import os
import re
from functools import lru_cache

VERSION = "0.14.0"
VOCAB_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
VOCABULARY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "o200k_base.tiktoken.gz")
PATTERN = (
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?"
    r"|[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?"
    r"|\p{N}{1,3}| ?[^\s\p{L}\p{N}]+[\r\n/]*|\s*[\r\n]+|\s+(?!\S)|\s+"
)


def load_ranks():
    """The bundled vocabulary as {token bytes: rank}, for every exact encoder.

    Raises ValueError when it fails its SHA-256 pin or is malformed, OSError
    when it can't be read. Only an encoder loader calls it, so a hook that
    never tokenizes never imports what it needs (#190)."""
    import binascii
    import hashlib
    import zlib
    with open(VOCABULARY, "rb") as stream:
        raw = zlib.decompress(stream.read(), 47)   # 47: a gzip member
    if hashlib.sha256(raw).hexdigest() != VOCAB_SHA256:
        raise ValueError("token vocabulary integrity check failed")
    # The SHA-256 pin above fixes every byte: line i is token i (a test checks
    # it), so the rank column is not parsed and per-token base64 validation
    # adds nothing. Decoding in bulk halves the cold-start cost, which every
    # replacing hook pays once per process against its budget (#140).
    fields = raw.split()
    size = len(fields) // 2
    if len(fields) % 2 or size != raw.count(b"\n"):
        raise ValueError("token vocabulary is malformed")
    ranks = dict(zip(map(binascii.a2b_base64, fields[0::2]), range(size)))
    if len(ranks) != size:
        raise ValueError("token vocabulary is malformed")
    return ranks


def tiktoken_encoder():
    """The pinned tiktoken over the bundled, hash-checked vocabulary."""
    import importlib.metadata
    if importlib.metadata.version("tiktoken") != VERSION:
        raise ValueError("pinned tiktoken dependency unavailable")
    # Before the vocabulary: without the module, fail without paying to decode it.
    import tiktoken
    return tiktoken.Encoding("o200k_base", pat_str=PATTERN, mergeable_ranks=load_ranks(),
                            special_tokens={"<|endoftext|>": 199999, "<|endofprompt|>": 200018})


# The exact-encoder chain, tried in order; the first that loads counts tokens.
# Each entry is a loader that takes no arguments and returns an object whose
# encode_ordinary(text) gives exactly the pinned o200k_base tokens of text, or
# raises when it can't. When none loads, permits() falls back to the byte-count
# gate, which depends on no encoder: only on the vocabulary bounds in the module
# docstring.
def pure_python_encoder():
    """The standard-library o200k_base encoder (#115); tests/pure_bpe_regressions.py
    checks it against tiktoken."""
    import pure_bpe
    return pure_bpe.encoder()


EXACT_ENCODERS = (tiktoken_encoder, pure_python_encoder)


@lru_cache(maxsize=1)
def _first_encoder():
    # A failure is cached too: a hook process tries each loader at most once,
    # and never pays the vocabulary load again for every gate call.
    failure = None
    for load in EXACT_ENCODERS:
        try:
            return load(), None
        except Exception as error:
            failure = error
    return None, failure


def encoding():
    """The first exact encoder in EXACT_ENCODERS that loads, once per process."""
    encoder, failure = _first_encoder()
    if encoder is None:
        raise ImportError("no exact o200k_base encoder loads") from failure
    return encoder


encoding.cache_clear = _first_encoder.cache_clear


# Longest token in the pinned vocabulary, in bytes. The SHA-256 pin fixes it;
# a test recomputes it. Every token decodes to at most this many bytes, so a
# text of n UTF-8 bytes has at least ceil(n / MAX_TOKEN_BYTES) tokens.
MAX_TOKEN_BYTES = 128
RESERVE = 512
# Bounds the tokenizer's time, not its correctness: the pinned tiktoken merges
# long pre-tokenization pieces in near-linear time (a 256 KiB single piece
# encodes in about 50 ms), so no per-piece scan precedes it. That scan cost
# four times the encoding itself (#130).
MAX_CHARS = 2 * 1024 * 1024
# A whitespace run of about a million characters overflows the pinned
# tiktoken's regex stack and raises a Rust panic, which is a BaseException.
# Refuse long runs before encoding; the original output is kept.
MAX_WHITESPACE_RUN = 512 * 1024
_WHITESPACE = re.compile(r"\s+")


def _bounded(data):
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    if len(text) > MAX_CHARS:
        raise ValueError("output exceeds offline tokenization bound")
    if any(m.end() - m.start() > MAX_WHITESPACE_RUN for m in _WHITESPACE.finditer(text)):
        raise ValueError("whitespace run exceeds offline tokenization bound")
    return text


def count(data):
    return len(encoding().encode_ordinary(_bounded(data)))


def counter():
    """``(count, exact)``: a function giving the tokens of a text, and whether
    they are exact. With an exact encoder it counts with it (the caller keeps
    the text within ``bounded``); without one it gives ceil(UTF-8 bytes / 4),
    the ledger's heuristic, which neither bound in the module docstring
    proves: use it to choose a reduction, never to admit one (``permits``
    does that)."""
    try:
        encode = encoding().encode_ordinary
    except Exception:
        return (lambda text: -(-len(text.encode("utf-8")) // 4)), False
    return (lambda text: len(encode(text))), True


def bounded(text):
    """Whether ``count`` would take ``text``: within ``MAX_CHARS`` and no
    whitespace run long enough to overflow the tokenizer."""
    try:
        _bounded(text)
        return True
    except (ValueError, UnicodeError):
        return False


def lower_bound(text):
    """Fewest tokens text can have: proven from its size, without tokenizing."""
    return -(-len(text.encode("utf-8")) // MAX_TOKEN_BYTES)


def upper_bound(data):
    """Most tokens text can have: one per UTF-8 byte (see the module docstring).

    Raises where ``count`` would refuse the text, so a fallback decision never
    admits text the exact gate could not count."""
    return len(_bounded(data).encode("utf-8"))


def available():
    """Whether an exact encoder loads; the exact gate needs one."""
    try:
        encoding()
        return True
    except Exception:
        return False


def _layouts(value):
    # Compact and indented JSON; one entry when they are equal, as for a string.
    return list(dict.fromkeys(json.dumps(value, ensure_ascii=False, indent=indent)
                              for indent in (None, 2)))


def permits(before, after, context=""):
    """Charge replacement metadata and notes; refuse uncertain/small savings.

    Compare the smaller original serialization with the larger replacement
    serialization. Native rendering is separately qualified by host fixtures.

    The replacement and context are bounded, so they are counted exactly. The
    original is tokenized only when its size alone cannot prove the saving,
    each distinct layout at most once, stopping at the first that falls short
    (#130). A large original is never tokenized, nor bound by count's limits.
    The compact layout is never longer than the indented one, so its size
    bound is the smaller: the indented layout is built only to be counted (#190).

    Without an exact encoder, every count is replaced by its bound (the module
    docstring proves both) and only the size-bound path can admit (#115).
    """
    try:
        exact = available()
        measure = count if exact else upper_bound
        needed = max(measure(text) for text in _layouts(after)) + (measure(context) if context else 0) + RESERVE
        compact = json.dumps(before, ensure_ascii=False)
        if lower_bound(compact) >= needed:
            return True
        if not exact or count(compact) < needed:
            return False
        indented = json.dumps(before, ensure_ascii=False, indent=2)
        return indented == compact or count(indented) >= needed
    except Exception:
        return False
    except BaseException as error:  # tiktoken's PanicException: refuse, never kill the hook
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return False


# hook.run gates every reply after its handler returns. A handler that needs
# the verdict first (trim-tool-output.py writes its full-output file only for
# an admitted reply, #130) would otherwise pay for the gate twice, which costs
# about 7 ms per MB of original output. One verdict is kept: for the same
# original response object and an equal replacement and context.
_last_verdict = None


def permits_reply(payload, reply):
    global _last_verdict
    out = reply.get("hookSpecificOutput", {}) if isinstance(reply, dict) else {}
    if "updatedToolOutput" not in out:
        return True
    before = payload.get("tool_response")
    try:
        key = json.dumps([out["updatedToolOutput"], out.get("additionalContext", "")], sort_keys=True)
    except Exception:
        key = None
    last = _last_verdict
    if key is not None and last is not None and last[0] is before and last[1] == key:
        return last[2]
    verdict = permits(before, out["updatedToolOutput"], out.get("additionalContext", ""))
    _last_verdict = (before, key, verdict) if key is not None else None
    return verdict
