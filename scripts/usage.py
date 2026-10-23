"""Normalize captured Claude transcripts or Codex app-server usage JSONL locally."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks/lib"))
from usage import UsageLedger


def _each_event(path, gaps, handle):
    """Call ``handle`` with each JSON line of ``path``. A line that isn't
    JSON or that ``handle`` refuses, and a file that can't be read, become
    gaps naming ``path``."""
    try:
        with open(path, encoding="utf-8") as stream:
            for line, text in enumerate(stream, 1):
                if not text.strip():
                    continue
                try:
                    handle(json.loads(text))
                except (ValueError, TypeError) as exc:
                    gaps.append({"stream": path, "line": line, "reason": str(exc)})
    except OSError as exc:
        gaps.append({"stream": path, "reason": exc.__class__.__name__})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=("claude", "codex"), required=True)
    parser.add_argument("--attribution", choices=("main", "subagent", "auxiliary", "unknown"), default="main")
    parser.add_argument("--scope", choices=("lifetime", "interval"), default="lifetime")
    parser.add_argument("--baseline", help="Codex starting usage JSONL for an interval report")
    parser.add_argument("--counter-epoch", default="lifetime", help="explicit counter series identity, not a file/attempt identity")
    parser.add_argument("--epoch-evidence", help="reset provenance required for a non-default counter epoch")
    parser.add_argument("files", nargs="+", help="JSONL capture files; overlapping provider counters merge")
    args = parser.parse_args(argv)
    if args.scope == "interval" and args.harness != "codex":
        parser.error("interval baselines apply to Codex cumulative counters")
    if args.baseline and args.scope != "interval":
        parser.error("--baseline requires --scope interval")
    if args.counter_epoch != "lifetime" and not args.epoch_evidence:
        parser.error("--counter-epoch requires --epoch-evidence for a reset")
    ledger = UsageLedger(args.scope)
    if args.baseline:
        def baseline(event):
            if isinstance(event, dict) and event.get("method") == "thread/tokenUsage/updated":
                ledger.begin_interval(event, str(Path(args.baseline).resolve()), args.attribution,
                                      counter_epoch=args.counter_epoch, epoch_evidence=args.epoch_evidence)
        _each_event(args.baseline, ledger.gaps, baseline)
    # Supplying the same path twice is not a second attempt.
    for path in dict.fromkeys(str(Path(p).resolve()) for p in args.files):
        _each_event(path, ledger.gaps, lambda event: ledger.add(
            args.harness, event, path, args.attribution,
            counter_epoch=args.counter_epoch, epoch_evidence=args.epoch_evidence))
    report = ledger.report()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
