"""Every workflow and composite action parses as YAML (#289).

The other workflow tests read the files as text, so a plain scalar holding
': ' (``run: pip install --only-binary=:all: -r x``) passed them and broke
ci.yml on GitHub. This parses each file with PyYAML if it is installed, else
with Ruby's Psych (on every GitHub-hosted runner image), and checks the shape
GitHub needs. Without either parser it skips, unless ORK_REQUIRE_YAML_PARSE=1,
which ci.yml's unit (ubuntu-latest) job sets."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT  # noqa: E402

FILES = sorted((ROOT / ".github/workflows").glob("*.yml")) + sorted((ROOT / ".github/workflows").glob("*.yaml"))
ACTIONS = sorted((ROOT / ".github/actions").glob("*/action.yml"))
# Psych 4 made load safe (no aliases); older Psych has no unsafe_load. These
# are this repository's own files.
RUBY = ("src = File.read(ARGV[0], encoding: 'UTF-8'); "
        "data = Psych.respond_to?(:unsafe_load) ? Psych.unsafe_load(src) : Psych.load(src); "
        "puts JSON.generate(data)")


def parser():
    """A function from a path to its parsed data, raising ValueError on a
    YAML error, or None if no parser is available."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        pass
    else:
        def parse_pyyaml(path):
            import yaml
            try:
                return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            except yaml.YAMLError as error:
                raise ValueError(str(error))
        return parse_pyyaml
    ruby = shutil.which("ruby")
    if not ruby:
        return None

    def parse_ruby(path):
        completed = subprocess.run([ruby, "-ryaml", "-rjson", "-e", RUBY, str(path)],
                                   capture_output=True, text=True, encoding="utf-8")
        if completed.returncode != 0:
            raise ValueError(completed.stderr.strip())
        return json.loads(completed.stdout)
    return parse_ruby


PARSE = parser()


@unittest.skipIf(PARSE is None and os.environ.get("ORK_REQUIRE_YAML_PARSE") != "1",
                 "no YAML parser (PyYAML or ruby)")
class WorkflowYaml(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(PARSE, "ORK_REQUIRE_YAML_PARSE=1 but neither PyYAML nor ruby is available")

    def test_the_parser_rejects_a_plain_scalar_with_colon_space(self):
        """The check would have caught the broken ci.yml."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "broken.yml")
            path.write_text("jobs:\n  a:\n    steps:\n      - run: pip install --only-binary=:all: -r x.txt\n",
                            encoding="utf-8")
            with self.assertRaises(ValueError):
                PARSE(path)
            path.write_text("jobs:\n  a:\n    steps:\n      - run: |\n          pip install --only-binary=:all: -r x\n",
                            encoding="utf-8")
            self.assertEqual(PARSE(path)["jobs"]["a"]["steps"][0]["run"], "pip install --only-binary=:all: -r x\n")

    def test_every_workflow_parses_with_jobs_and_steps(self):
        self.assertIn(ROOT / ".github/workflows/ci.yml", FILES)
        for path in FILES:
            with self.subTest(workflow=path.name):
                data = PARSE(path)
                self.assertIsInstance(data, dict)
                # YAML 1.1 parsers read the key `on` as true.
                self.assertTrue("on" in data or True in data or "true" in data, "no on: triggers")
                self.assertIsInstance(data.get("jobs"), dict)
                self.assertTrue(data["jobs"])
                for name, job in data["jobs"].items():
                    self.assertIsInstance(job, dict, name)
                    self.assertTrue("runs-on" in job or "uses" in job, name)
                    for step in job.get("steps", []):
                        self.assertIsInstance(step, dict, name)
                        self.assertEqual(("run" in step) + ("uses" in step), 1, (name, step))
                        if "run" in step:
                            self.assertIsInstance(step["run"], str, (name, step))

    def test_every_composite_action_parses(self):
        for path in ACTIONS:
            with self.subTest(action=str(path.relative_to(ROOT))):
                data = PARSE(path)
                self.assertIsInstance(data, dict)
                self.assertIsInstance(data.get("runs"), dict)


class Required(unittest.TestCase):
    def test_ci_requires_the_parse_in_one_unit_leg(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertEqual(ci.count("ORK_REQUIRE_YAML_PARSE:"), 1)
        self.assertIn("ORK_REQUIRE_YAML_PARSE: ${{ matrix.os == 'ubuntu-latest' && '1' || '0' }}", ci)


if __name__ == "__main__":
    unittest.main()
