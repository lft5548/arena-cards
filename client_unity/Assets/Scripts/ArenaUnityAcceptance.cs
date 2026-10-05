using System;
using System.Collections;
using System.IO;
using UnityEngine;

namespace ArenaCards.Client
{
    // Opt-in standalone acceptance uses the actual Unity lifecycle and transport.
    public sealed class ArenaUnityAcceptance : MonoBehaviour
    {
        private ArenaDemoController[] peers;
        private string output;
        private string[] args;
        private string failure;
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void Launch()
        {
            if (Array.IndexOf(Environment.GetCommandLineArgs(), "--arena-acceptance") < 0) return;
            new GameObject("ArenaAcceptance").AddComponent<ArenaUnityAcceptance>();
        }
        private string Argument(string key, string fallback)
        {
            int index = Array.IndexOf(args, key);
            return index >= 0 && index + 1 < args.Length ? args[index + 1] : fallback;
        }
        private IEnumerator Start()
        {
            args = Environment.GetCommandLineArgs();
            output = Path.GetFullPath(Argument("--arena-output", Path.Combine(Application.dataPath, "../AcceptanceEvidence")));
            Directory.CreateDirectory(output);
            peers = new[] { FindObjectOfType<ArenaDemoController>(),
                new GameObject("OpponentTransport", typeof(ArenaClient)).AddComponent<ArenaDemoController>() };
            string suffix = Guid.NewGuid().ToString("N").Substring(0, 10);
            int port; int.TryParse(Argument("--arena-port", "9000"), out port);
            var protocol = Argument("--arena-protocol", "text_v1") == "proto_v1" ? ArenaProtocolId.ProtoV1 : ArenaProtocolId.TextV1;
            foreach (var peer in peers)
                peer.Client.MessageReceived += m => { if (m.Type == ArenaMessageType.Error) failure = m.Payload; };
            peers[0].Login("127.0.0.1", port, "unity_qa_" + suffix + "_a", protocol);
            peers[1].Login("127.0.0.1", port, "unity_qa_" + suffix + "_b", protocol);
            yield return Wait(() => peers[0].State.LoggedIn && peers[1].State.LoggedIn, "login");
            if (failure != null) yield break;
            peers[0].JoinMatch();
            yield return new WaitForSecondsRealtime(.2f);
            peers[1].JoinMatch();
            yield return Wait(() => peers[0].State.CanAct || peers[1].State.CanAct, "match");
            if (failure != null) yield break;
            int active = peers[0].State.MyTurn ? 0 : 1;
            ulong turn = peers[active].State.TurnId;
            peers[active].PlayCard(1);
            yield return Wait(() => peers[0].State.TurnId > turn && peers[1].State.TurnId > turn, "play and snapshot");
            if (failure != null) yield break;
            if (peers[active].State.Hp[1 - peers[active].State.PlayerIndex] >= 30)
            { Fail("Strike did not change authoritative HP"); yield break; }
            yield return new WaitForSecondsRealtime(.3f);
            Capture("unity-live-battle.png");
            for (int step = 0; step < 3; step++)
            {
                active = peers[0].State.MyTurn ? 0 : 1;
                int card = step == 0 ? 1 : step == 1 ? 2 : 3;
                int player = peers[active].State.PlayerIndex;
                int previousHealth = peers[active].State.Hp[player];
                turn = peers[active].State.TurnId; peers[active].PlayCard(card);
                yield return Wait(() => peers[0].State.TurnId > turn && peers[1].State.TurnId > turn, "shield/heal and snapshot");
                if (failure != null) yield break;
                if (step == 1 && peers[active].State.Hp[player] <= previousHealth)
                { Fail("Healing did not change authoritative HP"); yield break; }
                if (step == 2 && peers[active].State.Shield[player] <= 0)
                { Fail("Barrier did not change authoritative shield"); yield break; }
                yield return new WaitForSecondsRealtime(.15f);
                if (step > 0) Capture(step == 2 ? "unity-live-shield.png" : "unity-live-heal.png");
            }
            string token = peers[0].State.SessionToken;
            peers[0].Disconnect(); yield return new WaitForSecondsRealtime(.3f);
            peers[0].Reconnect();
            yield return Wait(() => peers[0].State.Connection == "reconnected" && peers[0].State.Connected, "reconnect");
            if (failure != null) yield break;
            if (peers[0].State.SessionToken != token) { Fail("Resume identity changed"); yield break; }
            for (int i = 0; i < 41 && !peers[0].State.Done; i++)
            {
                active = peers[0].State.MyTurn ? 0 : 1;
                turn = peers[active].State.TurnId;
                peers[active].EndTurn();
                yield return Wait(() => peers[0].State.Done || peers[0].State.TurnId > turn && peers[1].State.TurnId > turn, "turn");
                if (failure != null) yield break;
            }
            yield return Wait(() => peers[0].State.Done && peers[1].State.Done, "terminal result on both clients");
            if (failure != null) yield break;
            yield return new WaitForSecondsRealtime(.3f);
            Capture("unity-live-result.png");
            yield return Wait(() => !peers[0].State.LeaderboardLoading && peers[0].State.LeaderboardSource == "mysql", "authoritative rankings");
            if (failure != null) yield break;
            if (peers[0].State.LeaderboardError != "" || peers[0].State.Leaderboard.Count == 0)
            { Fail("Leaderboard did not return real players"); yield break; }
            peers[0].GetComponent<ArenaPresentationView>().DisplayLeaderboard();
            Capture("unity-live-leaderboard.png");
            File.WriteAllText(Path.Combine(output, "passed.txt"),
                "Unity " + Application.unityVersion + " " + protocol + " login/match/damage/shield/heal/snapshot/reconnect/result/MySQL leaderboard passed\n" +
                peers[0].State.User + "\n" + peers[1].State.User + "\n" + peers[0].State.MatchId);
            Debug.Log("ARENA_UNITY_LIVE_ACCEPTANCE_PASSED " + protocol);
            Application.Quit(0);
        }
        private IEnumerator Wait(Func<bool> ready, string stage)
        {
            float deadline = Time.realtimeSinceStartup + 12;
            while (!ready() && failure == null && Time.realtimeSinceStartup < deadline) yield return null;
            if (failure != null || !ready()) Fail(stage + ": " + (failure ?? "timeout"));
        }
        private void Fail(string message)
        {
            failure = message; Debug.LogError(message);
            File.WriteAllText(Path.Combine(output, "failed.txt"), message); Application.Quit(1);
        }
        private void Capture(string name)
        {
            var view = peers[0].GetComponent<ArenaPresentationView>();
            var target = new RenderTexture(Screen.width, Screen.height, 24);
            view.Camera.targetTexture = target;
            Canvas.ForceUpdateCanvases(); view.Camera.Render();
            var previous = RenderTexture.active; RenderTexture.active = target;
            var image = new Texture2D(Screen.width, Screen.height, TextureFormat.RGB24, false);
            image.ReadPixels(new Rect(0, 0, Screen.width, Screen.height), 0, 0); image.Apply();
            File.WriteAllBytes(Path.Combine(output, name), image.EncodeToPNG());
            RenderTexture.active = previous; view.Camera.targetTexture = null;
            Destroy(image); Destroy(target);
        }
    }
}
