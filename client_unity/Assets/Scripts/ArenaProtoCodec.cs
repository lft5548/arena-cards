using System;
using System.Collections.Generic;
using System.Globalization;
using Google.Protobuf;
using Proto = ArenaCards.Proto;

namespace ArenaCards.Client
{
    public static class ArenaProtoCodec
    {
        public static byte[] Encode(ArenaMessageType type, IReadOnlyDictionary<string, string> fields)
        {
            string requestId = Field(fields, "request_id");
            IMessage message;
            switch (type)
            {
                case ArenaMessageType.LoginReq:
                    message = new Proto.LoginRequest { User = Field(fields, "user"), RequestId = requestId };
                    break;
                case ArenaMessageType.MatchJoinReq:
                    message = new Proto.MatchJoinRequest { RequestId = requestId };
                    break;
                case ArenaMessageType.MatchCancelReq:
                    message = new Proto.MatchCancelRequest { RequestId = requestId };
                    break;
                case ArenaMessageType.PlayCardReq:
                    message = new Proto.PlayCardRequest { Context = RequestContext(fields), CardId = Integer(fields, "card") };
                    break;
                case ArenaMessageType.EndTurnReq:
                    message = new Proto.EndTurnRequest { Context = RequestContext(fields) };
                    break;
                case ArenaMessageType.ReconnectReq:
                    message = new Proto.ReconnectRequest { SessionToken = Field(fields, "token"), RequestId = requestId };
                    break;
                case ArenaMessageType.Heartbeat:
                    message = new Proto.Heartbeat { RequestId = requestId };
                    break;
                case ArenaMessageType.AdminRoomsReq:
                    message = new Proto.AdminRoomsRequest { RequestId = requestId };
                    break;
                case ArenaMessageType.LeaderboardReq:
                    var rankingRequest = new Proto.LeaderboardRequest { RequestId = requestId };
                    if (fields.ContainsKey("limit")) rankingRequest.Limit = checked((uint)Integer(fields, "limit"));
                    message = rankingRequest;
                    break;
                default:
                    throw new ArgumentOutOfRangeException(nameof(type), type, "Not a client request");
            }
            return ArenaProtocol.EncodeFrame((ushort)(0x4000 + (ushort)type), message.ToByteArray());
        }

        public static ArenaMessage Decode(ushort wireType, byte[] bytes, int offset, int length)
        {
            if ((wireType < 0x4001 || wireType > 0x4012) && wireType != 0x4016)
                throw new FormatException("Unknown ProtoV1 message type: " + wireType);
            ArenaMessageType type = (ArenaMessageType)(wireType - 0x4000);
            var fields = new Dictionary<string, string>(StringComparer.Ordinal);
            switch (type)
            {
                case ArenaMessageType.LoginResp:
                    var login = Proto.LoginResponse.Parser.ParseFrom(bytes, offset, length);
                    Put(fields, "ok", login.Ok);
                    fields["user"] = login.User;
                    fields["token"] = login.SessionToken;
                    fields["code"] = login.ErrorCode;
                    fields["request_id"] = login.RequestId;
                    break;
                case ArenaMessageType.MatchJoinReq:
                    var join = Proto.MatchJoinResponse.Parser.ParseFrom(bytes, offset, length);
                    Put(fields, "queued", join.Queued);
                    fields["request_id"] = join.RequestId;
                    break;
                case ArenaMessageType.MatchCancelReq:
                    var cancel = Proto.MatchCancelResponse.Parser.ParseFrom(bytes, offset, length);
                    Put(fields, "cancelled", cancel.Cancelled);
                    fields["request_id"] = cancel.RequestId;
                    break;
                case ArenaMessageType.MatchFound:
                    var found = Proto.MatchFound.Parser.ParseFrom(bytes, offset, length);
                    AddContext(fields, found.Context);
                    Put(fields, "player_index", found.PlayerIndex);
                    Put(fields, "turn", found.Turn);
                    Put(fields, "room", found.Room);
                    break;
                case ArenaMessageType.BattleSnapshot:
                    var snapshot = Proto.BattleSnapshot.Parser.ParseFrom(bytes, offset, length);
                    AddContext(fields, snapshot.Context);
                    Put(fields, "player_index", snapshot.PlayerIndex);
                    AddState(fields, snapshot.State);
                    break;
                case ArenaMessageType.BattleEvent:
                    var battleEvent = Proto.BattleEvent.Parser.ParseFrom(bytes, offset, length);
                    AddContext(fields, battleEvent.Context);
                    fields["type"] = battleEvent.Type;
                    Put(fields, "player", battleEvent.Player);
                    Put(fields, "card", battleEvent.CardId);
                    Put(fields, "value", battleEvent.Value);
                    Put(fields, "duration", battleEvent.Duration);
                    fields["status"] = battleEvent.Status;
                    Put(fields, "remaining", battleEvent.Remaining);
                    Put(fields, "hp", battleEvent.Hp);
                    Put(fields, "target", battleEvent.Target);
                    Put(fields, "count", battleEvent.Count);
                    if (!string.IsNullOrEmpty(battleEvent.Phase)) fields["phase"] = battleEvent.Phase;
                    if (battleEvent.HasBonus) Put(fields, "bonus", battleEvent.Bonus);
                    if (battleEvent.Type == "attack_boost" || battleEvent.Type == "heal_boost")
                        Put(fields, "uses", battleEvent.Uses);
                    break;
                case ArenaMessageType.ActionAck:
                    var ack = Proto.ActionAck.Parser.ParseFrom(bytes, offset, length);
                    AddContext(fields, ack.Context);
                    fields["status"] = ack.Applied ? "applied" : "rejected";
                    fields["code"] = ack.ErrorCode;
                    break;
                case ArenaMessageType.MatchResult:
                    var result = Proto.MatchResult.Parser.ParseFrom(bytes, offset, length);
                    AddContext(fields, result.Context);
                    Put(fields, "winner", result.Winner);
                    fields["reason"] = result.Reason;
                    AddReplay(fields, result.Replay);
                    break;
                case ArenaMessageType.ReconnectResp:
                    var reconnect = Proto.ReconnectResponse.Parser.ParseFrom(bytes, offset, length);
                    AddContext(fields, reconnect.Context);
                    Put(fields, "ok", reconnect.Ok);
                    Put(fields, "player_index", reconnect.PlayerIndex);
                    fields["code"] = reconnect.ErrorCode;
                    break;
                case ArenaMessageType.Error:
                    var error = Proto.ErrorResponse.Parser.ParseFrom(bytes, offset, length);
                    AddContext(fields, error.Context);
                    fields["code"] = error.Code;
                    fields["message"] = error.Message;
                    break;
                case ArenaMessageType.Pong:
                    fields["request_id"] = Proto.Pong.Parser.ParseFrom(bytes, offset, length).RequestId;
                    break;
                case ArenaMessageType.AdminRoomsResp:
                    var rooms = Proto.AdminRoomsResponse.Parser.ParseFrom(bytes, offset, length);
                    foreach (KeyValuePair<string, ulong> counter in rooms.Counters)
                        Put(fields, counter.Key, counter.Value);
                    fields["request_id"] = rooms.RequestId;
                    break;
                case ArenaMessageType.LeaderboardResp:
                    var ranking = Proto.LeaderboardResponse.Parser.ParseFrom(bytes, offset, length);
                    Put(fields, "ok", ranking.Ok);
                    fields["code"] = ranking.ErrorCode; fields["source"] = ranking.Source;
                    fields["request_id"] = ranking.RequestId; Put(fields, "count", ranking.Entries.Count);
                    for (int index = 0; index < ranking.Entries.Count; index++)
                    {
                        var entry = ranking.Entries[index]; string prefix = "entry_" + index + "_";
                        Put(fields, prefix + "rank", entry.Rank); fields[prefix + "user"] = entry.PlayerId;
                        Put(fields, prefix + "rating", entry.Rating); Put(fields, prefix + "wins", entry.Wins);
                        Put(fields, prefix + "losses", entry.Losses);
                    }
                    break;
                default:
                    throw new FormatException("Unexpected ProtoV1 server message: " + type);
            }
            return new ArenaMessage(type, fields);
        }

        private static Proto.RequestContext RequestContext(IReadOnlyDictionary<string, string> fields)
        {
            return new Proto.RequestContext
            {
                MatchId = Field(fields, "match_id"),
                TurnId = Unsigned(fields, "turn_id"),
                ActionId = Unsigned(fields, "action_id"),
                Revision = Unsigned(fields, "revision"),
                RequestId = Field(fields, "request_id")
            };
        }

        private static void AddContext(Dictionary<string, string> fields, Proto.RequestContext context)
        {
            if (context == null)
                throw new FormatException("ProtoV1 response is missing its context");
            fields["match_id"] = context.MatchId;
            Put(fields, "turn_id", context.TurnId);
            Put(fields, "action_id", context.ActionId);
            Put(fields, "revision", context.Revision);
            fields["request_id"] = context.RequestId;
        }

        private static void AddState(Dictionary<string, string> fields, Proto.BattleState state)
        {
            if (state == null || state.Players.Count != 2)
                throw new FormatException("ProtoV1 snapshot must contain two players");
            Put(fields, "turn", state.Turn);
            Put(fields, "revision", state.SnapshotRevision);
            Put(fields, "remaining_ms", state.RemainingMs);
            Put(fields, "done", state.Done);
            Put(fields, "last_action_id", state.LastActionId);
            for (int index = 0; index < state.Players.Count; index++)
            {
                Proto.PlayerState player = state.Players[index];
                string prefix = "p" + index.ToString(CultureInfo.InvariantCulture) + "_";
                Put(fields, prefix + "hp", player.Hp);
                Put(fields, prefix + "energy", player.Energy);
                Put(fields, prefix + "shield", player.Shield);
                Put(fields, prefix + "poison_value", player.Poison == null ? 0 : player.Poison.Value);
                Put(fields, prefix + "poison_turns", player.Poison == null ? 0 : player.Poison.Turns);
                Put(fields, prefix + "regen_value", player.Regen == null ? 0 : player.Regen.Value);
                Put(fields, prefix + "regen_turns", player.Regen == null ? 0 : player.Regen.Turns);
                Put(fields, prefix + "burn_value", player.Burn == null ? 0 : player.Burn.Value);
                Put(fields, prefix + "burn_turns", player.Burn == null ? 0 : player.Burn.Turns);
                Put(fields, prefix + "attack_boost_value", player.AttackBoost == null ? 0 : player.AttackBoost.Value);
                Put(fields, prefix + "attack_boost_uses", player.AttackBoost == null ? 0 : player.AttackBoost.Uses);
                Put(fields, prefix + "heal_boost_value", player.HealBoost == null ? 0 : player.HealBoost.Value);
                Put(fields, prefix + "heal_boost_uses", player.HealBoost == null ? 0 : player.HealBoost.Uses);
            }
            fields["hand"] = CardList(state.Hand);
            fields["discard"] = CardList(state.Discard);
            Put(fields, "opponent_hand_count", state.OpponentHandCount);
            Put(fields, "deck_count", state.DeckCount);
            Put(fields, "discard_count", state.DiscardCount);
            Put(fields, "opponent_discard_count", state.OpponentDiscardCount);
            AddReplay(fields, state.Replay);
        }

        private static void AddReplay(Dictionary<string, string> fields, Proto.ReplayMetadata replay)
        {
            if (replay == null)
                throw new FormatException("ProtoV1 response is missing replay metadata");
            Put(fields, "replay_seed", replay.Seed);
            Put(fields, "replay_revision", replay.Revision);
            Put(fields, "replay_digest", replay.Digest);
            Put(fields, "replay_valid", replay.Valid);
        }

        private static string CardList(IEnumerable<int> cards)
        {
            var values = new List<string>();
            foreach (int card in cards)
                values.Add(card.ToString(CultureInfo.InvariantCulture));
            return string.Join(",", values);
        }

        private static string Field(IReadOnlyDictionary<string, string> fields, string key)
        {
            string value;
            return fields.TryGetValue(key, out value) ? value : string.Empty;
        }

        private static ulong Unsigned(IReadOnlyDictionary<string, string> fields, string key)
        {
            string value = Field(fields, key);
            return value.Length == 0 ? 0 : ulong.Parse(value, NumberStyles.None, CultureInfo.InvariantCulture);
        }

        private static int Integer(IReadOnlyDictionary<string, string> fields, string key)
        {
            return int.Parse(Field(fields, key), NumberStyles.Integer, CultureInfo.InvariantCulture);
        }

        private static void Put(Dictionary<string, string> fields, string key, bool value)
        {
            fields[key] = value ? "1" : "0";
        }

        private static void Put<T>(Dictionary<string, string> fields, string key, T value) where T : IFormattable
        {
            fields[key] = value.ToString(null, CultureInfo.InvariantCulture);
        }
    }
}
