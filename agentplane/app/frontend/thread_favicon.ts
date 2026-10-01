import type { ThreadTabStatus } from "./tab_metadata";

const FAVICON_ID = "agentplane-favicon";
const FAVICON_PATH = "/favicon.svg";
const PULSE_PERIOD_MS = 1_200;
const FRAME_INTERVAL_MS = 100;

export interface ThreadFaviconPulseEpoch {
  current: number | null;
}

const FALLBACK_COLORS = {
  green: "#2f9e44",
  yellow: "#f59f00",
  red: "#e03131",
  gray: "#495057",
} as const;

function faviconUrl(status: ThreadTabStatus, showDot: boolean): string {
  const color =
    getComputedStyle(document.documentElement).getPropertyValue(`--mantine-color-${status.color}-7`).trim() ||
    FALLBACK_COLORS[status.color];
  // Keep the paper-plane mark legible on both light and dark tab bars without a solid tile.
  // A dark outer ring and light inner ring keep the dot distinct from the transparent mark
  // on both light and dark browser tab backgrounds.
  const dot = showDot
    ? `<circle cx="24.5" cy="24.5" r="5.1" fill="${color}" stroke="#102a43" stroke-width="3"/><circle cx="24.5" cy="24.5" r="5.1" fill="none" stroke="#fff" stroke-width="1.5"/>`
    : "";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><path d="M5 14.5 27 5 18 27l-3.7-9.1L5 14.5Z" fill="none" stroke="#1c7ed6" stroke-linejoin="round" stroke-width="2.5"/><path d="m14.3 17.9 6.3-6.1" fill="none" stroke="#1c7ed6" stroke-linecap="round" stroke-width="2"/>${dot}</svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/** Set the thread's status in the favicon. An active turn blinks on and off every 600 ms. */
export function installThreadFavicon(status: ThreadTabStatus, pulseEpoch: ThreadFaviconPulseEpoch): () => void {
  const icon = document.getElementById(FAVICON_ID);
  if (!(icon instanceof HTMLLinkElement)) return () => {};

  const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
  let timer: number | undefined;

  const update = (): void => {
    const startedAt = pulseEpoch.current ?? Date.now();
    const showDot =
      !status.pulse || motionPreference.matches || (Date.now() - startedAt) % PULSE_PERIOD_MS < PULSE_PERIOD_MS / 2;
    icon.href = faviconUrl(status, showDot);
  };

  const syncAnimation = (event?: MediaQueryListEvent): void => {
    // The CSS dot's animation is removed for reduced motion. Re-enabling motion starts that
    // animation from its first frame, so restart the favicon's shared epoch at the same time.
    if (event && !event.matches && status.pulse) pulseEpoch.current = Date.now();
    if (status.pulse && !motionPreference.matches && timer === undefined) {
      timer = window.setInterval(update, FRAME_INTERVAL_MS);
    } else if ((!status.pulse || motionPreference.matches) && timer !== undefined) {
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
