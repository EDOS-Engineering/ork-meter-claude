"""Native-shell completion receipt writer. Stdout/stderr remain untouched."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def write(path, value):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.receipt-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    code = int(sys.argv[3])
    if not 0 <= code <= 255:
        raise ValueError('shell exit')
    try:
        write(sys.argv[1], dict(nonce=sys.argv[2], exit_code=code, completed_monotonic=time.monotonic()))
    except OSError:
        pass  # Failure to record must never change the original shell exit.
    raise SystemExit(code)


if __name__ == '__main__':
    main()
