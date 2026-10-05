using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace ArenaCards.Client
{
    public sealed class ArenaClient : MonoBehaviour
    {
        [Header("Arena server")]
        public string host = "127.0.0.1";
        public int port = 9000;
        public string username = "unity_player";
        public ArenaProtocolId protocol = ArenaProtocolId.TextV1;

        public event Action<ArenaMessage> MessageReceived;
        public event Action<string> ConnectionChanged;

        private sealed class Transport
        {
            public readonly TcpClient Socket = new TcpClient();
            public readonly CancellationTokenSource Cancellation = new CancellationTokenSource();
            public readonly ArenaFrameDecoder Decoder = new ArenaFrameDecoder();
            public readonly TaskCompletionSource<ArenaMessage> Hello =
                new TaskCompletionSource<ArenaMessage>(TaskCreationOptions.RunContinuationsAsynchronously);
            public readonly ArenaProtocolId Protocol;
            public NetworkStream Stream;
            public bool Ready;

            public Transport(ArenaProtocolId selected) { Protocol = selected; }
        }

        private readonly ConcurrentQueue<ArenaMessage> received = new ConcurrentQueue<ArenaMessage>();
        private readonly ConcurrentQueue<string> connectionChanges = new ConcurrentQueue<string>();
        private readonly object writeLock = new object();
        private readonly SemaphoreSlim connectLock = new SemaphoreSlim(1, 1);
        private Transport transport;
        private ulong nextActionId;
        private long nextRequestId;

        public bool IsConnected
        {
            get
            {
                lock (writeLock)
                    return transport != null && transport.Ready && transport.Socket.Connected;
            }
        }

        private void Update() { DispatchPending(); }

        public void DispatchPending()
        {
            string connection;
            while (connectionChanges.TryDequeue(out connection))
                ConnectionChanged?.Invoke(connection);
            ArenaMessage message;
            while (received.TryDequeue(out message))
                MessageReceived?.Invoke(message);
        }

        public async void LoginAndConnect()
        {
            try { await LoginAndConnectAsync(); }
            catch (Exception error) { PublishConnection("connect_error: " + error.Message); }
        }

        public Task LoginAndConnectAsync() { return ConnectAsync(null); }

        public void JoinMatch() { SendRequest(ArenaMessageType.MatchJoinReq, Fields()); }
        public void CancelMatch() { SendRequest(ArenaMessageType.MatchCancelReq, Fields()); }
        public void SendHeartbeat() { SendRequest(ArenaMessageType.Heartbeat, Fields()); }
        public void RequestRooms() { SendRequest(ArenaMessageType.AdminRoomsReq, Fields()); }
        public void RequestLeaderboard(int limit = 10)
        {
            if (limit < 1 || limit > 20) throw new ArgumentOutOfRangeException(nameof(limit));
            SendRequest(ArenaMessageType.LeaderboardReq, Fields("limit", Number(limit)));
        }

        public void PlayCard(string matchId, ulong turnId, int cardId)
        {
            PlayCard(matchId, turnId, cardId, NextActionId());
        }

        public void PlayCard(string matchId, ulong turnId, int cardId, ulong actionId,
            ulong revision = 0, string requestId = null)
        {
            SendRequest(ArenaMessageType.PlayCardReq, Fields(
                "match_id", matchId, "turn_id", Number(turnId), "card", Number(cardId),
                "action_id", Number(actionId), "revision", Number(revision),
                "request_id", requestId ?? NextRequestId()));
        }

        public void EndTurn(string matchId, ulong turnId)
        {
            EndTurn(matchId, turnId, NextActionId());
        }

        public void EndTurn(string matchId, ulong turnId, ulong actionId,
            ulong revision = 0, string requestId = null)
        {
            SendRequest(ArenaMessageType.EndTurnReq, Fields(
                "match_id", matchId, "turn_id", Number(turnId), "action_id", Number(actionId),
                "revision", Number(revision), "request_id", requestId ?? NextRequestId()));
        }

        public async void Reconnect(string resumeToken)
        {
            try { await ReconnectAsync(resumeToken); }
            catch (Exception error) { PublishConnection("connect_error: " + error.Message); }
        }

        public Task ReconnectAsync(string resumeToken)
        {
            if (string.IsNullOrEmpty(resumeToken))
                throw new ArgumentException("A session token is required", nameof(resumeToken));
            return ConnectAsync(resumeToken);
        }

        public void Disconnect()
        {
            lock (writeLock)
            {
                if (transport != null)
                    CloseTransport(transport);
            }
        }

        private async Task ConnectAsync(string resumeToken)
        {
            await connectLock.WaitAsync().ConfigureAwait(false);
            Transport current = null;
            try
            {
                if (resumeToken == null && IsConnected)
                    return;
                Disconnect();
                if (protocol != ArenaProtocolId.TextV1 && protocol != ArenaProtocolId.ProtoV1)
                    throw new ArgumentOutOfRangeException(nameof(protocol));
                current = new Transport(protocol);
                lock (writeLock) transport = current;
                PublishConnection("connecting");
                await current.Socket.ConnectAsync(host, port).ConfigureAwait(false);
                lock (writeLock)
                {
                    if (!ReferenceEquals(transport, current))
                        throw new OperationCanceledException("Connection was closed");
                    current.Stream = current.Socket.GetStream();
                }
                _ = ReadLoop(current);
                if (current.Protocol == ArenaProtocolId.ProtoV1)
                {
                    Write(current, ArenaProtocol.Encode(ArenaMessageType.ProtocolHelloReq,
                        "protocol=proto_v1;version=1"));
                    Task finished = await Task.WhenAny(current.Hello.Task, Task.Delay(5000)).ConfigureAwait(false);
                    if (finished != current.Hello.Task)
                        throw new TimeoutException("ProtoV1 negotiation timed out");
                    ArenaMessage hello = await current.Hello.Task.ConfigureAwait(false);
                    string runtime, selected;
                    if (!hello.Fields.TryGetValue("proto_v1_runtime", out runtime) || runtime != "1" ||
                        !hello.Fields.TryGetValue("selected", out selected) || selected != "proto_v1")
                        throw new InvalidOperationException("Server did not select an enabled ProtoV1 runtime");
                    current.Decoder.Protocol = ArenaProtocolId.ProtoV1;
                }
                lock (writeLock)
                {
                    if (!ReferenceEquals(transport, current))
                        throw new OperationCanceledException("Connection was closed");
                    current.Ready = true;
                    if (resumeToken == null)
                        SendRequest(ArenaMessageType.LoginReq, Fields("user", username));
                    else
                        SendRequest(ArenaMessageType.ReconnectReq, Fields("token", resumeToken));
                    PublishConnection("connected");
                }
            }
            catch
            {
                if (current != null) CloseTransport(current);
                throw;
            }
            finally { connectLock.Release(); }
        }

        private async Task ReadLoop(Transport current)
        {
            byte[] buffer = new byte[8192];
            try
            {
                while (!current.Cancellation.IsCancellationRequested)
                {
                    int count = await current.Stream.ReadAsync(buffer, 0, buffer.Length,
                        current.Cancellation.Token).ConfigureAwait(false);
                    if (count == 0) break;
                    var messages = new List<ArenaMessage>();
                    current.Decoder.Append(buffer, 0, count, messages);
                    foreach (ArenaMessage message in messages)
                    {
                        if (current.Protocol == ArenaProtocolId.ProtoV1 && !current.Ready)
                        {
                            if (message.Type != ArenaMessageType.ProtocolHelloResp)
                                throw new FormatException("ProtoV1 negotiation rejected: " + message);
                            current.Hello.TrySetResult(message);
                        }
                        lock (writeLock)
                        {
                            if (ReferenceEquals(transport, current))
                            {
                                string lastAction;
                                ulong lastActionId;
                                if (message.Type == ArenaMessageType.BattleSnapshot &&
                                    message.Fields.TryGetValue("last_action_id", out lastAction) &&
                                    ulong.TryParse(lastAction, NumberStyles.None, CultureInfo.InvariantCulture, out lastActionId) &&
                                    lastActionId > nextActionId)
                                    nextActionId = lastActionId;
                                received.Enqueue(message);
                            }
                        }
                    }
                }
            }
            catch (OperationCanceledException) { }
            catch (Exception error)
            {
                lock (writeLock)
                {
                    if (ReferenceEquals(transport, current))
                        received.Enqueue(new ArenaMessage(ArenaMessageType.Error,
                            "code=network_error;message=" + ArenaProtocol.Escape(error.Message)));
                }
                current.Hello.TrySetException(error);
            }
            finally
            {
                current.Hello.TrySetException(new IOException("Connection closed before negotiation completed"));
                bool wasCurrent = CloseTransport(current);
                if (wasCurrent)
                    received.Enqueue(new ArenaMessage(ArenaMessageType.Error, "code=disconnected"));
                current.Cancellation.Dispose();
            }
        }

        private void SendRequest(ArenaMessageType type, Dictionary<string, string> fields)
        {
            if (!fields.ContainsKey("request_id")) fields["request_id"] = NextRequestId();
            lock (writeLock)
            {
                Transport current = transport;
                if (current == null || !current.Ready) return;
                byte[] frame;
                if (current.Protocol == ArenaProtocolId.ProtoV1)
                    frame = ArenaProtoCodec.Encode(type, fields);
                else if (type == ArenaMessageType.LoginReq)
                    frame = ArenaProtocol.Encode(type, fields["user"]);
                else if (type == ArenaMessageType.ReconnectReq)
                    frame = ArenaProtocol.Encode(type, fields["token"]);
                else
                {
                    var pairs = new List<KeyValuePair<string, string>>(fields);
                    frame = ArenaProtocol.EncodeFields(type, pairs.ToArray());
                }
                Write(current, frame);
            }
        }

        private void Write(Transport current, byte[] frame)
        {
            lock (writeLock)
            {
                if (!ReferenceEquals(transport, current) || current.Stream == null)
                    throw new IOException("Arena connection is closed");
                try { current.Stream.Write(frame, 0, frame.Length); }
                catch (IOException)
                {
                    CloseTransport(current);
                    throw;
                }
            }
        }

        private bool CloseTransport(Transport current)
        {
            lock (writeLock)
            {
                bool wasCurrent = ReferenceEquals(transport, current);
                if (wasCurrent) transport = null;
                try { current.Cancellation.Cancel(); } catch (ObjectDisposedException) { }
                try { current.Stream?.Close(); } catch (IOException) { }
                current.Socket.Close();
                if (wasCurrent) PublishConnection("disconnected");
                return wasCurrent;
            }
        }

        private static Dictionary<string, string> Fields(params string[] values)
        {
            var fields = new Dictionary<string, string>(StringComparer.Ordinal);
            for (int index = 0; index < values.Length; index += 2)
                fields.Add(values[index], values[index + 1]);
            return fields;
        }

        private static string Number<T>(T value) where T : IFormattable
        {
            return value.ToString(null, CultureInfo.InvariantCulture);
        }

        private ulong NextActionId()
        {
            lock (writeLock)
            {
                if (nextActionId == ulong.MaxValue)
                    throw new InvalidOperationException("Action ID space exhausted");
                return ++nextActionId;
            }
        }
        private string NextRequestId() { return Number(Interlocked.Increment(ref nextRequestId)); }
        private void PublishConnection(string value) { connectionChanges.Enqueue(value); }
        private void OnDestroy() { Disconnect(); }
    }
}
