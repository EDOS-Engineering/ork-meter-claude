"""Opt-in local shadow capture, isolated from the hosted usage receiver.

Capture only hook-observed tool data; a host may already have truncated it.
One atomic file per observation avoids concurrent append/read-modify races.
Shadow capture never runs reducers, rewrites inputs, or reruns commands.
"""
import hashlib
import json
import time

import features
import session

SCHEMA_VERSION = 1
EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")


def capture(ctx):
    payload = ctx.payload
    document = {"schema_version": SCHEMA_VERSION, "harness": "claude",
                "harness_version": None, "plugin_version": features.plugin_version(ctx.env),
                "policy_version": "shadow-v1", "captured_at_ns": time.time_ns(),
                "provenance": "hook payload; completeness before native truncation unverified",
                "event": ctx.event, "session_id": payload.get("session_id"),
                "agent_id": payload.get("agent_id"), "tool_use_id": payload.get("tool_use_id"),
                "tool_name": payload.get("tool_name"), "tool_input": payload.get("tool_input"),
                "tool_response": payload.get("tool_response"), "error": payload.get("error"),
                "is_interrupt": payload.get("is_interrupt"), "cwd": payload.get("cwd")}
    raw = json.dumps(document, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    session.write_private(ctx.store().trace_path(digest), raw + "\n")
