/** Same-origin session authentication for the Airlock browser UI. */

let _authenticationRedirectStarted = false;

export function isAuthenticationFailurePage(): boolean {
  return new URLSearchParams(window.location.search).get("auth") === "failed";
}

/** Start at most one top-level login navigation for concurrent API 401s. */
export function redirectToLogin(): void {
  if (_authenticationRedirectStarted || isAuthenticationFailurePage()) return;
  _authenticationRedirectStarted = true;
  window.location.assign("/auth/login");
}

/** Let UI error boundaries stay quiet while the browser is navigating to Authentik. */
export function isAuthenticationRedirectStarted(): boolean {
  return _authenticationRedirectStarted;
}
