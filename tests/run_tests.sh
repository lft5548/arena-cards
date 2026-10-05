#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
python3 -m unittest discover -s tests -p 'test_*.py' -v
if [[ "${ARENA_RUN_SMOKE:-0}" == "1" ]]; then
  python3 tests/smoke_test.py
fi
