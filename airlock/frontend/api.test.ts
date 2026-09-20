import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({
  isAuthenticationFailurePage: vi.fn(),
  redirectToLogin: vi.fn(),
}));

vi.mock("./auth", () => auth);

beforeEach(() => {
  vi.resetModules();
  auth.isAuthenticationFailurePage.mockReset().mockReturnValue(false);
  auth.redirectToLogin.mockReset();
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => vi.unstubAllGlobals());

describe("Airlock API authentication", () => {
  it("uses the same-origin session cookie without exposing a bearer token", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ image_tag: null }), { status: 200 }));
    const { AirlockApiClient } = await import("./api");

    await new AirlockApiClient().getDeploymentInfo();

    expect(fetch).toHaveBeenCalledWith("/api/info", { credentials: "same-origin" });
  });

  it("starts one backend login redirect when the session is missing", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401 }));
    const { AirlockApiClient } = await import("./api");

    await expect(new AirlockApiClient().getDeploymentInfo()).rejects.toThrow("Redirecting to login");
    expect(auth.redirectToLogin).toHaveBeenCalledOnce();
  });

  it("does not immediately retry a sign-in rejected by the backend", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401 }));
    auth.isAuthenticationFailurePage.mockReturnValue(true);
    const { AirlockApiClient } = await import("./api");

    await expect(new AirlockApiClient().getDeploymentInfo()).rejects.toThrow("Sign-in failed. Please try again.");
    expect(auth.redirectToLogin).not.toHaveBeenCalled();
  });
});
