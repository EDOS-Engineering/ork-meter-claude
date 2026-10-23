"""Generates pure_bpe's Unicode category table (hooks/lib/pure_bpe.py, #115).

tiktoken 0.14.0's regex crate classifies characters by Unicode 16.0.0, the
database of Python 3.14. The table must come from that version, not from
whichever Python runs the hooks. To regenerate after a tiktoken upgrade, run
with a Python whose ``unicodedata.unidata_version`` matches the regex crate's
Unicode version and update ``pure_bpe.UNICODE_VERSION``:

    python3.14 -I -B tests/pure_bpe_tables.py
"""
import itertools
from pathlib import Path
import re
import unicodedata

CLASSES = {"Lu": "U", "Lt": "U", "Ll": "l", "Lm": "o", "Lo": "o",
           "Mn": "m", "Mc": "m", "Me": "m", "Nd": "n", "Nl": "n", "No": "n"}


def categories():
    """Runs of class letters over all code points: letter, then length in hex."""
    classes = (CLASSES.get(unicodedata.category(chr(point)), ".") for point in range(0x110000))
    return "".join("%s%x" % (key, len(list(run))) for key, run in itertools.groupby(classes))


def main():
    path = Path(__file__).resolve().parents[1] / "hooks/lib/pure_bpe.py"
    table = categories()
    lines = "\n".join('    "%s"' % table[i:i + 100] for i in range(0, len(table), 100))
    source = path.read_text(encoding="utf-8")
    source = re.sub(r'_CATEGORIES = \(\n(?:    ".*"\n)+\)', lambda _: "_CATEGORIES = (\n%s\n)" % lines, source)
    source = re.sub(r'UNICODE_VERSION = ".*"', 'UNICODE_VERSION = "%s"' % unicodedata.unidata_version, source)
    path.write_text(source, encoding="utf-8")
    print("pure_bpe: Unicode %s, %d runs" % (unicodedata.unidata_version, len(re.findall("[.Ulomn]", table))))


if __name__ == "__main__":
    main()
