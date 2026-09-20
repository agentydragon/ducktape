/**
 * OIDC authentication for the Airlock credential-broker SPA.
 *
 * Uses Authorization Code + PKCE flow via oidc-client-ts.
 * OIDC configuration (authority, client_id) is fetched from the backend's
 * /auth/config endpoint so nothing is hardcoded in the JS bundle.
 */
import { UserManager, WebStorageStateStore } from "oidc-client-ts";

const _unauthorizedRetryStartedAtKey = "airlock.auth.unauthorized-retry-started-at";
const _unauthorizedRetryCooldownMs = 60_000;

let _userManager: UserManager | null = null;
let _userManagerPromise: Promise<UserManager> | null = null;
let _loginRedirectPromise: Promise<void> | null = null;
let _unauthorizedRecoveryPromise: Promise<boolean> | null = null;
let _authenticationRedirectStarted = false;

async function getUserManager(): Promise<UserManager> {
  if (_userManager) return _userManager;

  _userManagerPromise ??= (async () => {
    const resp = await fetch("/auth/config");
    if (!resp.ok) throw new Error(`Failed to fetch /auth/config: ${resp.status}`);
    const config: {
      authority: string;
      client_id: string;
      redirect_uri: string;
    } = await resp.json();

    return new UserManager({
      authority: config.authority,
      client_id: config.client_id,
      redirect_uri: config.redirect_uri,
      response_type: "code",
      scope: "openid profile email",
      userStore: new WebStorageStateStore({ store: sessionStorage }),
      automaticSilentRenew: false,
    });
  })();

  try {
    _userManager = await _userManagerPromise;
    return _userManager;
  } catch (error) {
    _userManagerPromise = null;
    throw error;
  }
}

function hasRecentUnauthorizedRetry(): boolean {
  const now = Date.now();
  const retryStartedAt = Number(sessionStorage.getItem(_unauthorizedRetryStartedAtKey));
  if (
    !Number.isFinite(retryStartedAt) ||
    retryStartedAt <= 0 ||
    now < retryStartedAt ||
    now - retryStartedAt >= _unauthorizedRetryCooldownMs
  ) {
    sessionStorage.removeItem(_unauthorizedRetryStartedAtKey);
    return false;
  }
  return true;
}

function beginLoginRedirect(mgr: UserManager): Promise<void> {
  if (!_loginRedirectPromise) {
    _authenticationRedirectStarted = true;
    _loginRedirectPromise = Promise.resolve()
      .then(() => mgr.signinRedirect())
      .catch((error: unknown) => {
        _loginRedirectPromise = null;
        _authenticationRedirectStarted = false;
        throw error;
      });
  }
  return _loginRedirectPromise;
}

/** Get a valid access token, redirecting to login if needed. */
export async function getAccessToken(): Promise<string> {
  const mgr = await getUserManager();
  const user = await mgr.getUser();
  if (user && !user.expired) return user.access_token;
  if (hasRecentUnauthorizedRetry()) {
    throw new Error("Airlock rejected the last sign-in. Reload in about a minute to try again.");
  }
  await beginLoginRedirect(mgr);
  throw new Error("Redirecting to login");
}

/** Clear the rejected browser credential and start one login flow for concurrent 401s.
 *
 * Returns false after a recent retry already reached Airlock and was rejected again. The caller
 * should then surface that response instead of creating an authentication redirect loop.
 */
export function recoverAfterUnauthorized(): Promise<boolean> {
  if (_unauthorizedRecoveryPromise) return _unauthorizedRecoveryPromise;

  const recovery = (async () => {
    const mgr = await getUserManager();
    await mgr.removeUser();
    if (hasRecentUnauthorizedRetry()) return false;

    sessionStorage.setItem(_unauthorizedRetryStartedAtKey, String(Date.now()));
    try {
      await beginLoginRedirect(mgr);
      return true;
    } catch (error) {
      sessionStorage.removeItem(_unauthorizedRetryStartedAtKey);
      throw error;
    }
  })();

  _unauthorizedRecoveryPromise = recovery.catch((error: unknown) => {
    _unauthorizedRecoveryPromise = null;
    throw error;
  });
  return _unauthorizedRecoveryPromise;
}

/** Clear the one-retry guard after Airlock accepts the current credential. */
export function markAuthenticationAccepted(): void {
  sessionStorage.removeItem(_unauthorizedRetryStartedAtKey);
  _unauthorizedRecoveryPromise = null;
  _loginRedirectPromise = null;
  _authenticationRedirectStarted = false;
}

/** Let UI error boundaries stay quiet while the browser is navigating to Authentik. */
export function isAuthenticationRedirectStarted(): boolean {
  return _authenticationRedirectStarted;
}

/** Complete the OIDC callback after Authentik redirects back. */
export async function handleAuthCallback(): Promise<void> {
  const mgr = await getUserManager();
  try {
    await mgr.signinRedirectCallback();
  } catch (error) {
    await mgr.removeUser();
    throw error;
  }
  window.history.replaceState({}, "", "/");
}

/** Check if the current URL is an OIDC callback. */
export function isAuthCallback(): boolean {
  const params = new URLSearchParams(window.location.search);
  return params.has("code") && params.has("state");
}
