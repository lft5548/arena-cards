"""Check transport release, reconnect timeout, and room cleanup after gateway extraction."""
from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path

from status_effects_integration_test import MatchProbe
from protocol import read_message, send_message


async def run(server: str, port: int) -> None:
    rows = "1,Strike,2,damage,8,0\n2,Mend,1,heal,4,0\n3,Barrier,1,shield,6,0\n"
    with tempfile.TemporaryDirectory() as temporary:
        async with MatchProbe(server, port, Path(temporary) / "disconnect", rows) as match:
            peer = match.peers[0]
            peer["writer"].close()
            await peer["writer"].wait_closed()
            result = await asyncio.wait_for(read_message(match.peers[1]["reader"]), 20)
            if (result["type"] != "MatchResult" or result["payload"].get("reason") != "reconnect_timeout" or
                    result["payload"].get("winner") != "1" or result["payload"].get("turn_id") != "1"):
                raise AssertionError(f"disconnect was lost or duplicated: {result}")
            match.results = [result["payload"]]
            rebuilt, events = await match.verify_replay()
            if rebuilt["reason"] != "reconnect_timeout" or len(events) != 2:
                raise AssertionError("disconnect created extra battle events")
            match.compare_final_snapshot(rebuilt)
            deadline = asyncio.get_running_loop().time() + 3
            while True:
                await send_message(match.peers[1]["writer"], "AdminRoomsReq", "")
                metrics = await read_message(match.peers[1]["reader"])
                if (metrics["type"] == "AdminRoomsResp" and metrics["payload"].get("active_sessions") == "1" and
                        metrics["payload"].get("active_rooms") == "0"):
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(f"closed transport or finished room retained: {metrics}")
                await asyncio.sleep(0.02)
            await send_message(match.peers[1]["writer"], "Heartbeat", "")
            message = await read_message(match.peers[1]["reader"])
            if message["type"] != "Pong":
                raise AssertionError("terminal room emitted duplicate result")
    print("gateway disconnect/15-second timeout/transport release/room cleanup/replay passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19156)
    args = parser.parse_args()
    asyncio.run(run(os.path.abspath(args.server), args.port))


if __name__ == "__main__":
    main()
