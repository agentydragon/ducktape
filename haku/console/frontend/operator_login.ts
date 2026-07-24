type LoginLocation = Pick<Location, "pathname" | "search" | "replace">;

let redirectStarted = false;

/** Start at most one operator login flow for this document, even when requests fail concurrently.
 *
 * The current console page rides along as the continuation, so re-authenticating returns the tab
 * to the view it was on rather than dropping it at the root. The backend validates it as a local
 * console path (`operator_auth._validated_return_to`). */
export function redirectToOperatorLogin(location: LoginLocation = window.location): void {
  if (redirectStarted || location.pathname.startsWith("/auth/")) return;
  redirectStarted = true;
  const returnTo = encodeURIComponent(`${location.pathname}${location.search}`);
  location.replace(`/auth/login?return_to=${returnTo}`);
}
