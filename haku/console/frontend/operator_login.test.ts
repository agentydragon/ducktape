import { describe, expect, it, vi } from "vitest";

import { operatorLoginRedirectStarted, redirectToOperatorLogin } from "./operator_login";

describe("redirectToOperatorLogin", () => {
  it("carries the current console page and query as the continuation", async () => {
    // A fresh module registry: the one-flow-per-document latch below is module state, so this
    // case has to run against an untouched copy.
    vi.resetModules();
    const { redirectToOperatorLogin: redirect } = await import("./operator_login");
    const replace = vi.fn();

    redirect({ pathname: "/_console/tool-calls", search: "?show=all", replace });

    expect(replace).toHaveBeenCalledWith("/auth/login?return_to=%2F_console%2Ftool-calls%3Fshow%3Dall");
  });

  it("reports that the document is leaving, so surfaces can hold their error state", async () => {
    vi.resetModules();
    const { redirectToOperatorLogin: redirect, operatorLoginRedirectStarted: started } =
      await import("./operator_login");

    expect(started()).toBe(false);
    redirect({ pathname: "/", search: "", replace: vi.fn() });
    expect(started()).toBe(true);
  });

  it("stays quiet on the login pages themselves, so nothing suppresses their own errors", async () => {
    vi.resetModules();
    const { redirectToOperatorLogin: redirect, operatorLoginRedirectStarted: started } =
      await import("./operator_login");
    const replace = vi.fn();

    redirect({ pathname: "/auth/callback", search: "?state=abc", replace });

    expect(replace).not.toHaveBeenCalled();
    expect(started()).toBe(false);
  });

  it("starts only one login flow when requests fail concurrently", () => {
    const replace = vi.fn();
    const location = { pathname: "/", search: "", replace };

    redirectToOperatorLogin(location);
    redirectToOperatorLogin(location);

    expect(replace).toHaveBeenCalledOnce();
    expect(replace).toHaveBeenCalledWith("/auth/login?return_to=%2F");
    expect(operatorLoginRedirectStarted()).toBe(true);
  });
});
