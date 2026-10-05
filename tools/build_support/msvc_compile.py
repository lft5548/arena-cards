"""Feed MSVC's structured dependency list to Ninja without locale-dependent parsing."""
from __future__ import annotations

import json
import locale
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    arguments = [argument for argument in sys.argv[1:] if argument.lower() != "/showincludes"]
    descriptor, dependency_file = tempfile.mkstemp(prefix="arena-msvc-deps-", suffix=".json")
    os.close(descriptor)
    try:
        result = subprocess.run(arguments + ["/sourceDependencies", dependency_file],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        sys.stdout.write(result.stdout.decode(locale.getpreferredencoding(False), errors="replace"))
        sys.stdout.flush()
        if result.returncode != 0:
            return result.returncode
        metadata = json.loads(Path(dependency_file).read_text(encoding="utf-8-sig"))
        for include in metadata["Data"]["Includes"]:
            print("Note: including file: " + include)
        return 0
    finally:
        Path(dependency_file).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
