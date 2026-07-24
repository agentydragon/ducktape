import { describe, expect, it, vi } from "vitest";

import { redirectToOperatorLogin } from "./operator_login.ts";

describe("redirectToOperatorLogin", () => {
  it("carries the current console page and query as the continuation", async () => {
    // A fresh module registry: the one-flow-per-document latch below is module state, so this
    // case has to run against an untouched copy.
    vi.resetModules();
    const { redirectToOperatorLogin: redirect } = await import("./operator_login.ts");
    const replace = vi.fn();

    redirect({ pathname: "/_console/tool-calls", search: "?show=all", replace });

    expect(replace).toHaveBeenCalledWith("/auth/login?return_to=%2F_console%2Ftool-calls%3Fshow%3Dall");
  });

  it("starts only one login flow when requests fail concurrently", () => {
    const replace = vi.fn();
    const location = { pathname: "/", search: "", replace };

    redirectToOperatorLogin(location);
    redirectToOperatorLogin(location);

    expect(replace).toHaveBeenCalledOnce();
    expect(replace).toHaveBeenCalledWith("/auth/login?return_to=%2F");
  });
});
