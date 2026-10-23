"""Local usage normalization. Counters are observations, never dollar savings.

Claude message counters are per request; Codex app-server `total` counters
are cumulative per thread. Do not sum Codex `last` and `total`, and do not
add reasoning output to an output counter that already includes it.
"""
import copy

SCHEMA_VERSION = 2
CLAUDE_KEYS = ("input_tokens", "cache_read_input_tokens",
               "cache_creation_input_tokens", "output_tokens")
CODEX_KEYS = ("inputTokens", "cachedInputTokens", "outputTokens",
              "reasoningOutputTokens", "totalTokens")


def counters(value, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError("usage counters must be an object")
    for key in required:
        if key not in value:
            raise ValueError("usage counter missing: %s" % key)
    result = {}
    for key in required + optional:
        if key not in value:
            continue
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError("usage counter invalid: %s" % key)
        result[key] = value[key]
    return result


def identity(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("usage identity missing or invalid")
    return value


def reconcile(previous, current):
    """Select a whole monotone snapshot; reject contradictory updates.

    A delayed smaller snapshot is ignored. Never assemble maxima from
    incompatible observations, which could invent usage never reported.
    """
    if previous is None:
        return dict(current)
    common = previous.keys() & current.keys()
    up = any(current[k] > previous[k] for k in common)
    down = any(current[k] < previous[k] for k in common)
    if up and down:
        raise ValueError("conflicting usage snapshots")
    chosen = dict(previous if down else current)
    # Preserve optional fields when a later snapshot omits them. If the
    # cumulative counters changed, their absent optional values are unknown.
    if not up and not down:
        chosen.update(previous)
        chosen.update(current)
    return chosen


class UsageLedger:
    """Replay usage events without double counting or reading message content.

    `stream` identifies a capture file, not an independent counter series.
    Rotated/resumed captures of one provider identity merge within an epoch.
    Explicit reset provenance is required to count a reused identity anew.
    Interval reports need an observed baseline for each cumulative thread.
    """

    def __init__(self, scope="lifetime"):
        if scope not in ("lifetime", "interval"):
            raise ValueError("usage scope must be lifetime or interval")
        self.scope = scope
        self.records = {}
        self.gaps = []
        self.observations = 0
        self.baselines = {}

    def begin_interval(self, event, stream, attribution="main", *, counter_epoch="lifetime", epoch_evidence=None):
        """Use one known starting cumulative observation, never a guessed zero."""
        decoded = self._decode("codex", event, counter_epoch, epoch_evidence)
        if decoded is None:
            raise ValueError("baseline must be a Codex usage notification")
        key = decoded[0]
        if key in self.records:
            raise ValueError("interval baseline must precede its usage observations")
        self._add("codex", event, stream, attribution, counter_epoch, epoch_evidence, decoded)
        self.baselines[key] = copy.deepcopy(self.records[key])

    def _decode(self, harness, event, counter_epoch, epoch_evidence):
        identity(counter_epoch)
        if counter_epoch != "lifetime" or epoch_evidence is not None:
            identity(epoch_evidence)
        if not isinstance(event, dict):
            raise ValueError("usage event must be an object")
        if harness == "claude":
            if event.get("type") != "assistant":
                return None
            message = event.get("message")
            if not isinstance(message, dict):
                raise ValueError("assistant message missing")
            mid = identity(message.get("id") or event.get("requestId") or event.get("uuid"))
            raw = counters(message.get("usage"), CLAUDE_KEYS)
            provider_usage = message["usage"]
            key = (harness, counter_epoch, mid)
            semantics = "per-message"
        elif harness == "codex":
            if event.get("method") != "thread/tokenUsage/updated":
                return None
            params = event.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("tokenUsage"), dict):
                raise ValueError("Codex usage notification missing")
            thread = identity(params.get("threadId"))
            identity(params.get("turnId"))
            # `last` describes a request, not an additional billable counter.
            raw = counters(params["tokenUsage"].get("total"), CODEX_KEYS, ("cacheWriteInputTokens",))
            provider_usage = params["tokenUsage"]
            if raw["cachedInputTokens"] > raw["inputTokens"]:
                raise ValueError("cached input exceeds inclusive input")
            if raw["reasoningOutputTokens"] > raw["outputTokens"]:
                raise ValueError("reasoning output exceeds inclusive output")
            if raw["totalTokens"] != raw["inputTokens"] + raw["outputTokens"]:
                raise ValueError("Codex total does not reconcile with input and output")
            key = (harness, counter_epoch, thread)
            semantics = "cumulative-thread"
        else:
            raise ValueError("unsupported usage harness")
        return key, raw, provider_usage, semantics

    def add(self, harness, event, stream, attribution="main", *, counter_epoch="lifetime", epoch_evidence=None):
        return self._add(harness, event, stream, attribution, counter_epoch, epoch_evidence)

    def _add(self, harness, event, stream, attribution, counter_epoch, epoch_evidence, decoded=None):
        """``add``, given the event already decoded (``begin_interval``)."""
        identity(stream)
        if attribution not in ("main", "subagent", "auxiliary", "unknown"):
            raise ValueError("invalid usage attribution")
        decoded = decoded or self._decode(harness, event, counter_epoch, epoch_evidence)
        if decoded is None:
            return False
        key, raw, provider_usage, semantics = decoded
        previous = self.records.get(key)
        if previous and previous["attribution"] != attribution:
            raise ValueError("usage stream attribution changed")
        if previous and previous["epoch_evidence"] != epoch_evidence:
            raise ValueError("counter epoch provenance changed")
        candidate = raw
        raw = reconcile(previous["raw_usage"] if previous else None, candidate)
        if previous and raw != candidate:
            provider_usage = previous["provider_usage"]
            if harness == "codex":
                self.gaps.append({"identity": key[2], "counter_epoch": counter_epoch, "stream": stream,
                                  "reason": "lower cumulative snapshot; delayed capture versus counter reset is unverified"})
        streams = sorted(set((previous["capture_streams"] if previous else []) + [stream]))
        primary_stream = previous["stream"] if previous and raw != candidate else stream
        self.records[key] = {"harness": harness, "stream": primary_stream, "identity": key[2],
                             "attribution": attribution, "semantics": semantics, "raw_usage": raw,
                             "counter_epoch": counter_epoch, "epoch_evidence": epoch_evidence,
                             "capture_streams": streams,
                             "superseded_snapshots": (previous["superseded_snapshots"] if previous else 0)
                             + int(raw != candidate),
                             "provider_usage": copy.deepcopy(provider_usage)}
        self.observations += 1
        return True

    def report(self):
        records = []
        gaps = list(self.gaps)
        for key, record in sorted(self.records.items()):
            raw = record["raw_usage"]
            baseline = self.baselines.get(key)
            interval = self.scope == "interval" and record["harness"] == "codex"
            if interval:
                if baseline:
                    start = baseline["raw_usage"]
                    raw = {k: raw[k] - start[k] for k in raw.keys() & start.keys()}
                else:
                    gaps.append({"identity": record["identity"], "counter_epoch": record["counter_epoch"],
                                 "reason": "interval baseline missing; lifetime usage is not newly incurred work"})
            if record["harness"] == "claude":
                prompt = sum(raw[k] for k in CLAUDE_KEYS[:3])
                normalized = {"prompt_tokens": prompt, "uncached_input_tokens": raw["input_tokens"],
                              "cache_read_tokens": raw["cache_read_input_tokens"],
                              "cache_write_tokens": raw["cache_creation_input_tokens"],
                              "output_tokens": raw["output_tokens"], "reasoning_output_tokens": None,
                              "total_tokens": prompt + raw["output_tokens"]}
            else:
                normalized = {"prompt_tokens": raw["inputTokens"],
                              # Cache-write billing semantics are not established by the schema.
                              "uncached_input_tokens": None,
                              "cache_read_tokens": raw["cachedInputTokens"],
                              "cache_write_tokens": raw.get("cacheWriteInputTokens"),
                              "output_tokens": raw["outputTokens"],
                              "reasoning_output_tokens": raw["reasoningOutputTokens"],
                              "total_tokens": raw["totalTokens"]}
            if interval and not baseline:
                normalized = dict.fromkeys(normalized)
            records.append(dict(record, normalized=normalized,
                                interval_baseline=copy.deepcopy(baseline["raw_usage"]) if baseline else None))
        keys = ("prompt_tokens", "uncached_input_tokens", "cache_read_tokens", "cache_write_tokens",
                "output_tokens", "reasoning_output_tokens", "total_tokens")
        totals = {key: (sum(r["normalized"][key] for r in records)
                        if records and all(r["normalized"][key] is not None for r in records) else None)
                  for key in keys}
        return {"schema_version": SCHEMA_VERSION,
                "status": ("partial" if gaps else "ok") if records else "unavailable",
                "reporting_scope": self.scope,
                "scope": "supplied streams only; unobserved child/auxiliary work and retries are not inferred",
                "counter_note": "capture files are not counter epochs; smaller unordered snapshots are superseded, never inferred resets",
                "billing_status": "unverified; no price table or billed amount supplied",
                "observations": self.observations, "records": records, "totals": totals,
                "gaps": gaps}
