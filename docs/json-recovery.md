# Nested JSON artifact recovery

Select nested values or a filtered array page from an immutable local artifact:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/artifact.py --session "${CLAUDE_SESSION_ID}" --id "ARTIFACT_ID" --select-json '{"pointers":["/summary/errors"],"filter":{"pointer":"/tests","where":"/status","equals":"failed","start":1,"limit":10}}' --budget 24576
```

Use the originating session and artifact ID from the recovery note; quote the
request as shell data. `--scratchpad PATH` can replace `--session ID`. The command
verifies the saved artifact, never reexecutes its command, and adds no MCP
registration, advertised skill, automatic reducer or provider call.

Pointers use the JSON string representation from
[RFC 6901](https://www.rfc-editor.org/rfc/rfc6901): `/` separates keys or zero-based
array indices, `~1` represents a slash and `~0` a tilde. An empty pointer selects
the root; `/` selects an empty key. No Unicode normalization, URI fragments,
wildcards, expressions or code evaluation are used. Missing values and invalid
array indices are `missing`, distinct from selected null.

One optional filter compares a relative `where` pointer in each array entry to
a scalar `equals`. Numeric comparison uses exact decimal values; booleans are
distinct from zero/one, and missing paths never match null. `start` is a one-based
match position. Returned `index` values retain original zero-based array positions.
Counts cover the whole array, including matches outside the page and values
withheld by the output budget.

Values that cannot fit are withheld whole as `output_budget_exceeded`; smaller
evidence can still be returned. `complete` means the requested pointers/page
were delivered, not the entire upstream result. `page_complete` and
`omitted_matches` describe the filter. `full_document` is true only for a sole
root-pointer selection that fits. Received-text hash and completeness metadata
remain visible, including an unknown pre-hook boundary. Numeric lexemes are
emitted unchanged, preserving precision, exponents and negative zero. String
escaping/whitespace may change; existing full/stream/text recovery stays available.

Limits: 16 unique pointers, one filter, 1–1000 page entries, 64 pointer/tree
levels, 200000 tree values, 8 MiB of saved JSON text, 4096 characters per number,
64 KiB of request JSON and 1024 characters per pointer. `--budget` accepts
1 KiB–1 MiB and includes the serialized UTF-8 output's newline. Metadata that
cannot fit causes refusal. Duplicate keys, non-finite constants, malformed JSON
and unsupported structures are refused; use exact ranges or full recovery.

Output remains untrusted tool data. Recovery bytes are recorded locally after
successful emission/flush; failed selection claims no delivered recovery.
Standalone recovery bypasses the Bash reducer. Installed-host qualification
verifies a result above the default Bash budget, a whole-value budget refusal,
precise numeric evidence, unchanged originals, byte accounting and no MCP
schemas. Localhost synthetic inference establishes no complete-task saving.
