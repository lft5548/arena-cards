using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.UI;

namespace ArenaCards.Client.Editor
{
    public static class ArenaPresentationVerification
    {
        // Run in an isolated project so the user's active scene is never changed.
        public static void Verify()
        {
            ArenaDemoSceneBuilder.CreateDemoScene();
            var controller = UnityEngine.Object.FindObjectOfType<ArenaDemoController>();
            var view = controller.GetComponent<ArenaPresentationView>();
            string output = Path.GetFullPath("PresentationEvidence"); Directory.CreateDirectory(output);
            Render(view, 1280, 720, Path.Combine(output, "lobby-1280.png"));
            controller.State.SetConnection("connected");
            controller.State.Apply(new ArenaMessage(ArenaMessageType.LoginResp, "ok=1;user=VANGUARD;token=qa"));
            controller.State.Apply(new ArenaMessage(ArenaMessageType.MatchFound, "match_id=preview;player_index=0"));
            controller.State.Apply(new ArenaMessage(ArenaMessageType.BattleSnapshot,
                "match_id=preview;player_index=0;turn=0;turn_id=7;revision=18;remaining_ms=23000;hand=1,2,3,7,10;" +
                "p0_hp=24;p1_hp=18;p0_energy=3;p1_energy=2;p0_shield=6;p1_shield=0;opponent_hand_count=4;deck_count=19;" +
                "p1_poison_value=3;p1_poison_turns=2;done=0"));
            view.Refresh();
            Render(view, 1280, 720, Path.Combine(output, "battle-1280.png"));
            Render(view, 1920, 1080, Path.Combine(output, "battle-1920.png"));
            foreach (string kind in new[] { "damage", "heal", "shield" })
            {
                controller.State.Apply(new ArenaMessage(ArenaMessageType.BattleEvent,
                    "type=" + kind + ";player=0;value=" + (kind == "damage" ? "8" : kind == "heal" ? "4" : "6")));
                view.Refresh(); Render(view, 1280, 720, Path.Combine(output, kind + "-feedback.png"));
            }
            controller.State.Apply(new ArenaMessage(ArenaMessageType.LeaderboardResp,
                "ok=1;source=mysql;count=2;entry_0_rank=1;entry_0_user=VANGUARD;entry_0_rating=1010;entry_0_wins=1;entry_0_losses=0;" +
                "entry_1_rank=2;entry_1_user=ARCANIST;entry_1_rating=990;entry_1_wins=0;entry_1_losses=1"));
            view.Refresh(); view.DisplayLeaderboard();
            Render(view, 1280, 720, Path.Combine(output, "leaderboard-preview.png"));
            view.CloseLeaderboard();
            controller.State.Apply(new ArenaMessage(ArenaMessageType.MatchResult, "winner=0;reason=hp_zero;turn_id=7"));
            view.Refresh();
            Render(view, 1280, 720, Path.Combine(output, "result-1280.png"));
            if (controller.State.CanAct) throw new InvalidOperationException("Finished battle still permits actions");
            // Restore an honest offline scene before packaging; previews never ship as gameplay.
            controller.State.SetConnection("offline");
            controller.State.LoggedIn = false; controller.State.Done = false; controller.State.MatchId = "";
            controller.State.ResultTitle = controller.State.Hand = ""; view.Refresh();
            EditorSceneManager.SaveScene(EditorSceneManager.GetActiveScene());
            AssetDatabase.ExportPackage(new[] { "Assets/Scripts", "Assets/Editor", "Assets/Plugins", "Assets/ArenaDemo.unity" },
                Path.Combine(output, "ArenaCardsPresentation.unitypackage"), ExportPackageOptions.Recurse);
            var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions {
                scenes = new[] { "Assets/ArenaDemo.unity" }, locationPathName = "Builds/ArenaCardsDemo/ArenaCardsDemo.exe",
                target = BuildTarget.StandaloneWindows64, options = BuildOptions.None });
            if (report.summary.result != UnityEditor.Build.Reporting.BuildResult.Succeeded)
                throw new InvalidOperationException("Windows presentation build failed: " + report.summary.result);
            Debug.Log("ARENA_PRESENTATION_VERIFIED: editor compilation, eight renders, Unity package, Windows build");
        }
        private static void Render(ArenaPresentationView view, int width, int height, string path)
        {
            var target = new RenderTexture(width, height, 24); view.Camera.targetTexture = target;
            Canvas.ForceUpdateCanvases();
            foreach (var r in view.Canvas.GetComponentsInChildren<RectTransform>()) LayoutRebuilder.ForceRebuildLayoutImmediate(r);
            Canvas.ForceUpdateCanvases(); view.Camera.Render();
            var previous = RenderTexture.active; RenderTexture.active = target;
            var image = new Texture2D(width, height, TextureFormat.RGB24, false);
            image.ReadPixels(new Rect(0, 0, width, height), 0, 0); image.Apply();
            File.WriteAllBytes(path, image.EncodeToPNG());
            RenderTexture.active = previous; view.Camera.targetTexture = null;
            UnityEngine.Object.DestroyImmediate(image); UnityEngine.Object.DestroyImmediate(target);
        }
    }
}
