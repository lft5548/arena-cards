"""Compile the real Unity transport sources and run C# TCP acceptance without Editor."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from status_effects_integration_test import ROOT, replay, wait_for_server


async def run(server: str, port: int) -> None:
    project = ROOT / "tests/csharp_proto/CSharpProtoAcceptance.csproj"
    environment = os.environ.copy()
    home = ROOT / "build-csharp-test-home"
    home.mkdir(exist_ok=True)
    environment.update(DOTNET_CLI_HOME=str(home), APPDATA=str(home),
                       DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1")
    for command in (
            ["dotnet", "restore", str(project.parent / "UnityProtocolCompile.csproj"),
             "--configfile", str(project.parent / "NuGet.Config"), "--nologo"],
            ["dotnet", "build", str(project.parent / "UnityProtocolCompile.csproj"),
             "--no-restore", "-c", "Release", "--nologo"],
            ["dotnet", "restore", str(project), "--configfile", str(project.parent / "NuGet.Config"), "--nologo"],
            ["dotnet", "build", str(project), "--no-restore", "-c", "Release", "--nologo"]):
        await asyncio.to_thread(subprocess.run, command, cwd=ROOT, env=environment, check=True, timeout=60)
    with tempfile.TemporaryDirectory() as temporary:
        environment.update(ARENA_MYSQL_ENABLED="0", ARENA_MYSQL_REQUIRED="0", ARENA_REDIS_ENABLED="0",
                           ARENA_REPLAY_DIR=temporary)
        process = subprocess.Popen([server, str(port)], cwd=ROOT, env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            await wait_for_server(process, port)
            assembly = project.parent / "bin/Release/net9.0/CSharpProtoAcceptance.dll"
            await asyncio.to_thread(subprocess.run, ["dotnet", str(assembly), "--port", str(port)],
                                    cwd=ROOT, env=environment, check=True, timeout=45)
            deadline = asyncio.get_running_loop().time() + 3
            files = list(Path(temporary).glob("*.replay"))
            while len(files) < 2 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
                files = list(Path(temporary).glob("*.replay"))
            if len(files) != 2:
                raise AssertionError("C# matches did not persist both replays")
            for path in files:
                match_id, _, _, events = replay.load_replay(path)
                state = replay.reconstruct(events, replay.load_cards(ROOT / "server/config/cards.csv"), match_id)
                if (state["rules"] != "bonus_v1" or state["reason"] != "max_turns" or
                        state["players"][1]["discard"] != [1, 2]):
                    raise AssertionError("C# replay failed typed battle reconstruction")
            print("C# real transport and mixed TCP matches passed; both replays reconstructed")
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19130)
    args = parser.parse_args()
    asyncio.run(run(os.path.abspath(args.server), args.port))


if __name__ == "__main__":
    main()
