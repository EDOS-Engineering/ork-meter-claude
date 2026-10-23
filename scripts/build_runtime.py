"""Build a local development runtime, never a release or the legacy helper.

The release build (scripts/package_runtime.py build()) for one target, the
host's unless --os/--arch say otherwise, into .local/runtime/<os>-<arch>.
"""
import argparse
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))   # -I (3.11+) leaves the script's directory off
import package_runtime  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--platform-source", required=True, type=Path)
    parser.add_argument("--go", default="go")
    parser.add_argument("--os", choices=("windows", "linux", "darwin"))
    parser.add_argument("--arch", choices=("amd64", "arm64"))
    args = parser.parse_args(argv)
    source = args.platform_source.resolve()
    if not (source / package_runtime.RUNTIME_MAIN).is_file():
        parser.error("platform source does not contain the plugin-managed runtime")
    host = subprocess.check_output([args.go, "env", "GOHOSTOS", "GOHOSTARCH"], cwd=source, text=True).splitlines()
    target_os, target_arch = args.os or host[0], args.arch or host[1]
    digest = package_runtime.build(args.go, source, package_runtime.PLUGIN_ROOT / ".local/runtime", target_os, target_arch)
    print(f"Development runtime built for {target_os}/{target_arch}; SHA-256 {digest}. No release published.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
