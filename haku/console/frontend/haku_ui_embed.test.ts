import { beforeEach, describe, expect, it } from "vitest";

import { hasGeolocationGrant, setGeolocationGrant } from "./geolocation_grant";
import { initialFrameSrc, routeFromLocation, shouldCloseAutoOpenedApprovalQueue } from "./haku_ui_embed";
import { APPROVALS_EMBED_PATH, rememberEmbedPath, SETTINGS_PATH, TOOL_CALLS_PATH, viewForPathname } from "./routing";
import { hasScreenshotGrant, setScreenshotGrant } from "./screenshot_grant";

describe("initialFrameSrc", () => {
  it("pins the origin and carries the route into the frame's pathname", () => {
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "/runs")).toBe("https://haku-ui.allegedly.works/runs");
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "/garden/notes%2Ffoo.md")).toBe(
      "https://haku-ui.allegedly.works/garden/notes%2Ffoo.md"
    );
    // Legacy hash-form console URLs still restore (the leading # is stripped).
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "#/runs")).toBe("https://haku-ui.allegedly.works/runs");
  });

  it("falls back to the bare uiUrl when the route is absent or not a valid route path", () => {
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "")).toBe("https://haku-ui.allegedly.works/");
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "runs")).toBe("https://haku-ui.allegedly.works/");
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "https://evil.example")).toBe(
      "https://haku-ui.allegedly.works/"
    );
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "//evil.example/x")).toBe(
      "https://haku-ui.allegedly.works/"
    );
  });
});

describe("shouldCloseAutoOpenedApprovalQueue", () => {
  it("closes an automatically opened drawer when the pending queue drains", () => {
    expect(shouldCloseAutoOpenedApprovalQueue(true, true, 0)).toBe(true);
  });

  it("keeps a manually opened drawer open", () => {
    expect(shouldCloseAutoOpenedApprovalQueue(true, false, 0)).toBe(false);
  });

  it("keeps an automatically opened drawer open while approvals remain", () => {
    expect(shouldCloseAutoOpenedApprovalQueue(true, true, 1)).toBe(false);
  });
});

describe("routeFromLocation", () => {
  beforeEach(() => sessionStorage.clear());

  it("prefers a legacy #/ fragment, else mirrors the pathname", () => {
    expect(routeFromLocation({ pathname: "/", hash: "#/garden/a.md" })).toBe("/garden/a.md");
    expect(routeFromLocation({ pathname: "/garden/a.md", hash: "" })).toBe("/garden/a.md");
    expect(routeFromLocation({ pathname: "/", hash: "" })).toBe("/");
  });

  it("carries no frame route for console-own views", () => {
    rememberEmbedPath("/garden/remembered.md");
    expect(routeFromLocation({ pathname: TOOL_CALLS_PATH, hash: "" })).toBe("/garden/remembered.md");
    expect(routeFromLocation({ pathname: SETTINGS_PATH, hash: "" })).toBe("/garden/remembered.md");
    expect(routeFromLocation({ pathname: APPROVALS_EMBED_PATH, hash: "" })).toBe("/garden/remembered.md");
    expect(viewForPathname(APPROVALS_EMBED_PATH)).toBe("approvalsEmbed");
  });
});

describe("geolocation grant (standing consent, shell localStorage)", () => {
  beforeEach(() => localStorage.clear());

  it("is absent until granted, and cleared on withdraw", () => {
    expect(hasGeolocationGrant()).toBe(false);
    setGeolocationGrant(true);
    expect(hasGeolocationGrant()).toBe(true);
    setGeolocationGrant(false);
    expect(hasGeolocationGrant()).toBe(false);
  });
});

describe("screenshot grant (standing consent, shell localStorage)", () => {
  beforeEach(() => localStorage.clear());

  it("is absent until granted, and cleared on withdraw", () => {
    expect(hasScreenshotGrant()).toBe(false);
    setScreenshotGrant(true);
    expect(hasScreenshotGrant()).toBe(true);
    setScreenshotGrant(false);
    expect(hasScreenshotGrant()).toBe(false);
  });

  it("is independent of the geolocation grant", () => {
    setGeolocationGrant(true);
    expect(hasScreenshotGrant()).toBe(false);
  });
});
