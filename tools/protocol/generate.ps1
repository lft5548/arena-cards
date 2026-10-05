param([string]$Protoc = "D:\Dev\protobuf\bin\protoc.exe")
$ErrorActionPreference = "Stop"
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if ((& $Protoc --version) -ne "libprotoc 3.21.12") { throw "Expected protoc 3.21.12" }
$python = Join-Path $root "client_pygame/generated"
$csharp = Join-Path $root "client_unity/Assets/Scripts/Generated"
New-Item -ItemType Directory -Force $python, $csharp | Out-Null
Push-Location $root
try {
  & $Protoc --proto_path=proto --python_out=client_pygame/generated --csharp_out=client_unity/Assets/Scripts/Generated proto/arena_cards.proto
  if ($LASTEXITCODE -ne 0) { throw "Protobuf generation failed" }
} finally { Pop-Location }
