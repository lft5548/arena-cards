using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.UI;

namespace ArenaCards.Client
{
    [RequireComponent(typeof(ArenaDemoController))]
    public sealed class ArenaPresentationView : MonoBehaviour
    {
        private static readonly Color Ink = new Color32(18, 21, 25, 255);
        private static readonly Color Surface = new Color32(30, 35, 40, 255);
        private static readonly Color Line = new Color32(65, 73, 80, 255);
        private static readonly Color Paper = new Color32(238, 241, 239, 255);
        private static readonly Color Muted = new Color32(151, 163, 171, 255);
        private static readonly Color Green = new Color32(95, 214, 164, 255);
        private static readonly Color Red = new Color32(240, 117, 104, 255);
        private static readonly Color Blue = new Color32(104, 181, 232, 255);
        public Canvas Canvas { get; private set; }
        public Camera Camera { get; private set; }
        private ArenaDemoController controller;
        private Font font;
        private Text connection, phase, clock, match, effect, notice, handLabel, logText, adminText;
        private Text resultTitle, resultDetail, lobbyTitle, lobbyNotice;
        private readonly Text[] names = new Text[2], stats = new Text[2], statuses = new Text[2];
        private readonly Image[] health = new Image[2];
        private readonly Image[] shieldBars = new Image[2], hitFlash = new Image[2];
        private readonly Image[,] energyPips = new Image[2, 3];
        private readonly int[] shieldRange = { 12, 12 };
        private readonly Text[] resourceText = new Text[2], shieldText = new Text[2], hitText = new Text[2];
        private readonly ArenaPortraitGraphic[] portraits = new ArenaPortraitGraphic[2];
        private readonly float[] hitUntil = new float[2];
        private readonly Color[] hitColor = new Color[2];
        private Image countdown;
        private RectTransform rankingRows;
        private Text rankingStatus;
        private Button rankingButton, rankingRefresh;
        private GameObject ranking;
        private readonly List<Button> cards = new List<Button>();
        private readonly List<int> cardIds = new List<int>();
        private Button endTurn, find, cancel, resume, disconnect, connect, protocolButton;
        private RectTransform handRoot;
        private GameObject lobby, outcome, metrics;
        private InputField hostInput, portInput, userInput;
        private Text protocolText;
        private ArenaProtocolId protocol;
        private string shownHand;
        private string shownRanking;
        private float effectUntil;
        private bool resultDismissed;

        private void Awake() { Build(); }
        private void OnEnable() { if (controller != null) controller.Changed += Refresh; }
        private void OnDisable() { if (controller != null) controller.Changed -= Refresh; }
        public void Build()
        {
            if (Canvas != null) return;
            foreach (string name in new[] { "ArenaCanvas", "ArenaCamera", "ArenaEventSystem" })
            {
                var old = transform.Find(name);
                if (old == null) continue;
                old.gameObject.SetActive(false);
                if (Application.isPlaying) Destroy(old.gameObject); else DestroyImmediate(old.gameObject);
            }
            controller = GetComponent<ArenaDemoController>();
            var client = GetComponent<ArenaClient>();
            font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
            if (Font.GetOSInstalledFontNames().Contains("Microsoft YaHei"))
                font = Font.CreateDynamicFontFromOSFont("Microsoft YaHei", 18);
            protocol = client.protocol;
            Camera = new GameObject("ArenaCamera", typeof(Camera)).GetComponent<Camera>();
            Camera.transform.SetParent(transform, false);
            Camera.clearFlags = CameraClearFlags.SolidColor; Camera.backgroundColor = Ink;
            Camera.orthographic = true; Camera.nearClipPlane = 0.1f; Camera.farClipPlane = 100;
            Camera.transform.localPosition = new Vector3(0, 0, -10);
            var root = new GameObject("ArenaCanvas", typeof(RectTransform), typeof(Canvas), typeof(CanvasScaler), typeof(GraphicRaycaster));
            root.transform.SetParent(transform, false);
            Canvas = root.GetComponent<Canvas>(); Canvas.renderMode = RenderMode.ScreenSpaceCamera;
            Canvas.worldCamera = Camera; Canvas.planeDistance = 1;
            var scaler = root.GetComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1600, 900); scaler.matchWidthOrHeight = 0.5f;
            if (FindObjectOfType<EventSystem>() == null)
            {
                var events = new GameObject("ArenaEventSystem", typeof(EventSystem), typeof(StandaloneInputModule));
                events.transform.SetParent(transform, false);
            }
            var screen = (RectTransform)root.transform;
            Panel(screen, "Background", Ink, 0, 0, 1, 1);
            var header = Panel(screen, "Header", Ink, 0, .91f, 1, 1);
            Label(header, "ARENA CARDS", 32, Paper, .02f, .23f, .4f, .8f, TextAnchor.MiddleLeft);
            connection = Label(header, "OFFLINE", 16, Muted, .6f, .25f, .86f, .75f, TextAnchor.MiddleRight);
            disconnect = Button(header, "Disconnect", .875f, .2f, .98f, .8f, () => controller.Disconnect(), Surface);
            var board = Panel(screen, "Board", Ink, .015f, .02f, .79f, .9f);
            var background = new GameObject("ArenaGeometry", typeof(RectTransform), typeof(CanvasRenderer), typeof(ArenaSymbolGraphic));
            background.transform.SetParent(board, false); Fit((RectTransform)background.transform, .2f, .55f, .78f, .78f);
            background.GetComponent<ArenaSymbolGraphic>().Motif = "arena";
            background.GetComponent<ArenaSymbolGraphic>().color = new Color(.16f, .19f, .22f);
            Player(board, 1, .765f, 1, Red);
            Player(board, 0, .335f, .57f, Green);
            phase = Label(board, "THE ARENA AWAITS", 28, Paper, .05f, .62f, .8f, .72f, TextAnchor.MiddleCenter);
            clock = Label(board, "--", 46, Green, .8f, .59f, .98f, .75f, TextAnchor.MiddleCenter);
            var countdownTrack = Panel(board, "TurnProgressTrack", Line, .08f, .62f, .98f, .628f);
            countdown = Panel(countdownTrack, "TurnProgress", Green, 0, 0, 1, 1).GetComponent<Image>();
            effect = Label(board, "", 24, Paper, .1f, .54f, .8f, .62f, TextAnchor.MiddleCenter);
            notice = Label(board, "", 17, Red, .04f, .3f, .72f, .35f, TextAnchor.MiddleLeft);
            endTurn = Button(board, "End Turn  >", .79f, .275f, .98f, .335f, () => controller.EndTurn(), Green);
            handLabel = Label(board, "YOUR HAND", 17, Muted, .04f, .27f, .76f, .31f, TextAnchor.MiddleLeft);
            var hand = Panel(board, "Hand", Color.clear, .02f, .005f, .98f, .265f);
            var scroll = hand.gameObject.AddComponent<ScrollRect>(); scroll.horizontal = true; scroll.vertical = false;
            hand.gameObject.AddComponent<RectMask2D>(); scroll.viewport = hand;
            handRoot = Panel(hand, "Cards", Color.clear, 0, 0, 1, 1);
            handRoot.anchorMin = new Vector2(0, 0); handRoot.anchorMax = new Vector2(0, 1);
            handRoot.pivot = new Vector2(0, .5f); scroll.content = handRoot;
            var side = Panel(screen, "Activity", Surface, .81f, .02f, .985f, .9f);
            Label(side, "BATTLE RECORD", 19, Paper, .08f, .925f, .94f, .975f, TextAnchor.MiddleLeft);
            match = Label(side, "No active match", 13, Muted, .08f, .875f, .94f, .925f, TextAnchor.MiddleLeft);
            logText = ScrollingText(side, "Events", .07f, .34f, .94f, .865f, 16);
            rankingButton = Button(side, "Leaderboard", .08f, .255f, .94f, .315f, ShowLeaderboard, Blue);
            Button(side, "Diagnostics", .08f, .175f, .94f, .235f, () =>
            { metrics.SetActive(!metrics.activeSelf); controller.RequestRooms(); }, Line);
            resume = Button(side, "Resume match", .08f, .095f, .94f, .155f, () => controller.Reconnect(), Line);
            Button(side, "New match", .08f, .02f, .94f, .08f, () => controller.JoinMatch(), Line);
            metrics = Panel(screen, "DiagnosticsOverlay", Surface, .57f, .22f, .98f, .75f).gameObject;
            Label((RectTransform)metrics.transform, "SERVER METRICS", 24, Paper, .06f, .83f, .8f, .98f, TextAnchor.MiddleLeft);
            Button((RectTransform)metrics.transform, "X", .88f, .85f, .96f, .96f, () => metrics.SetActive(false), Line);
            adminText = ScrollingText((RectTransform)metrics.transform, "Metrics", .06f, .06f, .95f, .81f, 16);
            metrics.SetActive(false);
            CreateLobby(screen, client);
            CreateOutcome(screen);
            CreateLeaderboard(screen);
            Refresh();
        }

        private void Player(RectTransform board, int slot, float bottom, float top, Color accent)
        {
            var region = Panel(board, "Player" + slot, Color.clear, .03f, bottom, .97f, top);
            var avatar = new GameObject("CharacterPortrait", typeof(RectTransform), typeof(CanvasRenderer), typeof(ArenaPortraitGraphic));
            avatar.transform.SetParent(region, false); Fit((RectTransform)avatar.transform, 0, .06f, .11f, .95f);
            portraits[slot] = avatar.GetComponent<ArenaPortraitGraphic>(); portraits[slot].Character = slot; portraits[slot].raycastTarget = false;
            names[slot] = Label(region, slot == 0 ? "YOU" : "OPPONENT", 23, Paper, .14f, .68f, .82f, .98f, TextAnchor.MiddleLeft);
            names[slot].resizeTextForBestFit = true; names[slot].resizeTextMinSize = 10; names[slot].resizeTextMaxSize = 23;
            stats[slot] = Label(region, "30 / 30 HP", 18, accent, .14f, .43f, .7f, .68f, TextAnchor.MiddleLeft);
            var bar = Panel(region, "HealthTrack", Line, .14f, .35f, .97f, .39f);
            health[slot] = Panel(bar, "Health", accent, 0, 0, 1, 1).GetComponent<Image>();
            resourceText[slot] = Label(region, "ENERGY 3", 14, Muted, .14f, .16f, .46f, .32f, TextAnchor.MiddleLeft);
            shieldText[slot] = Label(region, "SHIELD 0", 14, Blue, .55f, .16f, .97f, .32f, TextAnchor.MiddleLeft);
            for (int pip = 0; pip < 3; pip++)
                energyPips[slot, pip] = Panel(region, "Energy" + pip, Green, .14f + pip * .046f, .065f, .179f + pip * .046f, .125f).GetComponent<Image>();
            var shieldTrack = Panel(region, "ShieldTrack", Line, .55f, .065f, .97f, .125f);
            shieldBars[slot] = Panel(shieldTrack, "Shield", Blue, 0, 0, 0, 1).GetComponent<Image>();
            statuses[slot] = Label(region, "", 12, Muted, .14f, -.07f, .97f, .04f, TextAnchor.MiddleLeft);
            hitText[slot] = Label(region, "", 21, Paper, .68f, .47f, .97f, .9f, TextAnchor.MiddleRight);
            hitFlash[slot] = Panel(region, "FeedbackFlash", Color.clear, 0, .06f, .11f, .95f).GetComponent<Image>();
            hitFlash[slot].raycastTarget = false;
        }
        private void CreateLobby(RectTransform screen, ArenaClient client)
        {
            lobby = Panel(screen, "LobbyOverlay", new Color(0, 0, 0, .7f), 0, 0, 1, 1).gameObject;
            var form = Panel((RectTransform)lobby.transform, "ConnectForm", Surface, .32f, .21f, .68f, .77f);
            lobbyTitle = Label(form, "ENTER THE ARENA", 28, Paper, .07f, .81f, .94f, .96f, TextAnchor.MiddleLeft);
            Label(form, "PLAYER", 14, Muted, .07f, .7f, .9f, .78f, TextAnchor.MiddleLeft);
            userInput = Input(form, client.username, .07f, .6f, .93f, .71f);
            Label(form, "SERVER", 14, Muted, .07f, .49f, .9f, .57f, TextAnchor.MiddleLeft);
            hostInput = Input(form, client.host, .07f, .39f, .69f, .5f);
            portInput = Input(form, client.port.ToString(), .72f, .39f, .93f, .5f);
            protocolButton = Button(form, "", .07f, .24f, .93f, .35f, () =>
            { protocol = protocol == ArenaProtocolId.TextV1 ? ArenaProtocolId.ProtoV1 : ArenaProtocolId.TextV1; Refresh(); }, Line);
            protocolText = protocolButton.GetComponentInChildren<Text>();
            lobbyNotice = Label(form, "", 14, Red, .07f, .005f, .93f, .075f, TextAnchor.MiddleLeft);
            connect = Button(form, "Connect", .07f, .08f, .93f, .2f, () =>
            { int port; int.TryParse(portInput.text, out port); controller.Login(hostInput.text, port, userInput.text, protocol); }, Green);
            find = Button(form, "Find opponent", .07f, .08f, .93f, .2f, () => controller.JoinMatch(), Green);
            cancel = Button(form, "Cancel search", .07f, .08f, .93f, .2f, () => controller.CancelMatch(), Line);
        }
        private void CreateOutcome(RectTransform screen)
        {
            outcome = Panel(screen, "ResultOverlay", new Color(0, 0, 0, .78f), 0, 0, 1, 1).gameObject;
            var form = Panel((RectTransform)outcome.transform, "Result", Surface, .3f, .28f, .7f, .72f);
            resultTitle = Label(form, "VICTORY", 48, Green, .06f, .6f, .94f, .88f, TextAnchor.MiddleCenter);
            resultDetail = Label(form, "", 20, Muted, .08f, .39f, .92f, .58f, TextAnchor.MiddleCenter);
            Button(form, "Play again", .08f, .12f, .63f, .28f, () =>
            { resultDismissed = true; controller.JoinMatch(); Refresh(); }, Green);
            Button(form, "Review", .67f, .12f, .92f, .28f, () => { resultDismissed = true; Refresh(); }, Line);
            Button(form, "Leaderboard", .3f, .015f, .7f, .09f, ShowLeaderboard, Surface);
        }
        private void CreateLeaderboard(RectTransform screen)
        {
            ranking = Panel(screen, "LeaderboardOverlay", new Color(0, 0, 0, .85f), 0, 0, 1, 1).gameObject;
            var panel = Panel((RectTransform)ranking.transform, "Leaderboard", Surface, .18f, .12f, .82f, .88f);
            Label(panel, "ARENA LEADERBOARD", 30, Paper, .05f, .875f, .78f, .98f, TextAnchor.MiddleLeft);
            Button(panel, "X", .88f, .89f, .96f, .97f, () => ranking.SetActive(false), Line);
            rankingStatus = Label(panel, "", 16, Muted, .05f, .8f, .78f, .87f, TextAnchor.MiddleLeft);
            var headings = Panel(panel, "Headings", Ink, .05f, .73f, .95f, .795f);
            Label(headings, "RANK", 14, Muted, .015f, 0, .12f, 1, TextAnchor.MiddleLeft);
            Label(headings, "PLAYER", 14, Muted, .14f, 0, .57f, 1, TextAnchor.MiddleLeft);
            Label(headings, "RATING", 14, Muted, .58f, 0, .78f, 1, TextAnchor.MiddleRight);
            Label(headings, "W / L", 14, Muted, .8f, 0, .98f, 1, TextAnchor.MiddleRight);
            var viewport = Panel(panel, "RankingViewport", Color.clear, .05f, .14f, .95f, .73f);
            viewport.gameObject.AddComponent<RectMask2D>(); var scroll = viewport.gameObject.AddComponent<ScrollRect>();
            scroll.horizontal = false; scroll.viewport = viewport;
            rankingRows = Panel(viewport, "Rows", Color.clear, 0, 1, 1, 1);
            rankingRows.pivot = new Vector2(.5f, 1); scroll.content = rankingRows;
            rankingRefresh = Button(panel, "Refresh", .68f, .035f, .95f, .105f, () => controller.RequestLeaderboard(), Blue);
            Label(panel, "RATING", 14, Muted, .05f, .035f, .35f, .105f, TextAnchor.MiddleLeft);
            ranking.SetActive(false);
        }
        public void ShowLeaderboard()
        {
            DisplayLeaderboard(); controller.RequestLeaderboard();
        }
        public void DisplayLeaderboard() { ranking.SetActive(true); RefreshLeaderboard(); }
        public void CloseLeaderboard() { ranking.SetActive(false); }
        private void RefreshLeaderboard()
        {
            var s = controller.State;
            rankingButton.interactable = s.Connected && s.LoggedIn;
            rankingRefresh.interactable = s.Connected && s.LoggedIn && !s.LeaderboardLoading;
            rankingStatus.text = s.LeaderboardLoading ? "Loading..." : s.LeaderboardError != "" ? s.LeaderboardError :
                s.LeaderboardSource == "" ? "Select Refresh to load rankings" : s.Leaderboard.Count == 0 ? "No ranked players yet" :
                "Top " + s.Leaderboard.Count + "  /  " + s.LeaderboardSource.ToUpperInvariant();
            rankingStatus.color = s.LeaderboardError != "" ? Red : Muted;
            string current = s.LeaderboardLoading + "|" + s.LeaderboardError + "|" + s.LeaderboardSource + "|" +
                string.Join(";", s.Leaderboard.Select(row => row.Rank + ":" + row.User + ":" + row.Rating + ":" + row.Wins + ":" + row.Losses));
            if (shownRanking == current) return;
            shownRanking = current;
            for (int i = rankingRows.childCount - 1; i >= 0; i--)
            {
                var old = rankingRows.GetChild(i).gameObject; old.SetActive(false);
                if (Application.isPlaying) Destroy(old); else DestroyImmediate(old);
            }
            int index = 0;
            if (!s.LeaderboardLoading && s.LeaderboardError == "")
            foreach (var entry in s.Leaderboard)
            {
                var row = Panel(rankingRows, "Rank" + entry.Rank, entry.User == s.User ? new Color(.13f,.24f,.2f) :
                    index % 2 == 0 ? Surface : Ink, 0, 1, 1, 1);
                row.pivot = new Vector2(.5f, 1); row.offsetMin = new Vector2(0, -(index + 1) * 48);
                row.offsetMax = new Vector2(0, -index * 48);
                Label(row, entry.Rank.ToString("00"), 18, entry.Rank == 1 ? Green : Muted, .015f, 0, .12f, 1, TextAnchor.MiddleLeft);
                var user = Label(row, entry.User, 18, Paper, .14f, .08f, .57f, .92f, TextAnchor.MiddleLeft);
                user.resizeTextForBestFit = true; user.resizeTextMinSize = 10; user.resizeTextMaxSize = 18;
                Label(row, entry.Rating.ToString(), 18, Blue, .58f, 0, .78f, 1, TextAnchor.MiddleRight);
                var record = Label(row, entry.Wins + " / " + entry.Losses, 16, Muted, .8f, .08f, .98f, .92f, TextAnchor.MiddleRight);
                record.resizeTextForBestFit = true; record.resizeTextMinSize = 9; record.resizeTextMaxSize = 16;
                index++;
            }
            rankingRows.sizeDelta = new Vector2(0, index * 48);
        }
        private static Color FeedbackColor(string kind)
            => kind == "heal" || kind == "regen" || kind == "heal_boost" ? Green :
                kind == "shield" || kind == "attack_boost" ? Blue : Red;
        private void ReadFeedback()
        {
            while (controller.State.TryTakeFeedback(out var hit))
            {
                int slot = controller.State.PlayerIndex < 0 ? hit.Player : hit.Player == controller.State.PlayerIndex ? 0 : 1;
                hitText[slot].text = hit.Label; hitText[slot].color = FeedbackColor(hit.Kind);
                hitUntil[slot] = Time.unscaledTime + 1.2f; hitColor[slot] = FeedbackColor(hit.Kind);
                hitFlash[slot].color = new Color(hitColor[slot].r, hitColor[slot].g, hitColor[slot].b, .4f);
                effect.text = hit.Label; effect.color = FeedbackColor(hit.Kind); effectUntil = Time.unscaledTime + 1.2f;
            }
        }
        private void Update()
        {
            if (Canvas == null) return;
            var state = controller.State;
            clock.text = state.InMatch ? state.RemainingSeconds.ToString("00") : "--";
            clock.color = state.RemainingSeconds <= 10 && state.InMatch ? Red : Green;
            countdown.rectTransform.anchorMax = new Vector2(state.RemainingFraction, 1);
            countdown.color = clock.color;
            endTurn.interactable = state.CanAct;
            for (int i = 0; i < cards.Count; i++) cards[i].interactable = state.CanPlay(cardIds[i]);
            for (int p = 0; p < 2; p++)
            {
                int source = state.PlayerIndex < 0 ? p : p == 0 ? state.PlayerIndex : 1 - state.PlayerIndex;
                var r = health[p].rectTransform;
                r.anchorMax = new Vector2(Mathf.MoveTowards(r.anchorMax.x, Mathf.Clamp01(state.Hp[source] / 30f), Time.unscaledDeltaTime * 1.4f), 1);
                float time = Mathf.Max(0, hitUntil[p] - Time.unscaledTime);
                hitFlash[p].color = new Color(hitColor[p].r, hitColor[p].g, hitColor[p].b, Mathf.Min(.5f, time * .65f));
                Color textColor = hitText[p].color; textColor.a = Mathf.Min(1, time * 2); hitText[p].color = textColor;
                hitText[p].rectTransform.anchoredPosition = new Vector2(0, (1.2f - time) * 16);
            }
            if (effectUntil > Time.unscaledTime)
            { Color tint = effect.color; tint.a = Mathf.Min(1, (effectUntil - Time.unscaledTime) * 2); effect.color = tint; }
            else effect.text = "";
        }
        public void Refresh()
        {
            if (Canvas == null) return;
            var s = controller.State;
            clock.text = s.InMatch ? s.RemainingSeconds.ToString("00") : "--";
            countdown.rectTransform.anchorMax = new Vector2(s.RemainingFraction, 1);
            countdown.color = s.RemainingSeconds <= 10 && s.InMatch ? Red : Green;
            connection.text = s.Connection.ToUpperInvariant() + "  /  " + protocol;
            connection.color = s.Connected ? Green : Muted;
            disconnect.interactable = s.Connected;
            phase.text = s.Done ? "MATCH COMPLETE" : s.Queued ? "FINDING AN OPPONENT" :
                !s.InMatch ? "THE ARENA AWAITS" : s.MyTurn ? "YOUR TURN" : "OPPONENT'S TURN";
            phase.color = s.MyTurn ? Green : Paper;
            match.text = s.InMatch || s.Done ? "Turn " + s.TurnId + "  /  Revision " + s.Revision : "No active match";
            for (int p = 0; p < 2; p++)
            {
                int source = s.PlayerIndex < 0 ? p : p == 0 ? s.PlayerIndex : 1 - s.PlayerIndex;
                names[p].text = p == 0 ? (s.User == "" ? "YOU" : s.User.ToUpperInvariant()) : s.Opponent.ToUpperInvariant();
                stats[p].text = s.Hp[source] + " / 30 HP";
                resourceText[p].text = "ENERGY " + s.Energy[source]; shieldText[p].text = "SHIELD " + s.Shield[source];
                for (int pip = 0; pip < 3; pip++) energyPips[p, pip].color = pip < s.Energy[source] ? Green : Line;
                shieldRange[p] = Math.Max(shieldRange[p], s.Shield[source]);
                shieldBars[p].rectTransform.anchorMax = new Vector2(s.Shield[source] / (float)shieldRange[p], 1);
                if (portraits[p].Character != source) { portraits[p].Character = source; portraits[p].SetVerticesDirty(); }
                statuses[p].text = s.Statuses[source];
                if (!Application.isPlaying)
                    health[p].rectTransform.anchorMax = new Vector2(Mathf.Clamp01(s.Hp[source] / 30f), 1);
            }
            notice.text = s.Notice;
            lobbyNotice.text = s.Notice;
            handLabel.text = "YOUR HAND  /  " + ArenaDemoState.Cards(s.Hand).Count() + "     DECK " + s.DeckCount +
                "     DISCARD " + ArenaDemoState.Cards(s.Discard).Count() + "     ENEMY HAND " + s.OpponentHandCount;
            if (shownHand != s.Hand) RebuildHand(s.Hand);
            logText.text = string.Join("\n\n", s.Log.Reverse().ToArray());
            adminText.text = string.IsNullOrEmpty(s.Rooms) ? "Waiting for server metrics" : s.Rooms.Replace(';', '\n');
            resume.interactable = !s.Connected && !string.IsNullOrEmpty(s.SessionToken) && !s.Done;
            ReadFeedback();
            if (!s.Done) resultDismissed = false;
            outcome.SetActive(s.Done && !resultDismissed && s.ResultTitle != "");
            resultTitle.text = s.ResultTitle;
            resultTitle.color = s.ResultTitle == "DEFEAT" ? Red : s.ResultTitle == "DRAW" ? Blue : Green;
            resultDetail.text = s.ResultDetail;
            lobby.SetActive(!outcome.activeSelf && (!s.LoggedIn || !s.InMatch && (!s.Done || !s.Connected) || s.Queued));
            lobbyTitle.text = s.Queued ? "FINDING AN OPPONENT" : s.LoggedIn && s.Connected ? "READY FOR A MATCH" : "ENTER THE ARENA";
            connect.gameObject.SetActive(!s.LoggedIn || !s.Connected);
            connect.interactable = s.Connection != "connecting";
            find.gameObject.SetActive(s.LoggedIn && s.Connected && !s.Queued);
            find.interactable = s.Connected;
            cancel.gameObject.SetActive(s.Queued);
            protocolText.text = "Protocol: " + protocol;
            protocolButton.interactable = !s.Connected;
            hostInput.interactable = portInput.interactable = userInput.interactable = !s.LoggedIn || !s.Connected;
            RefreshLeaderboard();
        }
        private void RebuildHand(string value)
        {
            shownHand = value;
            for (int i = handRoot.childCount - 1; i >= 0; i--)
            { if (Application.isPlaying) Destroy(handRoot.GetChild(i).gameObject); else DestroyImmediate(handRoot.GetChild(i).gameObject); }
            cards.Clear(); cardIds.Clear();
            int index = 0;
            foreach (int id in ArenaDemoState.Cards(value))
            {
                var info = ArenaCardCatalog.Get(id);
                Color accent = info.Effect == "damage" || info.Effect == "burn" ? Red :
                    info.Effect == "shield" || info.Effect == "discard" ? Blue : Green;
                var card = Panel(handRoot, info.Name, Surface, 0, 0, 0, 1);
                card.offsetMin = new Vector2(index * 174 + 4, 5); card.offsetMax = new Vector2(index * 174 + 166, -5);
                var button = card.gameObject.AddComponent<Button>(); button.targetGraphic = card.GetComponent<Image>();
                button.colors = ButtonColors(accent); int selected = id;
                button.onClick.AddListener(() => controller.PlayCard(selected));
                card.gameObject.AddComponent<ArenaCardHover>();
                Panel(card, "Accent", accent, 0, .975f, 1, 1);
                Label(card, ArenaCardCatalog.TryGet(id, out var _) ? info.Cost.ToString() : "?", 22, accent, .09f, .78f, .3f, .94f, TextAnchor.MiddleLeft);
                var art = Panel(card, "Art", Color.clear, .15f, .32f, .87f, .87f);
                Symbol(art, info.Effect, accent);
                Label(card, info.Name.ToUpperInvariant(), 20, Paper, .07f, .21f, .94f, .38f, TextAnchor.MiddleLeft);
                Label(card, info.Description, 14, Muted, .07f, .025f, .94f, .22f, TextAnchor.MiddleLeft);
                cards.Add(button); cardIds.Add(id); index++;
            }
            handRoot.sizeDelta = new Vector2(Math.Max(1, index * 174 + 8), 0);
        }

        private RectTransform Panel(Transform parent, string name, Color color, float x0, float y0, float x1, float y1)
        {
            var g = new GameObject(name, typeof(RectTransform), typeof(Image)); g.transform.SetParent(parent, false);
            var r = (RectTransform)g.transform; Fit(r, x0, y0, x1, y1);
            var image = g.GetComponent<Image>(); image.color = color; image.raycastTarget = color.a > 0;
            return r;
        }
        private void Symbol(Transform parent, string motif, Color color)
        {
            var g = new GameObject("Symbol", typeof(RectTransform), typeof(CanvasRenderer), typeof(ArenaSymbolGraphic));
            g.transform.SetParent(parent, false); Fit((RectTransform)g.transform, 0, 0, 1, 1);
            var symbol = g.GetComponent<ArenaSymbolGraphic>(); symbol.Motif = motif; symbol.color = color; symbol.raycastTarget = false;
        }
        private Text Label(Transform parent, string value, int size, Color color, float x0, float y0, float x1, float y1, TextAnchor alignment)
        {
            var g = new GameObject("Text", typeof(RectTransform), typeof(Text)); g.transform.SetParent(parent, false);
            Fit((RectTransform)g.transform, x0, y0, x1, y1);
            var text = g.GetComponent<Text>(); text.font = font; text.text = value; text.fontSize = size;
            text.color = color; text.alignment = alignment; text.raycastTarget = false;
            text.supportRichText = false; text.horizontalOverflow = HorizontalWrapMode.Wrap;
            text.verticalOverflow = VerticalWrapMode.Truncate;
            return text;
        }
        private static void Fit(RectTransform r, float x0, float y0, float x1, float y1)
        { r.anchorMin = new Vector2(x0, y0); r.anchorMax = new Vector2(x1, y1); r.offsetMin = r.offsetMax = Vector2.zero; }
        private static ColorBlock ButtonColors(Color accent)
        {
            var c = ColorBlock.defaultColorBlock; c.normalColor = Color.white;
            c.highlightedColor = new Color(1.18f, 1.18f, 1.18f); c.pressedColor = accent;
            c.disabledColor = new Color(.5f, .5f, .5f, .75f); return c;
        }
        private Button Button(Transform parent, string title, float x0, float y0, float x1, float y1, Action action, Color color)
        {
            var r = Panel(parent, title, color, x0, y0, x1, y1); var b = r.gameObject.AddComponent<Button>();
            b.colors = ButtonColors(color); b.onClick.AddListener(() => action());
            Label(r, title, 18, color == Green ? Ink : Paper, .025f, 0, .975f, 1, TextAnchor.MiddleCenter);
            return b;
        }
        private InputField Input(Transform parent, string value, float x0, float y0, float x1, float y1)
        {
            var r = Panel(parent, "Input", Ink, x0, y0, x1, y1); var input = r.gameObject.AddComponent<InputField>();
            input.textComponent = Label(r, "", 20, Paper, .04f, 0, .96f, 1, TextAnchor.MiddleLeft);
            input.text = value; input.characterLimit = 64; return input;
        }
        private Text ScrollingText(Transform parent, string name, float x0, float y0, float x1, float y1, int size)
        {
            var r = Panel(parent, name, Color.clear, x0, y0, x1, y1); r.gameObject.AddComponent<RectMask2D>();
            var scroll = r.gameObject.AddComponent<ScrollRect>(); scroll.viewport = r; scroll.horizontal = false;
            var text = Label(r, "", size, Muted, 0, 1, 1, 1, TextAnchor.UpperLeft);
            text.rectTransform.pivot = new Vector2(.5f, 1);
            var fitter = text.gameObject.AddComponent<ContentSizeFitter>(); fitter.verticalFit = ContentSizeFitter.FitMode.PreferredSize;
            scroll.content = text.rectTransform; return text;
        }
    }

}
