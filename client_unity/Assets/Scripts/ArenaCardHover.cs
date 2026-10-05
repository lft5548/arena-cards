using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.UI;

namespace ArenaCards.Client
{
    public sealed class ArenaCardHover : MonoBehaviour, IPointerEnterHandler, IPointerExitHandler
    {
        public void OnPointerEnter(PointerEventData data)
        { if (GetComponent<Button>().interactable) transform.localScale = Vector3.one * 1.025f; }
        public void OnPointerExit(PointerEventData data) { transform.localScale = Vector3.one; }
        private void OnDisable() { transform.localScale = Vector3.one; }
    }
}
