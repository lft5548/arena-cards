"""Minimal black-box smoke test for a running Arena Cards server.

Wire format: 4-byte big-endian body length, 2-byte big-endian message type,
then an UTF-8 key=value payload. The test checks login and heartbeat.
"""
import argparse, socket, struct, sys

LOGIN_REQ, LOGIN_RESP, HEARTBEAT, PONG = 1, 2, 10, 11

def send(sock, message_type, payload=""):
    body = struct.pack(">H", message_type) + payload.encode()
    sock.sendall(struct.pack(">I", len(body)) + body)

def recv(sock):
    header = sock.recv(4)
    if len(header) != 4:
        raise RuntimeError("server closed before length header")
    size = struct.unpack(">I", header)[0]
    if size > 1 << 20:
        raise RuntimeError(f"invalid response size: {size}")
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("server closed during response")
        data += chunk
    return struct.unpack(">H", data[:2])[0], data[2:].decode(errors="replace")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    args = ap.parse_args()
    with socket.create_connection((args.host, args.port), timeout=3) as sock:
        sock.settimeout(3)
        send(sock, LOGIN_REQ, "smoke")
        login_type, login_payload = recv(sock)
        if login_type != LOGIN_RESP or "ok=1" not in login_payload:
            raise RuntimeError(f"unexpected login response: {login_type}: {login_payload!r}")
        send(sock, HEARTBEAT)
        pong_type, _ = recv(sock)
        if pong_type != PONG:
            raise RuntimeError(f"unexpected heartbeat response type: {pong_type}")
        print("smoke test passed: login + heartbeat")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"smoke test failed: {exc}", file=sys.stderr)
        sys.exit(1)
