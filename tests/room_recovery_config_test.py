"""Recovery configuration must fail before opening sockets or databases."""
import argparse
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for required, cleanup in (("0", "0"), ("1", "1")):
        environment = os.environ.copy()
        environment.update(ARENA_ROOM_RECOVERY="1", ARENA_MYSQL_REQUIRED=required,
                           ARENA_MYSQL_ENABLED="0", ARENA_MYSQL_CLEANUP_RUNNING=cleanup)
        result = subprocess.run([args.server, "19270"], cwd=root, env=environment,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 3, result
        assert "room recovery requires" in result.stderr, result.stderr
    for key, values in (("ARENA_ROOM_RECOVERY_MODE", ("events", "FULL")),
                        ("ARENA_ROOM_RECOVERY_SNAPSHOT_INTERVAL", ("0", "129", "-1", "16x"))):
        for value in values:
            environment = os.environ.copy()
            environment.update(ARENA_ROOM_RECOVERY="0", ARENA_MYSQL_REQUIRED="0", ARENA_MYSQL_ENABLED="0",
                               ARENA_ROOM_RECOVERY_MODE="full", ARENA_ROOM_RECOVERY_SNAPSHOT_INTERVAL="16")
            environment[key] = value
            result = subprocess.run([args.server, "19270"], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=10)
            assert result.returncode == 3, result
            assert key in result.stderr, result.stderr
    print("room recovery config test passed")


if __name__ == "__main__":
    main()
