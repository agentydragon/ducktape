import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  manager: {
    getUser: vi.fn(),
    removeUser: vi.fn(),
    signinRedirect: vi.fn(),
    signinRedirectCallback: vi.fn(),
  },
  constructManager: vi.fn(),
  constructStore: vi.fn(),
}));

vi.mock("oidc-client-ts", () => ({
  UserManager: vi.fn((settings: unknown) => {
    mocks.constructManager(settings);
    return mocks.manager;
  }),
  WebStorageStateStore: vi.fn((options: unknown) => {
    mocks.constructStore(options);
    return {};
  }),
}));

beforeEach(() => {
  vi.resetModules();
  sessionStorage.clear();
  mocks.manager.getUser.mockReset();
  mocks.manager.removeUser.mockReset().mockResolvedValue(undefined);
  mocks.manager.signinRedirect.mockReset().mockResolvedValue(undefined);
  mocks.manager.signinRedirectCallback.mockReset();
  mocks.constructManager.mockClear();
  mocks.constructStore.mockClear();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            authority: "https://auth.example.test/application/o/airlock/",
            client_id: "airlock-test",
            redirect_uri: "https://airlock.example.test/auth/callback",
          }),
          { headers: { "Content-Type": "application/json" } }
        )
    )
  );
});

afterEach(() => vi.unstubAllGlobals());

describe("Airlock OIDC credentials", () => {
  it("shares manager initialization across concurrent API calls", async () => {
    mocks.manager.getUser.mockResolvedValue({ access_token: "valid-token", expired: false });
    const { getAccessToken } = await import("./auth");

    await expect(Promise.all([getAccessToken(), getAccessToken()])).resolves.toEqual(["valid-token", "valid-token"]);

    expect(fetch).toHaveBeenCalledOnce();
    expect(mocks.constructManager).toHaveBeenCalledOnce();
  });

  it("clears a rejected user and starts only one login for concurrent 401s", async () => {
    const { isAuthenticationRedirectStarted, recoverAfterUnauthorized } = await import("./auth");

    await expect(Promise.all([recoverAfterUnauthorized(), recoverAfterUnauthorized()])).resolves.toEqual([true, true]);

    expect(mocks.manager.removeUser).toHaveBeenCalledOnce();
    expect(mocks.manager.signinRedirect).toHaveBeenCalledOnce();
    expect(isAuthenticationRedirectStarted()).toBe(true);
  });

  it("does not loop when Airlock rejects the token after the re-login", async () => {
    const firstPage = await import("./auth");
    await expect(firstPage.recoverAfterUnauthorized()).resolves.toBe(true);

    vi.resetModules();
    const reloadedPage = await import("./auth");
    await expect(reloadedPage.recoverAfterUnauthorized()).resolves.toBe(false);

    expect(mocks.manager.removeUser).toHaveBeenCalledTimes(2);
    expect(mocks.manager.signinRedirect).toHaveBeenCalledOnce();
  });

  it("clears the retry guard after Airlock accepts a credential", async () => {
    const firstPage = await import("./auth");
    await firstPage.recoverAfterUnauthorized();
    firstPage.markAuthenticationAccepted();

    vi.resetModules();
    const nextPage = await import("./auth");
    await expect(nextPage.recoverAfterUnauthorized()).resolves.toBe(true);

    expect(mocks.manager.signinRedirect).toHaveBeenCalledTimes(2);
  });

  it("removes a stale user when OIDC callback processing fails", async () => {
    mocks.manager.signinRedirectCallback.mockRejectedValue(new Error("authorization code already used"));
    const { handleAuthCallback } = await import("./auth");

    await expect(handleAuthCallback()).rejects.toThrow("authorization code already used");
    expect(mocks.manager.removeUser).toHaveBeenCalledOnce();
  });
});
