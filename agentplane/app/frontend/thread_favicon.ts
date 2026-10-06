import type { ThreadTabStatus } from "./tab_metadata";
import { THREAD_STATUS_MARKS, type ThreadStatusMark } from "./status_mark";

const FAVICON_ID = "agentplane-favicon";
const FAVICON_PATH = "/favicon.svg";

// Two chevrons, 5 wide and 9 apart, in the 32x32 viewBox, where the dot would sit.
const CHEVRON_X = [15.5, 24.5];

// The dark rim every mark carries, so it reads on both light and dark tab bars.
const OUTLINE = "#102a43";

// A triangle with an exclamation mark cut into it, in the same corner: the warning sign.
const ALERT_PATH = "M24.5 19.4L30 28.8H19Z";

// A ring open at the top, 100 degrees wide, with a stem from above the gap to the centre: the power
// symbol, in the same corner. The gap stays wide because at 16px a narrower one lets the stem's
// outline merge into the ring's ends.
const POWER_PATH = "M28.5 21.6A5.4 5.4 0 1 1 20.5 21.6M24.5 19.3V24.4";

// A lidded box with a handle slot, in the same corner: the archive box.
const ARCHIVE_PATH = "M19.5 20H29.5V22.8H19.5ZM20.4 22.8V29.4H28.6V22.8M23 25.6H26";

// Keep the paper-plane mark legible on both light and dark tab bars without a solid tile.
// A dark outer ring and light inner ring keep the dot distinct from the transparent plane
// on both light and dark browser tab backgrounds. The chevrons, the power symbol, the archive box and the
// warning triangle use the dark outline alone: at 16px a second ring merges neighbouring chevrons into one
// blob and fills the power ring's hole.
function statusDot(color: string): string {
  return `<circle cx="24.5" cy="24.5" r="5.1" fill="${color}" stroke="${OUTLINE}" stroke-width="3"/><circle cx="24.5" cy="24.5" r="5.1" fill="none" stroke="#fff" stroke-width="1.5"/>`;
}

function outlinedStroke(path: string, color: string, outlineWidth: number, width: number): string {
  const stroke = (paint: string, strokeWidth: number): string =>
    `<path d="${path}" stroke="${paint}" stroke-width="${strokeWidth}"/>`;
  return `<g fill="none" stroke-linecap="round" stroke-linejoin="round">${stroke(OUTLINE, outlineWidth)}${stroke(color, width)}</g>`;
}

function statusChevrons(color: string): string {
  return outlinedStroke(CHEVRON_X.map((x) => `M${x} 19.5L${x + 5} 24.5L${x} 29.5`).join(""), color, 4, 2.6);
}

// The rim is drawn first and the colour over it, so the rim sits outside the colour and the "!" keeps its
// room at 16px.
function statusAlert(color: string): string {
  return (
    `<path d="${ALERT_PATH}" fill="${OUTLINE}" stroke="${OUTLINE}" stroke-width="3.6" stroke-linejoin="round"/>` +
    `<path d="${ALERT_PATH}" fill="${color}" stroke="${color}" stroke-width="1.6" stroke-linejoin="round"/>` +
    `<path d="M24.5 22.5V25.1" fill="none" stroke="${OUTLINE}" stroke-width="1.9" stroke-linecap="round"/>` +
    `<circle cx="24.5" cy="27.1" r="1" fill="${OUTLINE}"/>`
  );
}

function statusPower(color: string): string {
  return outlinedStroke(POWER_PATH, color, 3.6, 2.2);
}

function statusArchive(color: string): string {
  return outlinedStroke(ARCHIVE_PATH, color, 3.6, 2);
}

function statusGlyph(mark: ThreadStatusMark): string {
  switch (mark.favicon) {
    case "dot":
      return statusDot(mark.color);
    case "chevrons":
      return statusChevrons(mark.color);
    case "alert":
      return statusAlert(mark.color);
    case "power":
      return statusPower(mark.color);
    case "archive":
      return statusArchive(mark.color);
  }
}

function faviconUrl(mark: ThreadStatusMark): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><path d="M5 14.5 27 5 18 27l-3.7-9.1L5 14.5Z" fill="none" stroke="#1c7ed6" stroke-linejoin="round" stroke-width="2.5"/><path d="m14.3 17.9 6.3-6.1" fill="none" stroke="#1c7ed6" stroke-linecap="round" stroke-width="2"/>${statusGlyph(mark)}</svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/**
 * Set the thread's status in the favicon, drawn from the same mark as the in-page indicator. It stays
 * still: a favicon is only seen in a background tab, where the browser throttles timers.
 */
export function installThreadFavicon(kind: ThreadTabStatus["kind"]): () => void {
  const icon = document.getElementById(FAVICON_ID);
  if (!(icon instanceof HTMLLinkElement)) return () => {};
  icon.href = faviconUrl(THREAD_STATUS_MARKS[kind]);
  return () => icon.setAttribute("href", FAVICON_PATH);
}
