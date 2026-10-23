# Evidence behavior contract v1

`v1/fixtures.json` is a synthetic, offline golden corpus shared byte-for-byte
with the Ork Meter for Codex plugin's `contracts/` directory. The manifest pins its
SHA-256 and the first shared production module, `evidence_formats`. A checker
refuses a changed runtime copy before executing the fixtures. This directory
is a test dependency; it registers no skills, hooks, MCP servers or model tools.

The canonical format guard lives in `hooks/lib/evidence_formats.py`; Codex
vendors that exact file at `ork_codex/evidence_formats.py`. Both reducers use
it. Claude explicitly recognizes its fixed log-tag vocabulary; Codex keeps
its conservative bracket-prefix bypass. Update both copies and the shared
manifest together, then rerun both adapters. Other reducer implementations
are still separate; this is the first shared engine component.

The 26 cases require small/uncertain formats to pass through byte-identically,
distinct diagnostics and skipped-security evidence to survive reduction,
soft-budget overflow to preserve evidence, exact decorated Python source with
LF/CRLF/CR/BOM/Unicode to match physical spans and hashes, ambiguous/stale source
to be refused, immutable originals to recover exactly and reject corruption,
and write/flush/abandonment to earn no reduction credit. Recovery is a signed
cost: 10,000 original bytes minus 1,000 emitted bytes minus 20,000 recovery
bytes yields -11,000 net bytes avoided. These are bytes, not billed tokens.

Artifact namespaces remain distinct:

| Adapter | Artifact identity and stored representation |
| --- | --- |
| Claude | `claude-structured-response-v1`: SHA-256 of canonical structured snapshot JSON, including original native response, completeness and received-text hash; exact text companion |
| Codex | `codex-raw-bytes-v1`: SHA-256 of raw stream bytes; local delivery records provide run/policy/recovery attribution |

Matching 64-character handle syntax does not make stores interchangeable.
Tests normalize only the listed behavior, never fabricate shared native tool
status, stream envelopes or provider usage. Scope, completeness, source version,
policy and reducer identity must remain truthful in each adapter. A successful
local write proves emission, not model consumption or billing.

Required follow-up contract groups are still unqualified here: complete command
status/stream/completeness envelopes, native permissions/async lifecycle,
session/agent/context-epoch reset behavior, and usage categories with explicit
partial/unavailable states. Existing adapter-specific tests provide additional
evidence; they do not turn this corpus into full cross-adapter qualification.
Extend shared golden fixtures before extracting those implementations.

Run each checkout independently; neither needs the other checkout installed:

```text
python -I -B contracts/check.py --adapter claude --root . --report .local/contract-claude.json
python -I -B contracts/check.py --adapter codex --root ../ork-meter-codex --report .local/contract-codex.json
```

The report includes the corpus/module hashes, every case ID and its adapter's
failures. No inference or real user configuration is used. Local regression
discovery runs the same checker and verifies that module drift cannot qualify.
Do not describe this corpus as native-host, economic or release acceptance.
