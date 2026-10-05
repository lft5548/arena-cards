using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace ArenaCards.Client.Editor
{
    public static class ArenaDemoSceneBuilder
    {
        [MenuItem("Arena Cards/Create Demo Scene")]
        public static void CreateDemoScene()
        {
            if (!EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo()) return;
            Scene scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            GameObject root = new GameObject("ArenaCardsDemo");
            ArenaClient client = root.AddComponent<ArenaClient>();
            root.AddComponent<ArenaDemoController>();
            client.host = "127.0.0.1";
            client.port = 9000;
            client.username = "unity_player";
            client.protocol = ArenaProtocolId.TextV1;
            root.AddComponent<ArenaPresentationView>().Build();
            EditorSceneManager.MarkSceneDirty(scene);
            EditorSceneManager.SaveScene(scene, "Assets/ArenaDemo.unity");
            Selection.activeGameObject = root;
            Debug.Log("Arena Cards demo scene created at Assets/ArenaDemo.unity");
        }
    }
}
