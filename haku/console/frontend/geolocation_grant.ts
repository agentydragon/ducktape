// Standing operator consent to share location with Haku's UI — "allow until withdrawn".
// Persisted on the SHELL origin (haku.allegedly.works): the cross-origin iframe cannot
// read or forge it (cross-origin isolation, per docs/containment.md invariant #1), so it
// is a trusted, agent-untouchable gate. Set once via the top-layer consent confirm; the
// shell reads geolocation ONLY while this is set, and clears it when the operator
// withdraws — so withdrawing here actually stops reads even if the browser still remembers
// its own geolocation permission grant.
//
// localStorage is used directly (no defensive try/catch): the shell is a top-level,
// same-origin trusted document where Storage is available, matching how the rest of the
// shell uses browser APIs (history/window.open) without guards.
const GRANT_KEY = "haku.geolocation.grant.v1";
const GRANTED = "granted";

export function hasGeolocationGrant(): boolean {
  return localStorage.getItem(GRANT_KEY) === GRANTED;
}

export function setGeolocationGrant(granted: boolean): void {
  if (granted) localStorage.setItem(GRANT_KEY, GRANTED);
  else localStorage.removeItem(GRANT_KEY);
}
