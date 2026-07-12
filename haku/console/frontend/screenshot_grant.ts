// Standing operator consent to let Haku's UI request a screenshot of the shell's own frame —
// "allow until withdrawn", same shape as geolocation_grant.ts. Persisted on the SHELL origin:
// the cross-origin iframe cannot read or forge it. Set on the first approved request; the
// shell will only start (or resume) a `getDisplayMedia` capture while this is set, and
// withdrawing clears it so nothing captures again until re-granted.
//
// Deliberately a separate key from geolocation's, not reused: each capability is its own
// standing grant per docs/containment.md's consent doctrine.
const GRANT_KEY = "haku.screenshot.grant.v1";
const GRANTED = "granted";

export function hasScreenshotGrant(): boolean {
  return localStorage.getItem(GRANT_KEY) === GRANTED;
}

export function setScreenshotGrant(granted: boolean): void {
  if (granted) localStorage.setItem(GRANT_KEY, GRANTED);
  else localStorage.removeItem(GRANT_KEY);
}
