/**
 * Sending the browser to the app's own login when the API says it has no session.
 *
 * The deep link survives in sessionStorage rather than a `return_to` parameter: the router keeps
 * the route in the fragment, which a redirect drops and which the server never sees anyway, and
 * stashing it client-side means there is no server-side redirect target to validate and therefore
 * no open redirect to get wrong.
 */

const STASH = "agentplane.after-login";

type LoginLocation = Pick<Location, "pathname" | "hash" | "replace">;

let redirectStarted = false;

/**
 * Start at most one login for this document, however many requests fail at once. Reports whether a
 * login navigation is in flight for this document.
 */
export function redirectToLogin(location: LoginLocation = window.location): boolean {
  if (location.pathname.startsWith("/auth/")) return false;
  if (!redirectStarted) {
    redirectStarted = true;
    if (location.hash) sessionStorage.setItem(STASH, location.hash);
    location.replace("/auth/login");
  }
  return true;
}

/** Put the browser back on the route it was reading before it was sent to log in. */
export function restoreRouteAfterLogin(location: Pick<Location, "hash"> = window.location): void {
  const stashed = sessionStorage.getItem(STASH);
  sessionStorage.removeItem(STASH);
  if (stashed && !location.hash) location.hash = stashed;
}
