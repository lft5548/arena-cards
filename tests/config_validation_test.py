"""Verify malformed card configuration fails before the TCP listener starts."""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    args = parser.parse_args()
    environment = os.environ.copy()
    environment.update(ARENA_MYSQL_ENABLED="0", ARENA_MYSQL_REQUIRED="0", ARENA_REDIS_ENABLED="0")
    header = "id,name,cost,effect,value,duration\n"
    starter = "2,Mend,1,heal,4,0\n3,Barrier,1,shield,6,0\n"
    cases = [("id,name,cost,effect,value\n1,Broken,99,damage,8\n", "out of range")]
    for value, duration in ((0, 2), (31, 2), (3, 0), (3, 6), (3, -1)):
        for effect in ("poison", "regen", "burn"):
            cases.append((header + f"1,Status,1,{effect},{value},{duration}\n" + starter,
                          "invalid status parameters"))
    for value, duration in ((0, 0), (4, 0), (2, 1), (2, -1)):
        cases.append((header + f"1,Disrupt,2,discard,{value},{duration}\n" + starter,
                      "invalid discard parameters"))
    cases.append((header + "1,Disrupt,2,discard,-1,0\n" + starter, "out of range"))
    for effect in ("attack_boost", "heal_boost"):
        for value, uses in ((0, 2), (11, 2), (3, 0), (3, 6), (3, -1)):
            cases.append((header + f"1,Bonus,1,{effect},{value},{uses}\n" + starter,
                          "invalid bonus parameters"))
        cases.append((f"id,name,cost,effect,value\n1,Bonus,1,{effect},3\n2,Mend,1,heal,4\n3,Barrier,1,shield,6\n",
                      "invalid bonus parameters"))
    cases.extend([
        (header + "1,Strike,2,damage,8,1\n" + starter, "invalid status parameters"),
        (header + "1,Venom,1,poison,3,2.5\n" + starter, "invalid numeric field"),
        (header + "1,Venom,1,poison,3\n" + starter, "field count"),
        ("id,name,cost,effect,value\n1,Venom,1,poison,3\n2,Mend,1,heal,4\n3,Barrier,1,shield,6\n",
         "invalid status parameters"),
    ])
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "invalid.csv"
        for content, expected_error in cases:
            config.write_text(content, encoding="utf-8")
            result = subprocess.run(
                [os.path.abspath(args.server), "19101", str(config)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5,
                env=environment,
            )
            if result.returncode != 2 or expected_error not in result.stderr:
                raise SystemExit(f"unexpected validation result: code={result.returncode}, stderr={result.stderr!r}")
    print(f"config validation test passed: {len(cases)} invalid configurations rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
