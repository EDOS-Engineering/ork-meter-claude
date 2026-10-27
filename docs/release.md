# Releasing the plugin

How a release of the Ork Meter plugin for Claude Code is cut, what it
contains, how customers install it, and what the process does not cover.

## Shape of a release

The plugin's runtime is a Go program, `cmd/claude-plugin-runtime` in
`EDOS-Engineering/ork-meter` (not a public repository). Customers never build it. A release ships it inside the plugin as prebuilt
binaries:

```text
runtime/
  darwin-arm64/ork-meter-plugin-runtime      SHA256
  darwin-amd64/ork-meter-plugin-runtime      SHA256
  linux-amd64/ork-meter-plugin-runtime       SHA256
  linux-arm64/ork-meter-plugin-runtime       SHA256
  windows-amd64/ork-meter-plugin-runtime.exe SHA256
  THIRD_PARTY_NOTICES.txt                    licence texts of the Go modules linked into them
```

Each `SHA256` file holds the hex SHA-256 digest of the sibling binary (64
lowercase hex characters and a newline). That is exactly the layout
`scripts/runtime.sh` verifies and executes (see "How the runtime is
verified").

**A release is a tag on `main`, and customers install the tag the
marketplace names, not `main`.** There is no release branch. A release is one
commit on `main`, `release: plugin <version> with runtime from ork-meter
<ref>`, that holds exactly the five binaries, their `SHA256` files, their
`THIRD_PARTY_NOTICES.txt` and the new `version` in `.claude-plugin/plugin.json`, and nothing else. Its body
lists every target's SHA256. The `package-runtime` workflow opens it as a
pull request; a human merges it through the merge queue and puts a signed tag
**`v<version>`** on the merged commit ("Signed release tags"). A merge to `main` reaches no customer until a
reviewed pull request points the marketplace at a new tag ("Release
channel").

Between releases `main` carries the last released `runtime/`, and a source
checkout runs it too. A developer opts in to their own build in
`.local/runtime/` (built with `scripts/build_runtime.py`, README
"Development") with `ORK_DEV_RUNTIME=1`, and is told on stderr whenever it
runs (see "How the runtime is verified").

Why this shape, from the Claude Code plugin reference:

- A git-hosted plugin is cloned and copied to
  `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`. There is no
  per-platform asset field and no post-install hook, so every platform's
  binary travels in the same tree and `scripts/runtime.sh` picks the right
  one at run time.
- `/plugin marketplace add owner/repo` follows the default branch, `main`,
  for the marketplace file only. The plugin entry in it names a git source
  pinned to a release tag and its commit, so customers get that release, not
  whatever `main` carries ("Release channel").
- Git LFS is not supported, so the binaries are plain git objects. **Each
  release adds about 60 MB to `main`'s history** (five binaries of roughly
  11.6 to 12.6 MB each). Every full clone of `main` downloads all of them.
  Whether Claude Code clones a marketplace plugin shallowly, which would
  limit a customer install to the current binaries, has **not been
  verified**. Because each release is a single commit holding only the
  payload, a later size cap or history cleanup can find it; that cleanup is
  a future decision, not part of this process.
- `plugin.json` `version` controls cache invalidation. Every release bumps
  it; a release that reuses a version would not reach customers who already
  have that version cached.

## Installing a release

Customers add the EDOS marketplace (its file comes from `main`) and install
the plugin:

```text
/plugin marketplace add EDOS-Engineering/ork-meter-claude
/plugin install ork-meter@edos
```

The install is the commit the marketplace points at ("Release channel"):
`main` until the first tagged release, then that release tag. Updates arrive through the normal marketplace update path
once the marketplace is pointed at a newer tag. Claude Code does not
auto-update third-party marketplaces by default, so customers run
`/plugin marketplace update edos` and then update the plugin (`/plugin`, or
`claude plugin update ork-meter@edos`), or turn auto-update on for `edos`.

Each tagged release also has a GitHub Release `v<version>` on this repository
with the same plugin tree as a zip, each runtime binary, an SPDX SBOM, a
`.sha256` per asset, a `manifest.json` and build provenance attestations
("Publishing the GitHub Release", "SBOM"). It is the downloadable, verifiable form of the release, not a
second install channel: the marketplace install above is what customers use.

## Syncing from development

Development happens in the private repository EDOS-Engineering/claude-plugin.
Each version reaches this repository as a pull request before it is released
here:

1. The private release is cut and its tag `v<version>` is signed there.
2. A human dispatches this repository's **public-sync** workflow
   (`.github/workflows/public-sync.yml`) with `version`. It checks out the
   private repository at `v<version>` with `ORK_METER_READ_TOKEN` (read-only,
   `persist-credentials: false`), verifies the tag with main's
   `check-tag-signature.py` and `allowed_signers`, and runs that tag's
   `scripts/sync_public.py`. The private manifest decides which paths are
   mirrored, which this repository owns (LICENSE, README, SECURITY,
   CONTRIBUTING, notices, the ruleset, this file, the release workflows,
   `runtime/` and the marketplace pin are never touched) and which must never
   leave the private repository; any leak pattern refuses the whole run before
   a write. It then validates the plugin, runs `tests/run.sh`, pushes
   `sync/<version>` and opens **sync: &lt;version&gt; from &lt;private sha&gt;**
   with the workflow's own `GITHUB_TOKEN`. It never pushes to `main`, never
   tags and never publishes. If `main` already matches, it stops with a notice.
3. A human reviews and merges the sync pull request.
4. The release below is cut from the merged `main`.

## Cutting a release

Releases are cut by the `package-runtime` workflow
(`.github/workflows/package-runtime.yml`). It runs on **manual dispatch
only**, never on push, because publication is a reviewed operation
(CONTRIBUTING, "Agent-authored changes", condition 3: an agent never
builds, publishes or deploys a release). A human dispatches it, from
`main`, after the changes it will publish have been reviewed and merged.
The workflow never pushes to `main` and never creates a tag: it opens a
release pull request, and a human merges it and signs a tag on the merged
commit.

The workflow accepts dispatches only from `main`. Before building, its
read-only verification job runs `bash tests/run.sh` and checks the existing
GitHub `production` environment. A missing environment or weakened protections
fail the job and prevent publication; referencing an unprotected environment
does not suffice.

`production` must have the CEO or CSO as required reviewers, **prevent
self-review** enabled, administrator bypass disabled, and selected deployment
branches restricted to the single branch `main` (no tag policies). The package
job depends on verification and references this environment, so GitHub
requires approval before it can build or publish. This repository
configuration is separate from the workflow file, and a human makes it.

Dispatch inputs:

| input | meaning |
|---|---|
| `ork_meter_ref` | The ork-meter tag or commit to build the runtime from. It must contain `cmd/claude-plugin-runtime/main.go`; the workflow checks and fails fast otherwise. Prefer a tag or a full commit SHA over a branch name so the release is reproducible. |
| `plugin_version` | The plugin version to publish, semver (for example `0.3.0`). It is written to `.claude-plugin/plugin.json`, and the human who merges the release signs a tag `v0.3.0` on the merged commit. It must be new: an existing `v<version>` tag, or an existing `package-runtime/v<version>` branch, fails the run. |

What the workflow does, in order:

1. Checks out this repository at the dispatching ref (the branch chosen in
   the dispatch dialog; use `main`) with full history.
2. Checks out `EDOS-Engineering/ork-meter` at `ork_meter_ref` into
   `ork-meter/` using the `ORK_METER_READ_TOKEN` secret (below).
3. Validates the inputs: `plugin_version` is semver, tag `v<version>` and
   branch `package-runtime/v<version>` do not exist yet, and
   `ork-meter/cmd/claude-plugin-runtime/main.go` exists.
4. Installs the Go toolchain named by ork-meter's `go.mod`.
5. Runs `scripts/package_runtime.py`, which builds the five targets with
   `CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -buildid="` into
   `runtime/<os>-<arch>/`, writes each `SHA256`, prints each target and
   digest, and sets `version` in `.claude-plugin/plugin.json`. Right after
   the build, before any binary runs or anything is pushed, it attests build
   provenance for each binary with `actions/attest-build-provenance`
   (SHA-pinned; the `package` job has `id-token: write` and
   `attestations: write` for it). The attestation, stored on this
   repository, ties each digest to this run of `package-runtime.yml` on
   `main`; `publish-release` and CI refuse binaries without one ("How the
   runtime is verified").
6. Smoke-tests the linux-amd64 binary on the runner: it runs
   `ork-meter-plugin-runtime --version` directly, then `scripts/runtime.sh
   status` against a throwaway data directory, which exercises the
   digest check, install and exec path a customer's sessions and skills run. `status`
   with no connection is read-only and reports "not connected". This proves
   the binary starts; it is not platform acceptance for the other targets.
   The step unsets the job's OIDC token request variables first, so the
   binary cannot request a signing token.
7. Opens the release pull request: removes the `ork-meter/` checkout,
   creates the branch `package-runtime/v<version>` at the dispatching commit,
   stages `runtime/` and `.claude-plugin/plugin.json`, and refuses to go on
   if anything else is staged. It commits with the message `release: plugin
   <version> with runtime from ork-meter <ref>` and a body listing each
   target's SHA256, pushes that new branch without force (an existing branch
   is never replaced), and opens a pull request against `main` whose
   description lists the digests, states the history growth and fills in the
   attribution fields.
8. Writes the pull request, the commits and the digest table to the run
   summary.
9. **A human** reviews the release pull request and merges it through the
   merge queue. The merge queue rebases, so the release lands on `main` as
   the same single commit. Customers do not get it yet: the marketplace still
   points at the previous tag.
10. **A human** whose key is in `.github/allowed_signers` signs a tag on the
    merged commit on `main`, checks it with `main`'s copy of the check
    `publish-release` runs, and pushes it ("Signed release tags" has the
    one-time setup):

    ```sh
    git fetch origin main
    git checkout --detach origin/main          # main's allowed_signers and check script
    git tag -s v<version> <merged-commit> -m "Ork Meter plugin <version> (runtime from ork-meter <ref>)"
    python3 -I -B .github/scripts/check-tag-signature.py --tag v<version> --commit <merged-commit>
    git push origin v<version>
    ```

    `git tag -s` signs with SSH because of the one-time `gpg.format ssh`
    setting. `publish-release` refuses an unsigned tag, a lightweight one and
    one signed by a key that `.github/allowed_signers` does not list for the
    tagger's email.

11. **A human** dispatches the `publish-release` workflow from `main` with
    `plugin_version`, and a required reviewer approves the `production`
    deployment. It publishes the GitHub Release `v<version>` with the
    downloadable assets (next section), then opens the pull request
    `release: point the marketplace at v<version>` from the branch
    `release-channel/v<version>`.
12. **A human** records the release's canary evidence ("Per-release canary
    record" below) and attaches it to the GitHub Release.
13. **A human** installs the tag on each platform ("What this process does
    not cover"): re-runs the manual walkthroughs of the
    [platform matrix](platform-matrix.md) ([platform-walkthrough.md](platform-walkthrough.md);
    its "Release checklist" lists them) and records each one, then merges
    that pull request. **This is when customers get
    the release.** It is a controlled path (release configuration), so an
    agent never merges it.

The commit's trailers are `Dispatched-By: <GitHub user who dispatched>`,
`Agent: package-runtime`, `Agent-Version: github-actions` and `Model: none`;
`Dispatched-By` records the accountable human.

GitHub does not start workflows for a pull request that a workflow opened
with its own `GITHUB_TOKEN`, so `ci` does not run on the
release pull request when it opens. Close and reopen it to run it before
review. The merge queue runs it again on the merge group in any case.

The runtime's own `--version` string is set in ork-meter's
`cmd/claude-plugin-runtime/main.go`, independently of `plugin_version`.

### Signed release tags

A release tag is created by a human, so it carries that human's signature.
**The chosen scheme is git's SSH signing checked against an allowlist
committed in this repository**, `.github/allowed_signers`. The tagger signs
with `git tag -s` under `gpg.format=ssh`. `publish-release` runs
`.github/scripts/check-tag-signature.py` with `main`'s copy of the script and
of the allowlist, before it attests or uploads anything, and refuses unless:

- `refs/tags/v<version>` is an annotated tag object whose own name is
  `v<version>` (a signed tag cannot be replayed under another name) and
  whose target is the release commit;
- it carries one SSH signature with nothing after its END line; an unsigned
  tag, or an OpenPGP or X.509 signature, is refused;
- `ssh-keygen -Y verify` accepts the signature in the `git` namespace for the
  **tagger's email** as principal, against `.github/allowed_signers`: the key
  must be listed for exactly that email and still be valid now;
- `git verify-tag`, with only that allowlist as trust root and no user or
  system git config, agrees.

The allowlist is read byte for byte the way `ssh-keygen` reads it: lines end
only at `\n`, and only space and tab separate fields. The file must therefore
be printable ASCII plus tab and line endings. A Unicode space, a line or
paragraph separator, a zero-width character or any other control byte
anywhere in the file refuses every tag. Python would treat such a character
as whitespace, but `ssh-keygen` reads it as part of a field, so a line that
looks like a comment could be a live wildcard signer. The tagger's email must
be one plain address, like the allowlist's.
The allowlist must also be well formed: one email per line (no patterns),
`namespaces="git"` on every key, no `cert-authority`, and only the options
`valid-after` and `valid-before`, comma-separated in one options field.
**An empty allowlist refuses every tag.** That is the state this change
ships in: no release can be published until a signer is added.
`tests/tag_signature_regressions.py` covers each case with throwaway keys.

Why SSH and an allowlist rather than Sigstore keyless (`gitsign`):

- The trust root is a reviewed file in this repository. Adding or removing
  a signer is a pull request a human merges (`.github/` is a controlled
  path), with history, and verification needs no network and no third-party
  service.
- git, OpenSSH and the Ubuntu runner verify it natively. `gitsign` needs its
  own binary on the runner and on every verifier's machine, and an exact
  certificate identity and OIDC issuer policy to be meaningful.
- Keyless signing writes each signature, with the signer's email and the
  tag's commit, to the public Rekor transparency log. This repository is
  private.
- GitHub shows an SSH-signed tag as **Verified** once the same key is
  uploaded as a signing key. It does not do that for `gitsign` signatures.

The cost is key management: a signer's private key is long-lived, so keep it
passphrase-protected or on a hardware key (`ed25519-sk`), and remove or
expire it here when it is lost or the person leaves.

**One-time setup per signer.** The signer is Travis to begin with. In the
`claude-plugin` clone:

```sh
# 1. A dedicated signing key (on a FIDO2 key: -t ed25519-sk). Set a passphrase.
ssh-keygen -t ed25519 -C "claude-plugin release tags" -f ~/.ssh/claude_plugin_release_signing

# 2. Git config for this clone: sign tags with SSH, using that key, as the
#    email the allowlist names. Drop --local and use --global for every clone.
git config --local gpg.format ssh
git config --local user.signingkey ~/.ssh/claude_plugin_release_signing   # the .pub path works only with the key in ssh-agent
git config --local user.email <your-email>
git config --local gpg.ssh.allowedSignersFile "$PWD/.github/allowed_signers"   # for local git verify-tag

# 3. Add the public key to the allowlist on a branch, and open a pull request
#    for a human to merge (.github/ is a controlled path).
printf '%s namespaces="git" %s\n' "$(git config user.email)" "$(cut -d' ' -f1,2 ~/.ssh/claude_plugin_release_signing.pub)" >> .github/allowed_signers

# 4. Optional: upload it to GitHub as a signing key so the tag shows "Verified".
gh ssh-key add ~/.ssh/claude_plugin_release_signing.pub --type signing --title "claude-plugin release tags"
```

`<your-email>` must be the address in the allowlist line, because the check
binds the key to the tagger's email. For GitHub's **Verified** badge it must
also be a verified email on the signer's GitHub account. Until that pull
request merges, `publish-release` refuses every tag.

**Checking a tag by hand.** With `main` checked out, either command works:

```sh
python3 -I -B .github/scripts/check-tag-signature.py --tag v<version> --commit "$(git rev-parse 'v<version>^{commit}')"
git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="$PWD/.github/allowed_signers" verify-tag v<version>
```

**Rotation and revocation.** To add a key, add a line by pull request. To
retire one, delete its line or give it `valid-before="YYYYMMDD"`. Signatures
are checked against the current time, so a retired key's tags no longer
verify. That matters only to a release not yet published:
`publish-release` refuses to publish a version twice anyway.

**A wrong tag.** If an unsigned or wrongly signed `v<version>` was pushed,
`publish-release` refuses it and nothing is published. Before any GitHub
Release or marketplace pull request refers to it, a repository admin may
delete it (the `release-tags` ruleset allows only admins) and push a signed
tag on the same commit. That is the only case where a tag is
re-created; otherwise cut a new version ("Rollback"). Running the check
before `git push`, as in step 10, avoids it.

**v0.3.0 and v0.4.0 are unsigned annotated tags, and stay that way.**
Signing them now would mean replacing each tag object, which moves the tag:
anyone who already fetched it keeps the old object, because git does not
overwrite an existing tag on fetch. That breaks "a tag is never rewritten".
Their integrity rests on the marketplace's `sha` pin, the release commit
checks and the build attestations instead. `publish-release` never runs for
them again, since their GitHub Releases exist. Signed tags start with the
first release after this change.

The `release-tags` ruleset (active; "What this process does not cover")
complements the signature: the ruleset limits who can create, move or delete
a `v*` tag to repository admins, and the signature shows which person made
it. `publish-release` also pins the tag object it verified. It requires
GitHub's `v<version>` to still be that object before it creates the Release,
creates the Release as a draft, and publishes the draft (by its id) only
after GitHub's tag is again that object on that commit. That API check is
the guard: a git object id names its content, so the same object still
carries the signature verified earlier. (The step also re-runs the signature
check, but against the same local clone, so it adds nothing.) A failed
re-check, a failure or a cancellation deletes the draft, so nothing is
published. A run refuses to start if a Release or draft for the tag already
exists. Right after publishing, the tag is read again; if it is no longer
that object, the Release goes back to a draft and the run fails.

What remains open:

- A run cancelled between publishing and that re-read leaves the Release
  published without the re-read, so after a cancelled run check the tag by
  hand ("Checking a tag by hand" above).
- If the repository turns on immutable releases, turning a published
  Release back into a draft fails; the run then fails with an error saying
  so, and a human must deal with the Release.
- The publish job never pushes a tag, but publishing a Release on a tag
  that was deleted just before (possible only if the `release-tags` ruleset
  were loosened) makes GitHub create a lightweight `v<version>` on the
  release commit. The re-read then fails the run and reverts the Release,
  and that tag stays until a human deletes it.

`point-marketplace` requires the tag to still resolve to that object and
commit, and runs the signature check again before it opens its pull request.

### Per-release canary record (#44)

Each release keeps evidence that conversation content stays local
(tests/native_canary_qualification.py; failure behaviour in
[failure-behaviour.md](failure-behaviour.md)). On a machine with the
tagged commit checked out and the release's `claude` on `PATH`:

```sh
RUN_NATIVE_CLAUDE_TESTS=1 ORK_CANARY_REPORT=.local/checks/canary-v<version>.json \
  python3 -I -B .github/scripts/check-native-host.py --report .local/checks/native-host-v<version>.json
```

It runs only against the localhost fixture and the refusing proxy, with
private HOME, config and state directories; it needs no account and makes no
paid model call. The JSON records the date, the Claude Code version, the
runtime SHA-256 from `runtime/<os>-<arch>/SHA256`, every sink checked
(proxy, `$XDG_STATE_HOME/ork-meter`, plugin data, full-output locations, the
OTLP metrics bodies with the runtime's flags) and its hits, which must all be
0, plus the recorded observation for hostile project telemetry flags. Check
`"hits": 0` and `"runtime_sha256_verified": true`, then attach it:

```sh
gh release upload v<version> .local/checks/canary-v<version>.json
```

Then the server-side half, by a human with access to the hosted platform:
connect a test account, run a session with fresh canaries, and confirm the
platform's stored records and logs for that account hold none of them.
**Placeholder:** the procedure and its record format are tracked in #44;
until it is written there, note in the release who checked and when.

### The `ORK_METER_READ_TOKEN` secret

ork-meter is private, so the workflow needs a credential to read it. An
organization owner creates one of:

- a **fine-grained personal access token** scoped to the single repository
  `EDOS-Engineering/ork-meter` with repository permission **Contents:
  Read-only** (and Metadata: Read-only, which GitHub adds), or
- a **GitHub App** installation token with the same scope, minted at the
  start of the run.

Store it as the organization secret **`ORK_METER_READ_TOKEN`**, available to
`EDOS-Engineering/ork-meter-claude`. public-sync also reads
`EDOS-Engineering/claude-plugin` with it ("Syncing from development"), so its
repository access must include that repository too, still Contents:
Read-only.
The token is used only by `actions/checkout` steps with
`persist-credentials: false`, so it is not written into the checkout's git
config. Set an expiry and rotate it; a missing or expired token fails the
checkout step and nothing is published or proposed.

The push of `package-runtime/v<version>` and the pull request use the
workflow's own `GITHUB_TOKEN`, with `contents: write` (for that branch) and
`pull-requests: write` on the `package` job. Opening the pull request needs
the repository setting "Allow GitHub Actions to create and approve pull
requests" (Settings, Actions, General). The `GITHUB_TOKEN` never pushes to
`main`, which the `protecc-main` ruleset protects, and never creates a tag.

### Release channel

`.claude-plugin/marketplace.json` names the one commit customers install:

```json
"source": {
  "source": "url",
  "url": "https://github.com/EDOS-Engineering/ork-meter-claude.git",
  "ref": "v0.4.0",
  "sha": "21bda689504a8c78d064024c83b3e06fe4ec18a9"
}
```

This is a `url` (git) plugin source from the Claude Code marketplace
reference ("Plugin sources"): `ref` is a branch or tag and `sha` a full
40-character commit SHA, and "when you set both `ref` and `sha`, Claude Code
checks out `sha`", so moving or deleting the tag later changes nothing that
customers get. The marketplace file itself still comes from `main`, so a
change to the pointer reaches customers at their next marketplace update;
nothing else on `main` does. The source is `url` with the HTTPS address
rather than `github` because Claude Code 2.1.302 fetched a `github` source
over SSH on `claude plugin update`, which failed on a machine without an SSH
key for github.com even though the install had fallen back to HTTPS; the
HTTPS URL installed, updated and rolled back on the same machine.

Before the first release tag exists on this repository, the entry is the
bootstrap form `{"source": "url", "url": "https://github.com/EDOS-Engineering/ork-meter-claude.git"}`
with no `ref` or `sha`: Claude Code installs `main`. `check` accepts that form
only while no `v<version>` tag is merged into `HEAD`; the first `set` replaces
it, and from then on an unpinned entry is refused.

`.github/scripts/point-marketplace.py set <version>` writes that entry for
tag `v<version>`, and `point-marketplace.py check` verifies it. Both refuse a
tag that does not exist, is not merged into `HEAD`, or whose `plugin.json`
does not say `<version>`, and `check` refuses a `sha` that is not the tag's
commit. The `release channel` job in `ci.yml` runs `check` on every pull
request and merge group, and nightly on `main`. `publish-release` runs `set` to open
the pull request that moves the channel (step 11); a human can run it too:

```sh
git fetch origin --tags
python3 .github/scripts/point-marketplace.py set <version>
```

Every change to `marketplace.json` goes through a pull request that a human
merges.

### Rollback

A release is never rewritten, and neither is a tag. **To roll back, point the
marketplace at an earlier tag** by pull request:

```sh
git fetch origin --tags
python3 .github/scripts/point-marketplace.py set 0.3.0   # the last good release
```

Open a pull request with that one-file change (`release: roll the marketplace
back to v0.3.0`, with the reason and the bad version in the description); a
human merges it. Nothing is rebuilt, and the bad tag and its GitHub Release
stay as a record. Customers get the earlier release at their next marketplace
and plugin update: Claude Code replaces the cached plugin whenever the
version it computes differs from the installed one, lower or higher (seen
with Claude Code 2.1.302 re-pointing from 0.3.0 to a 0.2.0 commit). Customers
who never update stay on the bad version; there is no push to installed
copies.

`main` keeps its own, higher version ("Version bumps"): rolling the channel
back does not lower `plugin.json` on `main`. To fix forward, merge the fix,
cut a new release with a **new** `plugin_version` (for example `0.4.1` after
a bad `0.4.0`, or re-dispatch with an older `ork_meter_ref` to roll the
runtime alone back), and point the marketplace at it. A release pull request
that should not ship is closed, and its `package-runtime/v<version>` branch
deleted, before anyone merges it; a `release-channel/v<version>` pull request
that should not ship is closed the same way.

### Version bumps

`plugin.json`'s `version` is how Claude Code tells releases apart, and how
the platform tells plugin builds apart (`plugin_version` in the features
file). So `main` must never carry code that differs from a release under that
release's version. `.github/scripts/check-version-bump.py`, in the
`release channel` CI job, enforces:

> If the executable surface at `HEAD` differs from the latest release tag's,
> `plugin.json`'s version must be greater than that tag's version. It may
> never be lower.

- **The latest release tag** is the highest `v<version>` tag merged into
  `HEAD`; tags outside `HEAD`'s history are ignored. With no tag the check
  passes.
- **The executable surface** is what an install copies and runs: `hooks/`,
  `scripts/`, `bin/`, `runtime/`, `skills/` (their instructions run the
  scripts), `agents/`, `commands/`, `output-styles/`, `monitors/`,
  `.mcp.json`, `.lsp.json`, `settings.json`, `requirements-hooks.txt` (read
  by the hooks' tokenizer check; `requirements-hooks.in`, its input, is not
  installed and is not on the surface) and `.claude-plugin/plugin.json` except
  `version`. Docs, tests, `.github/` and
  `marketplace.json` are not.
- **Greater** is semver precedence. So the first pull request after a
  release that changes the surface bumps the version, and later ones pass
  without touching it until the next tag. A pull request that changes only
  the version is fine.
- **Pre-release suffixes are allowed and are the convention for `main`
  between releases**: bump to the next version with `-dev` (`0.4.0-dev`
  after `v0.3.0`; `0.3.0-dev` is lower than `0.3.0` and fails). The release
  itself sets a plain `MAJOR.MINOR.PATCH`, which ranks above its `-dev`. A
  suffix is letters, digits and dots, at most 16 characters (the platform's
  version rule in `tests/features-contract.test.sh`); build metadata (`+...`)
  is refused because semver ignores it, so it can never be a bump.

The check compares `HEAD` with the tag and needs no base commit, so it
behaves the same on a pull request (the merge commit with `main`), in the
merge queue and in the nightly run on `main`. The release commit passes because it
sets the new, higher version; once it is tagged, `HEAD` equals the tag and
passes with the same version; the next surface change then needs a bump
above the new tag. Two pull requests that both bump from the same version
conflict on that line in the queue, and the second rebases.

### Publishing the GitHub Release

The tag the marketplace points at is what customers install; the GitHub
Release is the same release in downloadable form, for people who want to inspect a build, verify a digest, or
install from a copy rather than a clone. The `publish-release` workflow
(`.github/workflows/publish-release.yml`) creates it. Like `package-runtime`
it runs on **manual dispatch only**, accepts dispatches only from `main`, runs
the same read-only verification job (`bash tests/run.sh` and the `production`
environment check), and its publish job runs behind the `production`
environment, so a required reviewer other than the dispatcher approves it.

It is a separate dispatch rather than a step of `package-runtime` because the
release pull request that `package-runtime` opens is merged and tagged by a
human later; nothing in that run can know the merged commit. It also cannot
run on the tag push itself: the `production` environment allows the single
branch `main` and no tags, and `check-release-gate.py` refuses a weaker
policy, so a tag-triggered job could not pass the gate.

Dispatch input: `plugin_version`, the version whose tag `v<version>` a human
already pushed. The publish job then:

1. Checks that the tag exists, that its commit is on `main`, that the commit
   is the release commit (`release: plugin <version> with runtime from
   ork-meter <ref>`, from which it takes `<ref>`), that `plugin.json` at the
   tag carries `<version>`, and that no GitHub Release `v<version>` exists yet.
   The subject alone is only a claim, so
   `.github/scripts/check-release-commit.py` also requires the commit to be
   authored by `github-actions[bot]` (the identity `package-runtime` commits
   as), to have one parent, and to change only `runtime/` binaries, their
   `SHA256` files and `.claude-plugin/plugin.json` relative to it. Any
   mismatch fails the run and publishes nothing.
2. Requires the tag to be SSH-signed by a signer in `.github/allowed_signers`
   (`check-tag-signature.py`, with `main`'s copies; "Signed release tags").
   An unsigned tag or an unknown signer fails the run and publishes nothing.
   The job records the verified tag object and commit as outputs. GitHub's
   `v<version>` must still be that tag object before the Release is created,
   again (object and commit, through the API) before the draft Release is
   published, and once more right after (step 6; creating a Release on a tag
   checks at most that the name exists).
   `point-marketplace` re-checks the object and the signature.
3. Verifies every runtime binary at the tag with
   `.github/scripts/verify-runtime-attestations.py` (`main`'s copy), before
   anything is attested or uploaded: each must carry a `package-runtime`
   build attestation (see "How the runtime is verified"). A binary without
   one fails the run and publishes nothing.
4. Checks out the tagged commit. It resolves `requirements-hooks.txt` with
   `pip install --dry-run --report --only-binary=:all:` (hash-checking, since
   the lock hashes every package), which installs
   nothing and runs no package code, in a step without the job's OIDC token
   request credentials. Then it runs
   `.github/scripts/build-release-assets.py`, which **builds nothing**: it
   re-verifies each `runtime/<os>-<arch>/` binary against its `SHA256` file,
   copies it out under a per-target asset name, zips the tagged tree with
   `git archive` (tracked files only, the plugin as a marketplace install
   copies it), writes the SBOM (`build-release-sbom.py`, "SBOM"), writes a
   `.sha256` file per asset in `sha256sum -c` format, and writes
   `manifest.json`, which records the SBOM's digest.
5. Attests build provenance for the zip, the binaries, the SBOM and `manifest.json`
   with `actions/attest-build-provenance` (SHA-pinned). The attestation is
   stored on this repository; verify an asset with
   `gh attestation verify <asset> --repo EDOS-Engineering/ork-meter-claude`.
   That attestation names `publish-release`, which assembled the asset; a
   binary's build attestation names `package-runtime`, and
   `--signer-workflow EDOS-Engineering/ork-meter-claude/.github/workflows/package-runtime.yml`
   selects it.
   If the step fails, the run fails before the Release is created and
   nothing is published.
6. Refuses to go on if any Release for `v<version>` exists, a draft included.
   Creates the GitHub Release `v<version>` on the existing tag as a draft
   through the API (`POST /repos/{owner}/{repo}/releases` with
   `target_commitish` set to the release commit, so a vanished tag could
   only ever be recreated there), which returns the draft's id, and uploads
   every asset to that id. The release notes list the digests. It then
   re-checks the tag through the API (GitHub's tag object and its commit)
   and publishes that draft by id. If the tag is not the verified object
   right after publishing, the Release goes back to a draft and the run
   fails. On any failure or cancellation after the draft is created, the
   draft is deleted by id (re-read first: only a draft for the tag is ever
   deleted) and nothing is published. It writes the same table to the run
   summary.

Assets of release `v<version>`:

```text
ork-meter-claude-plugin-<version>.zip                 the plugin tree at the tag, under ork-meter/
ork-meter-plugin-runtime-darwin-arm64                 each bundled runtime binary
ork-meter-plugin-runtime-darwin-amd64
ork-meter-plugin-runtime-linux-amd64
ork-meter-plugin-runtime-linux-arm64
ork-meter-plugin-runtime-windows-amd64.exe
ork-meter-claude-plugin-<version>.spdx.json           SPDX 2.3 SBOM of the assets above ("SBOM")
<asset>.sha256                                        "<hex>  <asset>", one per asset above
manifest.json                                         plugin version, tag, plugin commit, ork-meter ref and
                                                      commit, runtime targets, the SBOM's name, format and
                                                      sha256, sha256 per asset
```

The binaries in the Release are byte-for-byte the ones in the release commit's
`runtime/`; their digests equal the `SHA256` files there and the table in the
release pull request. Installing the zip by hand is not a supported install
path on macOS (a downloaded, quarantined copy hits Gatekeeper; see "Signing
and Gatekeeper on macOS"); it exists for inspection and for environments that
install from a reviewed copy.

The workflow uses its own `GITHUB_TOKEN` with `contents: write` to create the
Release on the tag that already exists, plus `id-token: write` and
`attestations: write` for the attestation; that job never pushes a branch or
a tag (for the one way publishing could create a tag, see "Signed release
tags"). The `point-marketplace` job that follows it pushes only the new branch
`release-channel/v<version>` (no force) and opens the pull request against
`main`, with `contents: write` and `pull-requests: write`; it never pushes to
`main` and never creates a tag. GitHub does not run CI on a pull request it
opens, so close and reopen it before review. A Release is never rewritten: if
one is wrong, roll back or publish a new version (see "Rollback").

### SBOM

Each GitHub Release carries one SBOM, `ork-meter-claude-plugin-<version>.spdx.json`,
in **SPDX 2.3 JSON**. It describes every release asset in one document:

- **The plugin zip** with its SHA-256, the tag and commit it was archived
  from, and what it contains: each runtime binary and the bundled vocabulary.
- **Each runtime binary** with its SHA-256, `GENERATED_FROM` the
  `EDOS-Engineering/ork-meter` commit, and `CONTAINS` the Go standard library
  version and every Go module linked into it, with its version, purl and
  `go.sum` `h1:` hash. These come from the build info the Go linker embeds in
  the binary (what `go version -m` prints; `-s -w` does not strip it), so
  they describe the shipped bytes rather than a `go.mod`. The build info's
  `vcs.revision` must equal the ork-meter commit the release names and
  `vcs.modified` must be false, or no SBOM is written. Build info lists the
  modules linked in, not the edges between them, so the SBOM has no
  module-to-module graph.
- **The bundled vocabulary** `hooks/lib/data/o200k_base.tiktoken.gz`, with
  the SHA-256 of the file and, in its comment, the decompressed SHA-256 pin
  from `hooks/lib/token_gate.py`, which it must match. Its licence is MIT
  (`LICENSE-tiktoken.txt`).
- **The hooks' Python packages**, all optional: the hooks fall back to the
  bundled pure-Python encoder without them. `requirements-hooks.in` asks for
  one, `tiktoken==0.14.0`; `requirements-hooks.txt` locks it and everything it
  needs (`regex`, `requests`, `charset-normalizer`, `idna`, `urllib3`,
  `certifi`) at exact versions, with the SHA-256 of every wheel and sdist of
  each version, so a customer's `pip install --require-hashes` gets the same
  versions and only those files on every platform (#265). The SBOM lists every
  package `publish-release`'s pip resolution chose, with the wheel URL, its
  SHA-256 as the SPDX checksum and the declared licence, and says whether
  `requirements-hooks.in` asks for it or it is a dependency. Every locked
  package must be an exact `name==version` pin with at least one
  `--hash=sha256:<hex>` (on the same line or continued with `\`, as
  `uv pip compile --generate-hashes` writes them), every pin in
  `requirements-hooks.in` must be locked at its version, and the resolution
  must pick exactly the locked packages and versions, each from a file whose
  SHA-256 the lock lists, or no SBOM is written. The SHA-256 is that of the
  wheel resolved on `publish-release`'s Linux x86_64 runner; other platforms
  install another locked wheel of the same version.

A runtime binary whose build info lacks `vcs.revision` or `vcs.modified` is
always refused, and `publish-release` refuses a release commit that names no
ork-meter commit. SPDX IDs carry a short
hash of the exact package name and version, so two modules whose names
differ only in punctuation stay two packages. Two different packages that
still share an ID are refused, never silently merged.

The generator is `.github/scripts/build-release-sbom.py`, which
`build-release-assets.py` calls. It is in-repo and uses the standard library
only. It downloads no tool into a job that holds `id-token: write`, and every
claim it makes can be read in one file. Its output is deterministic: the
timestamp is the release commit's committer date. A third-party scanner such
as syft would read the same Go build info and add no information about these
assets. `tests/release_sbom_regressions.py` checks the build-info reader
against `go version -m` on the committed binaries (when `go` is installed),
and `tests/release_assets_regressions.py` checks that the SBOM is an asset,
that its digest is in `manifest.json` and that it lists every other asset's
digest.

The SBOM is attested with the other assets. To check one:

```sh
gh release download v<version> --repo EDOS-Engineering/ork-meter-claude \
  --pattern 'ork-meter-claude-plugin-*.spdx.json*' --pattern manifest.json
sha256sum -c ork-meter-claude-plugin-<version>.spdx.json.sha256
gh attestation verify ork-meter-claude-plugin-<version>.spdx.json --repo EDOS-Engineering/ork-meter-claude
```

Its digest is also `manifest.json`'s `sbom.sha256`. Licences are recorded as
`NOASSERTION` wherever the source does not declare them; Go build info
carries none. There is no vulnerability report or VEX.

### Running the builder locally

The same builder runs on a developer machine for inspection (Python 3.10 or
later); it never commits or pushes:

```sh
python3 scripts/package_runtime.py --platform-source ../ork-meter \
  --targets linux-amd64 --output /tmp/ork-meter-runtime
```

Omit `--targets` for all five, add `--plugin-version 0.3.0` to rewrite the
manifest, and `--manifest` to point at a different `plugin.json`. The
default `--output` is the plugin's `runtime/`, which `main` tracks: a local
build written there shows up as a change to the released binaries, so pass
an `--output` outside the repository and never commit a local build. The
development builder `scripts/build_runtime.py` runs the same build for one
target (the host's by default) and writes only `.local/runtime/`: the binary
is stripped like a release's (`-ldflags "-s -w -buildid="`), and
`.local/runtime/<os>-<arch>/` is deleted before each build.

## How the runtime is verified

`scripts/runtime.sh` is the only entry to the runtime (the hooks and skills
call it). On every invocation it:

1. Picks the target `<os>-<arch>` from `uname`. In a source checkout (a
   `.git` next to `scripts/`) with `ORK_DEV_RUNTIME=1`, it uses the
   development build in `.local/runtime/<os>-<arch>/`, says so on stderr,
   and a broken or missing one fails the checks below rather than falling
   back to `runtime/`. Without the opt-in it notes an ignored development
   build on stderr. Otherwise, and always in an installed plugin, it uses
   `runtime/<os>-<arch>/`.
2. Requires both the binary and `SHA256`, and requires `SHA256` to be
   exactly 64 lowercase hex characters, or refuses with "missing its
   bundled runtime" / "invalid runtime manifest".
3. Checks the installed copy in `$CLAUDE_PLUGIN_DATA/runtime/<digest>/`:
   by its stamp (`.verified`: the digest, and the copy's inode, size, mtime
   and ctime when it was verified) if the stamp matches and the data
   directory, `runtime/`, `<digest>/`, the stamp and the copy are owned by
   the user and not group- or other-writable; otherwise by hashing the copy,
   and stamping it if it is unchanged meanwhile.
4. If the copy is missing or does not verify, installs: computes the
   package binary's SHA-256 (`sha256sum` or `shasum -a 256`) and refuses with
   "runtime integrity check failed" if it differs, copies it, mode `0700`,
   through a temp file it re-verifies before the rename, so a half-written
   copy is never executed, and writes the stamp. Staging files older than 5
   minutes, left by a killed install, are removed.
5. `exec`s the verified copy with the caller's arguments plus `--data-dir`.

`hooks/session-start.sh` runs it with `--if-installed`, which exits 75
instead of installing (after checking the package, so a broken package still
reports "unavailable"); the hook then starts `--install` detached, which
installs under a lock in `$CLAUDE_PLUGIN_DATA/runtime/.install-lock` and runs
the new copy once with `--version`.

`tests/runtime.test.sh` covers the tamper cases, the stamp, the install
modes and which build runs in a source checkout and in an installed plugin,
`tests/session-start.test.sh` the detached install, and
`tests/package-runtime.test.sh` covers that the packaged layout is accepted.

The digest guards against a corrupted or altered file in the installed
tree. It does not prove provenance: the `SHA256` file travels with the
binary, so whoever can change one can change the other. The stamp keeps that
guarantee for the installed copy against everyone but the user and root:
anyone else is kept out of the stamp by the ownership and mode check, and a
changed copy gets a new ctime. The user and root can rewrite the stamp, as
they can the plugin tree, `SHA256`, the hooks and `$CLAUDE_PLUGIN_DATA`'s
bytecode cache, so the per-session hash never protected against them.

Provenance is the build attestation `package-runtime` makes for each binary
when it builds it ("Cutting a release", step 5). Anyone with read access to
this repository can check one:

```sh
gh attestation verify runtime/linux-amd64/ork-meter-plugin-runtime \
  --repo EDOS-Engineering/ork-meter-claude \
  --signer-workflow EDOS-Engineering/ork-meter-claude/.github/workflows/package-runtime.yml \
  --source-ref refs/heads/main --deny-self-hosted-runners
```

`.github/scripts/verify-runtime-attestations.py` runs exactly that for every
target and fails closed: a missing or unverifiable attestation, a missing
`gh`, a missing target or any extra file under `runtime/` refuses. Two places
run it:

- **CI**, the `runtime-provenance` job in `ci.yml`, on every pull request
  and merge group that changes `runtime/` (compared with the base commit; an
  unknown base counts as changed, so the nightly run on `main` verifies every
  binary). `ci-gate` needs it, so a
  hand-made commit that swaps a binary and its `SHA256` fails the gate.
- **`publish-release`**, before it attests or uploads anything.

**v0.3.0 is the one exception.** Its binaries (release commit `fc742d7`)
were built before `package-runtime` attested anything, so they have no build
attestation and never will. The script carries their five digests, each
pinned to its own target, and reports them as unattested legacy binaries
(a `::warning::`) instead of verifying them; any other bytes must verify. A
GitHub Release for v0.3.0 would carry only `publish-release`'s attestation.
Build provenance starts with the first release after 0.3.0, and the list is
never extended.

The release commit itself is checked by
`.github/scripts/check-release-commit.py` in `publish-release` (bot author,
`runtime/` and `plugin.json` only). The git author is self-declared, so that
is a consistency check; the attestation is what ties the bytes to the build.

## Signing and Gatekeeper on macOS

**The binaries are not Developer ID signed and not notarized today.** The Go
linker gives `darwin-arm64` binaries the ad-hoc signature Apple Silicon
requires to execute at all, and that is the only signature present.

What that means on macOS, to the best of our understanding (not yet
verified on a customer machine):

- Gatekeeper assesses files that carry the `com.apple.quarantine` extended
  attribute. Browsers and archive tools set it; `git` does not. A plugin
  installed through `/plugin marketplace add`, which clones with git, has no
  quarantine attribute on the runtime, and `runtime.sh`'s `cp` into the data
  directory does not add one, so the runtime runs without a Gatekeeper
  prompt.
- A copy that does get quarantined, for example a repository downloaded as
  a zip from GitHub and expanded in Finder, is refused with a dialog along
  the lines of "cannot be opened because the developer cannot be verified"
  or "Apple could not verify ... is free of malware", and the plugin fails
  at `runtime.sh` (the `exec` fails). Developer ID signing plus notarization
  is the fix; until then that install path is unsupported.
- Endpoint-security products and MDM policies can refuse unsigned binaries
  regardless of quarantine. Customers under such policies need the signed
  build.

Windows SmartScreen likewise applies to downloaded files, not to files
written by git; the `.exe` is unsigned and will show a SmartScreen warning
if launched from a quarantined copy.

Code signing and notarization are a follow-up; this process ships unsigned
binaries knowingly.

## What this process does not cover

- **No automated acceptance.** The workflow proves the linux-amd64 binary
  starts on the runner. It does not run the plugin on macOS, Windows or
  linux-arm64, and does not test connecting to Ork Meter. Compilation is not
  native acceptance. Someone installs the release on each platform and runs
  `/ork-meter:connect`, `/ork-meter:status` and a normal session before
  announcing it, following [platform-walkthrough.md](platform-walkthrough.md)
  for the manual rows of [platform-matrix.md](platform-matrix.md). CI's
  `native-host`, `wsl` and `unit` jobs cover the plugin itself on the other
  rows, not a released install.
- **No automatic publication.** Dispatch is manual and deliberate, for both
  `package-runtime` and `publish-release`. Releases remain a reviewed
  operation; the CONTRIBUTING reminder about re-enabling review and Gate 2 (a
  protected `production` environment with required reviewers) applies before
  the first build goes to anyone outside EDOS, and both workflows enforce that
  environment gate and refuse to build or publish if its configuration is
  missing or weakened.
- **No code signing of the binaries**, as above. Release tags are signed
  ("Signed release tags"); the binaries and the zip are covered by build
  provenance attestations, not by a code signature.
- **Release refs are protected by rulesets, which are settings, not code.**
  Besides `protecc-main`, two rulesets are active (#124):
  - `release-tags` restricts creating, updating and deleting `refs/tags/v*`
    and blocks non-fast-forward updates. Only repository admins bypass it, so
    the human who signs and pushes a release tag must be an admin.
  - `release-branches-no-push-after-create` blocks updates and force pushes
    to `package-runtime/*` once a branch exists, with no bypass.

  They limit who can create or move a ref. They don't say whether the
  content is right: the attestation checks catch a swapped binary, and the
  commit and signature checks in `publish-release` catch a tag on the wrong
  commit or by the wrong signer. A change to either ruleset is a settings
  change a human makes.
- **No reproducibility claim.** The build flags are deterministic
  (`-trimpath`, `-buildid=`), and the ork-meter ref and Go version are
  recorded, but byte-for-byte reproducibility across runners has not been
  checked.
- **No changelog.** The release commit body lists digests, not changes.
  Write release notes by hand if they are needed.
