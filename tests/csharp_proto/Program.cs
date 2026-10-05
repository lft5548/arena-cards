using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using ArenaCards.Client;
using Google.Protobuf;
using Proto = ArenaCards.Proto;

internal static class Program
{
    private static int checks;

    private static void Require(bool condition, string message)
    {
        checks++;
        if (!condition) throw new InvalidOperationException(message);
    }

    private static void Reject(Action action, string message)
    {
        bool rejected = false;
        try { action(); }
        catch (FormatException) { rejected = true; }
        catch (InvalidProtocolBufferException) { rejected = true; }
        catch (ArgumentException) { rejected = true; }
        catch (OverflowException) { rejected = true; }
        Require(rejected, message);
    }

    private static byte[] Frame(ArenaMessageType type, IMessage message)
    {
        return ArenaProtocol.EncodeFrame((ushort)(0x4000 + (ushort)type), message.ToByteArray());
    }

    private static ArenaMessage Decode(ArenaMessageType type, IMessage message)
    {
        byte[] frame = Frame(type, message);
        var decoder = new ArenaFrameDecoder { Protocol = ArenaProtocolId.ProtoV1 };
        var messages = new List<ArenaMessage>();
        for (int index = 0; index < frame.Length; index++)
            decoder.Append(frame, index, 1, messages);
        Require(messages.Count == 1 && messages[0].Type == type, "Fragmented binary frame failed: " + type);
        return messages[0];
    }

    private static string Number(ulong value) { return value.ToString(CultureInfo.InvariantCulture); }

    private static Proto.RequestContext Context()
    {
        return new Proto.RequestContext
        {
            MatchId = "match-protocol", TurnId = ulong.MaxValue, ActionId = ulong.MaxValue - 1,
            Revision = ulong.MaxValue - 2, RequestId = "request;literal%3B"
        };
    }

    private static Proto.ReplayMetadata Replay()
    {
        return new Proto.ReplayMetadata
        {
            Seed = ulong.MaxValue, Revision = ulong.MaxValue - 3, Digest = ulong.MaxValue - 4, Valid = true
        };
    }

    private static void ProtocolTests()
    {
        var fields = new Dictionary<string, string>
        {
            ["user"] = "user;literal%3B", ["token"] = "token;literal%3B", ["request_id"] = "req-1",
            ["match_id"] = "match-protocol", ["turn_id"] = Number(ulong.MaxValue),
            ["action_id"] = Number(ulong.MaxValue - 1), ["revision"] = Number(ulong.MaxValue - 2),
            ["card"] = "9"
        };
        byte[] frame = ArenaProtoCodec.Encode(ArenaMessageType.LoginReq, fields);
        Require(frame[4] == 0x40 && frame[5] == 1, "Binary login ID wrong");
        Proto.LoginRequest loginRequest = Proto.LoginRequest.Parser.ParseFrom(frame, 6, frame.Length - 6);
        Require(loginRequest.User == fields["user"] && loginRequest.RequestId == "req-1", "Login encoding lost fields");
        frame = ArenaProtoCodec.Encode(ArenaMessageType.PlayCardReq, fields);
        Proto.PlayCardRequest playRequest = Proto.PlayCardRequest.Parser.ParseFrom(frame, 6, frame.Length - 6);
        Require(playRequest.CardId == 9 && playRequest.Context.TurnId == ulong.MaxValue &&
            playRequest.Context.ActionId == ulong.MaxValue - 1 && playRequest.Context.Revision == ulong.MaxValue - 2,
            "PlayCard lost uint64 values");
        frame = ArenaProtoCodec.Encode(ArenaMessageType.EndTurnReq, fields);
        Require(Proto.EndTurnRequest.Parser.ParseFrom(frame, 6, frame.Length - 6).Context.ActionId == ulong.MaxValue - 1,
            "EndTurn lost action ID");
        frame = ArenaProtoCodec.Encode(ArenaMessageType.ReconnectReq, fields);
        Require(Proto.ReconnectRequest.Parser.ParseFrom(frame, 6, frame.Length - 6).SessionToken == fields["token"],
            "Reconnect token was text-escaped");
        frame = ArenaProtoCodec.Encode(ArenaMessageType.MatchJoinReq, fields);
        Require(Proto.MatchJoinRequest.Parser.ParseFrom(frame, 6, frame.Length - 6).RequestId == "req-1", "Join encoding failed");
        frame = ArenaProtoCodec.Encode(ArenaMessageType.MatchCancelReq, fields);
        Require(Proto.MatchCancelRequest.Parser.ParseFrom(frame, 6, frame.Length - 6).RequestId == "req-1", "Cancel encoding failed");
        frame = ArenaProtoCodec.Encode(ArenaMessageType.Heartbeat, fields);
        Require(Proto.Heartbeat.Parser.ParseFrom(frame, 6, frame.Length - 6).RequestId == "req-1", "Heartbeat encoding failed");
        frame = ArenaProtoCodec.Encode(ArenaMessageType.AdminRoomsReq, fields);
        Require(Proto.AdminRoomsRequest.Parser.ParseFrom(frame, 6, frame.Length - 6).RequestId == "req-1", "Admin encoding failed");

        ArenaMessage login = Decode(ArenaMessageType.LoginResp, new Proto.LoginResponse
        {
            Ok = true, User = "user;literal%3B", SessionToken = "token;literal%3B", RequestId = "login-1"
        });
        Require(login.Fields["user"] == "user;literal%3B" && login.Fields["token"] == "token;literal%3B",
            "Structured response was reparsed through TextV1");
        Require(login.Fields["ok"] == "1" && login.Fields["request_id"] == "login-1", "Login response failed");
        Require(Decode(ArenaMessageType.MatchJoinReq, new Proto.MatchJoinResponse { Queued = true }).Fields["queued"] == "1",
            "Join response failed");
        Require(Decode(ArenaMessageType.MatchCancelReq, new Proto.MatchCancelResponse { Cancelled = true }).Fields["cancelled"] == "1",
            "Cancel response failed");
        ArenaMessage found = Decode(ArenaMessageType.MatchFound, new Proto.MatchFound
        {
            Context = Context(), PlayerIndex = 1, Turn = 0, Room = uint.MaxValue
        });
        Require(found.Fields["turn_id"] == Number(ulong.MaxValue) && found.Fields["room"] == "4294967295",
            "MatchFound precision failed");
        var state = new Proto.BattleState
        {
            Turn = 1, SnapshotRevision = ulong.MaxValue - 5, RemainingMs = 15000, Done = false,
            OpponentHandCount = 2, DeckCount = 17, DiscardCount = 2, OpponentDiscardCount = 1,
            LastActionId = ulong.MaxValue - 6, Replay = Replay()
        };
        state.Players.Add(new Proto.PlayerState
        {
            Hp = 29, Energy = 3, Shield = 6,
            Poison = new Proto.StatusState { Value = 3, Turns = 2 },
            Regen = new Proto.StatusState { Value = 2, Turns = 1 },
            Burn = new Proto.StatusState { Value = 4, Turns = 2 },
            AttackBoost = new Proto.BonusState { Value = 3, Uses = 2 },
            HealBoost = new Proto.BonusState { Value = 4, Uses = 1 }
        });
        state.Players.Add(new Proto.PlayerState { Hp = 25, Energy = 1, Shield = 0 });
        state.Hand.Add(new[] { 9, 8, 3 });
        state.Discard.Add(new[] { 1, 2 });
        ArenaMessage snapshot = Decode(ArenaMessageType.BattleSnapshot, new Proto.BattleSnapshot
        {
            Context = Context(), PlayerIndex = 0, State = state
        });
        Require(snapshot.Fields["hand"] == "9,8,3" && snapshot.Fields["discard"] == "1,2", "Private card order failed");
        Require(snapshot.Fields["opponent_hand_count"] == "2" && snapshot.Fields["opponent_discard_count"] == "1" &&
            !snapshot.Fields.ContainsKey("opponent_hand") && !snapshot.Fields.ContainsKey("opponent_discard"), "Privacy fields wrong");
        Require(snapshot.Fields["p0_poison_value"] == "3" && snapshot.Fields["p0_poison_turns"] == "2" &&
            snapshot.Fields["p0_regen_value"] == "2" && snapshot.Fields["p0_regen_turns"] == "1" &&
            snapshot.Fields["p1_poison_value"] == "0", "Status fields wrong");
        Require(snapshot.Fields["p0_hp"] == "29" && snapshot.Fields["p0_energy"] == "3" &&
            snapshot.Fields["p0_shield"] == "6" && snapshot.Fields["p1_hp"] == "25", "Player state wrong");
        Require(snapshot.Fields["p0_burn_value"] == "4" && snapshot.Fields["p0_burn_turns"] == "2" &&
            snapshot.Fields["p1_burn_turns"] == "0", "Burn state failed");
        Require(snapshot.Fields["p0_attack_boost_uses"] == "2" && snapshot.Fields["p0_heal_boost_value"] == "4" &&
            snapshot.Fields["p1_heal_boost_uses"] == "0", "Bonus slots failed");
        var bonusEvent = new Proto.BattleEvent { Context = Context(), Type = "damage", CardId = 1, Value = 8 };
        Require(!Decode(ArenaMessageType.BattleEvent, bonusEvent).Fields.ContainsKey("bonus"), "Legacy bonus absence failed");
        bonusEvent.Bonus = 0;
        Require(Decode(ArenaMessageType.BattleEvent, bonusEvent).Fields["bonus"] == "0", "Explicit zero bonus lost");
        bonusEvent.Bonus = 2;
        Require(Decode(ArenaMessageType.BattleEvent, bonusEvent).Fields["bonus"] == "2", "Applied bonus lost");
        Require(Decode(ArenaMessageType.BattleEvent, new Proto.BattleEvent {
            Context = Context(), Type = "attack_boost", CardId = 11, Value = 2, Uses = 2
        }).Fields["uses"] == "2", "Bonus application count failed");
        Require(snapshot.Fields["revision"] == Number(ulong.MaxValue - 5) &&
            snapshot.Fields["replay_revision"] == Number(ulong.MaxValue - 3) &&
            snapshot.Fields["replay_seed"] == Number(ulong.MaxValue) &&
            snapshot.Fields["replay_digest"] == Number(ulong.MaxValue - 4) &&
            snapshot.Fields["last_action_id"] == Number(ulong.MaxValue - 6), "Snapshot uint64 precision failed");
        ArenaMessage battleEvent = Decode(ArenaMessageType.BattleEvent, new Proto.BattleEvent
        {
            Context = Context(), Type = "discard", Player = 0, CardId = 9, Value = 2,
            Duration = 2, Status = "poison", Remaining = 1, Hp = 24, Target = 1, Count = 2
        });
        Require(battleEvent.Fields["card"] == "9" && battleEvent.Fields["target"] == "1" &&
            battleEvent.Fields["count"] == "2" && battleEvent.Fields["status"] == "poison" &&
            battleEvent.Fields["remaining"] == "1" && battleEvent.Fields["hp"] == "24", "Event fields failed");
        ArenaMessage endTick = Decode(ArenaMessageType.BattleEvent, new Proto.BattleEvent
        {
            Context = Context(), Type = "status_tick", Player = 1, Status = "burn",
            Phase = "end", Value = 3, Remaining = 0, Hp = 22
        });
        Require(endTick.Fields["phase"] == "end" && endTick.Fields["status"] == "burn" &&
            endTick.Fields["remaining"] == "0", "Turn-end event phase failed");
        ArenaMessage ack = Decode(ArenaMessageType.ActionAck, new Proto.ActionAck { Context = Context(), Applied = true });
        Require(ack.Fields["status"] == "applied" && ack.Fields["action_id"] == Number(ulong.MaxValue - 1), "ACK failed");
        Require(Decode(ArenaMessageType.ActionAck, new Proto.ActionAck
        {
            Context = Context(), Applied = false, ErrorCode = "stale_turn"
        }).Fields["code"] == "stale_turn", "Rejected ACK failed");
        ArenaMessage result = Decode(ArenaMessageType.MatchResult, new Proto.MatchResult
        {
            Context = Context(), Winner = -1, Reason = "draw", Replay = Replay()
        });
        Require(result.Fields["winner"] == "-1" && result.Fields["replay_digest"] == Number(ulong.MaxValue - 4), "Result failed");
        Require(Decode(ArenaMessageType.ReconnectResp, new Proto.ReconnectResponse
        {
            Context = Context(), Ok = true, PlayerIndex = 1
        }).Fields["player_index"] == "1", "Reconnect response failed");
        Require(Decode(ArenaMessageType.Error, new Proto.ErrorResponse
        {
            Context = Context(), Code = "bad_action", Message = "error;literal%3B"
        }).Fields["message"] == "error;literal%3B", "Error fields failed");
        Require(Decode(ArenaMessageType.Pong, new Proto.Pong { RequestId = "ping-1" }).Fields["request_id"] == "ping-1", "Pong failed");
        var counters = new Proto.AdminRoomsResponse { RequestId = "rooms-1" };
        counters.Counters.Add("active_rooms", ulong.MaxValue);
        Require(Decode(ArenaMessageType.AdminRoomsResp, counters).Fields["active_rooms"] == Number(ulong.MaxValue), "Admin uint64 failed");

        var textDecoder = new ArenaFrameDecoder();
        var textMessages = new List<ArenaMessage>();
        byte[] textFrames = ArenaProtocol.Encode(ArenaMessageType.Pong, "").Concat(
            ArenaProtocol.Encode(ArenaMessageType.LoginResp, "ok=1;token=text-token")).ToArray();
        textDecoder.Append(textFrames, 0, 3, textMessages);
        Require(textMessages.Count == 0, "Partial text frame produced a message");
        textDecoder.Append(textFrames, 3, textFrames.Length - 3, textMessages);
        Require(textMessages.Count == 2 && textMessages[1].Fields["token"] == "text-token", "Coalesced text frames failed");
        var protoDecoder = new ArenaFrameDecoder { Protocol = ArenaProtocolId.ProtoV1 };
        var protoMessages = new List<ArenaMessage>();
        byte[] protoFrames = Frame(ArenaMessageType.Pong, new Proto.Pong { RequestId = "one" }).Concat(
            Frame(ArenaMessageType.Pong, new Proto.Pong { RequestId = "two" })).ToArray();
        protoDecoder.Append(protoFrames, 0, protoFrames.Length, protoMessages);
        Require(protoMessages.Count == 2 && protoMessages[1].Fields["request_id"] == "two", "Coalesced ProtoV1 frames failed");
        Reject(() => ArenaProtoCodec.Encode(ArenaMessageType.MatchResult, fields), "Client encoded a server response");
        Reject(() => ArenaProtoCodec.Decode(0x4013, Array.Empty<byte>(), 0, 0), "Unknown binary ID accepted");
        Reject(() => ArenaProtoCodec.Decode(0x4001, Array.Empty<byte>(), 0, 0), "Server sent a request direction");
        Reject(() => ArenaProtoCodec.Decode(0x400B, new byte[] { 0x80 }, 0, 1), "Malformed protobuf accepted");
        Reject(() => Decode(ArenaMessageType.BattleSnapshot, new Proto.BattleSnapshot { Context = Context() }), "Missing state accepted");
        Reject(() => Decode(ArenaMessageType.ActionAck, new Proto.ActionAck()), "Missing context accepted");
        Reject(() => new ArenaFrameDecoder().Append(protoFrames, 0, protoFrames.Length, new List<ArenaMessage>()), "Unnegotiated binary accepted");
        byte[] textPong = ArenaProtocol.Encode(ArenaMessageType.Pong, "");
        Reject(() => new ArenaFrameDecoder { Protocol = ArenaProtocolId.ProtoV1 }.Append(
            textPong, 0, textPong.Length, new List<ArenaMessage>()), "Text frame accepted after ProtoV1 selection");
        Reject(() => new ArenaFrameDecoder().Append(new byte[] { 0, 0, 0, 1 }, 0, 4, new List<ArenaMessage>()), "Short frame accepted");
        Reject(() => new ArenaFrameDecoder().Append(new byte[] { 0, 1, 0, 1 }, 0, 4, new List<ArenaMessage>()), "Oversized frame accepted");
        Reject(() => ArenaProtocol.Encode(ArenaMessageType.LoginReq, new string('x', ArenaProtocol.MaxFrame)), "Oversized encode accepted");
        fields["action_id"] = "18446744073709551616";
        Reject(() => ArenaProtoCodec.Encode(ArenaMessageType.EndTurnReq, fields), "Overflowing uint64 accepted");
        Require(new ArenaClient().protocol == ArenaProtocolId.TextV1, "TextV1 is no longer the default");
        var rankingRequest = Proto.LeaderboardRequest.Parser.ParseFrom(
            ArenaProtoCodec.Encode(ArenaMessageType.LeaderboardReq,
                new Dictionary<string, string> { { "limit", "10" }, { "request_id", "rank-query" } }).Skip(6).ToArray());
        Require(rankingRequest.HasLimit && rankingRequest.Limit == 10 && rankingRequest.RequestId == "rank-query",
                "Typed ranking request fields");
        var ranking = new Proto.LeaderboardResponse { Ok = true, Source = "mysql", RequestId = "rank-query" };
        ranking.Entries.Add(new Proto.LeaderboardEntry { Rank = 1, PlayerId = "alice;literal%3B", Rating = long.MinValue,
            Wins = ulong.MaxValue, Losses = 2 });
        var rankingDecoded = Decode(ArenaMessageType.LeaderboardResp, ranking);
        Require(rankingDecoded.Fields["entry_0_user"] == "alice;literal%3B" &&
            rankingDecoded.Fields["entry_0_rating"] == long.MinValue.ToString(CultureInfo.InvariantCulture) &&
            rankingDecoded.Fields["entry_0_wins"] == Number(ulong.MaxValue), "Ranking names and 64-bit fields preserved");
        var rankingText = new ArenaMessage(ArenaMessageType.LeaderboardResp,
            "entry_0_user=alice%3Bliteral%253B;ok=1;count=1;request_id=rank%253B%2525");
        Require(rankingText.Fields["entry_0_user"] == "alice;literal%3B" && rankingText.Fields["request_id"] == "rank%3B%25",
            "Text ranking percent escapes decode exactly once");
        Console.WriteLine("C# protocol acceptance passed: " + checks + " checks");
    }

    private sealed class Peer : IDisposable
    {
        public readonly ArenaClient Client;
        public readonly List<ArenaMessage> History = new List<ArenaMessage>();
        public readonly List<ArenaMessage> Errors = new List<ArenaMessage>();
        public ArenaMessage? Snapshot;
        public ArenaMessage? Result;
        public string Token;
        public ulong ActionId = ulong.MaxValue - 1000;

        public Peer(string host, int port, ArenaProtocolId protocol, string user)
        {
            Client = new ArenaClient { host = host, port = port, protocol = protocol, username = user };
            Client.MessageReceived += message =>
            {
                History.Add(message);
                if (message.Type == ArenaMessageType.LoginResp) Token = message.Fields["token"];
                if (message.Type == ArenaMessageType.BattleSnapshot) Snapshot = message;
                if (message.Type == ArenaMessageType.MatchResult) Result = message;
                if (message.Type == ArenaMessageType.Error && message.Fields["code"] != "disconnected")
                    Errors.Add(message);
            };
        }

        public int Count(ArenaMessageType type) { return History.Count(message => message.Type == type); }
        public IReadOnlyDictionary<string, string> State { get { return Snapshot.Value.Fields; } }
        public ulong TurnId { get { return ulong.Parse(State["turn_id"], CultureInfo.InvariantCulture); } }
        public int Index { get { return int.Parse(State["player_index"], CultureInfo.InvariantCulture); } }
        public void Dispose() { Client.Disconnect(); Client.DispatchPending(); }
    }

    private static async Task WaitFor(Peer[] peers, Func<bool> predicate, string description, bool allowErrors = false)
    {
        var stopwatch = Stopwatch.StartNew();
        while (true)
        {
            foreach (Peer peer in peers)
            {
                peer.Client.DispatchPending();
                if (!allowErrors && peer.Errors.Count > 0)
                    throw new InvalidOperationException(description + ": " + peer.Errors[0]);
            }
            if (predicate()) return;
            if (stopwatch.ElapsedMilliseconds > 10000)
                throw new TimeoutException(description);
            await Task.Delay(5);
        }
    }

    private static async Task Apply(Peer[] peers, int? card = null)
    {
        Peer actor = peers[int.Parse(peers[0].State["turn"], CultureInfo.InvariantCulture)];
        ulong before = actor.TurnId;
        ulong actionId = ++actor.ActionId;
        string requestId = "action-" + Number(actionId);
        string matchId = actor.State["match_id"];
        if (card.HasValue) actor.Client.PlayCard(matchId, before, card.Value, actionId, 0, requestId);
        else actor.Client.EndTurn(matchId, before, actionId, 0, requestId);
        await WaitFor(peers, () => actor.History.Any(message => message.Type == ArenaMessageType.ActionAck &&
            message.Fields["action_id"] == Number(actionId)) &&
            (peers.All(peer => peer.Result.HasValue) || peers.All(peer => peer.TurnId > before)), "action acknowledgement/snapshots");
        ArenaMessage ack = actor.History.Last(message => message.Type == ArenaMessageType.ActionAck);
        Require(ack.Fields["status"] == "applied" && ack.Fields["action_id"] == Number(actionId), "Action ACK lost uint64 ID");
        if (actor.Client.protocol == ArenaProtocolId.ProtoV1)
            Require(ack.Fields["request_id"] == requestId, "ACK did not echo request_id");
    }

    private static void CheckPrivate(Peer[] peers)
    {
        foreach (Peer peer in peers)
        {
            Require(!peer.State.ContainsKey("opponent_hand") && !peer.State.ContainsKey("opponent_discard"), "Private identities leaked");
            Require(peer.State["replay_digest"] == peers[1 - peer.Index].State["replay_digest"], "Peers disagree on digest");
            int ownDiscard = peer.State["discard"].Length == 0 ? 0 : peer.State["discard"].Split(',').Length;
            Require(ownDiscard.ToString(CultureInfo.InvariantCulture) == peer.State["discard_count"], "Discard count disagrees with private history");
            Require(peer.State["opponent_discard_count"] == peers[1 - peer.Index].State["discard_count"], "Opponent discard count wrong");
        }
    }

    private static async Task NetworkBattle(string host, int port, ArenaProtocolId otherProtocol)
    {
        string suffix = Guid.NewGuid().ToString("N").Substring(0, 12);
        using (var first = new Peer(host, port, ArenaProtocolId.ProtoV1, "csharp_a_" + suffix))
        using (var second = new Peer(host, port, otherProtocol, "csharp_b_" + suffix))
        {
            Peer[] peers = { first, second };
            await first.Client.LoginAndConnectAsync();
            await second.Client.LoginAndConnectAsync();
            await WaitFor(peers, () => first.Token != null && second.Token != null, "login");
            Require(first.Count(ArenaMessageType.ProtocolHelloResp) == 1, "ProtoV1 login was not negotiated");
            if (otherProtocol == ArenaProtocolId.TextV1)
                Require(second.Count(ArenaMessageType.ProtocolHelloResp) == 0, "Default TextV1 unnecessarily negotiated");
            first.Client.SendHeartbeat();
            second.Client.SendHeartbeat();
            await WaitFor(peers, () => peers.All(peer => peer.Count(ArenaMessageType.Pong) == 1), "heartbeat");
            first.Client.RequestRooms();
            await WaitFor(peers, () => first.Count(ArenaMessageType.AdminRoomsResp) == 1, "structured admin counters");
            first.Client.JoinMatch();
            await WaitFor(peers, () => first.Count(ArenaMessageType.MatchJoinReq) == 1, "queue join");
            first.Client.CancelMatch();
            await WaitFor(peers, () => first.Count(ArenaMessageType.MatchCancelReq) == 1, "queue cancellation");
            first.Client.JoinMatch();
            second.Client.JoinMatch();
            await WaitFor(peers, () => peers.All(peer => peer.Snapshot.HasValue && peer.Count(ArenaMessageType.MatchFound) == 1), "match and private snapshots");
            peers = peers.OrderBy(peer => peer.Index).ToArray();
            Require(peers[0].State["match_id"] == peers[1].State["match_id"], "Players joined different matches");
            Require(peers.All(peer => peer.State["hand"] == "1,2,3"), "Acceptance requires the default card catalog");
            CheckPrivate(peers);
            foreach (int card in new[] { 3, 2, 2, 3, 7, 8 })
            {
                await Apply(peers, card);
                await Apply(peers);
            }
            Require(peers[0].History.Any(message => message.Type == ArenaMessageType.BattleEvent &&
                message.Fields["type"] == "status_tick" && message.Fields["status"] == "poison"), "Poison turn event was not decoded");
            Require(peers[0].History.Any(message => message.Type == ArenaMessageType.BattleEvent &&
                message.Fields["type"] == "status_tick" && message.Fields["status"] == "regen"), "Regen turn event was not decoded");
            ulong discardTurn = peers[0].TurnId;
            await Apply(peers, 9);
            Require(peers[1].State["discard"] == "1,2" && peers[1].State["hand"] == "3", "Deterministic discard private state wrong");
            Require(peers[0].History.Any(message => message.Type == ArenaMessageType.BattleEvent &&
                message.Fields["type"] == "discard" && message.Fields["target"] == "1" && message.Fields["count"] == "2"), "Discard event was not decoded");
            CheckPrivate(peers);

            Peer actor = peers[0];
            int beforeAcks = actor.Count(ArenaMessageType.ActionAck);
            int beforeEvents = actor.Count(ArenaMessageType.BattleEvent);
            string digest = actor.State["replay_digest"];
            actor.Client.PlayCard(actor.State["match_id"], discardTurn, 9, actor.ActionId, 0, "action-" + Number(actor.ActionId));
            await WaitFor(peers, () => actor.Count(ArenaMessageType.ActionAck) == beforeAcks + 1, "idempotent discard retry");
            int beforePongs = actor.Count(ArenaMessageType.Pong);
            actor.Client.SendHeartbeat();
            await WaitFor(peers, () => actor.Count(ArenaMessageType.Pong) == beforePongs + 1, "retry barrier");
            Require(actor.Count(ArenaMessageType.BattleEvent) == beforeEvents && actor.State["replay_digest"] == digest,
                "Retry applied discard twice");
            actor.Client.PlayCard(actor.State["match_id"], discardTurn, 1, actor.ActionId);
            await WaitFor(peers, () => actor.Errors.Count > 0, "conflicting retry", true);
            Require(actor.Errors.Count == 1 && actor.Errors[0].Fields["code"] == "action_id_conflict", "Conflicting action did not fail");
            actor.Errors.Clear();

            Peer resumed = peers[1];
            var beforeReconnect = new Dictionary<string, string>(resumed.State);
            int helloCount = resumed.Count(ArenaMessageType.ProtocolHelloResp);
            int reconnectCount = resumed.Count(ArenaMessageType.ReconnectResp);
            resumed.Client.Disconnect();
            await Task.Delay(200);
            resumed.Client.DispatchPending();
            resumed.Snapshot = null;
            await resumed.Client.ReconnectAsync(resumed.Token);
            await WaitFor(peers, () => resumed.Snapshot.HasValue && resumed.Count(ArenaMessageType.ReconnectResp) == reconnectCount + 1,
                "fresh-transport reconnect");
            Require(resumed.History.Last(message => message.Type == ArenaMessageType.ReconnectResp).Fields["ok"] == "1", "Reconnect rejected");
            if (resumed.Client.protocol == ArenaProtocolId.ProtoV1)
                Require(resumed.Count(ArenaMessageType.ProtocolHelloResp) == helloCount + 1, "Reconnect did not renegotiate ProtoV1");
            foreach (string key in new[] { "match_id", "player_index", "turn", "turn_id", "revision", "hand", "discard", "deck_count",
                "p0_hp", "p1_hp", "p0_energy", "p1_energy", "p0_poison_turns", "p1_regen_turns", "replay_seed", "replay_revision", "replay_digest" })
                Require(resumed.State[key] == beforeReconnect[key], "Reconnect changed state: " + key);
            beforePongs = resumed.Count(ArenaMessageType.Pong);
            resumed.Client.SendHeartbeat();
            await WaitFor(peers, () => resumed.Count(ArenaMessageType.Pong) == beforePongs + 1, "post-reconnect heartbeat");
            Require(resumed.Client.IsConnected, "Old reader closed the new transport");
            CheckPrivate(peers);
            ulong resumedTurn = resumed.TurnId;
            ulong resumedAction = resumed.ActionId + 1;
            resumed.Client.EndTurn(resumed.State["match_id"], resumedTurn);
            await WaitFor(peers, () => peers.All(peer => peer.TurnId > resumedTurn), "restored automatic action ID");
            Require(resumed.History.Last(message => message.Type == ArenaMessageType.ActionAck).Fields["action_id"] == Number(resumedAction),
                "Reconnect did not restore the uint64 automatic action ID high watermark");
            resumed.ActionId = resumedAction;
            await Apply(peers, 10);
            Require(peers[1].State["p1_burn_turns"] == "2", "Burn triggered at turn start");
            await Apply(peers);
            Require(peers[1].State["p1_burn_turns"] == "1", "Burn did not trigger on turn end");
            await Apply(peers, 11);
            await Apply(peers);
            await Apply(peers, 12);
            await Apply(peers);
            Require(peers[0].State["p0_attack_boost_uses"] == "2" && peers[0].State["p0_heal_boost_uses"] == "2",
                "C# bonus application snapshots failed");
            await Apply(peers, 1);
            Require(peers[0].History.Last(message => message.Type == ArenaMessageType.BattleEvent).Fields["bonus"] == "2",
                "C# enhanced damage event failed");
            await Apply(peers);
            await Apply(peers, 2);
            Require(peers[0].State["p0_attack_boost_uses"] == "1" && peers[0].State["p0_heal_boost_uses"] == "1",
                "C# independent bonus consumption failed");
            await Apply(peers);
            while (!peers.All(peer => peer.Result.HasValue)) await Apply(peers);
            Require(peers[0].History.Any(message => message.Type == ArenaMessageType.BattleEvent &&
                message.Fields["type"] == "status_tick" && message.Fields["status"] == "burn" &&
                message.Fields["phase"] == "end"), "No C# turn-end burn event observed");
            Require(peers.All(peer => peer.Result.Value.Fields["reason"] == "max_turns" && peer.Result.Value.Fields["replay_valid"] == "1"),
                "Complete battle result invalid");
            Require(peers[0].Result.Value.Fields["replay_digest"] == peers[1].Result.Value.Fields["replay_digest"] &&
                peers[0].Result.Value.Fields["replay_revision"] == peers[1].Result.Value.Fields["replay_revision"], "Result replay metadata differs");
            Console.WriteLine("C# network acceptance passed: ProtoV1/" + otherProtocol +
                " login/heartbeat/admin/match/status/discard/privacy/ACK/reconnect/result match=" + peers[0].State["match_id"]);
        }
    }

    public static async Task<int> Main(string[] args)
    {
        try
        {
            string host = "127.0.0.1";
            int port = 0;
            for (int index = 0; index < args.Length; index++)
            {
                if (args[index] == "--host" && index + 1 < args.Length) host = args[++index];
                else if (args[index] == "--port" && index + 1 < args.Length) port = int.Parse(args[++index], CultureInfo.InvariantCulture);
                else throw new ArgumentException("Usage: CSharpProtoAcceptance [--host HOST] [--port PORT]");
            }
            ProtocolTests();
            PresentationTests();
            if (port != 0)
            {
                await NetworkBattle(host, port, ArenaProtocolId.ProtoV1);
                await NetworkBattle(host, port, ArenaProtocolId.TextV1);
            }
            else Console.WriteLine("Network acceptance not run; supply --port to connect to a server using the default card catalog.");
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error);
            return 1;
        }
    }

    private static void PresentationTests()
    {
        var state = new ArenaDemoState();
        state.SetConnection("connected");
        state.Apply(new ArenaMessage(ArenaMessageType.LoginResp, "ok=1;user=alice;token=secret-resume"));
        state.Apply(new ArenaMessage(ArenaMessageType.MatchFound, "match_id=demo;player_index=0"));
        state.Apply(new ArenaMessage(ArenaMessageType.BattleSnapshot,
            "match_id=demo;player_index=0;turn=0;turn_id=1;revision=1;remaining_ms=30000;hand=1,2,3;p0_energy=1;done=0"));
        Require(state.CanAct && !state.CanPlay(1) && state.CanPlay(2), "Presentation cost and turn gates");
        state.ActionPending = true;
        Require(!state.CanAct, "Pending action disables buttons");
        state.Apply(new ArenaMessage(ArenaMessageType.Error, "code=insufficient_energy"));
        Require(state.CanAct && state.Notice == "insufficient energy", "Server rejection releases pending action");
        state.Apply(new ArenaMessage(ArenaMessageType.MatchResult, "winner=0;reason=opponent_hp_zero;turn_id=1"));
        Require(!state.CanAct && !state.CanPlay(2) && state.ResultTitle == "VICTORY", "Ended match forbids no_room requests");
        state.Apply(new ArenaMessage(ArenaMessageType.MatchFound, "match_id=next;player_index=1"));
        Require(!state.Done && state.ResultTitle == "" && state.Hand == "", "Rematch resets presentation");
        state.Apply(new ArenaMessage(ArenaMessageType.BattleSnapshot,
            "match_id=next;player_index=1;turn=0;turn_id=1;remaining_ms=30000;hand=1,2;done=0"));
        Require(!state.CanAct, "Opponent turn disables own actions");
        state.Apply(new ArenaMessage(ArenaMessageType.BattleSnapshot,
            "match_id=next;player_index=1;turn=1;turn_id=2;remaining_ms=0;hand=1,2;done=0"));
        Require(!state.CanAct, "Expired countdown disables actions");
        Require(state.Log.All(entry => !entry.Contains("secret-resume")), "Resume credentials excluded from display logs");
        state.Apply(new ArenaMessage(ArenaMessageType.BattleEvent, "type=damage;player=0;value=8;bonus=2"));
        state.Apply(new ArenaMessage(ArenaMessageType.BattleEvent, "type=damage;player=0;value=8;bonus=2"));
        Require(state.TryTakeFeedback(out var first) && first.Kind == "damage" && first.Player == 1 && first.Label == "DAMAGE 10",
            "Damage feedback follows authoritative event and target");
        Require(state.TryTakeFeedback(out var repeated) && repeated.Sequence > first.Sequence, "Repeated equal events remain distinct");
        state.Apply(new ArenaMessage(ArenaMessageType.BattleEvent, "type=heal;player=1;value=4"));
        Require(state.TryTakeFeedback(out var healing) && healing.Player == 1 && healing.Label == "HEAL +4", "Healing targets self");
        state.Apply(new ArenaMessage(ArenaMessageType.BattleEvent, "type=shield;player=0;value=6"));
        Require(state.TryTakeFeedback(out var barrier) && barrier.Player == 0 && barrier.Kind == "shield", "Shield feedback targets self");
        state.Apply(new ArenaMessage(ArenaMessageType.BattleEvent, "type=status_tick;player=1;status=regen;value=3"));
        Require(state.TryTakeFeedback(out var regen) && regen.Kind == "heal" && regen.Player == 1, "Regeneration visual is healing");
        Require(state.RemainingFraction == 0, "Expired progress bar is empty");
        state.LeaderboardLoading = true;
        state.Apply(new ArenaMessage(ArenaMessageType.LeaderboardResp,
            "ok=1;source=mysql;count=1;entry_0_rank=1;entry_0_user=alice;entry_0_rating=1010;entry_0_wins=1;entry_0_losses=0"));
        Require(!state.LeaderboardLoading && state.Leaderboard.Count == 1 && state.Leaderboard[0].Rating == 1010,
            "Authoritative ranking response updates UI");
        state.Apply(new ArenaMessage(ArenaMessageType.LeaderboardResp, "ok=0;code=leaderboard_unavailable;count=0"));
        Require(state.Leaderboard.Count == 0 && state.LeaderboardError != "", "Failed ranking clears stale entries");
        state.Apply(new ArenaMessage(ArenaMessageType.LeaderboardResp, "ok=1;source=mysql;count=0"));
        Require(state.LeaderboardError == "" && state.Leaderboard.Count == 0, "Empty ranking is not an error");
        state.Apply(new ArenaMessage(ArenaMessageType.LeaderboardResp, "ok=1;source=mysql;count=21"));
        Require(state.LeaderboardError != "", "Malformed oversized ranking is rejected");
        Console.WriteLine("C# presentation state acceptance passed");
    }
}
