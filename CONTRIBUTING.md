# Contributing

Ork Meter for Claude Code is developed in this repository. Every change goes
through a pull request and the merge queue on `main`: `ci-gate` is the required
check, history stays linear, force-pushes and branch deletion are blocked, and
open review threads block merge. Releases are a separate, reviewed operation
([docs/release.md](docs/release.md)); nothing in CI publishes anything.

Keep output reduction local and deterministic. Add a regression case for a
demonstrated loss of diagnostic evidence, stale source, status, recovery,
concurrency or privacy. Preserve explicit full/exact reads, ordinary shell
permissions and the platform's strict observation contract. Use synthetic
fixtures; never commit real transcripts, raw provider exports, credentials,
`.local/` or customer configuration. Do not report byte reduction as observed
billing or quality improvement.

Security reports go to the address in [SECURITY.md](SECURITY.md), never to an
issue or pull request.

## Local dev loop

Setup, the development runtime build and `claude plugin validate` are in the [README's Development section](README.md#development). The loop itself:

1. **Run the plugin from this checkout:** `claude --plugin-dir .` loads the working tree instead of the installed plugin. Set `ORK_DEV_RUNTIME=1` to use a `.local/runtime` build.
2. **Debug hooks:** `claude --plugin-dir . --debug-file /tmp/ork-debug.log` turns debug mode on and writes the log, including each hook run, to that file. Add `--debug hooks` to keep only hook lines.
3. **Replay one hook against a fixture.** `tests/fixtures/` holds a synthetic payload per event, with no real output, paths or ids: `pretooluse.*`, `posttooluse.*`, `posttoolusefailure.bash.json`, `sessionstart.*`, `subagentstop.*`, `sessionend.json` and `postcompact.json`, which `python3 tests/fixtures/make-fixtures.py` regenerates, and the hand-written `session-start.startup.json`. Hooks read the payload on stdin through `hooks/py.sh`. Run them against throwaway state, and write an entitlement file, since the savings levers stay silent without one:

   ```sh
   (  # a subshell, so HOME and the rest don't stay set in your shell
     tmp=$(mktemp -d)
     export HOME="$tmp/home" XDG_STATE_HOME="$tmp/state" CLAUDE_PLUGIN_DATA="$tmp/data"
     mkdir -p "$HOME" "$CLAUDE_PLUGIN_DATA" "$tmp/scratch"
     printf '{"optimize":true,"state":"active","until":"2099-01-01T00:00:00Z","checked_at":"2026-10-14T12:00:00Z"}\n' \
       > "$CLAUDE_PLUGIN_DATA/entitlement.json" && chmod 600 "$CLAUDE_PLUGIN_DATA/entitlement.json"
     sed "s#__SCRATCH__#$tmp/scratch#g" tests/fixtures/posttooluse.bash.large.json \
       | bash hooks/py.sh trim-tool-output.py
   )
   ```

   Some fixtures also use `__FILE__` and `__TRANSCRIPT__` placeholders; `run_hook` in `tests/lib.sh` shows the full substitution.
4. **Run the tests:** `bash tests/run.sh` runs every `tests/*.test.sh` and `tests/*regressions.py`, with your `ORK_METER_*` settings cleared. `ORK_SLOW_TESTS=1 bash tests/run.sh` adds the full `pure_bpe` code-point sweep, which CI runs in one job, `unit (ubuntu-latest)`, and the release workflows' verify jobs run too. The sweep compares against tiktoken and is skipped without it, so first install the pinned version: `python3 -m pip install --require-hashes --only-binary=:all: -r requirements-hooks.txt`.
5. **Change a hook dependency** in `requirements-hooks.in`, never in `requirements-hooks.txt`: the latter is the hash-locked closure that `python3 .github/scripts/lock-hook-requirements.py` generates (needs [uv](https://docs.astral.sh/uv/) and PyPI). `--upgrade` re-locks at today's date to take newer dependency versions; `--check` re-resolves and fails on a stale lock, or on a missing wheel for a supported platform or Python. `tests/hook_requirements_lock_regressions.py` checks offline, on every test run, that the lock records the current `requirements-hooks.in` and hashes every package; `ORK_LOCK_CHECK=1` makes it run `--check` too. A changed lock changes the executable surface, so it needs a version bump (`docs/release.md`, "Version bumps").
6. **Qualify against the installed host** when a change touches what Claude Code receives (hook output shapes, recovery, skills): `RUN_NATIVE_CLAUDE_TESTS=1 python3 -I -B .github/scripts/check-native-host.py` runs every `tests/native_*qualification.py` suite except the connected one against the `claude` on your `PATH`, with localhost synthetic inference and fake credentials (no paid model calls). Skips fail it. Add `--report <path>` to save the JSON report, as CI does. Results are specific to the host version. The connected failure suite (`tests/native_failure_connected_qualification.py`) runs only in CI's Linux `connected-failure` job, which trusts a throwaway CA and starts a private gnome-keyring. Elsewhere it refuses to run.

## CI

`ci` (`.github/workflows/ci.yml`) runs on pull requests, in the merge queue, nightly on `main` and by hand; its `ci-gate` job is the required check. The design (#256, #258, #260):

- **No push run.** `main` changes only through the merge queue, which has already run `ci` on the same commit. The nightly run catches drift against `claude@latest` and the runner images.
- **Path filter.** The `changes` job (`.github/scripts/ci_paths.py`) diffs against the event's base. If every changed file is a root `*.md` or `LICENSE`, `unit`, `native-host`, `wsl` and `connected-failure` skip, and `ci-gate` accepts the skip. A change under `.github/`, or an unknown base, runs everything.
- **Lighter pull requests.** A pull request skips `native-host (macos-latest, claude 2.1.280)`; merge groups and nightly runs keep all six legs, so every combination qualifies before a change reaches `main`.
- **Merge-group reuse (#289).** In the merge queue, `changes` also runs `.github/scripts/ci_reuse.py`. It sets `reuse=true` only if all of these hold:
  - the group holds exactly one PR;
  - the merge-group commit's tree is the PR head's tree, so `ci.yml`, the scripts and the tests are identical;
  - the queue's base is an ancestor of the PR head, so the PR is up to date and its `pull_request` run tested this same tree;
  - that head has exactly one `pull_request` run of `ci`, completed and successful, and every job of its latest attempt succeeded, none skipped.

  Then `unit`, `wsl` and `connected-failure` skip, `native-host` runs only the macOS 2.1.280 leg the PR run left out, and `ci-gate` accepts the skips. The step summary of `changes` links the reused run. `runtime-provenance` and `release-channel` are cheap and depend on tags, attestations and the base, so they always run. Anything else means a full run: a PR behind `main`, a fork or docs-only run with skipped jobs, a failed, cancelled or re-running run, more than one run, or an API error. Pull requests, nightly and dispatched runs never reuse.
