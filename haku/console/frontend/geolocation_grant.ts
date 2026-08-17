// Standing operator consent to share location with Haku's UI — "allow until withdrawn". Persisted
// on the SHELL origin, which the cross-origin iframe can neither read nor forge (docs/containment.md
// invariant #1), so it is an agent-untouchable gate. The shell reads geolocation ONLY while this is
// set, so withdrawing stops reads even if the browser still remembers its own permission grant.
const GRANT_KEY = "haku.geolocation.grant.v1";
const GRANTED = "granted";

export function hasGeolocationGrant(): boolean {
  return localStorage.getItem(GRANT_KEY) === GRANTED;
}

export function setGeolocationGrant(granted: boolean): void {
  if (granted) localStorage.setItem(GRANT_KEY, GRANTED);
  else localStorage.removeItem(GRANT_KEY);
}
