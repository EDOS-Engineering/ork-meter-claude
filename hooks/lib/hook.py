"""The hook contract, in one place.

Every Python hook in hooks/ is a module with ``EVENTS`` (a tuple of
``hook_event_name`` values) and ``handle(ctx)``, which returns a reply dict
or None, and optionally ``FEATURE`` (a lever key in features.py), ``BUDGET``
(seconds), ``REDUCER`` (its circuit-breaker scope) and ``wanted(env)``. It
never writes to stdout itself. hooks/lib/run.py loads the module and calls
``run`` below, which:

* parse and guard: the payload is a JSON object on stdin whose
  ``hook_event_name`` is in ``EVENTS``; anything else ends the hook;
* early exit: ``wanted(env)`` is asked first, on the live environment; it may
  only stop a hook, never start one;
* entitlement, session pin, circuit: ``features.gate`` turns every lever off
  without Ork Meter Plus and applies the signed lever policy with it,
  ``policy.bind`` applies the session's pinned settings (a new pin records
  the policy's defaults, which is why the gate comes first) and keeps the
  gate's verdict, and ``circuit.bind`` skips a ``REDUCER`` quarantined for
  the session;
* mode: outside ``reduce`` the handler never runs (``shadow`` only captures),
  unless the hook is a guard rather than a saving (``every_mode``, the Write
  guard's ``EVERY_MODE``), which runs in every mode;
* opt-out: when ``FEATURE`` is off (``features.enabled``) the hook does nothing;
* session store: ``ctx.store()`` resolves the session's ``ork-meter``
  directory (hooks/lib/session.py) and records its transcript path, once;
* deadline: ``ctx.remaining()`` is the time left of ``BUDGET`` (1.5 s by
  default). A ready reply is printed while that budget lasts or, past it,
  until the emit deadline (hooks.json kills at 5 s), since dropping a
  finished reply that can still arrive in time only loses it; it is never
  dropped sooner than the budget alone would. The emit deadline is
  ``EMIT_LIMIT_FROM_LAUNCH`` after hooks/py.sh started, when it passed a
  plausible launch time (``launch_age``), else ``EMIT_LIMIT`` after run.py
  started;
* emission: the reply must pass ``circuit.validate_reply`` and the offline
  token gate; the builders below are the only place reply JSON is spelled
  out. Ledger rows from ``ctx.record`` are staged and committed only after
  the reply is written and flushed, so an unsent reply earns no savings;
* delta bases: unless the handler ran with the ``delta`` lever on, the result
  reached the model unchanged and ``delta.invalidate`` drops its stored base;
* fail-open: any exception anywhere is swallowed and nothing is printed,
  so Claude Code is never blocked. ``run`` always returns normally; the
  caller exits 0.
"""

import json
import os
import sys
import time

import features
import circuit
import ledger
import policy
import token_gate
import session
import trace

DEFAULT_BUDGET = 1.5
# Seconds after run.py started (before its imports) past which a finished
# reply is no longer printed, when hooks/py.sh's launch time is unknown:
# hooks.json kills a Python hook at 5 s, counted from before bash and the
# interpreter start, which this clock can't see. A cold Windows runner spends
# most of a second there, and a 2 s kill cancelled finished replies (#140),
# so the margin is a full second.
EMIT_LIMIT = 4.0
# The same limit counted from hooks/py.sh's first line ($EPOCHREALTIME, bash
# 5 and Git Bash), which sees bash and interpreter start-up: only the host's
# own spawn time is left unseen, so the margin under the 5 s kill is 0.5 s.
# A stalled Windows runner finished replies 4-5 s in and lost them to the
# 4.0 s limit counted from Python start (#140).
EMIT_LIMIT_FROM_LAUNCH = 4.5
# The variable py.sh passes the launch time in (seconds since the epoch). Not
# ORK_METER_*, which is reserved for settings (features.py); run.py removes
# it from the environment before anything else can see it.
LAUNCH_ENV = "ORK_HOOK_LAUNCHED_AT"
# A launch time older than this (inherited from a parent shell) or in the
# future (a clock step) is ignored.
LAUNCH_MAX_AGE = 30.0


def launch_age(value, now):
    """Seconds from hooks/py.sh's launch time ``value`` (the ``LAUNCH_ENV``
    text, seconds since the epoch) to ``now`` (``time.time()``), or None when
    it is missing, malformed or implausible: in the future or more than
    ``LAUNCH_MAX_AGE`` ago. The wall clock is used only here: the age is
    taken once, at run.py's start, and every later comparison is monotonic."""
    try:
        age = float(now) - float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError, OverflowError):
        return None
    return age if 0.0 <= age <= LAUNCH_MAX_AGE else None   # False for NaN


def emit_deadline(started, age=None):
    """The ``time.monotonic()`` past which a finished reply is dropped:
    ``EMIT_LIMIT_FROM_LAUNCH`` after the launch, ``age`` seconds before
    ``started``, or without it ``EMIT_LIMIT`` after ``started``."""
    if age is not None:
        return started - age + EMIT_LIMIT_FROM_LAUNCH
    return started + EMIT_LIMIT


class Context:
    """What one hook invocation knows: the environment, the payload, the
    event name and when its time is up."""

    def __init__(self, env, payload, event, deadline, defer_records=False, started=None, launch_age=None):
        self.env = env
        self.payload = payload
        self.event = event
        self.deadline = deadline
        self.started = started   # the hook process's start (time.monotonic()), when known
        self.launch_age = launch_age   # seconds from py.sh's launch to ``started`` (launch_age), when known
        self._store = None
        self._defer_records = defer_records
        self._pending_records = []
        self.session_policy_sha256 = None
        self.policy_sha256 = None
        self.reducer_health_sha256 = None

    def remaining(self):
        """Seconds left before the deadline, never negative."""
        return max(0.0, self.deadline - time.monotonic())

    def store(self):
        """The session's store (``session.Store``), its directory created on
        first use; the session's transcript path is remembered there at the
        same time."""
        if self._store is None:
            store = session.Store.for_payload(self.payload)
            store.remember(self.payload)
            self._store = store
        return self._store

    def record(self, row):
        """Stamp ``row``, built by one of the ledger's constructors
        (``ledger.trimmed``, ``ledger.loop``, ...), to this session's ledger,
        with time/session/tool-call identity. Production invocations stage rows until
        emission; direct Context users retain immediate-write behavior."""
        row = ledger.stamp(row, self.payload.get("session_id"), self.payload.get("tool_use_id"))
        if self.policy_sha256:
            row["policy_sha256"] = self.policy_sha256
        if self.reducer_health_sha256:
            row["reducer_health_sha256"] = self.reducer_health_sha256
        if self._defer_records:
            self._pending_records.append(row)
        else:
            ledger.append(self.store(), row)
        return row

    def commit_records(self, emitted):
        """Persist staged statistics after reply serialization and emission.

        An abandoned replacement has no successful byte interventions.
        Accounting failure after emission must not discard an already-safe
        replacement or silently present the surviving stage rows as complete.
        """
        rows = [row for row in self._pending_records if emitted or
                (row.get("kind") in ledger.KINDS and ledger.KINDS[row["kind"]].counts == "count")]
        self._pending_records = []
        for index, row in enumerate(rows):
            try:
                ledger.append(self.store(), row)
            except Exception as exc:
                # A failed accounting store cannot withdraw emitted data.
                try:
                    self.store().record_gap(self.event, self.payload.get("tool_name"), len(rows), index, exc, emitted,
                                            tool_call_sha256=ledger.sha256_hex(str(self.payload.get("tool_use_id", ""))))
                except Exception:
                    pass
                break


def updated_tool_output(event, updated, additional_context=None):
    """PostToolUse: replace the tool result, optionally with a note for Claude."""
    out = {"hookEventName": event, "updatedToolOutput": updated}
    if additional_context:
        out["additionalContext"] = additional_context
    return {"hookSpecificOutput": out}


def updated_input(event, new_input, additional_context=None):
    """PreToolUse: replace the tool input, optionally with a note for Claude."""
    out = {"hookEventName": event, "updatedInput": new_input}
    if additional_context:
        out["additionalContext"] = additional_context
    return {"hookSpecificOutput": out}


def denied(event, reason):
    """PreToolUse: refuse the tool call; ``reason`` is shown to Claude."""
    return {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def additional_context(event, text):
    """A note for Claude alongside the event, nothing else changed."""
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def system_message(text, stop=False):
    """A ``systemMessage`` for the person. ``stop`` adds ``"continue": false``
    (Claude stops until the person steps in); when it is a string it is
    also the ``stopReason``. ``text`` may be None for a stop alone."""
    out = {}
    if text is not None:
        out["systemMessage"] = text
    if stop:
        out["continue"] = False
        if isinstance(stop, str):
            out["stopReason"] = stop
    return out


def may_emit(ctx):
    """Whether a reply that is ready now should still be printed: while the
    budget lasts, or until the emit deadline (``emit_deadline``: 4.5 s after
    py.sh's launch when known, else 4.0 s after the hook process started). A
    handler that writes files a reply depends on asks this first, so it
    never writes for a reply hook.run would then drop."""
    if ctx.remaining() > 0:
        return True
    return ctx.started is not None and time.monotonic() < emit_deadline(ctx.started, ctx.launch_age)


def run(events, handler, feature=None, budget=DEFAULT_BUDGET, reducer=None, wanted=None, started=None,
        launch_age=None, every_mode=False):
    """Run one hook: parse, guard, opt-out, call ``handler(ctx)``, print its
    reply as one JSON line. Never raises, never prints on error.

    ``started`` is the hook process's start (``time.monotonic()``, taken by
    run.py before its imports); it defaults to now. ``launch_age`` is the
    plausible time from hooks/py.sh's launch to ``started`` (``launch_age``),
    or None."""
    started = time.monotonic() if started is None else started
    payload = None
    kept_bases = False   # the handler ran with the delta lever on
    try:
        env = os.environ
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("hook_event_name") not in events:
            return
        if wanted is not None and not wanted(env):
            return
        ctx = Context(env, payload, payload["hook_event_name"], time.monotonic() + budget, defer_records=True,
                      started=started, launch_age=launch_age)
        # Savings levers are an Ork Meter Plus feature: without it every lever
        # is off and the hooks pass Claude's results through untouched. The
        # gate comes before the pin so a new session's pin records the signed
        # policy's defaults; the verdict is carried through the pin's dicts.
        gated = ctx.env = features.gate(ctx.env)
        policy.bind(ctx, feature, publish=False)
        ctx.env = features.carried(ctx.env, gated)
        blocked = circuit.bind(ctx, reducer)
        env = ctx.env
        mode = features.mode(env)
        if mode != "reduce" and ctx.event in trace.EVENTS:
            if every_mode:
                pass   # a guard, not a saving, runs in every mode; the reducing hooks capture
            else:
                if mode == "shadow":
                    trace.capture(ctx)
                return
        if feature is not None and not features.enabled(env, feature):
            return
        if blocked:
            return
        try:
            reply = handler(ctx)
            kept_bases = features.enabled(ctx.env, "delta")
            circuit.validate_reply(ctx, reply)
            if not token_gate.permits_reply(payload, reply) or not may_emit(ctx):
                reply = None
        except Exception as exc:
            circuit.fault(ctx, ctx.reducer_scope, "invariant" if isinstance(exc, circuit.InvariantError) else "handler", exc)
            return
        emitted = False
        if isinstance(reply, dict) and reply:
            try:
                serialized = json.dumps(reply) + "\n"
            except Exception as exc:
                circuit.fault(ctx, ctx.reducer_scope, "serialization", exc)
                return
            sys.stdout.write(serialized)
            sys.stdout.flush()
            emitted = True
        ctx.commit_records(emitted)
    except Exception:
        pass
    finally:
        # Past the budget, the cleanup would eat into the margin before
        # hooks.json's kill; the base then stays, like a killed hook's.
        if not kept_bases and time.monotonic() - started < budget:
            _invalidate_delta_bases(reducer, payload)


# The hooks that keep delta bases (delta.KEEPERS). Checked here so the
# others never import delta, and with it difflib and trimmer.
DELTA_KEEPERS = ("trim-tool-output", "dedupe-reads")


def _invalidate_delta_bases(reducer, payload):
    """Fail-open and outside the circuit breaker: never blocks the reply."""
    if reducer not in DELTA_KEEPERS:
        return
    try:
        import delta
        delta.invalidate(reducer, payload, os.environ)
    except Exception:
        pass
