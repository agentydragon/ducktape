import { describe, expect, it, vi } from "vitest";

import { replaceSearchParams } from "./url_state";

function browserAt(search = "?unknown=keep&fmt=exact") {
  const replaceState = vi.fn();
  return {
    browser: {
      location: { pathname: "/augur", search, hash: "#chart" },
      history: { replaceState },
    } as unknown as Window,
    replaceState,
  };
}

describe("replaceSearchParams", () => {
  it("updates owned parameters while preserving unrelated URL state", () => {
    const { browser, replaceState } = browserAt();

    expect(replaceSearchParams({ scenarios: "new", fmt: null, scale: "log" }, browser)).toBe(true);
    expect(replaceState).toHaveBeenCalledExactlyOnceWith(null, "", "/augur?unknown=keep&scenarios=new&scale=log#chart");
  });

  it("does not write an unchanged URL", () => {
    const { browser, replaceState } = browserAt("?n=100");

    expect(replaceSearchParams({ n: 100 }, browser)).toBe(false);
    expect(replaceState).not.toHaveBeenCalled();
  });

  it("removes the query marker when deleting the final parameter", () => {
    const { browser, replaceState } = browserAt("?tab=budget");

    expect(replaceSearchParams({ tab: null }, browser)).toBe(true);
    expect(replaceState).toHaveBeenCalledWith(null, "", "/augur#chart");
  });
});
