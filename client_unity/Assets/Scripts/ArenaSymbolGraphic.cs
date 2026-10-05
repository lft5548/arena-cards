using UnityEngine;
using UnityEngine.UI;

namespace ArenaCards.Client
{
    // Native artwork stays crisp at both window and full-screen sizes.
    [RequireComponent(typeof(CanvasRenderer))]
    public sealed class ArenaSymbolGraphic : MaskableGraphic
    {
        public string Motif = "damage";
        protected override void OnPopulateMesh(VertexHelper vh)
        {
            vh.Clear(); var r = rectTransform.rect;
            Color tint = color;
            void Poly(params Vector2[] points)
            {
                int start = vh.currentVertCount;
                foreach (var p in points) vh.AddVert(new Vector3(r.xMin + p.x * r.width, r.yMin + p.y * r.height, 0), tint, Vector2.zero);
                for (int i = 1; i + 1 < points.Length; i++) vh.AddTriangle(start, start + i, start + i + 1);
            }
            if (Motif == "shield")
            {
                Poly(new Vector2(.2f,.82f),new Vector2(.8f,.82f),new Vector2(.74f,.4f),new Vector2(.5f,.14f),new Vector2(.26f,.4f));
                tint = new Color(.11f,.14f,.18f,tint.a);
                Poly(new Vector2(.3f,.74f),new Vector2(.7f,.74f),new Vector2(.64f,.44f),new Vector2(.5f,.28f),new Vector2(.36f,.44f));
            }
            else if (Motif == "damage" || Motif == "attack_boost")
            {
                Poly(new Vector2(.32f,.38f),new Vector2(.38f,.32f),new Vector2(.8f,.75f),new Vector2(.81f,.87f),new Vector2(.68f,.84f));
                Poly(new Vector2(.17f,.47f),new Vector2(.23f,.54f),new Vector2(.53f,.24f),new Vector2(.46f,.18f));
                Poly(new Vector2(.23f,.2f),new Vector2(.3f,.13f),new Vector2(.42f,.25f),new Vector2(.35f,.32f));
            }
            else if (Motif == "heal" || Motif == "heal_boost")
            {
                Poly(new Vector2(.4f,.2f),new Vector2(.6f,.2f),new Vector2(.6f,.8f),new Vector2(.4f,.8f));
                Poly(new Vector2(.2f,.4f),new Vector2(.8f,.4f),new Vector2(.8f,.6f),new Vector2(.2f,.6f));
            }
            else if (Motif == "burn")
                Poly(new Vector2(.5f,.12f),new Vector2(.2f,.42f),new Vector2(.36f,.7f),new Vector2(.42f,.5f),new Vector2(.59f,.88f),new Vector2(.8f,.42f));
            else if (Motif == "regen" || Motif == "poison")
            {
                Poly(new Vector2(.2f,.18f),new Vector2(.28f,.66f),new Vector2(.78f,.84f),new Vector2(.72f,.33f));
                tint = new Color(.11f,.14f,.18f,tint.a);
                Poly(new Vector2(.3f,.25f),new Vector2(.34f,.25f),new Vector2(.69f,.74f),new Vector2(.65f,.74f));
            }
            else if (Motif == "arena")
            {
                for (int i = 0; i < 4; i++)
                {
                    float s = .1f + i * .08f;
                    Poly(new Vector2(.5f,s),new Vector2(1-s,.5f),new Vector2(1-s-.008f,.5f),new Vector2(.5f,s+.012f));
                    Poly(new Vector2(s,.5f),new Vector2(.5f,1-s),new Vector2(.5f,1-s-.012f),new Vector2(s+.008f,.5f));
                    Poly(new Vector2(.5f,1-s),new Vector2(1-s,.5f),new Vector2(1-s-.008f,.5f),new Vector2(.5f,1-s-.012f));
                    Poly(new Vector2(s,.5f),new Vector2(.5f,s),new Vector2(.5f,s+.012f),new Vector2(s+.008f,.5f));
                }
            }
            else
            {
                Poly(new Vector2(.23f,.2f),new Vector2(.7f,.2f),new Vector2(.7f,.7f),new Vector2(.23f,.7f));
                Poly(new Vector2(.35f,.77f),new Vector2(.35f,.84f),new Vector2(.84f,.84f),new Vector2(.84f,.36f),new Vector2(.77f,.36f),new Vector2(.77f,.77f));
            }
        }
    }
}
