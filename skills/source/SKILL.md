---
disable-model-invocation: true
name: source
description: Retrieve Python outlines, exact symbols, or bounded batches of source ranges and literal searches with content hashes. Other languages support exact ranges.
argument-hint: "<file> [symbol or line range]"
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/source.py *)
---

Use a local, read-only retrieval command when an outline would help locate evidence:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/source.py "PATH" --outline
```

For Python, retrieve exact source with `--symbol "Class.method"`. Include `--expected-hash "SHA256"` from the outline to refuse stale source. The result includes decorators and the complete body. For other languages or unsupported syntax, use normal lexical search and `--start N --limit N` to retrieve exact source lines. Quote all paths and values as shell data. Single retrieval reads only files inside the current working directory or Ork Meter's own tool-output store, symlinks resolved; for anything else it refuses, and the person's normal Read tool and permission rules apply instead.

Before editing, retrieve exact target source plus surrounding initialization, imports, related definitions and tests as needed. An outline alone does not establish behavior. Do not substitute an outline for an explicitly requested exact read. These commands never execute source or change files and use no model for parsing.

When independent selections are already known, return related source and tests in one read-only call:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/source.py --workspace "TASK_WORKSPACE" --batch '[{"id":"target","path":"units.py","action":"symbol","symbol":"millis","expected_hash":"SHA256"},{"id":"imports","path":"units.py","start":1,"limit":10},{"id":"test","path":"tests/test_units.py","action":"search","query":"millis","context":2}]'
```

Replace the example paths and digest with actual task evidence. Each request chooses `outline`, `symbol`, `lines` (default), or literal `search`. Batch paths resolve inside the explicit task workspace, independently of the plugin directory. At most 16 requests share one byte snapshot per file. Later requests should include `expected_hash` to refuse edits made since that snapshot. Search reports scanned lines and omitted matches; its context is exact source, not semantic dependency inference.

The default serialized output budget is 24 KiB (`--budget BYTES`). A symbol or range that cannot fit is withheld whole with `status: output_budget_exceeded` and its hash; it is never cut into unsafe edit source. Inspect every item and `complete`. Request a larger budget or use exact single-file retrieval for withheld items. Errors do not conceal other valid results. This batches retrieval only; verify the current hash before applying edits and run the relevant tests afterward.

For Python dependencies whose names are not yet known, use bounded static selection:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/source.py "service.py" --related "Handler.run" --workspace "TASK_WORKSPACE" --hops 1 --references "tests/test_service.py"
```

This returns the exact target and enclosing definition, referenced module assignments, same-file definitions and resolvable workspace imports, plus literal reference candidates from the explicitly supplied test files. It executes no source and uses no MCP. `--hops` is 0..2; `--fanout` is 1..16 (default 8). Inspect `issues`, omitted counts and withheld items. `complete` means the bounded selections fit without reported issues; `semantic_closure: false` always applies. Dynamic bindings, external libraries and ambiguous names require exact reads or lexical search. Every returned file has its own hash and snapshot; files are not captured atomically together. `--expected-hash` checks the target file only. Verify each edited file's current hash separately.
