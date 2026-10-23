---
name: explore
description: Ranked workspace search plus multi-file reads in one read-only call.
when_to_use: Call first when a task needs code you haven't read, before reading 3+ files, to find where a name is defined or used, or instead of find/grep/cat | head in Bash.
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/source.py --workspace "${CLAUDE_PROJECT_DIR}" --search *), Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/source.py --workspace "${CLAUDE_PROJECT_DIR}" --batch *)
---

Search the workspace once instead of several Grep and Read round-trips:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/source.py --workspace "${CLAUDE_PROJECT_DIR}" --search "QUERY" --glob "*.py" --context 1
```

The query is literal and case-sensitive; add `--regex` or `--ignore-case` when needed. `--glob` is optional and repeatable (`*.py`, `src/**`). Files are ranked: lines that define the query first, then paths containing it, then hit count.

The output is compact text. The first line gives the totals and says `complete` or `incomplete`. Each file then has a header line, `path sha256=<64 hex>`, with `[def]` when it defines the query and `+N hits not shown` beyond `--per-file` (default 8). Below the header come its lines: `N: text` for a hit, `N- text` for context (`--context` 0..5, default 1), and `--` between ranges. `N= other/path:M` means the same snippet was already shown for that file. `N:"..."` is a line with control characters, written as a JSON string. Only the first line, headers and the footer lack a leading line number. If the search stopped early (2 s limit, file cap) or the output budget filled (`--budget`, default 12 KiB), a final `incomplete:` line gives the reasons and counts. Files that don't fit are listed as `withheld`, never cut. Ignored, vendored, generated, binary, oversized and secret-looking files are skipped and counted, and symlinks are never followed. `--json` prints the same result as JSON.

Then read the exact ranges, symbols and tests you need in one call:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/source.py --workspace "${CLAUDE_PROJECT_DIR}" --batch '[{"id":"target","path":"src/units.py","action":"symbol","symbol":"millis","expected_hash":"SHA256"},{"id":"head","path":"src/units.py","start":1,"limit":20},{"id":"test","path":"tests/test_units.py","action":"search","query":"millis","context":2}]'
```

Replace the example query, paths and digest with real ones and quote them as shell data. Each request is `lines` (default), `symbol`, `outline` or a one-file literal `search`; at most 16 per call, 24 KiB of output by default. Items that do not fit are withheld whole with their hash. Secret-looking files (`.env`, keys, anything under `.ssh` and similar) are refused per item; read those with Read, under the person's permission rules.

Both commands are read-only: they never edit, execute source or use the network. Make changes with Edit or Write, and pass a file's `sha256` as `expected_hash` in a batch first to confirm it has not changed. Results are lexical evidence, not semantic analysis. Normal Read and Grep remain available.
