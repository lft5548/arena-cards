using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;

namespace ArenaCards.Client
{
    // Presentation follows authoritative snapshots; it never applies battle rules.
    public sealed class ArenaDemoState
    {
        public sealed class RankingEntry
        {
            public uint Rank; public string User; public long Rating; public ulong Wins, Losses;
        }
        public sealed class Feedback
        {
            public int Player; public string Kind, Label; public ulong Sequence;
        }
        public bool Connected, LoggedIn, Queued, Done, ActionPending;
        public string Connection = "offline", MatchId = "", User = "", Opponent = "Opponent";
        public string Notice = "", ResultTitle = "", ResultDetail = "", Hand = "", Discard = "", Rooms = "", LastEffect = "";
        public string SessionToken = "";
        public bool LeaderboardLoading;
        public string LeaderboardError = "", LeaderboardSource = "";
        public readonly List<RankingEntry> Leaderboard = new List<RankingEntry>();
        private readonly Queue<Feedback> feedback = new Queue<Feedback>();
        public ulong EffectSequence { get; private set; }
        public ulong TurnId, Revision;
        public int PlayerIndex = -1, Turn, OpponentHandCount, DeckCount;
        public readonly int[] Hp = { 30, 30 }, Energy = { 3, 3 }, Shield = { 0, 0 };
        public readonly string[] Statuses = { "", "" };
        private readonly List<string> log = new List<string>();
        private DateTime deadline = DateTime.MinValue;
        public IReadOnlyList<string> Log => log;
        public bool InMatch => !string.IsNullOrEmpty(MatchId) && !Done;
        public bool MyTurn => InMatch && PlayerIndex == Turn;
        public int RemainingSeconds => deadline == DateTime.MinValue || Done ? 0 :
            Math.Max(0, (int)Math.Ceiling((deadline - DateTime.UtcNow).TotalSeconds));
        public float RemainingFraction => Done || deadline == DateTime.MinValue ? 0 :
            (float)Math.Max(0, Math.Min(1, (deadline - DateTime.UtcNow).TotalMilliseconds / 30000));
        public bool TryTakeFeedback(out Feedback value)
        {
            if (feedback.Count == 0) { value = null; return false; }
            value = feedback.Dequeue(); return true;
        }
        public bool CanAct => Connected && LoggedIn && MyTurn && !ActionPending && RemainingSeconds > 0;
        public bool CanPlay(int id) => CanAct && Cards(Hand).Contains(id) &&
            (!ArenaCardCatalog.TryGet(id, out var card) || Energy[PlayerIndex] >= card.Cost);
        public static IEnumerable<int> Cards(string value)
        {
            foreach (string raw in (value ?? "").Split(','))
                if (int.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out int id)) yield return id;
        }
        public void SetConnection(string value)
        {
            Connection = value; Connected = value == "connected" || value == "reconnected";
            if (!Connected) { ActionPending = false; Queued = false; LeaderboardLoading = false; }
            if (value.StartsWith("connect_error", StringComparison.Ordinal)) Notice = value;
        }
        public void Apply(ArenaMessage message)
        {
            var f = message.Fields;
            string Get(string key, string fallback = "") => f.TryGetValue(key, out string value) ? value : fallback;
            int Int(string key, int fallback = 0) => int.TryParse(Get(key), NumberStyles.Integer,
                CultureInfo.InvariantCulture, out int value) ? value : fallback;
            ulong Ulong(string key) => ulong.TryParse(Get(key), NumberStyles.None,
                CultureInfo.InvariantCulture, out ulong value) ? value : 0;
            switch (message.Type)
            {
                case ArenaMessageType.LoginResp:
                    LoggedIn = Get("ok") == "1";
                    if (LoggedIn)
                    {
                        User = Get("user", User); SessionToken = Get("token"); Notice = "";
                        MatchId = Hand = ResultTitle = ResultDetail = ""; Done = Queued = ActionPending = false;
                        Leaderboard.Clear(); LeaderboardError = LeaderboardSource = ""; LeaderboardLoading = false;
                        PlayerIndex = -1;
                    }
                    else Notice = Get("code", "Login failed");
                    break;
                case ArenaMessageType.MatchFound:
                    MatchId = Get("match_id"); PlayerIndex = Int("player_index", -1);
                    Done = false; Queued = false; ActionPending = false;
                    ResultTitle = ResultDetail = Hand = Discard = Notice = LastEffect = "";
                    TurnId = Revision = 0; deadline = DateTime.MinValue;
                    feedback.Clear();
                    break;
                case ArenaMessageType.BattleSnapshot:
                    MatchId = Get("match_id", MatchId); PlayerIndex = Int("player_index", PlayerIndex);
                    Turn = Int("turn"); TurnId = Ulong("turn_id"); Revision = Ulong("revision");
                    Done = Get("done") == "1"; Hand = Get("hand"); Discard = Get("discard");
                    deadline = DateTime.UtcNow.AddMilliseconds(Math.Max(0, Int("remaining_ms")));
                    OpponentHandCount = Int("opponent_hand_count"); DeckCount = Int("deck_count");
                    for (int p = 0; p < 2; p++)
                    {
                        Hp[p] = Int("p" + p + "_hp", Hp[p]); Energy[p] = Int("p" + p + "_energy", Energy[p]);
                        Shield[p] = Int("p" + p + "_shield", Shield[p]);
                        var effects = new List<string>();
                        foreach (string kind in new[] { "poison", "regen", "burn" })
                            if (Int("p" + p + "_" + kind + "_turns") > 0)
                                effects.Add(kind.ToUpperInvariant() + " " + Int("p" + p + "_" + kind + "_value") +
                                    " / " + Int("p" + p + "_" + kind + "_turns") + "T");
                        foreach (string kind in new[] { "attack_boost", "heal_boost" })
                            if (Int("p" + p + "_" + kind + "_uses") > 0)
                                effects.Add(kind.ToUpperInvariant().Replace('_', ' ') + " +" +
                                    Int("p" + p + "_" + kind + "_value"));
                        Statuses[p] = string.Join("   ", effects);
                    }
                    ActionPending = false;
                    break;
                case ArenaMessageType.ActionAck:
                    if (Get("ok") == "0" || Get("applied") == "0" || Get("status") == "rejected") ActionPending = false;
                    break;
                case ArenaMessageType.ReconnectResp:
                    if (Get("ok") == "1")
                    { Connected = LoggedIn = true; Connection = "reconnected"; MatchId = Get("match_id"); Notice = ""; }
                    else { Notice = Get("code", "Unable to resume"); LoggedIn = false; Done = true; }
                    break;
                case ArenaMessageType.BattleEvent:
                {
                    LastEffect = Get("type").Replace('_', ' ').ToUpperInvariant();
                    if (Get("value") != "") LastEffect += " " + Get("value");
                    string kind = Get("type"); int actor = Int("player", -1);
                    if (actor >= 0 && actor <= 1 && kind != "end_turn")
                    {
                        int target = kind == "damage" || kind == "poison" || kind == "burn" || kind == "discard" ? 1 - actor : actor;
                        string visual = kind;
                        if (kind == "status_tick")
                        { visual = Get("status") == "regen" ? "heal" : "damage"; target = actor; }
                        int amount = Int("value") + Int("bonus");
                        string caption = visual == "heal" ? "HEAL +" + amount : visual == "shield" ? "SHIELD +" + amount :
                            visual == "damage" ? "DAMAGE " + amount : LastEffect;
                        feedback.Enqueue(new Feedback { Player = target, Kind = visual, Label = caption, Sequence = ++EffectSequence });
                        if (feedback.Count > 32) feedback.Dequeue();
                    }
                    break;
                }
                case ArenaMessageType.LeaderboardResp:
                    LeaderboardLoading = false;
                    Leaderboard.Clear(); LeaderboardSource = Get("source");
                    LeaderboardError = Get("ok") == "1" ? "" : Get("code", "leaderboard_unavailable").Replace('_', ' ');
                    if (LeaderboardError == "")
                    {
                        int count = Int("count", -1);
                        if (count < 0 || count > 20) { LeaderboardError = "Invalid leaderboard response"; break; }
                        for (int i = 0; i < count; i++)
                        {
                            string prefix = "entry_" + i + "_";
                            if (string.IsNullOrEmpty(Get(prefix + "user")) || !uint.TryParse(Get(prefix + "rank"), out uint rank) || rank != i + 1 ||
                                !long.TryParse(Get(prefix + "rating"), NumberStyles.Integer, CultureInfo.InvariantCulture, out long rating) ||
                                !ulong.TryParse(Get(prefix + "wins"), out ulong wins) || !ulong.TryParse(Get(prefix + "losses"), out ulong losses))
                            { Leaderboard.Clear(); LeaderboardError = "Invalid leaderboard response"; break; }
                            Leaderboard.Add(new RankingEntry { Rank = rank, User = Get(prefix + "user"), Rating = rating, Wins = wins, Losses = losses });
                        }
                    }
                    break;
                case ArenaMessageType.MatchResult:
                    Done = true; Queued = ActionPending = false;
                    int winner = Int("winner", -1);
                    ResultTitle = winner < 0 ? "DRAW" : winner == PlayerIndex ? "VICTORY" : "DEFEAT";
                    ResultDetail = Get("reason").Replace('_', ' ') + "  /  Turn " + Get("turn_id");
                    break;
                case ArenaMessageType.AdminRoomsResp: Rooms = message.Payload; break;
                case ArenaMessageType.Error:
                    Notice = Get("code", "Request failed").Replace('_', ' '); ActionPending = false;
                    if (Get("code") == "no_room" || Get("code") == "resume_expired") Done = true;
                    Queued = false;
                    break;
            }
            if (message.Type == ArenaMessageType.Pong) return;
            // Resume tokens are credentials, not display/debug log content.
            string entry = message.Type == ArenaMessageType.LoginResp ? "Signed in as " + User :
                message.Type == ArenaMessageType.BattleSnapshot ? "Turn " + TurnId + " / revision " + Revision :
                message.Type == ArenaMessageType.MatchResult ? ResultTitle + " - " + ResultDetail :
                message.Type == ArenaMessageType.BattleEvent ? LastEffect :
                message.Type == ArenaMessageType.Error ? Notice : message.Type.ToString();
            log.Add(DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture) + "  " + entry);
            if (log.Count > 80) log.RemoveAt(0);
        }
    }
}
