"""Export measured evidence with local identities and calendar metadata removed.

Original logs remain local. Numeric measurements and binary hashes are preserved;
the manifest records both the original and published content hashes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


PRIVATE_KEYS = {
    "args", "argv", "binary", "build", "command", "console_pids", "coordinator_pid",
    "evidence", "error", "cleanup_error", "fixture", "fixtures", "host", "log",
    "logs", "match_id", "mysql_database", "mysql_password", "mysql_user", "password",
    "players", "player_id", "raw_file", "server", "server_path", "server_pid",
    "stderr", "stdout", "time_zone", "token", "tokens", "user", "username",
    "worker_pid", "recorded_at_source", "recorded_at_sources", "timestamp",
}
CALENDAR_KEY = re.compile(r"(?:^|_)(?:at|utc)(?:$|_)|^generated_at$", re.I)
CALENDAR_VALUE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+Z-]+)?\b|\b\d{8}T\d{6}Z\b")
LOCAL_VALUE = re.compile(r"(?:[A-Za-z]:[\\/]|/workspace/|/Users/|/home/|\\Users\\)", re.I)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def sanitize(value):
    """Remove metadata without changing booleans, counters or duration values."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lowered = key.lower()
            if (lowered in PRIVATE_KEYS or CALENDAR_KEY.search(lowered)
                    or lowered.endswith(("_path", "_pid", "_password", "_token"))):
                continue
            if lowered in ("run", "source") and isinstance(item, str):
                result[key] = "sample-" + digest(item.encode())[:12]
                continue
            result[key] = sanitize(item)
        return result
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        if LOCAL_VALUE.search(value) or CALENDAR_VALUE.search(value):
            return "[local metadata omitted]"
    return value


def export(source, target):
    source, target = Path(source), Path(target)
    raw = source.read_bytes()
    if source.suffix == ".jsonl":
        content = "\n".join(json.dumps(sanitize(json.loads(line)), ensure_ascii=False,
                                    separators=(",", ":"))
                            for line in raw.decode("utf-8-sig").splitlines() if line.strip()) + "\n"
    else:
        value = sanitize(json.loads(raw.decode("utf-8-sig")))
        content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")
    return {"source_sha256": digest(raw), "published_sha256": digest(target.read_bytes())}


def refresh_manifest(root, entries):
    root = Path(root).resolve()
    target = root / "docs/evidence/manifest.json"
    previous = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    merged = {item["file"]: item for item in previous.get("files", [])}
    for name, original_hash in entries:
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("evidence must be a file inside the repository")
        merged[path.relative_to(root).as_posix()] = {
            "file": path.relative_to(root).as_posix(), "source_sha256": original_hash,
            "published_sha256": digest(path.read_bytes()), "bytes": path.stat().st_size}
    for item in merged.values():
        path = root / item["file"]
        item.update(published_sha256=digest(path.read_bytes()), bytes=path.stat().st_size)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"schema_version": 1,
        "publication": "Measurements and binary hashes retained; local paths, identities, calendar metadata and logs omitted.",
        "files": sorted(merged.values(), key=lambda item: item["file"])},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--manifest-root", type=Path)
    args = parser.parse_args()
    hashes = export(args.source, args.target)
    if args.manifest_root:
        root = args.manifest_root.resolve()
        refresh_manifest(root, [(args.target.resolve().relative_to(root).as_posix(), hashes["source_sha256"])])
    print(json.dumps(hashes))


if __name__ == "__main__":
    main()
