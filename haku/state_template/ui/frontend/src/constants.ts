// Public Forgejo web URLs for the browser-facing source links the operator clicks
// (distinct from the internal git URL the backend uses for git operations).
export const FORGEJO = "https://git.allegedly.works/haku/haku-state";
export const ITEM_SRC = `${FORGEJO}/src/branch/main/items`;
export const CLAUDE_NEW = "https://claude.ai/new?q=";

// Fall back to the item source file when the encoded prompt exceeds this length.
export const MAX_DEEPLINK = 2000;

// Top N value-ranked open items shown in "Up next"; the rest go to the backlog.
export const UP_NEXT = 7;

// The trusted console shell that frames this UI. The openLink bridge only posts to —
// and accepts results from — this exact origin. See ./bridge.ts.
export const SHELL_ORIGIN = "https://haku.allegedly.works";
