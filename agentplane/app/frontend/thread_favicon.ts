import type { ThreadTabStatus } from "./tab_metadata";

const FAVICON_ID = "agentplane-favicon";
const FAVICON_PATH = "/favicon.svg";
const PULSE_PERIOD_MS = 1_200;
const FRAME_INTERVAL_MS = 100;

export interface ThreadFaviconPulseEpoch {
  current: number | null;
}

const FALLBACK_COLORS = {
  green: "#40c057",
  yellow: "#fab005",
  red: "#fa5252",
  gray: "#868e96",
} as const;

function faviconUrl(status: ThreadTabStatus, level: number): string {
  const color =
    getComputedStyle(document.documentElement).getPropertyValue(`--mantine-color-${status.color}-6`).trim() ||
    FALLBACK_COLORS[status.color];
  const radius = 2.8 + 1.2 * level;
  const opacity = 0.35 + 0.65 * level;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect x="1" y="1" width="30" height="30" rx="8" fill="#1c7ed6"/><path d="M5 14.5 27 5 18 27l-3.7-9.1L5 14.5Z" fill="#fff"/><path d="m14.3 17.9 6.3-6.1" fill="none" stroke="#1c7ed6" stroke-linecap="round" stroke-width="1.5"/><circle cx="25" cy="24.5" r="${radius.toFixed(2)}" fill="${color}" fill-opacity="${opacity.toFixed(2)}" stroke="#fff" stroke-width="1.5"/></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/** Set the thread's status in the favicon. A pulsing status follows the in-page dot's 1.2s breath. */
export function installThreadFavicon(status: ThreadTabStatus, pulseEpoch: ThreadFaviconPulseEpoch): () => void {
  const icon = document.getElementById(FAVICON_ID);
  if (!(icon instanceof HTMLLinkElement)) return () => {};

  const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
  let timer: number | undefined;

  const update = (): void => {
    const startedAt = pulseEpoch.current ?? Date.now();
    const level =
      status.pulse && !motionPreference.matches
        ? (1 - Math.cos((2 * Math.PI * ((Date.now() - startedAt) % PULSE_PERIOD_MS)) / PULSE_PERIOD_MS)) / 2
        : 1;
    icon.href = faviconUrl(status, level);
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
