$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
  python -m unittest discover -s tests -p "test_*.py" -v
  if ($env:ARENA_RUN_SMOKE -eq "1") { python tests/smoke_test.py }
} finally {
  Pop-Location
}
