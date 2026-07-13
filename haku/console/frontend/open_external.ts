// Open an external link in a new tab with the opener severed. Shared by the embed shell
// (openLink bridge action) and the Settings panel (MCP OAuth popup).
//
// `noopener`/`noreferrer` force window.open() to return null even when the tab opened, so
// open a same-origin blank tab first. The handle is the only reliable popup-block signal;
// once it exists, sever opener before navigating it.
export function openExternal(url: string): boolean {
  const parsed = new URL(url);
  const opened = window.open(parsed.protocol === "mailto:" ? url : "about:blank", "_blank");
  if (!opened) return false;
  opened.opener = null;
  if (parsed.protocol !== "mailto:") opened.location.replace(url);
  return true;
}

export const POPUP_HINT = "Allow pop-ups for this site so the console can open links.";
