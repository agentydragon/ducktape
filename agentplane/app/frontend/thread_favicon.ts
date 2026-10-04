import type { ThreadTabStatus } from "./tab_metadata";
import { THREAD_STATUS_BLINK_MS, THREAD_STATUS_COLORS } from "./thread_status_palette";

const FAVICON_ID = "agentplane-favicon";
const FAVICON_PATH = "/favicon.svg";
const FRAME_INTERVAL_MS = 100;

export interface ThreadFaviconPulseEpoch {
  current: number | null;
}

function faviconUrl(status: ThreadTabStatus, showDot: boolean): string {
  // Keep the paper-plane mark legible on both light and dark tab bars without a solid tile.
  // A dark outer ring and light inner ring keep the dot distinct from the transparent mark
  // on both light and dark browser tab backgrounds.
  const dot = showDot
    ? `<circle cx="24.5" cy="24.5" r="5.1" fill="${THREAD_STATUS_COLORS[status.kind]}" stroke="#102a43" stroke-width="3"/><circle cx="24.5" cy="24.5" r="5.1" fill="none" stroke="#fff" stroke-width="1.5"/>`
    : "";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><path d="M5 14.5 27 5 18 27l-3.7-9.1L5 14.5Z" fill="none" stroke="#1c7ed6" stroke-linejoin="round" stroke-width="2.5"/><path d="m14.3 17.9 6.3-6.1" fill="none" stroke="#1c7ed6" stroke-linecap="round" stroke-width="2"/>${dot}</svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/** Set the thread's status in the favicon. A running turn blinks the dot on and off in step with the in-page dot. */
export function installThreadFavicon(status: ThreadTabStatus, pulseEpoch: ThreadFaviconPulseEpoch): () => void {
  const icon = document.getElementById(FAVICON_ID);
  if (!(icon instanceof HTMLLinkElement)) return () => {};

  const blinks = status.kind === "running";
  const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
  let timer: number | undefined;

  const update = (): void => {
    const startedAt = pulseEpoch.current ?? Date.now();
    const showDot =
      !blinks ||
      motionPreference.matches ||
      (Date.now() - startedAt) % THREAD_STATUS_BLINK_MS < THREAD_STATUS_BLINK_MS / 2;
    icon.href = faviconUrl(status, showDot);
  };

  const syncAnimation = (event?: MediaQueryListEvent): void => {
    // The CSS dot's animation is removed for reduced motion. Re-enabling motion starts that
    // animation from its first frame, so restart the favicon's shared epoch at the same time.
    if (event && !event.matches && blinks) pulseEpoch.current = Date.now();
    if (blinks && !motionPreference.matches && timer === undefined) {
      timer = window.setInterval(update, FRAME_INTERVAL_MS);
    } else if ((!blinks || motionPreference.matches) && timer !== undefined) {
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
