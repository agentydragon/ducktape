import { describe, expect, it } from "vitest";

import { detailMessage } from "./api";

describe("detailMessage", () => {
  it("takes the string an HTTPException carries", () => {
    expect(detailMessage({ detail: "state differs" }, "fallback")).toBe("state differs");
  });

  it("joins the issues of a rejected request body", () => {
    expect(
      detailMessage({ detail: [{ msg: "Field required" }, { msg: "Input should be a string" }] }, "fallback")
    ).toBe("Field required; Input should be a string");
  });

  it("falls back for a body that says nothing usable", () => {
    expect(detailMessage(null, "fallback")).toBe("fallback");
    expect(detailMessage({ detail: [] }, "fallback")).toBe("fallback");
  });
});
