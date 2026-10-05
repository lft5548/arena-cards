import struct, unittest

from client_pygame.protocol import (PROTO_MESSAGE_TYPES, PROTO_V1_MESSAGE_BASE,
                                    ProtocolId, make_proto_message, parse_message)

def frame(message_type, payload=""):
    body = struct.pack(">H", message_type) + payload.encode()
    return struct.pack(">I", len(body)) + body

class ProtocolFramingTest(unittest.TestCase):
    def test_leaderboard_percent_escape_preserves_literal_sequences(self):
        packet = frame(22, "ok=1;entry_0_user=rank%3B%253B%2525player;request_id=rank%253B%2525")
        self.assertEqual(parse_message(packet[4:])["payload"]["entry_0_user"], "rank;%3B%25player")
        self.assertEqual(parse_message(packet[4:])["payload"]["request_id"], "rank%3B%25")
        legacy = frame(2, "user=legacy%253B")
        self.assertEqual(parse_message(legacy[4:])["payload"]["user"], "legacy%253B")

    def test_big_endian_length(self):
        packet = frame(10)
        self.assertEqual(struct.unpack(">I", packet[:4])[0], len(packet) - 4)
        self.assertEqual(struct.unpack(">H", packet[4:6])[0], 10)

    def test_split_packet_reassembly(self):
        packet = frame(1, "player_id=7")
        left, right = packet[:3], packet[3:]
        buffer = left + right
        size = struct.unpack(">I", buffer[:4])[0]
        self.assertEqual(struct.unpack(">H", buffer[4:6])[0], 1)
        self.assertEqual(buffer[6:4 + size].decode(), "player_id=7")

    def test_protocol_namespaces_do_not_overlap(self):
        self.assertEqual(ProtocolId.TEXT_V1, 1)
        self.assertEqual(ProtocolId.PROTO_V1, 2)
        self.assertTrue(all(value >= PROTO_V1_MESSAGE_BASE for value in PROTO_MESSAGE_TYPES.values()))
        self.assertTrue(all(value > 20 for value in PROTO_MESSAGE_TYPES.values()))

    def test_binary_proto_payload_is_not_decoded_as_text(self):
        packet = make_proto_message("LoginReq", b"\x08\x03")
        decoded = parse_message(packet[4:])
        self.assertEqual(decoded["protocol"], "ProtoV1")
        self.assertEqual(decoded["payload_bytes"], b"\x08\x03")

if __name__ == "__main__":
    unittest.main()
