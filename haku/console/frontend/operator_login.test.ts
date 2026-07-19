import { describe, expect, it, vi } from "vitest";

import { redirectToOperatorLogin } from "./operator_login.ts";

describe("redirectToOperatorLogin", () => {
  it("starts only one login flow when requests fail concurrently", () => {
    const replace = vi.fn();
    const location = { pathname: "/", replace };

    redirectToOperatorLogin(location);
    redirectToOperatorLogin(location);

    expect(replace).toHaveBeenCalledOnce();
    expect(replace).toHaveBeenCalledWith("/auth/login");
  });
});
