import { beforeEach, describe, expect, it } from "vitest";

import { hasGeolocationGrant, setGeolocationGrant } from "./geolocation_grant.ts";
import { initialFrameSrc, routeFromLocation } from "./haku_ui_embed.tsx";
import { hasScreenshotGrant, setScreenshotGrant } from "./screenshot_grant.ts";

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

describe("routeFromLocation", () => {
  it("prefers a legacy #/ fragment, else mirrors the pathname", () => {
    expect(routeFromLocation({ pathname: "/", hash: "#/garden/a.md" })).toBe("/garden/a.md");
    expect(routeFromLocation({ pathname: "/garden/a.md", hash: "" })).toBe("/garden/a.md");
    expect(routeFromLocation({ pathname: "/", hash: "" })).toBe("/");
  });

  it("carries no frame route for console-own views", () => {
    expect(routeFromLocation({ pathname: "/tool-calls", hash: "" })).toBe("/");
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
