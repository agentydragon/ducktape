import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({
  getAccessToken: vi.fn(),
  markAuthenticationAccepted: vi.fn(),
  recoverAfterUnauthorized: vi.fn(),
}));

vi.mock("./auth", () => auth);

beforeEach(() => {
  vi.resetModules();
  auth.getAccessToken.mockReset().mockResolvedValue("cached-token");
  auth.markAuthenticationAccepted.mockReset();
  auth.recoverAfterUnauthorized.mockReset();
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => vi.unstubAllGlobals());

describe("Airlock API authentication", () => {
  it("uses the OIDC access token and clears the retry guard after an accepted response", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ image_tag: null }), { status: 200 }));
    const { AirlockApiClient } = await import("./api");

    await new AirlockApiClient().getDeploymentInfo();

    expect(fetch).toHaveBeenCalledWith("/api/info", { headers: { Authorization: "Bearer cached-token" } });
    expect(auth.markAuthenticationAccepted).toHaveBeenCalledOnce();
  });

  it("starts reauthentication and does not surface the triggering 401 to the page", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid or expired token" }), { status: 401 })
    );
    auth.recoverAfterUnauthorized.mockResolvedValue(true);
    const { AirlockApiClient } = await import("./api");

    await expect(new AirlockApiClient().getDeploymentInfo()).rejects.toThrow("Redirecting to login");
    expect(auth.recoverAfterUnauthorized).toHaveBeenCalledOnce();
    expect(auth.markAuthenticationAccepted).not.toHaveBeenCalled();
  });

  it("shows a second 401 after the retry guard prevents a redirect loop", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid or expired token" }), { status: 401 })
    );
    auth.recoverAfterUnauthorized.mockResolvedValue(false);
    const { AirlockApiClient } = await import("./api");

    await expect(new AirlockApiClient().getDeploymentInfo()).rejects.toThrow(
      'API error 401: {"detail":"Invalid or expired token"}'
    );
    expect(auth.recoverAfterUnauthorized).toHaveBeenCalledOnce();
  });
});
