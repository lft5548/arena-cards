"""Start, inspect, or package a local Arena Cards demonstration.

Docker CLI details stay private: only operation labels and allowlisted status
fields reach the terminal. No command removes containers, tables, or volumes.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PROFILE = {
    "ARENA_ROOM_RECOVERY": "1",
    "ARENA_MYSQL_CLEANUP_RUNNING": "0",
    "ARENA_REDIS_FAIL_APPLY_COUNT": "0",
    "ARENA_MYSQL_POOL_SIZE": "2",
    "ARENA_CHECKPOINT_BATCH_SIZE": "16",
    "ARENA_ROOM_RECOVERY_MODE": "full",
    "ARENA_ROOM_RECOVERY_SNAPSHOT_INTERVAL": "16",
}
CONTAINERS = {
    "mysql": "arena-cards-mysql",
    "redis": "arena-cards-redis",
    "arena-server": "arena-cards-server",
}
DATABASE_QUERY = """SELECT
 (SELECT COUNT(*) FROM matches WHERE status='running'),
 (SELECT COUNT(*) FROM settlement_outbox WHERE status='pending'),
 (SELECT COUNT(*) FROM matches m
  LEFT JOIN match_results r ON r.match_id=m.match_id
  LEFT JOIN settlement_outbox o ON o.match_id=m.match_id
  WHERE (m.status IN ('finished','settled') AND r.match_id IS NULL)
     OR (m.status NOT IN ('finished','settled') AND
         (r.match_id IS NOT NULL OR o.match_id IS NOT NULL)));
"""


class DemoError(RuntimeError):
    pass


def profile_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    result = dict(os.environ if environment is None else environment)
    result.update(PROFILE)
    return result


def run(command: list[str], *, label: str, input_text: str | None = None,
        environment: dict[str, str] | None = None, timeout: int = 60) -> str:
    try:
        result = subprocess.run(command, cwd=ROOT, env=environment, input=input_text,
                                text=True, encoding="utf-8", errors="replace",
                                capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise DemoError(label + " failed; inspect the local Docker/Git state.") from error
    if result.returncode:
        raise DemoError(label + " failed; inspect the local Docker/Git state.")
    return result.stdout.strip()


class DockerDemo:
    def __init__(self):
        self.environment = profile_environment()

    def command(self, *arguments: str, label: str, input_text: str | None = None,
                timeout: int = 60) -> str:
        return run(["docker", *arguments], label=label, input_text=input_text,
                   environment=self.environment, timeout=timeout)

    def compose(self, *arguments: str, timeout: int = 180) -> str:
        return self.command("compose", "-f", "deploy/docker-compose.yml", *arguments,
                            label="Compose operation", timeout=timeout)

    def container_names(self) -> set[str]:
        # A daemon failure is an error, never an empty / first-run installation.
        return set(self.command("container", "ls", "--all", "--format", "{{.Names}}",
                                label="Container inventory").splitlines())

    def inspect(self, service: str) -> dict:
        try:
            value = json.loads(self.command("inspect", CONTAINERS[service],
                                            label=service + " inspection"))
            if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
                raise ValueError("invalid inspection")
            return value[0]
        except (ValueError, TypeError) as error:
            raise DemoError(service + " inspection returned invalid data.") from error

    def database_state(self) -> dict[str, int]:
        # Password comes from the existing container environment; neither the
        # process arguments nor terminal output include its value.
        output = self.command(
            "exec", "-i", CONTAINERS["mysql"], "sh", "-c",
            'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql --batch --skip-column-names '
            '--user="$MYSQL_USER" "$MYSQL_DATABASE"',
            label="Database state query", input_text=DATABASE_QUERY)
        try:
            values = [int(item) for item in output.split()]
            if len(values) != 3 or any(value < 0 for value in values):
                raise ValueError("invalid counts")
            return dict(zip(("running", "pending_outbox", "partial_settlements"), values))
        except ValueError as error:
            raise DemoError("Database state query returned invalid counts.") from error

    def require_no_running_rooms(self) -> None:
        values = self.database_state()
        if values["running"]:
            raise DemoError("Running matches exist; finish them before rebuilding the demo server.")
        if values["partial_settlements"]:
            raise DemoError("Incomplete settlement rows exist; inspect the database before deployment.")

    def start(self, no_build: bool) -> dict:
        names = self.container_names()
        if CONTAINERS["mysql"] in names:
            if not self.inspect("mysql").get("State", {}).get("Running"):
                self.command("start", CONTAINERS["mysql"], label="Database start")
                self.compose("up", "-d", "--wait", "mysql")
            self.require_no_running_rooms()
        else:
            # First installation: schema.sql initializes empty named volumes.
            self.compose("up", "-d", "--wait", "mysql", "redis")
            self.require_no_running_rooms()
        if not no_build:
            self.compose("build", "arena-server", timeout=1200)
        # Build can take time; recheck before changing the running server.
        self.require_no_running_rooms()
        self.compose("up", "-d", "--wait", "--no-build", "mysql", "redis", "arena-server")
        return self.check()

    def check(self) -> dict:
        result = {"services": {}, "configuration": dict(PROFILE)}
        for service in CONTAINERS:
            inspected = self.inspect(service)
            state = inspected.get("State", {})
            health = state.get("Health", {}).get("Status", "missing")
            if not state.get("Running") or health != "healthy":
                raise DemoError(service + " is not running and healthy.")
            result["services"][service] = "healthy"
            container_port, host_port = {"mysql": ("3306/tcp", "3307"),
                                         "redis": ("6379/tcp", "6379"),
                                         "arena-server": ("9000/tcp", "9000")}[service]
            ports = inspected.get("NetworkSettings", {}).get("Ports", {})
            if not any(item.get("HostPort") == host_port for item in ports.get(container_port, []) or []):
                raise DemoError(service + " expected port is not published.")
            if service == "arena-server":
                environment = dict(item.split("=", 1) for item in inspected.get("Config", {}).get("Env", [])
                                   if "=" in item)
                expected = dict(PROFILE, ARENA_MYSQL_ENABLED="1", ARENA_MYSQL_REQUIRED="1",
                                ARENA_REDIS_ENABLED="1", ARENA_PORT="9000")
                for key, value in expected.items():
                    if environment.get(key) != value:
                        raise DemoError("Demo configuration mismatch: " + key)
        result["database"] = self.database_state()
        if result["database"]["partial_settlements"]:
            raise DemoError("Incomplete settlement rows exist.")
        result["admin"] = asyncio.run(check_admin())
        result["ready"] = True
        return result


async def check_admin() -> dict[str, str]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from client_pygame.protocol import negotiate_protocol, read_message, send_message

    async def one(protocol: str) -> str:
        reader, writer = await asyncio.open_connection("127.0.0.1", 9000)
        try:
            await negotiate_protocol(reader, writer, protocol)
            request_id = "demo-check-" + protocol
            await send_message(writer, "AdminRoomsReq", {"request_id": request_id}, protocol=protocol)
            response = await read_message(reader, protocol=protocol)
            if response["type"] != "AdminRoomsResp":
                raise DemoError("Admin returned an unexpected response.")
            if protocol == "proto_v1" and response["payload"].get("request_id") != request_id:
                raise DemoError("ProtoV1 Admin request correlation failed.")
            return "reachable"
        finally:
            writer.close()
            await writer.wait_closed()

    output = {}
    for protocol in ("text_v1", "proto_v1"):
        try:
            output[protocol] = await asyncio.wait_for(one(protocol), 8)
        except DemoError:
            raise
        except (OSError, ValueError, asyncio.TimeoutError, ImportError, EOFError) as error:
            raise DemoError(protocol + " Admin check failed.") from error
    return output


EXCLUDED_PARTS = {".git", ".local", ".venv", ".aws", "__pycache__", ".pytest_cache", "node_modules",
                  "library", "temp", "logs", "usersettings", "acceptanceevidence"}
EXCLUDED_SUFFIXES = {".log", ".pyc", ".pdb", ".replay"}


def public_path(relative: PurePosixPath) -> bool:
    return (not relative.is_absolute() and ".." not in relative.parts
            and not any(part.lower() in EXCLUDED_PARTS for part in relative.parts)
            and not any(part.startswith(".") and part != ".github" for part in relative.parts[:-1])
            and not any(part.endswith("_BackUpThisFolder_ButDontShipItWithYourGame") for part in relative.parts)
            and relative.suffix.lower() not in EXCLUDED_SUFFIXES)


def tracked_paths(root: Path) -> list[str]:
    # Include additions only after git add; do not sweep untracked local data.
    return run(["git", "ls-files", "--cached", "-z"], label="Tracked source inventory").split("\0")


def require_inside(root: Path, path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise DemoError(label + " must stay inside the repository.")
    return resolved


def source_files(root: Path) -> list[tuple[Path, str]]:
    files = []
    for name in tracked_paths(root):
        if not name:
            continue
        relative = PurePosixPath(name)
        if not public_path(relative) or any(part == "build" or part.startswith("build-") for part in relative.parts):
            continue
        path = root.joinpath(*relative.parts)
        # Deleted tracked files are intentionally absent in the release.
        if not path.exists():
            continue
        require_inside(root, path, "Source file")
        if path.is_symlink() or not path.is_file():
            raise DemoError("Source inventory contains a link or non-regular file.")
        files.append((path, "arena_cards/" + relative.as_posix()))
    return files


def package(output: Path, unity_build: Path | None = None) -> dict:
    root = ROOT.resolve()
    output = require_inside(root, output if output.is_absolute() else root / output, "Package output")
    if output.suffix.lower() != ".zip" or output.exists():
        raise DemoError("Package output must be a new .zip file.")
    files = source_files(root)
    if not files:
        raise DemoError("No tracked source files were found.")
    if unity_build is not None:
        directory = require_inside(root, unity_build if unity_build.is_absolute() else root / unity_build,
                                   "Unity build")
        if not public_path(PurePosixPath(directory.relative_to(root).as_posix())):
            raise DemoError("Unity build cannot be selected from a private or generated-cache directory.")
        if not directory.is_dir() or not any(directory.glob("*.exe")):
            raise DemoError("Unity build must contain an exported player executable.")
        # Only an explicitly selected ignored player directory can add binaries.
        relative = directory.relative_to(root).as_posix()
        run(["git", "check-ignore", "--quiet", relative], label="Unity build isolation check")
        for path in sorted(directory.rglob("*")):
            relative = PurePosixPath(path.relative_to(directory).as_posix())
            if not public_path(relative):
                continue
            require_inside(root, path, "Unity file")
            if path.is_symlink():
                raise DemoError("Unity build contains a symbolic link.")
            if path.is_file():
                files.append((path, "unity_player/" + relative.as_posix()))
    if any(path == output for path, _ in files):
        raise DemoError("Package output cannot overwrite a source or player file.")
    commit = run(["git", "rev-parse", "HEAD"], label="Source revision")
    source_state = "dirty" if run(["git", "status", "--porcelain", "--untracked-files=normal"],
                                   label="Source working state") else "clean"
    manifest = {"source_revision": commit, "source_state": source_state, "files": []}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, name in sorted(files, key=lambda item: item[1]):
                payload = path.read_bytes()
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | (0o755 if os.access(path, os.X_OK) else 0o644)) << 16
                archive.writestr(info, payload)
                manifest["files"].append({"path": name, "bytes": len(payload),
                                           "sha256": hashlib.sha256(payload).hexdigest()})
            info = zipfile.ZipInfo("release-manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    except (OSError, zipfile.BadZipFile) as error:
        raise DemoError("Release package creation failed.") from error
    return {"package": output.relative_to(root).as_posix(), "files": len(files),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "contains_git_history": False, "contains_unity_player": unity_build is not None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="build and start the fixed local demo profile")
    start.add_argument("--no-build", action="store_true", help="reuse the existing server image")
    commands.add_parser("check", help="check service health, database state, and both Admin protocols")
    release = commands.add_parser("package", help="create a source ZIP without Git history or local data")
    release.add_argument("--output", type=Path, default=Path("build-delivery/ArenaCards-source.zip"))
    release.add_argument("--unity-build", type=Path, help="optional ignored exported Windows player directory")
    args = parser.parse_args()
    try:
        if args.command == "package":
            result = package(args.output, args.unity_build)
        elif args.command == "start":
            result = DockerDemo().start(args.no_build)
        else:
            result = DockerDemo().check()
    except DemoError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Avoid echoing exception messages containing private paths or payloads.
        print("Demo operation failed; inspect local configuration and dependencies.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
