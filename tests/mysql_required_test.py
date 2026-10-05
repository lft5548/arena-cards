"""Required mode must fail closed when the configured database is unavailable."""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    args = parser.parse_args()
    env = os.environ.copy()
    env.update({
        "ARENA_MYSQL_ENABLED": "1",
        "ARENA_MYSQL_REQUIRED": "1",
        "ARENA_MYSQL_HOST": "127.0.0.1",
        "ARENA_MYSQL_PORT": "3306",
        "ARENA_MYSQL_USER": "__arena_invalid_test_user__",
        "ARENA_MYSQL_PASSWORD": "",
        "ARENA_MYSQL_DATABASE": "__arena_invalid_test_database__",
    })
    result = subprocess.run([os.path.abspath(args.server), "19105"], cwd=Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, text=True, timeout=10, check=False)
    if result.returncode != 3 or "MySQL startup error" not in result.stderr:
        raise AssertionError(f"expected fail-closed startup, got code={result.returncode}, stderr={result.stderr!r}")
    print("required MySQL startup test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
