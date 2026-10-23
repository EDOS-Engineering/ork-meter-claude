"""Refuse a release tag that is not SSH-signed by a signer in .github/allowed_signers.

A human tags the merged release commit (docs/release.md, "Cutting a release").
publish-release runs this, with main's copy of the script and of
.github/allowed_signers, before it attests or uploads anything:

  check-tag-signature.py --tag v<version> --commit <sha> [--allowed-signers FILE]

It passes only when all of these hold:

* refs/tags/<tag> is an annotated tag object (a lightweight tag has nowhere to
  hold a signature), whose own `tag` header is <tag> and whose `object` is
  <commit>, a commit;
* the tag carries one SSH signature (`git tag -s` with gpg.format=ssh) and
  nothing after its END line; an unsigned tag, or an OpenPGP or X.509
  signature, is refused;
* `ssh-keygen -Y verify` accepts that signature in the `git` namespace for the
  principal named by the tag's tagger email, against the allowed signers file:
  the key must be listed there for exactly that email, and still valid now;
* `git verify-tag`, with only this file as its trust root and no user or
  system git config, agrees.

The allowed signers file is checked first, byte for byte as ssh-keygen reads
it: lines end only at \n, and only space and tab are blanks, so the file must
be printable ASCII plus tab and line endings (a Unicode space or line
separator that Python would treat as whitespace is refused, not skipped).
Every entry must name one email principal (no wildcards), carry
namespaces="git" in a single options field, and use no cert-authority. The tagger's email must be such
an address too.
An empty file allows nobody, so every tag is refused until a signer is added by
pull request. Python 3.10+, standard library, git and OpenSSH 8.2+.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SIGNERS = ROOT / ".github" / "allowed_signers"
SSH_BEGIN = b"-----BEGIN SSH SIGNATURE-----"
SSH_END = b"-----END SSH SIGNATURE-----"
OTHER_SIGNATURES = (b"-----BEGIN PGP SIGNATURE-----", b"-----BEGIN SIGNED MESSAGE-----")
# Used with fullmatch only: "$" alone would also match before a final newline.
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
KEY_TYPES = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com", "ecdsa-sha2-nistp256",
             "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "sk-ecdsa-sha2-nistp256@openssh.com",
             "ssh-rsa")
OPTION = re.compile(r'(namespaces|valid-after|valid-before)="[^" \t]*"')
# What ssh-keygen treats as blank between fields: space and tab, nothing else.
BLANKS = re.compile(rb"[ \t]+")
# Printable ASCII, tab, and \r only as part of a \r\n line ending.
ALLOWED_BYTES = re.compile(rb"(?:[\x20-\x7e\t\n]|\r(?=\n)|\r\Z)*")


class Refused(Exception):
    """The tag or the allowed signers file does not meet the rules above."""


def parse_allowed_signers(data):
    """The (principal, options, key type) of each entry; Refused on a weak or malformed one.

    ``data`` is the file's bytes (a str is encoded as UTF-8 first). It is split
    and trimmed exactly as ssh-keygen does, so a line this parser skips is a
    line ssh-keygen skips; anything outside printable ASCII is refused."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not ALLOWED_BYTES.fullmatch(data):
        bad = next(i for i, byte in enumerate(data)
                   if not (0x20 <= byte <= 0x7E or byte in (0x09, 0x0A))
                   and not (byte == 0x0D and data[i + 1:i + 2] in (b"\n", b"")))
        line = data.count(b"\n", 0, bad) + 1
        raise Refused(f"allowed_signers byte {bad} (line {line}) is {data[bad:bad + 1]!r}: "
                      "only printable ASCII, tab and line endings are allowed, as ssh-keygen and this check "
                      "must read the file the same way")
    entries = []
    for number, raw in enumerate(data.split(b"\n"), 1):
        line = raw.strip(b" \t\r")
        if not line or line.startswith(b"#"):
            continue
        fields = [field.decode("ascii") for field in BLANKS.split(line)]
        if len(fields) < 3:
            raise Refused(f"allowed_signers line {number}: expected '<email> namespaces=\"git\" <key type> <key>'")
        principal, rest = fields[0], fields[1:]
        if not EMAIL.fullmatch(principal):
            raise Refused(f"allowed_signers line {number}: principal {principal!r} must be one email address, no patterns or lists")
        options = []
        # ssh-keygen reads one options field, then the key type: options are
        # comma-separated within that field, never a second blank-separated one.
        if rest[0] not in KEY_TYPES:
            for option in rest.pop(0).split(","):
                if not OPTION.fullmatch(option):
                    raise Refused(f"allowed_signers line {number}: option {option!r} is not allowed (namespaces, valid-after, valid-before only)")
                options.append(option)
        if len(rest) < 2 or rest[0] not in KEY_TYPES:
            raise Refused(f"allowed_signers line {number}: missing or unsupported key type (one of {', '.join(KEY_TYPES)})")
        if 'namespaces="git"' not in options:
            raise Refused(f"allowed_signers line {number}: must carry namespaces=\"git\" so the key signs only git objects here")
        entries.append((principal, tuple(options), rest[0]))
    return entries


def git(repo, *args, data=None):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C")
    return subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True, env=env)


def read_tag(repo, tag):
    """The raw tag object of refs/tags/<tag>; Refused if it is missing or lightweight."""
    kind = git(repo, "cat-file", "-t", f"refs/tags/{tag}")
    if kind.returncode != 0:
        raise Refused(f"tag {tag} does not exist")
    if kind.stdout.strip() != b"tag":
        raise Refused(f"{tag} is a lightweight tag; a release tag must be annotated and signed (git tag -s)")
    raw = git(repo, "cat-file", "tag", f"refs/tags/{tag}")
    if raw.returncode != 0:
        raise Refused(f"cannot read tag object {tag}: {raw.stderr.decode(errors='replace').strip()}")
    return raw.stdout


def split_tag(raw):
    """(headers, payload, signature) of a raw tag object."""
    headers = {}
    for line in raw.split(b"\n\n", 1)[0].split(b"\n"):   # git splits on \n only
        key, _, value = line.decode("utf-8", errors="replace").partition(" ")
        headers.setdefault(key, value)
    for marker in OTHER_SIGNATURES:
        if b"\n" + marker in raw:
            raise Refused("the tag is signed with OpenPGP or X.509; release tags are SSH-signed (gpg.format=ssh, docs/release.md)")
    at = raw.rfind(b"\n" + SSH_BEGIN)
    if at < 0:
        raise Refused("the tag is not signed; sign it with git tag -s under gpg.format=ssh (docs/release.md, \"Signed release tags\")")
    signature = raw[at + 1:]
    # One armored block, ending the object: bytes after it would be passed to
    # ssh-keygen unread, so the object would carry data no signature covers.
    if signature.count(SSH_END) != 1 or not signature.endswith(b"\n" + SSH_END + b"\n"):
        raise Refused("the tag has bytes after its SSH signature's END line, or no END line; a release tag ends with its signature")
    return headers, raw[:at + 1], signature


def tagger_email(headers):
    """The tagger's email, which becomes the principal ssh-keygen checks; Refused
    unless it is one plain address like those allowed_signers may list."""
    match = re.fullmatch(r"[^<>\n]*<([^<>\n]*)> \d+ [+-]\d{4}", headers.get("tagger", ""))
    if not match:
        raise Refused("the tag has no tagger identity")
    if not EMAIL.fullmatch(match.group(1)):
        raise Refused(f"the tagger email {match.group(1)!r} is not one plain email address")
    return match.group(1)


def check(repo, tag, commit, allowed_signers):
    """The signing principal of a valid release tag; Refused otherwise."""
    signers = Path(allowed_signers).resolve()
    if not parse_allowed_signers(signers.read_bytes()):
        raise Refused(f"{signers.name} lists no signer, so no tag can be trusted; add one by pull request (docs/release.md, \"Signed release tags\")")
    headers, payload, signature = split_tag(read_tag(repo, tag))
    if headers.get("tag") != tag:
        raise Refused(f"refs/tags/{tag} holds the tag object named {headers.get('tag')!r}; a signed tag cannot be reused under another name")
    if headers.get("type") != "commit" or headers.get("object") != commit:
        raise Refused(f"tag {tag} points at {headers.get('type')} {headers.get('object')}, not the release commit {commit}")
    principal = tagger_email(headers)
    with tempfile.TemporaryDirectory() as temp:
        sig = Path(temp) / "tag.sig"
        sig.write_bytes(signature)
        verify = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", principal,
                                 "-n", "git", "-s", str(sig)], input=payload, capture_output=True)
    if verify.returncode != 0:
        detail = (verify.stdout + verify.stderr).decode(errors="replace").strip()
        raise Refused(f"tag {tag}: the signature is not by a key {signers.name} allows for the tagger {principal} ({detail})")
    agreed = git(repo, "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={signers}",
                 "verify-tag", "--raw", f"refs/tags/{tag}")
    if agreed.returncode != 0:
        raise Refused(f"git verify-tag refuses {tag}: {agreed.stderr.decode(errors='replace').strip()}")
    return principal, verify.stdout.decode(errors="replace").strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True, help="the release tag, v<version>")
    parser.add_argument("--commit", required=True, help="the full SHA the tag must point at")
    parser.add_argument("--allowed-signers", type=Path, default=DEFAULT_SIGNERS)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        principal, detail = check(args.repo, args.tag, args.commit, args.allowed_signers)
    except Refused as refused:
        print(f"::error::{refused}", file=sys.stderr)
        return 1
    print(f"tag {args.tag} at {args.commit}: SSH signature by {principal}, a signer in {args.allowed_signers.name}. {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
