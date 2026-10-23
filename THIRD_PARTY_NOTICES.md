# Third-party notices

Ork Meter for Claude Code is licensed by EDOS under a limited-use licence (see
[LICENSE](LICENSE)). It includes or depends on the third-party components
below, each under its own licence.

## Included in this repository

| Component | Where | Licence |
| --- | --- | --- |
| OpenAI `o200k_base` vocabulary, compressed | `hooks/lib/data/o200k_base.tiktoken.gz` | MIT ([`hooks/lib/data/LICENSE-tiktoken.txt`](hooks/lib/data/LICENSE-tiktoken.txt)) |
| tiktoken 0.9.0 splitting pattern and special-token definitions | `hooks/lib/pure_bpe.py` | MIT ([`hooks/lib/data/LICENSE-tiktoken.txt`](hooks/lib/data/LICENSE-tiktoken.txt)) |
| Go standard library and the Go modules linked into the runtime binaries | `runtime/<os>-<arch>/ork-meter-plugin-runtime` | BSD-3-Clause and the licences reproduced in [`runtime/THIRD_PARTY_NOTICES.txt`](runtime/THIRD_PARTY_NOTICES.txt) |

Provenance and the vocabulary's SHA-256 are in
[`hooks/lib/data/README.md`](hooks/lib/data/README.md).

## Installed from PyPI, not included

The hooks' optional tokenizer and its dependencies, pinned with hashes in
`requirements-hooks.txt` (`requirements-hooks.in` lists the direct ones):

| Package | Used by | Licence |
| --- | --- | --- |
| `tiktoken` | the hooks' exact token counts (optional) | MIT |
| `regex` | `tiktoken` | Apache-2.0 |
| `requests` | `tiktoken` | Apache-2.0 |
| `certifi` | `requests` | MPL-2.0 |
| `charset-normalizer` | `requests` | MIT |
| `idna` | `requests` | BSD-3-Clause |
| `urllib3` | `requests` | MIT |

Each package's licence ships with the package you install. Without them the
hooks use the bundled vocabulary with a pure-Python encoder.

## Runtime binaries

The five `runtime/<os>-<arch>/ork-meter-plugin-runtime` binaries are built
from `cmd/claude-plugin-runtime` of the Ork Meter platform by the
package-runtime workflow. They statically link the Go standard library and
the Go modules listed, with their licence texts, in
[`runtime/THIRD_PARTY_NOTICES.txt`](runtime/THIRD_PARTY_NOTICES.txt), which
`scripts/package_runtime.py` regenerates from the exact platform source each
release builds.
