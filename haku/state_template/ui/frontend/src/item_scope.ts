import { createContext } from "react";

// The current item's slug, provided by <ItemCard> to the affordances rendered in its body, so an
// item-scoped `<signal-toggle field="status">` resolves its `scope` without the author repeating
// the slug. Standalone module so both affordances.tsx (reader) and item_card.tsx (provider) can
// import it without an affordances ⇄ item_card import cycle (item_card → mdx → affordances).
export const ItemScopeContext = createContext<string | null>(null);
