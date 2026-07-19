type LoginLocation = Pick<Location, "pathname" | "replace">;

let redirectStarted = false;

/** Start at most one operator login flow for this document, even when requests fail concurrently. */
export function redirectToOperatorLogin(location: LoginLocation = window.location): void {
  if (redirectStarted || location.pathname.startsWith("/auth/")) return;
  redirectStarted = true;
  location.replace("/auth/login");
}
