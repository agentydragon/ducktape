/**
 * OIDC authentication for the approval gate operator SPA.
 *
 * Uses Authorization Code + PKCE flow via oidc-client-ts.
 * OIDC configuration (authority, client_id) is fetched from the backend's
 * /auth/config endpoint so nothing is hardcoded in the JS bundle.
 */
import { UserManager, WebStorageStateStore } from "oidc-client-ts";

let _userManager: UserManager | null = null;

async function getUserManager(): Promise<UserManager> {
  if (_userManager) return _userManager;

  const resp = await fetch("/auth/config");
  if (!resp.ok) throw new Error(`Failed to fetch /auth/config: ${resp.status}`);
  const config: {
    authority: string;
    client_id: string;
    redirect_uri: string;
  } = await resp.json();

  _userManager = new UserManager({
    authority: config.authority,
    client_id: config.client_id,
    redirect_uri: config.redirect_uri,
    response_type: "code",
    scope: "openid decide read",
    userStore: new WebStorageStateStore({ store: sessionStorage }),
    automaticSilentRenew: false,
  });
  return _userManager;
}

/** Get a valid access token, redirecting to login if needed. */
export async function getAccessToken(): Promise<string> {
  const mgr = await getUserManager();
  const user = await mgr.getUser();
  if (user && !user.expired) return user.access_token;
  await mgr.signinRedirect();
  throw new Error("Redirecting to login");
}

/** Complete the OIDC callback after Authentik redirects back. */
export async function handleAuthCallback(): Promise<void> {
  const mgr = await getUserManager();
  await mgr.signinRedirectCallback();
  window.history.replaceState({}, "", "/");
}

/** Check if the current URL is an OIDC callback. */
export function isAuthCallback(): boolean {
  const params = new URLSearchParams(window.location.search);
  return params.has("code") && params.has("state");
}
