"""Ork Meter Enterprise: the vendored contract files and the hooks' own check
that EDOS vouches for a deployment (hooks/lib/entitlement.py).

The contract is EDOS-Engineering/ork-meter docs/deployment-attestation.md and
docs/plugin-origins.md. Its two Python files are vendored byte for byte, and
its fixtures (tests/fixtures/deployment-attestation, signed by the published
TEST root, never compiled in) must give the answers the contract lists.
"""
import calendar
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (paths, and no developer ORK_METER_* settings)

ROOT = str(support.ROOT)
import deployment_attestation  # noqa: E402
import entitlement  # noqa: E402
import features  # noqa: E402
import plugin_origins  # noqa: E402
import plus_policy  # noqa: E402
import plus_policy_sign  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "deployment-attestation")
# ork-meter docs/plugin-origins/plugin_origins.py and
# docs/deployment-attestation-fixtures/deployment_attestation.py.
VENDORED = {
    "plugin_origins.py": "e4c9d1ed9cd814552dc21feaa7a1d8e427667275e300f2295b0a5d28e0116c6e",
    "deployment_attestation.py": "37f2afc848c14b5870c216641dafdc94fc40efb979a0e29c4b22ea2cd2131227",
}
ORIGIN = "https://ome.attestation-test.invalid"
HOST = "ome.attestation-test.invalid"
POLICY_KEY_ID = "ome-test-2026-11"
POLICY_SEED = hashlib.sha256(b"ork-meter attestation fixture policy key a").digest()
DURING = calendar.timegm((2026, 11, 14, 0, 0, 0))
AFTER = calendar.timegm((2027, 2, 11, 0, 0, 1))


def stamp(text):
    return calendar.timegm(tuple(int(part) for part in (text[0:4], text[5:7], text[8:10], text[11:13], text[14:16], text[17:19])))


def index():
    with open(os.path.join(FIXTURES, "cases.json"), encoding="utf-8") as stream:
        return json.load(stream)


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as stream:
        return stream.read()


class Contract(unittest.TestCase):
    def test_each_vendored_file_is_the_contract_byte_for_byte(self):
        for name, expected in VENDORED.items():
            with open(os.path.join(ROOT, "hooks", "lib", name), "rb") as stream:
                self.assertEqual(hashlib.sha256(stream.read()).hexdigest(), expected, name)

    def test_the_registry_lists_public_cells_only(self):
        entries = plugin_origins.all_origins()
        self.assertIn("https://orkmeter.com", [entry["origin"] for entry in entries])
        for entry in entries:
            self.assertEqual(entry["kind"], plugin_origins.KIND_PUBLIC, entry["origin"])
            self.assertFalse(plugin_origins.is_enterprise(entry["origin"]), entry["origin"])
            self.assertFalse(entitlement.enterprise(entry["origin"]), entry["origin"])
        self.assertFalse(plugin_origins.allowed("https://enterprise.orkmeter.com"))
        self.assertFalse(plugin_origins.allowed(ORIGIN))

    def test_no_test_root_is_compiled_in(self):
        cases = index()
        self.assertNotIn(cases["root_key_id"], deployment_attestation.ROOT_KEYS)
        compiled = [value.hex() if isinstance(value, bytes) else value for value in deployment_attestation.ROOT_KEYS.values()]
        self.assertNotIn(cases["root_public"], compiled)

    def test_the_compiled_root_is_the_one_edos_holds_in_kms(self):
        # ork-meter docs/deployment-attestation.md, "Root keys": the id is the KMS alias, and the SHA-256 is that of
        # the DER SubjectPublicKeyInfo `aws kms get-public-key` returns for it.
        self.assertEqual(list(deployment_attestation.ROOT_KEYS), ["edos-ome-deployment-root-1"])
        raw = bytes.fromhex(deployment_attestation.ROOT_KEYS["edos-ome-deployment-root-1"])
        der = bytes.fromhex("302a300506032b6570032100") + raw
        self.assertEqual(len(raw), 32)
        self.assertEqual(hashlib.sha256(der).hexdigest(), "f1f37079b9024c4a403e11c5042fe55df180bac58b4b6b12f7f7afa21d3a53b4")

    def test_every_published_case_gives_its_expected_answer(self):
        cases = index()
        roots = {cases["root_key_id"]: cases["root_public"]}
        self.assertGreaterEqual(len(cases["cases"]), 70)
        for case in cases["cases"]:
            with self.subTest(case=case["name"]):
                raw, now = fixture(case["file"]), stamp(case["now"])
                if case["ok"]:
                    verified = deployment_attestation.verify(raw, case["origin"], now, roots)
                    self.assertEqual(verified["origin"], case["origin"])
                else:
                    with self.assertRaises(ValueError) as refusal:
                        deployment_attestation.verify(raw, case["origin"], now, roots)
                    if "reason" in case:
                        self.assertEqual(str(refusal.exception), case["reason"])
                # With the roots this plugin ships, no fixture admits anything.
                with self.assertRaises(ValueError):
                    deployment_attestation.verify(raw, case["origin"], now)


class EnterpriseEntitlement(unittest.TestCase):
    """The hooks admit an enterprise connection only while a current
    attestation for exactly its origin is on file, and verify its lever
    policy with the attested keys only."""

    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data, True)
        self.env = {"CLAUDE_PLUGIN_DATA": self.data}
        cases = index()
        roots = mock.patch.dict(deployment_attestation.ROOT_KEYS, {cases["root_key_id"]: cases["root_public"]})
        roots.start()
        self.addCleanup(roots.stop)

    def write(self, name, raw, mode=0o600):
        path = os.path.join(self.data, name)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "wb") as stream:
            stream.write(raw)
        os.chmod(path, mode)
        return path

    def connect(self, origin=ORIGIN, attestation="valid.json", host=HOST, now=DURING, key_id=POLICY_KEY_ID,
                seed=POLICY_SEED, issuer=None, optimize=True):
        self.write("connection.json", json.dumps({"reference": plus_policy_sign.REFERENCE, "origin": origin}).encode())
        if attestation:
            self.write(os.path.join("attestations", host + ".json"), fixture(attestation))
        self.write("plus-policy.json", plus_policy_sign.envelope(
            key_id=key_id, seed=seed, issuer=origin if issuer is None else issuer, now=now, optimize=optimize,
            state="active" if optimize else "free", until="2099-01-01T00:00:00Z" if optimize else None))

    def test_an_attested_deployment_is_entitled_by_its_attested_key(self):
        self.connect()
        status, policy = entitlement.resolve(self.env, DURING)
        self.assertEqual(status, entitlement.ENTITLED)
        self.assertEqual(policy["id"], plus_policy_sign.DEFAULT_POLICY["id"])
        self.assertTrue(entitlement.connected_to_enterprise(self.env))

    def test_the_shipped_roots_admit_nothing(self):
        self.connect()
        with mock.patch.dict(deployment_attestation.ROOT_KEYS, clear=True):
            self.assertEqual(entitlement.resolve(self.env, DURING), (entitlement.NOT_ENTITLED, None))

    def test_no_attestation_on_file_is_not_entitled(self):
        self.connect(attestation=None)
        self.assertEqual(entitlement.resolve(self.env, DURING), (entitlement.NOT_ENTITLED, None))

    def test_an_attestation_past_not_after_is_not_entitled(self):
        self.connect(now=AFTER)
        self.assertEqual(entitlement.resolve(self.env, AFTER), (entitlement.NOT_ENTITLED, None))

    def test_an_attestation_for_another_origin_does_not_admit_this_one(self):
        other = "https://other.attestation-test.invalid"
        self.connect(origin=other, host="other.attestation-test.invalid")
        self.assertEqual(entitlement.resolve(self.env, DURING), (entitlement.NOT_ENTITLED, None))

    def test_no_compiled_key_signs_for_an_enterprise_origin(self):
        self.connect(key_id=plus_policy_sign.KEY_ID, seed=plus_policy_sign.SEED)
        self.assertEqual(entitlement.resolve(self.env, DURING), (entitlement.NOT_ENTITLED, None))

    def test_another_key_under_the_attested_id_is_refused(self):
        self.connect(seed=hashlib.sha256(b"not the attested key").digest())
        self.assertEqual(entitlement.resolve(self.env, DURING), (entitlement.NOT_ENTITLED, None))

    def test_no_attested_key_signs_for_a_public_cell(self):
        self.connect(origin="https://orkmeter.com", host="orkmeter.com")
        self.assertFalse(entitlement.connected_to_enterprise(self.env))
        self.assertEqual(entitlement.resolve(self.env, DURING), (entitlement.NOT_ENTITLED, None))

    def test_a_policy_issued_for_another_origin_is_refused(self):
        self.connect(issuer="https://orkmeter.com")
        self.assertEqual(entitlement.resolve(self.env, DURING), (entitlement.NOT_ENTITLED, None))

    def test_an_origin_that_is_not_exact_is_refused(self):
        for origin, host in (("https://127.0.0.1", "127.0.0.1"), ("https://ome.attestation-test.invalid:8443", HOST),
                             ("https://OME.attestation-test.invalid", HOST), ("https://localhost", "localhost")):
            with self.subTest(origin=origin):
                self.connect(origin=origin, host=host)
                self.assertEqual(entitlement.resolve(self.env, DURING), (entitlement.NOT_ENTITLED, None))

    @unittest.skipUnless(hasattr(os, "getuid"), "ownership and modes are POSIX only")
    def test_an_attestation_file_others_can_write_is_refused(self):
        self.connect()
        os.chmod(os.path.join(self.data, "attestations", HOST + ".json"), 0o666)
        self.assertEqual(entitlement.resolve(self.env, DURING), (entitlement.NOT_ENTITLED, None))

    def test_there_is_no_unsigned_answer_for_an_enterprise_connection(self):
        self.assertLess(DURING, entitlement.LEGACY_UNTIL)
        answer = {"optimize": True, "state": "active", "until": "2099-01-01T00:00:00Z", "checked_at": "2026-11-14T00:00:00Z"}
        self.write("entitlement.json", json.dumps(answer).encode())
        self.write("connection.json", json.dumps({"reference": plus_policy_sign.REFERENCE, "origin": "https://orkmeter.com"}).encode())
        self.assertEqual(entitlement.status(self.env, DURING), entitlement.ENTITLED, "a public cell keeps the legacy window")
        self.write("connection.json", json.dumps({"reference": plus_policy_sign.REFERENCE, "origin": ORIGIN}).encode())
        self.write(os.path.join("attestations", HOST + ".json"), fixture("valid.json"))
        self.assertEqual(entitlement.status(self.env, DURING), entitlement.NOT_ENTITLED)

    def test_nothing_shown_for_an_enterprise_connection_mentions_plus_or_billing(self):
        self.connect(optimize=False)
        self.assertEqual(entitlement.status(self.env, DURING), entitlement.NOT_ENTITLED)
        text = entitlement.message(self.env)
        for word in ("Plus", "billing", "Upgrade", "orkmeter.com"):
            self.assertNotIn(word, text)
        environment = features.RuntimeEnvironment(self.env, (), entitlement.NOT_ENTITLED, None)
        self.assertEqual(features.status_line(environment), text)
        self.write("connection.json", json.dumps({"reference": plus_policy_sign.REFERENCE, "origin": "https://orkmeter.com"}).encode())
        self.assertEqual(entitlement.message(self.env), entitlement.MESSAGE)
        self.assertEqual(entitlement.message({}), entitlement.MESSAGE)


if __name__ == "__main__":
    unittest.main()
