// Public Forgejo web URLs for the browser-facing links the operator clicks
// (distinct from the internal git URL the backend uses for git operations).
export const FORGEJO = "https://git.allegedly.works/haku/haku-state";
export const INTAKE_NEW = `${FORGEJO}/_new/main/intake/`;
export const ITEM_SRC = `${FORGEJO}/src/branch/main/items`;
export const CLAUDE_NEW = "https://claude.ai/new?q=";
export const MAX_DEEPLINK = 2000; // fall back to the item file when the encoded prompt exceeds this
export const UP_NEXT = 7;
