"""Prompt-cache report: where this project's Claude Code tokens went, and
which cache misses re-sent whole conversations.

Usage: cache_report.py [--cwd <dir> | --all] [--json]

Reads Claude Code's own transcripts (``$CLAUDE_CONFIG_DIR/projects`` or
``~/.claude/projects``): for this working directory by default, every
project with ``--all``. From each transcript it uses only the per-request
usage counters, timestamps, model and effort names, request-shape hashes and
compaction markers. It never reads message content and sends nothing anywhere.

Token counts are **measured**: they are the usage Claude Code recorded for
each request. Dollar figures are **estimates** at API list prices; on a
subscription they are relative weights, not a bill.

A cache miss is a request that wrote at least half its context to the cache
after an earlier request in the same transcript had cached a comparable
prefix and did not read most of it. Each miss gets the first matching cause:
compaction, idle past the cache lifetime, model switch, effort change,
request-shape change (tools, system prompt or MCP servers), or unexplained.
The first request of each transcript is reported separately, not as a miss.
"""

import argparse
import datetime
import glob
import json
import os
from pathlib import Path
import re
import sys

# $ per million tokens at API list prices (2026-10), by model id prefix:
# base input, cache read, output. Cache writes cost 1.25x base input
# (5-minute TTL) or 2x (1-hour). The longest matching prefix wins, so dated
# ids match their model. An unrecognised model is priced as Opus 5.5 and the
# report names it.
PRICES = {
    "claude-fable-5-1": (10.0, 0.25, 50.0), "claude-mythos-5-1": (10.0, 0.25, 50.0),
    "claude-fable-5": (10.0, 1.0, 50.0), "claude-mythos-5": (10.0, 1.0, 50.0),
    "claude-opus-5-5": (4.0, 0.20, 20.0), "claude-opus-5": (5.0, 0.50, 25.0),
    "claude-opus-4-8": (5.0, 0.50, 25.0), "claude-opus-4-7": (5.0, 0.50, 25.0),
    "claude-opus-4-6": (5.0, 0.50, 25.0),
    "claude-sonnet-5-5": (2.0, 0.20, 10.0), "claude-sonnet-5": (2.0, 0.20, 10.0),
    "claude-sonnet-4-6": (3.0, 0.30, 15.0),
    "claude-haiku-5-5": (0.10, 0.01, 0.50), "claude-haiku-4-5": (1.0, 0.10, 5.0),
}
# Models with a second rate card for long prompts: (threshold, prices). A
# request whose prompt (fresh input + cache reads + cache writes) is over the
# threshold is priced entirely at the long-prompt card.
LONG_PROMPT = {
    "claude-haiku-5-5": (100000, (0.50, 0.05, 2.50)),
}
FALLBACK_MODEL = "claude-opus-5-5"
MIN_CONTEXT = 8000      # smaller contexts are never counted as misses
MISS_SHARE = 0.5        # a miss writes at least this share of its context


def priced(model):
    """The PRICES key for a model id, or None when it is not recognised."""
    model = model or ""
    matches = [key for key in PRICES if model == key or model.startswith(key + "-")]
    return max(matches, key=len) if matches else None


def prices(name, prompt):
    """(base input, cache read, output) for a PRICES key and a prompt size."""
    threshold, card = LONG_PROMPT.get(name, (None, None))
    return card if threshold is not None and prompt > threshold else PRICES[name]


def parse_time(value):
    try:
        when = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    # Requests from different files are compared, so every time is aware.
    return when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)


def count(usage, key):
    value = usage.get(key)
    return value if type(value) is int and value >= 0 else 0


def project_dir(config, cwd):
    """Claude Code's transcript folder for a working directory (named from
    its real path: /tmp is /private/tmp on macOS)."""
    return os.path.join(config, "projects", re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd)))


def find_project(config, cwd):
    """The transcript folder for ``cwd`` or the nearest parent that has one,
    so the report works from a subdirectory of the project. (root, label)."""
    path = os.path.realpath(cwd)
    while True:
        candidate = project_dir(config, path)
        if os.path.isdir(candidate):
            return candidate, path
        parent = os.path.dirname(path)
        if parent == path:
            return project_dir(config, cwd), os.path.realpath(cwd)
        path = parent


USED_ROWS = ('"assistant"', '"api-request"', '"compact_boundary"')   # read_transcript's row types, as JSON strings


def read_transcript(path):
    """Requests (deduplicated by message id), request shapes and compactions."""
    requests, shapes, compactions = {}, [], []
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return [], [], []
    with handle:
        for line in handle:
            # Only these rows are used, so most lines (tool results, user
            # turns) are skipped unparsed. A JSON \u escape could spell one of
            # these strings another way: such a line is parsed anyway.
            if "\\u" not in line and not any(word in line for word in USED_ROWS):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            kind = row.get("type")
            when = parse_time(row.get("timestamp"))
            if kind == "api-request" and when:
                shapes.append((when, row.get("shapeHash")))
            elif kind == "system" and row.get("subtype") == "compact_boundary" and when:
                compactions.append(when)
            elif kind == "assistant" and when:
                message = row.get("message")
                usage = message.get("usage") if isinstance(message, dict) else None
                if not isinstance(usage, dict) or not message.get("id") or message.get("model") == "<synthetic>":
                    continue
                # Streamed blocks of one message repeat its usage; keep one.
                requests[message["id"]] = {"id": message["id"], "t": when, "model": message.get("model"),
                                           "effort": row.get("perTurnEffort") or row.get("effort"),
                                           "side": bool(row.get("isSidechain")), "usage": usage,
                                           "context": sum(count(usage, key) for key in (
                                               "cache_read_input_tokens", "cache_creation_input_tokens",
                                               "input_tokens"))}
    rows = sorted(requests.values(), key=lambda r: r["t"])
    shapes.sort(key=lambda s: s[0])
    index = 0
    shape = None
    for row in rows:
        while index < len(shapes) and shapes[index][0] <= row["t"]:
            shape = shapes[index][1]
            index += 1
        row["shape"] = shape
    return rows, shapes, compactions


def requests_in_order(paths):
    """(requests, transcripts): every request once, in time order, as (row,
    previous request in its stream or None, the stream's compactions), and the
    number of files with any request.

    A stream is one file's main or sidechain requests, ordered by time. A
    resumed or forked session copies earlier messages into a new file; each
    file is chained with its copies included, so the first new request after
    an idle resume follows the copied ones whatever order the files come in.
    Of a message's copies, the one with the earliest previous request is kept
    (ties by path), so the result does not depend on the order of ``paths``."""
    best, transcripts = {}, 0
    for path in paths:
        rows, _, compactions = read_transcript(path)
        transcripts += bool(rows)
        for side in (False, True):
            previous = None
            for row in (r for r in rows if r["side"] == side):
                rank = (previous is not None, previous["t"] if previous else 0, path)
                if row["id"] not in best or rank < best[row["id"]][0]:
                    best[row["id"]] = (rank, row, previous, compactions)
                previous = row
    ordered = sorted(best.values(), key=lambda b: (b[1]["t"], b[0]))
    return [entry[1:] for entry in ordered], transcripts


def analyse(paths):
    report = {"transcripts": 0, "requests": 0, "unpriced_models": [],
              "tokens": {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0},
              "cost": {"input": 0.0, "cache_read": 0.0, "cache_write": 0.0, "output": 0.0},
              "causes": {}, "first_requests": {"requests": 0, "tokens_written": 0, "write_cost": 0.0},
              "gaps": {"under_5_min": 0, "5_to_60_min": 0, "over_60_min": 0},
              "ttl_1h": {"write_premium": 0.0, "reads_kept": 0.0}}
    unpriced = set()
    requests, report["transcripts"] = requests_in_order(paths)
    for row, previous, compactions in requests:
        u = row["usage"]
        name = priced(row["model"])
        if name is None:
            unpriced.add(str(row["model"]))
        read, write, fresh, out = (count(u, "cache_read_input_tokens"), count(u, "cache_creation_input_tokens"),
                                   count(u, "input_tokens"), count(u, "output_tokens"))
        base, read_price, out_price = prices(name or FALLBACK_MODEL, read + write + fresh)
        split = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        hour = count(split, "ephemeral_1h_input_tokens")
        five = count(split, "ephemeral_5m_input_tokens") if split else max(write - hour, 0)
        write_cost = (five * 1.25 + hour * 2.0) * base / 1e6
        report["requests"] += 1
        for key, value in (("input", fresh), ("cache_read", read), ("cache_write", write), ("output", out)):
            report["tokens"][key] += value
        report["cost"]["input"] += fresh * base / 1e6
        report["cost"]["cache_read"] += read * read_price / 1e6
        report["cost"]["cache_write"] += write_cost
        report["cost"]["output"] += out * out_price / 1e6
        report["ttl_1h"]["write_premium"] += hour * 0.75 * base / 1e6
        context = row["context"]
        cause = None
        if previous is None:
            first = report["first_requests"]
            first["requests"] += 1
            first["tokens_written"] += write
            first["write_cost"] += write_cost
        else:
            gap = (row["t"] - previous["t"]).total_seconds() / 60
            band = "under_5_min" if gap <= 5 else ("5_to_60_min" if gap <= 60 else "over_60_min")
            report["gaps"][band] += 1
            if band == "5_to_60_min" and read:
                # A read the 1-hour TTL kept that a 5-minute one would have lost.
                report["ttl_1h"]["reads_kept"] += read * (1.25 * base - read_price) / 1e6
            # A miss re-wrote most of its context *and* failed to read most
            # of the prefix the previous request had cached; a context
            # that merely grew (a large tool result) reads its prefix.
            if (write >= MISS_SHARE * context and read < MISS_SHARE * previous["context"]
                    and context >= MIN_CONTEXT and previous["context"] >= MIN_CONTEXT):
                ttl = 60 if hour > five else 5
                if any(previous["t"] < c <= row["t"] for c in compactions):
                    cause = "after compaction"
                elif gap > ttl:
                    cause = "idle past the %d-minute cache" % ttl
                elif row["model"] != previous["model"]:
                    cause = "model switch"
                elif row["effort"] != previous["effort"]:
                    cause = "effort change"
                elif row["shape"] and previous["shape"] and row["shape"] != previous["shape"]:
                    cause = "tools, system prompt or MCP changed"
                else:
                    cause = "unexplained"
        if cause:
            entry = report["causes"].setdefault(cause, {"misses": 0, "tokens_written": 0, "write_cost": 0.0,
                                                         "largest": 0})
            entry["misses"] += 1
            entry["tokens_written"] += write
            entry["write_cost"] += write_cost
            entry["largest"] = max(entry["largest"], write)
    report["unpriced_models"] = sorted(unpriced)
    total = sum(report["cost"].values())
    report["estimated_cost"] = round(total, 2)
    for entry in report["causes"].values():
        entry["write_cost"] = round(entry["write_cost"], 2)
        entry["share_of_cost"] = round(entry["write_cost"] / total, 3) if total else 0.0
    report["first_requests"]["write_cost"] = round(report["first_requests"]["write_cost"], 2)
    report["cost"] = {k: round(v, 2) for k, v in report["cost"].items()}
    report["ttl_1h"] = {k: round(v, 2) for k, v in report["ttl_1h"].items()}
    return report


def render(report, scope):
    lines = ["Ork Meter cache report: %s" % scope]
    if not report["requests"]:
        lines.append("  No Claude Code transcripts with usage found.")
        return "\n".join(lines)
    total = report["estimated_cost"]
    lines.append("  %d transcripts, %d requests. Estimated cost at API list prices: $%.2f" %
                 (report["transcripts"], report["requests"], total))
    parts = [("cache writes", "cache_write"), ("cache reads", "cache_read"), ("output", "output"), ("input", "input")]
    lines.append("  Share of cost: " + ", ".join("%s %.0f%%" % (label, 100 * report["cost"][key] / total if total else 0)
                                                 for label, key in parts))
    lines.append("")
    lines.append("  %-38s %7s %16s %12s %8s" % ("Cache misses by cause", "misses", "tokens written", "est. cost", "of total"))
    for cause, entry in sorted(report["causes"].items(), key=lambda kv: -kv[1]["write_cost"]):
        lines.append("  %-38s %7d %16s %12s %7.1f%%" % (cause, entry["misses"], format(entry["tokens_written"], ","),
                                                       "$%.2f" % entry["write_cost"], 100 * entry["share_of_cost"]))
    if not report["causes"]:
        lines.append("  (none)")
    first = report["first_requests"]
    lines.append("  First request of each transcript (not a miss): %d, %s tokens written, $%.2f" %
                 (first["requests"], format(first["tokens_written"], ","), first["write_cost"]))
    gaps = report["gaps"]
    lines.append("")
    lines.append("  Gaps between requests: %d under 5 min, %d of 5-60 min, %d over 60 min." %
                 (gaps["under_5_min"], gaps["5_to_60_min"], gaps["over_60_min"]))
    ttl = report["ttl_1h"]
    if ttl["write_premium"]:
        lines.append("  1-hour cache: $%.2f extra write premium; it kept $%.2f of reads across 5-60 min gaps." %
                     (ttl["write_premium"], ttl["reads_kept"]))
    lines.append("")
    lines.append("  Token counts are measured from Claude Code's usage records. Dollar figures are estimates at API")
    lines.append("  list prices; on a subscription they are relative weights, not a bill. Message content is never read.")
    if report["unpriced_models"]:
        lines.append("  Priced as Opus 5.5 (unrecognised model): " + ", ".join(report["unpriced_models"]))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prompt-cache report from local Claude Code transcripts.")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--cwd", default=os.getcwd(), help="project directory (default: the current one)")
    scope.add_argument("--all", action="store_true", help="every project's transcripts")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks/lib"))
    import project_env
    config = project_env.config_dir(os.environ, os.path.expanduser("~"))
    if args.all:
        root, label = os.path.join(config, "projects"), "all projects"
    else:
        root, label = find_project(config, args.cwd)
    paths = sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))
    report = analyse(paths)
    report["scope"] = label
    print(json.dumps(report, indent=2) if args.json else render(report, label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
