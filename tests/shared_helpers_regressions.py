"""Helpers shared across hooks/lib instead of copied (#191): one owner-only
check (``session.check_private``), one bounded read (``session.read_bounded``)
and one lever-kind table (``features.SETTING_KINDS``).

The lever classifications that used to be computed in four places are now
derived from ``SETTING_KINDS``. The frozen copies below are those places as
they were before #191; every derived table, and the behaviour of
``apply_policy_settings``, ``policy.adapt`` and the project rule, must match
them exactly.
"""
import itertools
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402
import artifacts  # noqa: E402
import circuit  # noqa: E402
import entitlement  # noqa: E402
import features  # noqa: E402
import policy  # noqa: E402
import project_env  # noqa: E402
import session  # noqa: E402


# --- frozen pre-#191 classifications --------------------------------------

def old_opt_outs():
    return {name: lever["opt_outs"] for name, lever in features.LEVERS.items()}


def old_opt_ins():
    return {name: lever["opt_in"] for name, lever in features.LEVERS.items() if lever.get("opt_in")}


def old_switches():
    names = set(old_opt_ins().values())
    for lever in features.LEVERS.values():
        names.update(lever["opt_outs"])
    return names


def old_switch_tunables():
    return {tunable.env for lever in features.LEVERS.values()
            for tunable in lever["tunables"].values() if tunable.unit == "switch"}


def old_names():
    names = {features.MODE_ENV: ("mode", None), features.RETENTION_ENV: ("retention", None)}
    for feature, lever in features.LEVERS.items():
        for name in lever["opt_outs"]:
            names[name] = ("opt_out", None)
        if lever.get("opt_in"):
            names[lever["opt_in"]] = ("opt_in", None)
        for key, tunable in lever["tunables"].items():
            kind = "opt_in" if tunable.unit == "switch" else "opt_out" if tunable.off_means_unset else "tunable"
            names[tunable.env] = (kind, (feature, key))
    return names


def old_apply_policy_settings(env, settings):
    effective = dict(env)
    effective.update(settings)
    if features.mode(env) != settings[features.MODE_ENV]:
        effective[features.MODE_ENV] = "off"
    for lever in features.LEVERS.values():
        for name in lever["opt_outs"]:
            if features.is_off(env, name):
                effective[name] = "off"
        if lever.get("opt_in"):
            off = (features.is_off(env, lever["opt_in"]) if lever.get("per_token_default")
                   else not features.is_on(env, lever["opt_in"]))
            if off:
                effective[lever["opt_in"]] = "off"
        for tunable in lever["tunables"].values():
            if (tunable.off_means_unset or tunable.unit == "switch") and features.is_off(env, tunable.env):
                effective[tunable.env] = "off"
    return effective


def old_adapt(settings):
    current = features.policy_settings({})
    named, switched = old_switches(), old_switch_tunables()
    def missing(key, default):
        if key in named:
            return "off"
        return "0" if key in switched else default
    return {key: settings[key] if key in settings else missing(key, default) for key, default in current.items()}


OLD_REPLACING_LEVERS = ("trim", "test_filter", "dedupe", "read_guard", "delta")
SPELLINGS = (None, "on", "off", "1", "0", "maybe", " OFF ", "12k")


class LeverKinds(unittest.TestCase):
    def test_derived_tables_equal_the_old_ones(self):
        self.assertEqual(features.OPT_OUTS, old_opt_outs())
        self.assertEqual(list(features.OPT_OUTS), list(old_opt_outs()))
        self.assertEqual(features.OPT_INS, old_opt_ins())
        self.assertEqual(policy.switches(), old_switches())
        self.assertEqual(policy.switch_tunables(), old_switch_tunables())
        self.assertEqual(features.SETTING_KINDS, old_names())
        self.assertEqual(list(features.SETTING_KINDS), list(old_names()))
        self.assertEqual(features.REPLACING_FEATURES, OLD_REPLACING_LEVERS)
        self.assertTrue(set(features.REPLACING_FEATURES) <= set(features.AUTOMATIC_FEATURES))
        for names in features.OPT_OUTS.values():
            for name in names:
                self.assertEqual(features.SETTING_KINDS[name], ("opt_out", None), name)
        for name in features.OPT_INS.values():
            self.assertEqual(features.SETTING_KINDS[name], ("opt_in", None), name)

    def test_project_rule_unchanged_for_every_setting_and_spelling(self):
        names = old_names()
        for name in names:
            for raw in SPELLINGS:
                kind = names[name][0]
                probe = {name: raw}
                expected = (features.mode(probe) == "off" if kind == "mode"
                            else features.is_off(probe, name) if kind == "opt_out"
                            else not features.is_on(probe, name) if kind == "opt_in" else False)
                with self.subTest(name=name, raw=raw):
                    if raw is not None:
                        self.assertEqual(project_env.honoured(name, raw), expected)

    def test_apply_policy_settings_and_adapt_unchanged(self):
        names = sorted(old_names())
        pinned = features.policy_settings({})
        # Each setting alone at every spelling, then pairs of off/on spellings.
        envs = [{}]
        envs += [{name: raw} for name in names for raw in SPELLINGS if raw is not None]
        envs += [{a: x, b: y} for a, b in itertools.combinations(names, 2) for x in ("off", "on") for y in ("0", "yes")]
        for env in envs:
            for settings in (pinned, features.policy_settings(env), dict(pinned, **{features.MODE_ENV: "shadow"})):
                with self.subTest(env=env, settings=settings[features.MODE_ENV]):
                    self.assertEqual(features.apply_policy_settings(env, settings), old_apply_policy_settings(env, settings))
        for drop in names:
            partial = {k: v for k, v in pinned.items() if k != drop}
            with self.subTest(missing=drop):
                self.assertEqual(policy.adapt(partial), old_adapt(partial))
        self.assertEqual(policy.adapt({}), old_adapt({}))


class PrivateChecks(unittest.TestCase):
    """Every caller's strictness, as it was before the shared helper."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def entries(self):
        """(label, path, is_dir, owner_only_mode) for files and directories with
        private and loose modes, plus links to each."""
        out = []
        for label, mode in (("private", 0o600), ("group-readable", 0o640), ("world-writable", 0o606)):
            path = self.root / ("file-" + label)
            path.write_bytes(b"{}")
            os.chmod(path, mode)
            out.append(("file " + label, path))
        for label, mode in (("private", 0o700), ("group-readable", 0o750)):
            path = self.root / ("dir-" + label)
            path.mkdir()
            os.chmod(path, mode)
            out.append(("dir " + label, path))
        if hasattr(os, "symlink"):
            for target in ("file-private", "dir-private"):
                link = self.root / ("link-to-" + target)
                try:
                    os.symlink(self.root / target, link)
                except OSError:
                    continue
                out.append(("link to " + target, link))
        return out

    def test_each_caller_mode(self):
        posix = hasattr(os, "getuid")
        for label, path in self.entries():
            info = os.lstat(path)
            regular, directory = stat.S_ISREG(info.st_mode), stat.S_ISDIR(info.st_mode)
            loose = bool(info.st_mode & 0o077) and posix
            with self.subTest(entry=label):
                # policy, entitlement and circuit files: regular, ours, no group/other bits.
                self.assertEqual(session.check_private(info) is None, regular and not loose)
                self.assertEqual(session.check_private(info), None if regular and not loose
                                 else "type" if not regular else "owner")
                # circuit directories: a directory, ours, no group/other bits.
                self.assertEqual(session.check_private(info, directory=True) is None, directory and not loose)
                # private_dir and initialize_json: ownership only (private_dir then chmods 0700).
                self.assertEqual(session.check_private(info, directory=True, owner_only=False) is None, directory)
                self.assertEqual(session.check_private(info, owner_only=False) is None, regular)

    def test_foreign_owner_refused_in_every_mode(self):
        if not hasattr(os, "getuid"):
            self.skipTest("POSIX ownership")
        path = self.root / "file"
        path.write_bytes(b"x")
        os.chmod(path, 0o600)
        info = os.lstat(path)
        with patch.object(os, "getuid", return_value=info.st_uid + 1):
            self.assertEqual(session.check_private(info), "owner")
            self.assertEqual(session.check_private(info, owner_only=False), "owner")

    def test_private_dir_keeps_chmod_and_refuses_links(self):
        loose = self.root / "loose"
        loose.mkdir()
        os.chmod(loose, 0o755)
        session.private_dir(str(loose))
        if hasattr(os, "getuid"):
            self.assertEqual(stat.S_IMODE(os.lstat(loose).st_mode), 0o700)
        if hasattr(os, "symlink"):
            link = self.root / "link"
            try:
                os.symlink(loose, link)
            except OSError:
                return
            with self.assertRaisesRegex(OSError, "must not be a link"):
                session.private_dir(str(link))

    def test_callers_keep_their_strictness(self):
        if not hasattr(os, "getuid"):
            self.skipTest("POSIX modes")
        # entitlement: owner-only file, refused once group-readable.
        answer = self.root / "entitlement.json"
        answer.write_text('{"optimize": true, "state": "active", "until": null, "checked_at": "2026-10-14T12:00:00Z"}')
        os.chmod(answer, 0o600)
        self.assertTrue(entitlement.read(str(answer))["optimize"])
        os.chmod(answer, 0o640)
        with self.assertRaises(PermissionError):
            entitlement.read(str(answer))
        answer.write_text("{" + " " * entitlement.MAX_BYTES + "}")
        os.chmod(answer, 0o600)
        with self.assertRaises(ValueError):
            entitlement.read(str(answer))
        # initialize_json: an existing winner needs only to be ours and regular.
        winner = self.root / "session.json"
        session.initialize_json(str(winner), {"session_id": "s"})
        os.chmod(winner, 0o644)
        self.assertFalse(session.initialize_json(str(winner), {"session_id": "s"}))
        # circuit: a reducer-health directory others can read is unusable.
        store = session.Store(str(self.root / "store"))
        key = sorted(circuit.SCOPES)[0]
        directory = store.reducer_health_path(circuit.sha(key), create=True)
        self.assertEqual(circuit.read(store, key)["status"], "available")
        os.chmod(directory, 0o750)
        self.assertEqual(circuit.read(store, key)["status"], "unavailable")

    def test_read_bounded(self):
        path = self.root / "bounded"
        for size, limit in ((0, 0), (4096, 4096), (4097, 4096), (10, 9), (10, 10)):
            path.write_bytes(b"x" * size)
            with self.subTest(size=size, limit=limit):
                if size > limit:
                    with self.assertRaises(ValueError):
                        session.read_bounded(str(path), limit)
                else:
                    self.assertEqual(session.read_bounded(str(path), limit), b"x" * size)
        with self.assertRaises(FileNotFoundError):
            session.read_bounded(str(self.root / "missing"), 10)


    def test_read_bounded_without_following(self):
        """``follow=False``: a regular file reads as with ``follow`` (bytes
        exact, CRLF kept on Windows too); a link, dangling or not, and a
        directory are ValueError; a missing path is FileNotFoundError."""
        path = self.root / "plain"
        path.write_bytes(b"a\r\nb\rc\n")
        for limit in (None, 7, 100):
            self.assertEqual(session.read_bounded(str(path), limit, follow=False), b"a\r\nb\rc\n")
        with self.assertRaisesRegex(ValueError, "size allowance"):
            session.read_bounded(str(path), 6, follow=False)
        with self.assertRaisesRegex(ValueError, "regular file"):
            session.read_bounded(str(self.root), None, follow=False)
        with self.assertRaises(FileNotFoundError):
            session.read_bounded(str(self.root / "missing"), None, follow=False)
        for label, target in (("link", path), ("dangling link", self.root / "missing")):
            link = self.root / label.replace(" ", "-")
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError, AttributeError):
                continue   # no symlinks here (Windows without the privilege)
            with self.subTest(entry=label):
                with self.assertRaisesRegex(ValueError, "regular file"):
                    session.read_bounded(str(link), None, follow=False)
                if label == "link":
                    self.assertEqual(session.read_bounded(str(link), None), b"a\r\nb\rc\n")

    def swapped(self, make):
        """Read ``checked`` with follow=False while ``os.lstat`` reports the
        regular file it was, though ``make(path)`` has replaced it: the race
        between the check and the open. Returns the outcome, and checks that
        no descriptor was left open."""
        path = self.root / "checked"
        if os.path.lexists(path):
            os.unlink(path)
        path.write_bytes(b"mine")
        checked = os.lstat(path)
        os.unlink(path)
        make(path)
        opened = []
        real_open = os.open

        def recording_open(*args, **kwargs):
            opened.append(real_open(*args, **kwargs))
            return opened[-1]
        with patch.object(session.os, "lstat", lambda target: checked), \
                patch.object(session.os, "open", recording_open):
            try:
                outcome = session.read_bounded(str(path), None, follow=False)
            except Exception as exc:   # noqa: BLE001 - the exception is the outcome
                outcome = exc
        for fd in opened:
            with self.assertRaises(OSError):   # closed: no descriptor leaked
                os.fstat(fd)
        return outcome

    def test_read_bounded_refuses_whatever_is_swapped_in_after_the_check(self):
        other = self.root / "other"
        other.write_bytes(b"theirs")
        # Another regular file (renamed over it): not the file checked, on every platform.
        outcome = self.swapped(lambda path: os.link(other, path) if hasattr(os, "link") else path.write_bytes(b"x"))
        self.assertIsInstance(outcome, session.FileChanged)
        self.assertIsInstance(outcome, ValueError)
        # A directory: refused as not regular, before any fdopen or read.
        if os.name != "nt":   # Windows can't os.open a directory
            outcome = self.swapped(lambda path: path.mkdir())
            self.assertEqual((type(outcome), str(outcome)), (ValueError, session.NOT_REGULAR))
            os.rmdir(self.root / "checked")
        # A FIFO: O_NONBLOCK means the open doesn't wait for a writer.
        if hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"):
            outcome = self.swapped(os.mkfifo)
            self.assertEqual((type(outcome), str(outcome)), (ValueError, session.NOT_REGULAR))
        # A link: O_NOFOLLOW's ELOOP is the same ValueError as a link seen by lstat.
        try:
            os.symlink(other, self.root / "probe")
        except (OSError, NotImplementedError, AttributeError):
            return   # no symlinks here
        outcome = self.swapped(lambda path: os.symlink(other, path))
        if hasattr(os, "O_NOFOLLOW"):
            self.assertEqual((type(outcome), str(outcome)), (ValueError, session.NOT_REGULAR))
        else:
            # Windows: no O_NOFOLLOW, so the open reaches the link's target,
            # which is not the file checked. Still refused, still a ValueError.
            self.assertIsInstance(outcome, session.FileChanged)

    def test_read_bounded_reads_blocking_after_the_check(self):
        if not hasattr(os, "O_NONBLOCK"):
            self.skipTest("no O_NONBLOCK")
        path = self.root / "plain"
        path.write_bytes(b"x" * 100000)
        seen = []
        real_fdopen = os.fdopen

        def fdopen(fd, *args, **kwargs):
            seen.append(os.get_blocking(fd))
            return real_fdopen(fd, *args, **kwargs)
        with patch.object(session.os, "fdopen", fdopen):
            self.assertEqual(len(session.read_bounded(str(path), None, follow=False)), 100000)
        self.assertEqual(seen, [True])


def old_artifact_load(store, identity):
    """artifacts.load before it used ``session.read_bounded`` (#191)."""
    import hashlib
    import json
    if not isinstance(identity, str) or not artifacts.ID_RE.fullmatch(identity):
        raise ValueError("artifact ID must be a 64-character lowercase SHA-256")

    def regular_file(path):
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("artifact must be a regular file")
    path = session.fs_path(store.tool_output_path(identity, ".json"))
    regular_file(path)
    with open(path, "r", encoding="utf-8") as stream:
        document = json.load(stream)
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True)
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != identity:
        raise ValueError("artifact integrity check failed")
    if document.get("schema_version") != artifacts.FORMAT_VERSION:
        raise ValueError("unsupported artifact schema")
    stored = document.pop("text_stored_in", None)
    if stored is None:
        return document
    if stored != artifacts.TEXT_IN_TXT:
        raise ValueError("unsupported artifact layout")
    path = session.fs_path(store.tool_output_path(identity))
    regular_file(path)
    with open(path, "r", encoding="utf-8", newline="") as stream:
        text = stream.read()
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != document.get("received_text_sha256"):
        raise ValueError("artifact integrity check failed")
    at = document.pop("text_at") if "text_at" in document else artifacts._legacy_text_at(document.get("response"))
    if at is not None:
        if not isinstance(at, list) or not at:
            raise ValueError("unsupported artifact layout")
        container = document.get("response")
        for step, key in enumerate(at):
            if not (isinstance(container, dict) and type(key) is str and key in container
                    or isinstance(container, list) and type(key) is int and 0 <= key < len(container)):
                raise ValueError("unsupported artifact layout")
            if step == len(at) - 1:
                if container[key] is not None:
                    raise ValueError("unsupported artifact layout")
                container[key] = text
            else:
                container = container[key]
    document["text"] = text
    return document


class ArtifactLoad(unittest.TestCase):
    """``artifacts.load`` gives what it gave before it used the shared read:
    the same document, or the same exception type and message, for every
    layout written so far and for damaged, linked and missing files."""

    TEXTS = ("plain\n", "crlf\r\nline\r\n", "cr\ronly", "", "caf\u00e9 \u2603\n", "\ufeffbom text\n")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = session.Store(str(Path(self.temporary.name) / "store"))

    def outcome(self, load, identity):
        try:
            return ("ok", load(self.store, identity))
        except Exception as exc:   # noqa: BLE001 - the exception is the outcome
            return (type(exc).__name__, str(exc))

    def legacy(self, document):
        """Write a pre-#190 layout by hand; its ID is the digest of its JSON."""
        import hashlib
        import json
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True)
        identity = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        session.write_private(self.store.tool_output_path(identity, ".json"), raw + "\n")
        return identity

    def layouts(self):
        """(label, identity, text or None) for every layout written so far."""
        import hashlib
        out = []
        for index, text in enumerate(self.TEXTS):
            path = artifacts.save(self.store, "Bash", {"stdout": text, "stderr": "", "n": index}, text)
            out.append(("current %d" % index, Path(path).stem, text))
            path = artifacts.save(self.store, "Read", {"other": index}, text)   # text not in the response
            out.append(("current, text elsewhere %d" % index, Path(path).stem, text))
        # Before #190: the text kept inside the .json.
        out.append(("text in json", self.legacy({"schema_version": 1, "tool": "Bash", "text": "old\r\n",
                                                 "response": {"stdout": "old\r\n"}}), None))
        # #143 record handle: the text in the .txt, no text_at.
        text = '[{"a": 1}]'
        identity = self.legacy({"schema_version": 1, "tool": "mcp__x", "text_stored_in": "txt",
                                "received_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                                "response": {"content": [{"type": "text", "text": None}]}})
        Path(self.store.tool_output_path(identity)).write_bytes(text.encode())
        out.append(("record handle", identity, text))
        return out

    def damage(self, identity):
        """Ways a stored artifact can be wrong, applied to copies: yields
        (label, identity) after changing the files on disk."""
        json_path = Path(self.store.tool_output_path(identity, ".json"))
        txt_path = Path(self.store.tool_output_path(identity))
        original = json_path.read_bytes()
        text = txt_path.read_bytes() if txt_path.exists() else None

        def restore():
            for path, raw in ((json_path, original), (txt_path, text)):
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                elif os.path.lexists(path):
                    path.unlink()
                if raw is not None:
                    path.write_bytes(raw)
        changes = [
            ("as written", lambda: None),
            ("json CRLF whitespace", lambda: json_path.write_bytes(original.replace(b", ", b",\r\n "))),
            ("json UTF-8 BOM", lambda: json_path.write_bytes(b"\xef\xbb\xbf" + original)),
            ("json not UTF-8", lambda: json_path.write_bytes(original[:-1] + b"\xff")),
            ("json truncated", lambda: json_path.write_bytes(original[:len(original) // 2])),
            ("json CRLF, then truncated", lambda: json_path.write_bytes(
                original.replace(b", ", b",\r\n ")[:len(original) // 2])),
            ("json CR-only, then bad", lambda: json_path.write_bytes(original.replace(b", ", b",\r ") + b"x")),
            ("json a directory", lambda: (json_path.unlink(), json_path.mkdir())),
            ("json missing", lambda: json_path.unlink()),
        ]
        if text is not None:
            changes += [
                ("txt edited", lambda: txt_path.write_bytes(text + b"x")),
                ("txt CRLF", lambda: txt_path.write_bytes(text.replace(b"\n", b"\r\n"))),
                ("txt not UTF-8", lambda: txt_path.write_bytes(text + b"\xff")),
                ("txt a directory", lambda: (txt_path.unlink(), txt_path.mkdir())),
                ("txt missing", lambda: txt_path.unlink()),
            ]
        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir(exist_ok=True)
        for which, path, raw in (("json", json_path, original), ("txt", txt_path, text)):
            if raw is None:
                continue
            copy = elsewhere / path.name
            copy.write_bytes(raw)
            changes.append(("%s a link" % which, lambda path=path, copy=copy: (path.unlink(), os.symlink(copy, path))))
            changes.append(("%s a dangling link" % which,
                            lambda path=path: (path.unlink(), os.symlink(elsewhere / "gone", path))))
        for label, change in changes:
            try:
                change()
            except (OSError, NotImplementedError, AttributeError):
                restore()
                continue   # e.g. no symlinks on this Windows: that case is skipped
            yield label
            restore()

    def test_every_layout_and_damage_loads_as_before(self):
        cases = 0
        for label, identity, text in self.layouts():
            for damage in self.damage(identity):
                with self.subTest(layout=label, damage=damage):
                    old = self.outcome(old_artifact_load, identity)
                    self.assertEqual(self.outcome(artifacts.load, identity), old)
                    if damage == "as written":
                        self.assertEqual(old[0], "ok")
                        if text is not None:
                            self.assertEqual(old[1]["text"], text)
                    if damage.endswith("link"):
                        self.assertEqual(old, ("ValueError", "artifact must be a regular file"))
                    cases += 1
        self.assertGreater(cases, 100)

    def test_bad_ids_and_missing_files_unchanged(self):
        for identity in (None, "", "A" * 64, "a" * 63, "../" + "a" * 61, "a" * 64):
            with self.subTest(identity=identity):
                self.assertEqual(self.outcome(artifacts.load, identity), self.outcome(old_artifact_load, identity))
        with self.assertRaises(FileNotFoundError):   # artifact.py's "pruned" message depends on it
            artifacts.load(self.store, "a" * 64)


def old_is_fresh(path, wanted):
    """refresh_features_file's skip test before #191 (calendar/strptime)."""
    import calendar
    import json
    import time
    try:
        with open(path, "rb") as stream:
            raw = stream.read(4097)
        actual = json.loads(raw) if len(raw) <= 4096 else None
        updated = calendar.timegm(time.strptime(actual["updated_at"], "%Y-%m-%dT%H:%M:%SZ")) if isinstance(actual, dict) else 0
        return bool(isinstance(actual, dict) and set(actual) == set(wanted)
                    and actual["features"] == wanted["features"] and actual["plugin_version"] == wanted["plugin_version"]
                    and -300 <= time.time() - updated < 3600
                    and 0 <= time.time() - os.stat(path).st_mtime < 3600)
    except (OSError, ValueError, KeyError, TypeError):
        return False


class FeaturesFreshness(unittest.TestCase):
    """The lever file is rewritten exactly when it was before: by age, by
    updated_at, with other content, malformed or oversized."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.env = {"XDG_STATE_HOME": self.temporary.name, "HOME": self.temporary.name, "CLAUDE_PLUGIN_ROOT": str(ROOT)}

    def test_refresh_decision_unchanged(self):
        import json
        import time
        path = features.features_path(self.env, "fresh-session")
        wanted = features.features_payload(self.env)
        def written(age, updated_age=None, **change):
            stamp = time.time() - age
            text_stamp = stamp if updated_age is None else time.time() - updated_age
            value = dict(wanted, updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(text_stamp)))
            value.update(change)
            session.write_json(path, value)
            os.utime(path, (stamp, stamp))
        cases = {"just written": lambda: written(0), "59 minutes": lambda: written(59 * 60),
                 "61 minutes": lambda: written(61 * 60), "a day": lambda: written(86400),
                 "from the future": lambda: written(-120), "far future": lambda: written(-3600),
                 "other levers": lambda: written(0, features=["nope"]),
                 "other version": lambda: written(0, plugin_version="9.9.9"),
                 "updated_at stale, mtime fresh": lambda: written(0, updated_age=2 * 3600),
                 "updated_at 59 minutes": lambda: written(0, updated_age=59 * 60),
                 "updated_at 4 minutes ahead": lambda: written(0, updated_age=-240),
                 "updated_at 6 minutes ahead": lambda: written(0, updated_age=-360),
                 "updated_at year 2000": lambda: written(0, updated_at="2000-01-01T00:00:00Z"),
                 "updated_at year 2999": lambda: written(0, updated_at="2999-01-01T00:00:00Z"),
                 "updated_at null": lambda: written(0, updated_at=None),
                 "bad updated_at": lambda: written(0, updated_at="yesterday"),
                 "numeric updated_at": lambda: written(0, updated_at=12),
                 "extra key": lambda: written(0, extra=1),
                 "not an object": lambda: Path(path).write_text("[]"),
                 "not JSON": lambda: Path(path).write_text("{"),
                 "too large": lambda: Path(path).write_text(json.dumps(dict(wanted, pad="x" * 5000))),
                 "missing": lambda: os.unlink(path) if os.path.exists(path) else None}
        for label, make in cases.items():
            with self.subTest(case=label):
                make()
                expected = old_is_fresh(path, wanted)
                before = os.stat(path).st_mtime_ns if os.path.exists(path) else None
                with patch.object(features, "write_features_file") as write:
                    features.refresh_features_file(self.env, "fresh-session")
                self.assertEqual(not write.called, expected, "rewritten" if write.called else "kept")
                if before is not None:
                    self.assertEqual(os.stat(path).st_mtime_ns, before)


class SessionStateAndGaps(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = {"XDG_STATE_HOME": str(self.root / "state"), "HOME": str(self.root)}

    def test_state_paths_are_where_they_were(self):
        state = os.path.join(features.state_dir(self.env))
        stale, nudge, start = load("hooks/stale-context.py"), load("hooks/context-nudge.py"), load("hooks/trim-session.py")
        for sid in ("abc-123", "x" * 300, "../escape"):
            name = session.safe_name(sid)
            self.assertEqual(stale.state_path(self.env, sid), os.path.join(state, "stale-guard", name + ".json"))
            self.assertEqual(nudge.nudge_path(self.env, sid), os.path.join(state, "context-nudges", name + ".json"))
            self.assertEqual(start.nudge_path(self.env, sid), os.path.join(state, "cache-nudges", name + ".json"))
        for sid in (None, "", 7):
            self.assertIsNone(stale.state_path(self.env, sid))
            self.assertIsNone(features.session_state_path(self.env, "context-nudges", sid, create=False))
        self.assertEqual(set(start.STATE_DIRS), {stale.STATE_DIR, nudge.NUDGE_DIR, start.NUDGE_DIR, start.CONTEXT_NUDGE_DIR})
        missing = os.path.join(state, "not-made")
        self.assertEqual(features.session_state_path(self.env, "not-made", "s", create=False), os.path.join(missing, "s.json"))
        self.assertFalse(os.path.exists(missing), "create=False makes nothing")

    def test_prune_removes_only_old_state_files(self):
        import time
        now = time.time()
        stamp = now - features.STATE_PRUNE_SECONDS - 60
        def aged(path):
            Path(path).write_text("{}")
            os.utime(path, (stamp, stamp))
            return Path(path)
        old = aged(features.session_state_path(self.env, "stale-guard", "old"))
        hashed = aged(features.session_state_path(self.env, "stale-guard", "x" * 300))
        recent = Path(features.session_state_path(self.env, "stale-guard", "recent"))
        recent.write_text("{}")
        directory = old.parent
        strays = [aged(directory / name) for name in ("notes.txt", ".hidden.json", "has space.json", "old.json.bak")]
        # A state directory that is a link to somewhere else is never entered.
        outside = self.root / "outside"
        outside.mkdir()
        victim = aged(outside / "victim.json")
        linked = os.path.join(features.state_dir(self.env), "cache-nudges")
        try:
            os.symlink(outside, linked, target_is_directory=True)
        except (OSError, NotImplementedError):
            linked = None
        features.prune_session_state(self.env, ("stale-guard", "cache-nudges", "context-nudges"), now)
        self.assertFalse(old.exists())
        self.assertFalse(hashed.exists())
        self.assertTrue(recent.exists())
        for stray in strays:
            self.assertTrue(stray.exists(), stray.name)
        self.assertTrue(victim.exists(), "a linked directory's files survive")
        if linked:
            self.assertTrue(os.path.islink(linked))

    def test_record_gap_writes_the_record_savings_reads(self):
        store = session.Store(str(self.root / "store"))
        self.assertTrue(store.record_gap("recovery", "Bash", 1, 0, OSError("x")))
        self.assertTrue(store.record_gap("PostToolUse", "Read", 3, 1, ValueError("y"), False, tool_call_sha256="ab"))
        gaps = sorted(store.accounting_gaps(), key=lambda gap: gap["event"])
        self.assertEqual(gaps, [
            {"schema_version": 1, "event": "PostToolUse", "tool": "Read", "tool_call_sha256": "ab", "reply_emitted": False,
             "rows_planned": 3, "rows_written": 1, "reason": "ValueError"},
            {"schema_version": 1, "event": "recovery", "tool": "Bash", "reply_emitted": True,
             "rows_planned": 1, "rows_written": 0, "reason": "OSError"}])
        names = os.listdir(os.path.join(store.path, session.ACCOUNTING_GAP_DIR))
        self.assertTrue(all(len(name) == 37 and name.endswith(".json") for name in names), names)
        blocked = self.root / "blocked"
        blocked.write_text("a file where the store should be")
        self.assertFalse(session.Store(str(blocked)).record_gap("recovery", "Bash", 1, 0, OSError()))


class ConfigDir(unittest.TestCase):
    """One rule for $CLAUDE_CONFIG_DIR: absolute wins, else <home>/.claude."""

    def test_rule(self):
        home = os.path.abspath("home-dir")
        config = os.path.abspath("config-dir")
        self.assertEqual(project_env.config_dir({"CLAUDE_CONFIG_DIR": config, "HOME": home}), config)
        self.assertEqual(project_env.config_dir({"CLAUDE_CONFIG_DIR": "relative", "HOME": home}), os.path.join(home, ".claude"))
        self.assertEqual(project_env.config_dir({"HOME": home}, "other"), os.path.join("other", ".claude"))
        self.assertEqual(project_env.user_settings_path({"CLAUDE_CONFIG_DIR": config}), os.path.join(config, "settings.json"))
        self.assertEqual(project_env.global_config_file({"CLAUDE_CONFIG_DIR": config, "HOME": home}), os.path.join(config, ".claude.json"))
        self.assertEqual(project_env.global_config_file({"CLAUDE_CONFIG_DIR": "relative", "HOME": home}), os.path.join(home, ".claude.json"))
        self.assertEqual(project_env.global_config_file({"HOME": home}, "other"), os.path.join("other", ".claude.json"))

    def test_audit_reads_the_config_directory(self):
        audit = load("scripts/audit.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, config, project = root / "home", root / "config", root / "project"
            for directory in (home / ".claude", config / "rules", project):
                directory.mkdir(parents=True)
            (home / ".claude" / "CLAUDE.md").write_text("home\n")
            (config / "CLAUDE.md").write_text("config\n")
            (config / "rules" / "style.md").write_text("rule\n")
            (config / "settings.json").write_text('{"mcpServers": {"from-config": {}}}')
            (home / ".claude.json").write_text('{"mcpServers": {"home-global": {}}}')
            (config / ".claude.json").write_text('{"mcpServers": {"config-global": {}}}')
            def users(env):
                result = audit.audit(str(project), str(home), env)
                return ([(item["scope"], item["path"]) for item in result["instruction_files"] if item["scope"].startswith("user")],
                        [server["source"] for server in result["mcp_servers"]])
            files, servers = users({"HOME": str(home)})
            self.assertEqual(files, [("user", str(home / ".claude" / "CLAUDE.md"))])
            self.assertEqual(servers, ["~/.claude.json (global)"])
            files, servers = users({"HOME": str(home), "CLAUDE_CONFIG_DIR": str(config)})
            self.assertEqual(files, [("user", str(config / "CLAUDE.md")), ("user rules", str(config / "rules" / "style.md"))])
            self.assertEqual(servers, [str(config / ".claude.json") + " (global)", str(config / "settings.json")])
            servers = audit.audit(str(project), str(home), {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(config)})["mcp_servers"]
            self.assertEqual(servers[0]["servers"], ["config-global"], "the global file follows CLAUDE_CONFIG_DIR")


if __name__ == "__main__":
    unittest.main()
