"""Summarize scalar hook records, without interpreting emission as delivery."""
import collections
import json
from pathlib import Path
import sys


def summarize(path):
    started, finished = {}, {}
    malformed = 0
    with Path(path).open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
                target = finished if row['phase']=='finished' else started
                target[row['invocation_id']] = row
            except (ValueError, KeyError, TypeError):
                malformed += 1
    groups = collections.defaultdict(list)
    for row in finished.values(): groups[row['hook']].append(row)
    return dict(schema='claude-hook-telemetry-summary-v1',
        started=len(started), finished=len(finished), unfinished=len(set(started)-set(finished)),
        malformed=malformed, coverage_complete=None,
        coverage_note='Shell-only hooks, killed-before-start hooks, storage failures and saturated logs are not fully observable',
        hooks={name:dict(decisions=dict(collections.Counter(r['decision'] for r in rows)),
            reasons=dict(collections.Counter(r['reason'] for r in rows)),
            emitted=sum(r['stdout_emitted'] for r in rows),
            model_delivery_confirmed=sum(r['model_delivery_confirmed'] is True for r in rows),
            model_delivery_unknown=sum(r['model_delivery_confirmed'] is None for r in rows),
            actual_waits=sum(r['effects'].get('waited_seconds',0)>.02 for r in rows),
            completions_collected=sum(r['effects'].get('background_completion_collected') is True for r in rows),
            elapsed_ms=sum(r['elapsed_ms'] for r in rows)) for name,rows in groups.items()})


if __name__=='__main__':
    print(json.dumps(summarize(sys.argv[1]),indent=2))
