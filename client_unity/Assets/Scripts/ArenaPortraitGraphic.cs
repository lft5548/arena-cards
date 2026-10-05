using UnityEngine;
using UnityEngine.UI;

namespace ArenaCards.Client
{
    // Original character portraits, rendered as native UI geometry.
    [RequireComponent(typeof(CanvasRenderer))]
    public sealed class ArenaPortraitGraphic : MaskableGraphic
    {
        public int Character;
        protected override void OnPopulateMesh(VertexHelper mesh)
        {
            mesh.Clear(); Rect r = rectTransform.rect;
            void Shape(Color tint, params Vector2[] points)
            {
                int start = mesh.currentVertCount;
                foreach (var p in points)
                    mesh.AddVert(new Vector3(r.xMin + p.x * r.width, r.yMin + p.y * r.height, 0), tint, Vector2.zero);
                for (int i = 1; i + 1 < points.Length; i++) mesh.AddTriangle(start, start + i, start + i + 1);
            }
            void Box(Color tint, float x0, float y0, float x1, float y1)
            { Shape(tint, new Vector2(x0,y0), new Vector2(x1,y0), new Vector2(x1,y1), new Vector2(x0,y1)); }
            Color accent = Character == 0 ? new Color32(95,214,164,255) : new Color32(240,117,104,255);
            Color dark = new Color32(26,32,40,255), steel = new Color32(95,116,131,255);
            Color skin = new Color32(214,166,134,255), shade = new Color32(158,106,87,255);
            Box(new Color32(35,44,53,255), 0,0,1,1);
            Shape(new Color32(46,60,68,255),new Vector2(0,0),new Vector2(1,0),new Vector2(1,.38f),new Vector2(.5f,.74f));
            Shape(accent,new Vector2(.08f,0),new Vector2(.17f,.28f),new Vector2(.38f,.42f),new Vector2(.62f,.42f),new Vector2(.83f,.28f),new Vector2(.92f,0));
            Shape(dark,new Vector2(.3f,0),new Vector2(.38f,.36f),new Vector2(.62f,.36f),new Vector2(.7f,0));
            Box(shade,.41f,.31f,.59f,.51f);
            Shape(skin,new Vector2(.29f,.73f),new Vector2(.71f,.73f),new Vector2(.67f,.43f),new Vector2(.5f,.34f),new Vector2(.33f,.43f));
            Shape(shade,new Vector2(.5f,.73f),new Vector2(.71f,.73f),new Vector2(.67f,.43f),new Vector2(.5f,.34f));
            if (Character == 0)
            {
                Shape(steel,new Vector2(.22f,.55f),new Vector2(.2f,.8f),new Vector2(.5f,.96f),new Vector2(.8f,.8f),new Vector2(.78f,.55f),new Vector2(.66f,.69f),new Vector2(.34f,.69f));
                Shape(new Color32(163,185,194,255),new Vector2(.2f,.8f),new Vector2(.5f,.96f),new Vector2(.5f,.72f),new Vector2(.29f,.7f));
                Box(accent,.47f,.7f,.53f,.97f);
                Shape(steel,new Vector2(.24f,.7f),new Vector2(.35f,.63f),new Vector2(.38f,.43f),new Vector2(.25f,.52f));
                Shape(steel,new Vector2(.76f,.7f),new Vector2(.65f,.63f),new Vector2(.62f,.43f),new Vector2(.75f,.52f));
                Shape(new Color32(113,139,151,255),new Vector2(.1f,.17f),new Vector2(.2f,.32f),new Vector2(.38f,.36f),new Vector2(.31f,.09f));
                Shape(new Color32(70,91,111,255),new Vector2(.9f,.17f),new Vector2(.8f,.32f),new Vector2(.62f,.36f),new Vector2(.69f,.09f));
            }
            else
            {
                Shape(new Color32(112,67,83,255),new Vector2(.5f,.97f),new Vector2(.85f,.78f),new Vector2(.87f,.3f),new Vector2(.69f,.43f),new Vector2(.68f,.76f),new Vector2(.5f,.84f));
                Shape(new Color32(169,87,95,255),new Vector2(.15f,.78f),new Vector2(.5f,.97f),new Vector2(.5f,.84f),new Vector2(.32f,.76f),new Vector2(.25f,.46f),new Vector2(.13f,.3f));
                Shape(new Color32(55,41,54,255),new Vector2(.29f,.7f),new Vector2(.41f,.84f),new Vector2(.7f,.74f),new Vector2(.65f,.66f),new Vector2(.41f,.76f));
                Shape(new Color32(215,177,96,255),new Vector2(.43f,.2f),new Vector2(.5f,.27f),new Vector2(.57f,.2f),new Vector2(.5f,.12f));
            }
            Box(dark,.33f,.58f,.45f,.62f); Box(dark,.55f,.58f,.67f,.62f);
            Box(new Color32(225,240,226,255),.37f,.586f,.43f,.602f);
            Box(new Color32(225,240,226,255),.57f,.586f,.63f,.602f);
            Shape(shade,new Vector2(.48f,.58f),new Vector2(.53f,.49f),new Vector2(.46f,.49f));
            Box(dark,.43f,.43f,.57f,.45f);
            Box(accent,0,0,1,.025f);
        }
    }
}
