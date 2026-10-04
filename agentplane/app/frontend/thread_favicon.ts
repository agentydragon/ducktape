import type { ThreadTabStatus } from "./tab_metadata";
import { THREAD_STATUS_COLORS, THREAD_STATUS_RUN_CYCLE_MS } from "./thread_status_palette";

const FAVICON_ID = "agentplane-favicon";
const FAVICON_PATH = "/favicon.svg";
const FRAME_INTERVAL_MS = 100;

// Chevrons are 5 wide and one every CHEVRON_PITCH, in the 32x32 viewBox, seen through a window two
// pitches wide (see statusChevrons). The row is long enough that shifting it right by one pitch looks
// the same as not shifting it, so the animation can loop.
const CHEVRON_PITCH = 9;
const CHEVRON_X = [6.5, 15.5, 24.5, 33.5];

export interface ThreadFaviconPulseEpoch {
  current: number | null;
}

// Keep the paper-plane mark legible on both light and dark tab bars without a solid tile.
// A dark outer ring and light inner ring keep the dot distinct from the transparent plane
// on both light and dark browser tab backgrounds. The chevrons below use the dark outline alone: at
// 16px a second ring merges neighbouring chevrons into one blob.
function statusDot(color: string): string {
  return `<circle cx="24.5" cy="24.5" r="5.1" fill="${color}" stroke="#102a43" stroke-width="3"/><circle cx="24.5" cy="24.5" r="5.1" fill="none" stroke="#fff" stroke-width="1.5"/>`;
}

/**
 * `phase` is how far through its cycle the chevrons are, in [0, 1). They travel the way they point.
 * At phase 0 the clip window shows exactly two chevrons, caps and outline included.
 */
function statusChevrons(color: string, phase: number): string {
  const shift = phase * CHEVRON_PITCH;
  const at = (x: number): string => (x + shift).toFixed(2);
  const path = CHEVRON_X.map((x) => `M${at(x)} 19.5L${at(x + 5)} 24.5L${at(x)} 29.5`).join("");
  const stroke = (paint: string, width: number): string =>
    `<path d="${path}" stroke="${paint}" stroke-width="${width}"/>`;
  return `<clipPath id="corner"><path d="M13.5 16H31.5V32H13.5Z"/></clipPath><g clip-path="url(#corner)" fill="none" stroke-linecap="round" stroke-linejoin="round">${stroke("#102a43", 4)}${stroke(color, 2.6)}</g>`;
}

function faviconUrl(status: ThreadTabStatus, phase: number): string {
  const color = THREAD_STATUS_COLORS[status.kind];
  const mark = status.kind === "running" ? statusChevrons(color, phase) : statusDot(color);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><path d="M5 14.5 27 5 18 27l-3.7-9.1L5 14.5Z" fill="none" stroke="#1c7ed6" stroke-linejoin="round" stroke-width="2.5"/><path d="m14.3 17.9 6.3-6.1" fill="none" stroke="#1c7ed6" stroke-linecap="round" stroke-width="2"/>${mark}</svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/** Set the thread's status in the favicon. A running turn moves its chevrons in step with the in-page indicator. */
export function installThreadFavicon(status: ThreadTabStatus, pulseEpoch: ThreadFaviconPulseEpoch): () => void {
  const icon = document.getElementById(FAVICON_ID);
  if (!(icon instanceof HTMLLinkElement)) return () => {};

  const running = status.kind === "running";
  const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
  let timer: number | undefined;

  const update = (): void => {
    const startedAt = pulseEpoch.current ?? Date.now();
    const phase =
      running && !motionPreference.matches
        ? ((Date.now() - startedAt) % THREAD_STATUS_RUN_CYCLE_MS) / THREAD_STATUS_RUN_CYCLE_MS
        : 0;
    icon.href = faviconUrl(status, phase);
  };

  const syncAnimation = (event?: MediaQueryListEvent): void => {
    // The CSS animation is removed for reduced motion. Re-enabling motion starts that
    // animation from its first frame, so restart the favicon's shared epoch at the same time.
    if (event && !event.matches && running) pulseEpoch.current = Date.now();
    if (running && !motionPreference.matches && timer === undefined) {
      timer = window.setInterval(update, FRAME_INTERVAL_MS);
    } else if ((!running || motionPreference.matches) && timer !== undefined) {
      window.clearInterval(timer);
      timer = undefined;
    }
    update();
  };

  motionPreference.addEventListener("change", syncAnimation);
  syncAnimation();

  return () => {
    motionPreference.removeEventListener("change", syncAnimation);
    if (timer !== undefined) window.clearInterval(timer);
    icon.setAttribute("href", FAVICON_PATH);
  };
}
