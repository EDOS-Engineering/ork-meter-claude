"""SubagentStop hook for the parked ork-meter agents: count and measure runs.

Registered for ``ork-meter:explore``, ``logs`` and ``test-runner``, which only
run when someone installs the definitions from docs/optional-agents/. The
payload carries no usage, so the hook sums the subagent's own transcript
(hooks/lib/measure.py, bounded to one second) and writes one ``haiku_agent``
ledger line with ``measured_*`` fields. Nothing here claims a saving.
"""

import os
import time

import ledger
import measure

EVENTS = ("SubagentStop",)
OUR_AGENTS = ("ork-meter:explore", "ork-meter:logs", "ork-meter:test-runner")


def handle(ctx):
    payload = ctx.payload
    agent = payload.get("agent_type")
    if agent not in OUR_AGENTS:
        return None
    measured = None
    transcript = payload.get("agent_transcript_path")
    if isinstance(transcript, str) and transcript:
        deadline = time.monotonic() + min(1.0, ctx.remaining())
        measured = measure.measure_transcript(os.path.expanduser(transcript), deadline)
    ctx.record(ledger.agent(agent, measured))
    return None
