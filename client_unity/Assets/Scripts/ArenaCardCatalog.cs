using System.Collections.Generic;

namespace ArenaCards.Client
{
    // Display metadata for the shipped cards; the server validates all effects.
    public sealed class ArenaCardInfo
    {
        public int Id, Cost, Value, Duration;
        public string Name, Effect, Description;
    }
    public static class ArenaCardCatalog
    {
        private static readonly Dictionary<int, ArenaCardInfo> cards = new Dictionary<int, ArenaCardInfo>
        {
            { 1, Card(1, "Strike", 2, "damage", 8, 0, "Deal 8 damage.") },
            { 2, Card(2, "Mend", 1, "heal", 4, 0, "Restore 4 health.") },
            { 3, Card(3, "Barrier", 1, "shield", 6, 0, "Gain 6 shield.") },
            { 7, Card(7, "Venom", 1, "poison", 3, 2, "Poison 3 for 2 turns.") },
            { 8, Card(8, "Renew", 1, "regen", 3, 2, "Regenerate 3 for 2 turns.") },
            { 9, Card(9, "Disrupt", 2, "discard", 2, 0, "Discard 2 enemy cards.") },
            { 10, Card(10, "Ember", 1, "burn", 3, 2, "Burn 3 for 2 turn ends.") },
            { 11, Card(11, "Focus", 1, "attack_boost", 2, 2, "Attack +2 for 2 uses.") },
            { 12, Card(12, "Bless", 1, "heal_boost", 2, 2, "Healing +2 for 2 uses.") }
        };
        private static ArenaCardInfo Card(int id, string name, int cost, string effect, int value, int duration, string text)
            => new ArenaCardInfo { Id = id, Name = name, Cost = cost, Effect = effect,
                Value = value, Duration = duration, Description = text };
        public static bool TryGet(int id, out ArenaCardInfo card) => cards.TryGetValue(id, out card);
        public static ArenaCardInfo Get(int id) => TryGet(id, out var card) ? card :
            Card(id, "Card " + id, 0, "unknown", 0, 0, "Server configured card");
    }
}
