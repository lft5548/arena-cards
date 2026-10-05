using System;

namespace UnityEngine
{
    public class MonoBehaviour
    {
        public T GetComponent<T>() where T : new() { return new T(); }
        public int GetInstanceID() { return 1; }
    }

    [AttributeUsage(AttributeTargets.Field)]
    public sealed class HeaderAttribute : Attribute
    {
        public HeaderAttribute(string text) { }
    }

    [AttributeUsage(AttributeTargets.Class)]
    public sealed class RequireComponentAttribute : Attribute
    {
        public RequireComponentAttribute(Type type) { }
    }

    public struct Rect
    {
        public Rect(float left, float top, float width, float height) { }
    }

    public sealed class GUILayoutOption { }

    public static class GUILayout
    {
        public static void Label(string text) { }
        public static void Label(string text, params GUILayoutOption[] options) { }
        public static void BeginHorizontal() { }
        public static void EndHorizontal() { }
        public static bool Button(string text, params GUILayoutOption[] options) { return false; }
        public static string TextArea(string text, params GUILayoutOption[] options) { return text; }
        public static GUILayoutOption Width(float value) { return new GUILayoutOption(); }
        public static GUILayoutOption Height(float value) { return new GUILayoutOption(); }
    }

    public static class GUI
    {
        public static Rect Window(int id, Rect window, Action<int> draw, string title) { return window; }
        public static void DragWindow() { }
    }
}
