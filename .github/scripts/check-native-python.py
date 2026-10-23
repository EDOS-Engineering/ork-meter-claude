"""Check Python hook syntax and run regressions with the runner's native Python."""
import ast
from pathlib import Path
import sys
import unittest

root = Path(__file__).resolve().parents[2]
for directory in ("hooks", "scripts", ".github/scripts", "tests"):
    for path in (root / directory).rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="*regressions.py")
if suite.countTestCases() == 0:
    raise SystemExit("No native Python regressions found")
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
