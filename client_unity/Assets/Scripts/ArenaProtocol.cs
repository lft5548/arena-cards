using System;
using System.Collections.Generic;
using System.Text;

namespace ArenaCards.Client
{
    public enum ArenaProtocolId : byte
    {
        TextV1 = 1,
        ProtoV1 = 2
    }

    public enum ArenaProtoMessageType : ushort
    {
        LoginReq = 0x4001,
        LoginResp = 0x4002,
        MatchJoinReq = 0x4003,
        MatchCancelReq = 0x4004,
        MatchFound = 0x4005,
        PlayCardReq = 0x4006,
        BattleSnapshot = 0x4007,
        BattleEvent = 0x4008,
        Error = 0x4009,
        Heartbeat = 0x400A,
        Pong = 0x400B,
        ReconnectReq = 0x400C,
        ReconnectResp = 0x400D,
        MatchResult = 0x400E,
        EndTurnReq = 0x400F,
        AdminRoomsReq = 0x4010,
        AdminRoomsResp = 0x4011,
        ActionAck = 0x4012,
        LeaderboardReq = 0x4015,
        LeaderboardResp = 0x4016
    }

    public enum ArenaMessageType : ushort
    {
        LoginReq = 1,
        LoginResp = 2,
        MatchJoinReq = 3,
        MatchCancelReq = 4,
        MatchFound = 5,
        PlayCardReq = 6,
        BattleSnapshot = 7,
        BattleEvent = 8,
        Error = 9,
        Heartbeat = 10,
        Pong = 11,
        ReconnectReq = 12,
        ReconnectResp = 13,
        MatchResult = 14,
        EndTurnReq = 15,
        AdminRoomsReq = 16,
        AdminRoomsResp = 17,
        ActionAck = 18,
        ProtocolHelloReq = 19,
        ProtocolHelloResp = 20,
        LeaderboardReq = 21,
        LeaderboardResp = 22
    }

    public readonly struct ArenaMessage
    {
        public readonly ArenaMessageType Type;
        public readonly string Payload;
        public readonly IReadOnlyDictionary<string, string> Fields;

        public ArenaMessage(ArenaMessageType type, string payload)
        {
            Type = type;
            Payload = payload ?? string.Empty;
            var parsed = ArenaProtocol.ParseFields(Payload);
            if (type == ArenaMessageType.LeaderboardResp)
            {
                var ranking = new Dictionary<string, string>(StringComparer.Ordinal);
                foreach (var field in parsed)
                    ranking[field.Key] = field.Key == "request_id" || field.Key.StartsWith("entry_", StringComparison.Ordinal) &&
                        field.Key.EndsWith("_user", StringComparison.Ordinal) ? field.Value.Replace("%25", "%") : field.Value;
                Fields = ranking;
            }
            else Fields = parsed;
        }

        public ArenaMessage(ArenaMessageType type, IReadOnlyDictionary<string, string> fields)
        {
            Type = type;
            Fields = fields ?? throw new ArgumentNullException(nameof(fields));
            var values = new List<string>();
            foreach (KeyValuePair<string, string> field in fields)
                values.Add(field.Key + "=" + ArenaProtocol.Escape(field.Value));
            Payload = string.Join(";", values);
        }

        public override string ToString()
        {
            return Type + (string.IsNullOrEmpty(Payload) ? string.Empty : " " + Payload);
        }
    }

    public static class ArenaProtocol
    {
        public const int HeaderSize = 6;
        public const int MaxFrame = 65536;

        public static byte[] Encode(ArenaMessageType type, string payload)
        {
            return EncodeFrame((ushort)type, Encoding.UTF8.GetBytes(payload ?? string.Empty));
        }

        internal static byte[] EncodeFrame(ushort type, byte[] payload)
        {
            int bodyLength = 2 + payload.Length;
            if (bodyLength > MaxFrame)
                throw new ArgumentException("Arena frame is too large", nameof(payload));
            byte[] frame = new byte[4 + bodyLength];
            WriteUInt32BE(frame, 0, (uint)bodyLength);
            WriteUInt16BE(frame, 4, type);
            Buffer.BlockCopy(payload, 0, frame, HeaderSize, payload.Length);
            return frame;
        }

        public static byte[] EncodeFields(ArenaMessageType type, params KeyValuePair<string, string>[] fields)
        {
            var values = new List<string>(fields.Length);
            foreach (KeyValuePair<string, string> field in fields)
                values.Add(field.Key + "=" + Escape(field.Value));
            return Encode(type, string.Join(";", values));
        }

        public static IReadOnlyDictionary<string, string> ParseFields(string payload)
        {
            var result = new Dictionary<string, string>(StringComparer.Ordinal);
            if (string.IsNullOrEmpty(payload))
                return result;
            foreach (string field in payload.Split(';'))
            {
                int separator = field.IndexOf('=');
                if (separator <= 0)
                    continue;
                string key = field.Substring(0, separator);
                result[key] = field.Substring(separator + 1).Replace("%3B", ";");
            }
            return result;
        }

        public static string Escape(string value)
        {
            return (value ?? string.Empty).Replace(";", "%3B");
        }

        internal static ArenaMessage DecodeBody(byte[] body, int offset, int length, ArenaProtocolId protocol)
        {
            if (length < 2)
                throw new FormatException("Arena message body is too short");
            ushort type = ReadUInt16BE(body, offset);
            if (protocol == ArenaProtocolId.ProtoV1)
                return ArenaProtoCodec.Decode(type, body, offset + 2, length - 2);
            if (type < 1 || type > 22)
                throw new FormatException("Unknown TextV1 message type: " + type);
            return new ArenaMessage((ArenaMessageType)type,
                Encoding.UTF8.GetString(body, offset + 2, length - 2));
        }

        internal static uint ReadUInt32BE(byte[] bytes, int offset)
        {
            return ((uint)bytes[offset] << 24) | ((uint)bytes[offset + 1] << 16) |
                ((uint)bytes[offset + 2] << 8) | bytes[offset + 3];
        }

        internal static ushort ReadUInt16BE(byte[] bytes, int offset)
        {
            return (ushort)(((uint)bytes[offset] << 8) | bytes[offset + 1]);
        }

        private static void WriteUInt32BE(byte[] bytes, int offset, uint value)
        {
            bytes[offset] = (byte)(value >> 24);
            bytes[offset + 1] = (byte)(value >> 16);
            bytes[offset + 2] = (byte)(value >> 8);
            bytes[offset + 3] = (byte)value;
        }

        private static void WriteUInt16BE(byte[] bytes, int offset, ushort value)
        {
            bytes[offset] = (byte)(value >> 8);
            bytes[offset + 1] = (byte)value;
        }
    }

    public sealed class ArenaFrameDecoder
    {
        private readonly List<byte> buffer = new List<byte>();
        public ArenaProtocolId Protocol { get; set; } = ArenaProtocolId.TextV1;

        public void Append(byte[] bytes, int offset, int count, List<ArenaMessage> output)
        {
            if (offset < 0 || count < 0 || offset > bytes.Length - count)
                throw new ArgumentOutOfRangeException(nameof(count));
            for (int index = 0; index < count; index++)
                buffer.Add(bytes[offset + index]);
            while (buffer.Count >= 4)
            {
                byte[] header = buffer.ToArray();
                uint bodyLength = ArenaProtocol.ReadUInt32BE(header, 0);
                if (bodyLength < 2 || bodyLength > ArenaProtocol.MaxFrame)
                    throw new FormatException("Invalid Arena frame length: " + bodyLength);
                int frameLength = 4 + (int)bodyLength;
                if (buffer.Count < frameLength)
                    return;
                byte[] body = buffer.GetRange(4, (int)bodyLength).ToArray();
                buffer.RemoveRange(0, frameLength);
                output.Add(ArenaProtocol.DecodeBody(body, 0, body.Length, Protocol));
            }
        }
    }
}
