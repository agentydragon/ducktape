import { beforeEach, describe, expect, it } from "vitest";

import { hasGeolocationGrant, setGeolocationGrant } from "./geolocation_grant.ts";
import { initialFrameSrc } from "./haku_ui_embed.tsx";

describe("initialFrameSrc", () => {
  it("pins the origin and carries only the console hash into the frame's fragment", () => {
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "#/runs")).toBe(
      "https://haku-ui.allegedly.works/#/runs"
    );
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "#/garden/notes%2Ffoo.md")).toBe(
      "https://haku-ui.allegedly.works/#/garden/notes%2Ffoo.md"
    );
  });

  it("falls back to the bare uiUrl when the hash is absent or not a valid route path", () => {
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "")).toBe("https://haku-ui.allegedly.works/");
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "#runs")).toBe("https://haku-ui.allegedly.works/");
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "#https://evil.example")).toBe(
      "https://haku-ui.allegedly.works/"
    );
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "#//evil.example/x")).toBe(
      "https://haku-ui.allegedly.works/"
    );
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
