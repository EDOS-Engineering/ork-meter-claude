"""Shared deterministic format guard, ork-evidence-behavior contract v1.

Canonical source: ClaudePlugin/hooks/lib/evidence_formats.py. CodexPlugin
vendors this exact file; the contract manifest verifies both copies' digest.
This module has no I/O, settings, model calls or harness dependencies.
"""
import re

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\ud800-\udfff]")
NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?\Z")
LOG_TAG = re.compile(r"\[(?:build|test|progress|info|debug|warn(?:ing)?|error|trace|fatal|pass|fail)\](?:\s|$)", re.I)


def unsupported_line_format(text, *, allow_log_tags=False):
    """Decline line elision for unsupported or uncertain received payloads.

Inspect syntax without parsing or reserializing. Broken structured prefixes
also pass through. Adapters may explicitly recognize the fixed log-tag set.
"""
    plain = ANSI.sub("", text)
    if CONTROL.search(plain):
        return True
    value = plain.lstrip("\ufeff \t\r\n")
    if value.startswith(("{", '"', "<", "---\n", "---\r\n")):
        return True
    if value.startswith("["):
        return not allow_log_tags or LOG_TAG.match(value) is None
    return (value.rstrip() in ("null", "true", "false", "NaN", "Infinity", "-Infinity")
            or NUMBER.fullmatch(value.rstrip()) is not None)
