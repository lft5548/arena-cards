using System;
using UnityEngine;

namespace ArenaCards.Client
{
    [RequireComponent(typeof(ArenaClient))]
    public sealed class ArenaDemoController : MonoBehaviour
    {
        public ArenaDemoState State { get; } = new ArenaDemoState();
        public ArenaClient Client { get; private set; }
        public event Action Changed;
        private DateTime heartbeatAt;
        private DateTime leaderboardDeadline;
        private void Awake()
        {
            Client = GetComponent<ArenaClient>();
            State.User = Client.username;
            Client.MessageReceived += OnMessage;
            Client.ConnectionChanged += OnConnectionChanged;
        }
        private void Update()
        {
            if (Client.IsConnected && DateTime.UtcNow >= heartbeatAt)
            { Invoke(Client.SendHeartbeat); heartbeatAt = DateTime.UtcNow.AddSeconds(5); }
            if (State.LeaderboardLoading && DateTime.UtcNow >= leaderboardDeadline)
            { State.LeaderboardLoading = false; State.LeaderboardError = "Ranking request timed out"; Changed?.Invoke(); }
        }
        private void OnDestroy()
        {
            if (Client == null) return;
            Client.MessageReceived -= OnMessage;
            Client.ConnectionChanged -= OnConnectionChanged;
        }
        public void Login(string host, int port, string user, ArenaProtocolId protocol)
        {
            if (string.IsNullOrWhiteSpace(host) || string.IsNullOrWhiteSpace(user) || port < 1 || port > 65535)
            { State.Notice = "Enter a server address, port and player name."; Changed?.Invoke(); return; }
            Client.host = host.Trim(); Client.port = port; Client.username = user.Trim(); Client.protocol = protocol;
            State.User = Client.username;
            if (Client.IsConnected) Client.Disconnect();
            State.LoggedIn = false;
            Invoke(Client.LoginAndConnect);
        }
        public void JoinMatch()
        {
            if (!State.LoggedIn || !State.Connected || State.Queued || State.InMatch) return;
            State.Queued = true; State.Notice = string.Empty; Invoke(Client.JoinMatch);
        }
        public void CancelMatch() { State.Queued = false; Invoke(Client.CancelMatch); }
        public void Disconnect() { Invoke(Client.Disconnect); }
        public void Reconnect()
        { if (!string.IsNullOrEmpty(State.SessionToken)) Invoke(() => Client.Reconnect(State.SessionToken)); }
        public void RequestRooms() { Invoke(Client.RequestRooms); }
        public void RequestLeaderboard()
        {
            if (!State.Connected || !State.LoggedIn) return;
            State.LeaderboardLoading = true; State.LeaderboardError = "";
            leaderboardDeadline = DateTime.UtcNow.AddSeconds(10);
            try { Client.RequestLeaderboard(); }
            catch (Exception error) { State.LeaderboardLoading = false; State.LeaderboardError = error.Message; }
            Changed?.Invoke();
        }
        public void PlayCard(int card)
        {
            if (!State.CanPlay(card)) return;
            State.ActionPending = true; Invoke(() => Client.PlayCard(State.MatchId, State.TurnId, card));
        }
        public void EndTurn()
        {
            if (!State.CanAct) return;
            State.ActionPending = true; Invoke(() => Client.EndTurn(State.MatchId, State.TurnId));
        }
        private void Invoke(Action action)
        {
            try { action(); }
            catch (Exception error) { State.ActionPending = false; State.Notice = error.Message; }
            Changed?.Invoke();
        }
        private void OnConnectionChanged(string value) { State.SetConnection(value); Changed?.Invoke(); }
        private void OnMessage(ArenaMessage message)
        {
            State.Apply(message); Changed?.Invoke();
            if (message.Type == ArenaMessageType.MatchResult) RequestLeaderboard();
        }
    }
}
