import { SvelteSet } from "svelte/reactivity";

/**
 * Reusable expansion state manager using Svelte 5 runes.
 * Manages a set of expanded item IDs with toggle functionality.
 */
export function createExpansionState() {
  let expanded = new SvelteSet<string>();

  return {
    get expanded() {
      return expanded;
    },
    isExpanded(id: string): boolean {
      return expanded.has(id);
    },
    toggle(id: string) {
      if (expanded.has(id)) {
        expanded.delete(id);
      } else {
        expanded.add(id);
      }
    },
    expand(id: string) {
      expanded.add(id);
    },
    collapse(id: string) {
      expanded.delete(id);
    },
    expandAll(ids: string[]) {
      expanded.clear();
      for (const id of ids) expanded.add(id);
    },
    collapseAll() {
      expanded.clear();
    },
  };
}
