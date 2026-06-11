import { writable } from "svelte/store";

export type Prefs = {
  renderMarkdown: boolean;
  leftSidebarWidth: number;
  rightSidebarWidth: number;
  // Height in px of the Agents section in the combined left pane (top/bottom split)
  leftTopHeight: number;
  showAgentsSidebar: boolean;
};

const KEY = "adgn_prefs_v1";

function load(): Prefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const obj = JSON.parse(raw);
      return {
        renderMarkdown: obj.renderMarkdown ?? true,
        leftSidebarWidth: obj.leftSidebarWidth ?? 220,
        rightSidebarWidth: obj.rightSidebarWidth ?? 280,
        leftTopHeight: obj.leftTopHeight ?? 260,
        showAgentsSidebar: obj.showAgentsSidebar ?? true,
      };
    }
  } catch {
    // Ignore localStorage or parse errors - fall through to defaults
  }
  return {
    renderMarkdown: true,
    leftSidebarWidth: 220,
    rightSidebarWidth: 280,
    leftTopHeight: 260,
    showAgentsSidebar: true,
  };
}

export const prefs = writable<Prefs>(load());

// persist on change
prefs.subscribe((p) => {
  try {
    localStorage.setItem(KEY, JSON.stringify(p));
  } catch {
    // Ignore localStorage errors - preferences won't persist but app continues
  }
});
