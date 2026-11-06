Python hook diagnostics are written to the session store's
`hook-invocations.jsonl`. A benchmark can set `ORK_HOOK_TELEMETRY_DIR` to a
private persistent directory so records survive temporary-profile cleanup.
This also disables the shell's write-guard shortcut for complete Python-hook
coverage. Shell-only SessionStart hooks are outside this stream.

Each invocation has a start and finish record with the same ID, hook/event,
hashed session/tool-call identities, known tool name, decision/reason, duration,
JSON-serialized response byte counts, and scalar polling effects. A start with
no finish means incomplete observation, not success. Handler-no-reply means
no reported effect; it does not establish why an individual reducer abstained.
Counts of completed commands, actual waits, replacements and invocations must
remain separate. Byte counts are serialized JSON bytes, not provider tokens.

`stdout_emitted` records a successful write and flush. `model_delivery_confirmed`
remains null: native delivery or provider-boundary evidence is required before
claiming the model received it. Telemetry does not retain commands, arguments,
paths, tool contents, credentials or exception messages. The owner-only log is
bounded to 8 MiB (concurrent appends can exceed the bound by a few records);
after saturation further records are dropped. A saturated log cannot establish
complete coverage. Storage errors fail open and do not change hook replies.

Summarize a retained file with `python3 scripts/hook-telemetry.py PATH`.
The summary keeps starts, finishes, actual waits, collected completions and
emitted replies separate. It never upgrades unknown delivery or coverage.
