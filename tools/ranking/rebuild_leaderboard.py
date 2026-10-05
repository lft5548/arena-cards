"""Rebuild the Redis rating leaderboard from the MySQL players table.

MySQL remains the source of truth. Redis is treated as a disposable cache,
so this command is safe to rerun after a Redis restart or cache loss.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import uuid
from dataclasses import dataclass


class RedisProtocolError(RuntimeError):
    pass


class RedisClient:
    def __init__(self, host: str, port: int, timeout: float = 3.0) -> None:
        self._socket = socket.create_connection((host, port), timeout=timeout)
        self._socket.settimeout(timeout)
        self._buffer = bytearray()

    def close(self) -> None:
        self._socket.close()

    def command(self, *parts: str) -> object:
        payload = bytearray(f"*{len(parts)}\r\n".encode())
        for part in parts:
            raw = str(part).encode("utf-8")
            payload.extend(f"${len(raw)}\r\n".encode())
            payload.extend(raw)
            payload.extend(b"\r\n")
        self._socket.sendall(payload)
        return self._read_reply()

    def _readline(self) -> bytes:
        while b"\r\n" not in self._buffer:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise RedisProtocolError("Redis closed the connection")
            self._buffer.extend(chunk)
        line, _, rest = self._buffer.partition(b"\r\n")
        self._buffer = bytearray(rest)
        return bytes(line)

    def _read_reply(self) -> object:
        prefix = self._readline()
        if not prefix:
            raise RedisProtocolError("empty Redis response")
        kind, value = chr(prefix[0]), prefix[1:]
        if kind == "+":
            return value.decode("utf-8")
        if kind == "-":
            raise RedisProtocolError(value.decode("utf-8"))
        if kind == ":":
            return int(value)
        if kind == "$":
            length = int(value)
            if length < 0:
                return None
            while len(self._buffer) < length + 2:
                chunk = self._socket.recv(4096)
                if not chunk:
                    raise RedisProtocolError("Redis closed during bulk response")
                self._buffer.extend(chunk)
            data = bytes(self._buffer[:length])
            del self._buffer[: length + 2]
            return data.decode("utf-8")
        if kind == "*":
            count = int(value)
            return [self._read_reply() for _ in range(count)]
        raise RedisProtocolError(f"unknown Redis response type: {kind!r}")


@dataclass(frozen=True)
class PlayerRating:
    player_id: str
    rating: int


def mysql_query(client: str, query: str, container: str = "", database: str = "") -> str:
    env = os.environ.copy()
    password = env.get("ARENA_MYSQL_PASSWORD", "")
    database = database or env.get("ARENA_MYSQL_DATABASE", "arena_cards")
    if container:
        command = [
            "docker", "exec", "-e", "MYSQL_PWD=" + password, container, "mysql",
            "--batch", "--skip-column-names", "--host=127.0.0.1", "--port=3306",
            "--user=" + env.get("ARENA_MYSQL_USER", "arena"),
            database, "-e", query,
        ]
    else:
        env["MYSQL_PWD"] = password
        command = [
            client, "--batch", "--skip-column-names",
            "--host=" + env.get("ARENA_MYSQL_HOST", "127.0.0.1"),
            "--port=" + env.get("ARENA_MYSQL_PORT", "3306"),
            "--user=" + env.get("ARENA_MYSQL_USER", "arena"),
            database, "-e", query,
        ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "mysql query failed")
    return result.stdout.strip()


def read_players(client: str, database: str, container: str) -> list[PlayerRating]:
    raw = mysql_query(
        client,
        "SELECT player_id,rating FROM players ORDER BY rating DESC,player_id ASC",
        container,
        database,
    )
    players: list[PlayerRating] = []
    for line in raw.splitlines():
        player_id, rating = line.split("\t", 1)
        players.append(PlayerRating(player_id, int(rating)))
    return players


def rebuild(players: list[PlayerRating], host: str, port: int, key: str) -> int:
    redis = RedisClient(host, port)
    temporary_key = f"{key}:rebuild:{os.getpid()}:{uuid.uuid4().hex}"
    renamed = False
    try:
        redis.command("DEL", temporary_key)
        for player in players:
            redis.command("ZADD", temporary_key, str(player.rating), player.player_id)
        if players:
            redis.command("RENAME", temporary_key, key)
            renamed = True
        else:
            redis.command("DEL", key)
        return len(players)
    finally:
        if not renamed:
            try:
                redis.command("DEL", temporary_key)
            except (OSError, RedisProtocolError):
                pass
        redis.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mysql-client", default=os.getenv("ARENA_MYSQL_CLIENT", "mysql"))
    parser.add_argument("--mysql-container", default=os.getenv("ARENA_MYSQL_CONTAINER", ""))
    parser.add_argument("--mysql-database", default=os.getenv("ARENA_MYSQL_DATABASE", "arena_cards"))
    parser.add_argument("--redis-host", default=os.getenv("ARENA_REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.getenv("ARENA_REDIS_PORT", "6379")))
    parser.add_argument("--key", default=os.getenv("ARENA_REDIS_LEADERBOARD_KEY", "arena:leaderboard:rating"))
    args = parser.parse_args()
    players = read_players(args.mysql_client, args.mysql_database, args.mysql_container)
    count = rebuild(players, args.redis_host, args.redis_port, args.key)
    print(f"rebuilt {args.key}: {count} players")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
