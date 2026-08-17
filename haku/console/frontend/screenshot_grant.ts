// Standing operator consent to let Haku's UI request a screenshot of the shell's own frame —
// "allow until withdrawn", same shape as geolocation_grant.ts, under its own key because each
// capability is its own standing grant (docs/containment.md's consent doctrine). Persisted on the
// SHELL origin, which the cross-origin iframe can neither read nor forge. The shell starts or
// resumes a `getDisplayMedia` capture only while this is set.
const GRANT_KEY = "haku.screenshot.grant.v1";
const GRANTED = "granted";

export function hasScreenshotGrant(): boolean {
  return localStorage.getItem(GRANT_KEY) === GRANTED;
}

export function setScreenshotGrant(granted: boolean): void {
  if (granted) localStorage.setItem(GRANT_KEY, GRANTED);
  else localStorage.removeItem(GRANT_KEY);
}
