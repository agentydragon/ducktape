import { describe, expect, it } from "vitest";

import { parseGeolocationOptions, parseInbound, vetOpenLink } from "./bridge.ts";

describe("parseInbound", () => {
  it("accepts a well-formed openLink", () => {
    expect(parseInbound({ type: "openLink", url: "https://x" })).toEqual({ type: "openLink", url: "https://x" });
  });

  it("accepts a well-formed requestLaunch", () => {
    expect(parseInbound({ type: "requestLaunch", id: "abc", prompt: "do the thing" })).toEqual({
      type: "requestLaunch",
      id: "abc",
      prompt: "do the thing",
    });
    // an empty prompt is valid (run the default routine)
    expect(parseInbound({ type: "requestLaunch", id: "abc", prompt: "" })).toEqual({
      type: "requestLaunch",
      id: "abc",
      prompt: "",
    });
  });

  it("accepts a well-formed routeChanged", () => {
    expect(parseInbound({ type: "routeChanged", path: "/" })).toEqual({ type: "routeChanged", path: "/" });
    expect(parseInbound({ type: "routeChanged", path: "/garden/notes%2Ffoo.md" })).toEqual({
      type: "routeChanged",
      path: "/garden/notes%2Ffoo.md",
    });
  });

  it("rejects routeChanged payloads that aren't validated route paths", () => {
    expect(parseInbound({ type: "routeChanged" })).toBeNull(); // missing path
    expect(parseInbound({ type: "routeChanged", path: 42 })).toBeNull(); // wrong type
    expect(parseInbound({ type: "routeChanged", path: "runs" })).toBeNull(); // no leading slash
    expect(parseInbound({ type: "routeChanged", path: "//evil.example/x" })).toBeNull(); // protocol-relative
    expect(parseInbound({ type: "routeChanged", path: "https://evil.example" })).toBeNull(); // a URL, not a path
    expect(parseInbound({ type: "routeChanged", path: "/has space" })).toBeNull(); // outside the charset
    expect(parseInbound({ type: "routeChanged", path: "/garden/%2G" })).toBeNull(); // malformed %-escape
    expect(parseInbound({ type: "routeChanged", path: "/garden/x%" })).toBeNull(); // trailing bare %
    expect(parseInbound({ type: "routeChanged", path: `/${"a".repeat(600)}` })).toBeNull(); // over the length cap
  });

  it("accepts requestGeolocation, sanitizing its optional options bag", () => {
    expect(parseInbound({ type: "requestGeolocation", id: "g1" })).toEqual({
      type: "requestGeolocation",
      id: "g1",
      options: undefined,
    });
    expect(
      parseInbound({ type: "requestGeolocation", id: "g1", options: { enableHighAccuracy: true, timeout: 5000 } })
    ).toEqual({
      type: "requestGeolocation",
      id: "g1",
      options: { enableHighAccuracy: true, timeout: 5000 },
    });
    // a mistyped option field is dropped, not fatal (mirrors the lenient browser API)
    expect(parseInbound({ type: "requestGeolocation", id: "g1", options: { timeout: "soon" } })).toEqual({
      type: "requestGeolocation",
      id: "g1",
      options: {},
    });
  });

  it("rejects requestGeolocation without a string id", () => {
    expect(parseInbound({ type: "requestGeolocation" })).toBeNull();
    expect(parseInbound({ type: "requestGeolocation", id: 7 })).toBeNull();
  });

  it("rejects malformed / unknown payloads", () => {
    expect(parseInbound(null)).toBeNull();
    expect(parseInbound("nope")).toBeNull();
    expect(parseInbound({ type: "openLink" })).toBeNull(); // missing url
    expect(parseInbound({ type: "openLink", url: 42 })).toBeNull(); // wrong type
    expect(parseInbound({ type: "requestLaunch", id: "x" })).toBeNull(); // missing prompt
    expect(parseInbound({ type: "requestLaunch", prompt: "x" })).toBeNull(); // missing id
    expect(parseInbound({ type: "requestLaunch", id: 1, prompt: "x" })).toBeNull(); // wrong id type
    expect(parseInbound({ type: "evalThis", code: "x" })).toBeNull(); // unknown verb
  });
});

describe("parseGeolocationOptions", () => {
  it("keeps only recognized, correctly-typed fields", () => {
    expect(parseGeolocationOptions({ enableHighAccuracy: true, timeout: 5000, maximumAge: 0 })).toEqual({
      enableHighAccuracy: true,
      timeout: 5000,
      maximumAge: 0,
    });
    expect(parseGeolocationOptions({ timeout: "soon", maximumAge: 1000, junk: "x" })).toEqual({ maximumAge: 1000 });
  });

  it("returns undefined for a missing or non-object bag", () => {
    expect(parseGeolocationOptions(undefined)).toBeUndefined();
    expect(parseGeolocationOptions(null)).toBeUndefined();
    expect(parseGeolocationOptions("nope")).toBeUndefined();
  });
});

describe("vetOpenLink", () => {
  it("opens whitelisted https hosts (and their subdomains) directly", () => {
    expect(vetOpenLink("https://claude.ai/new?q=hi").action).toBe("open");
    expect(vetOpenLink("https://www.github.com/a/b").action).toBe("open");
    expect(vetOpenLink("https://haku-ui.allegedly.works/x").action).toBe("open");
  });

  it("confirms off-whitelist https hosts", () => {
    expect(vetOpenLink("https://evil.example.com/phish").action).toBe("confirm");
    // a host that merely ends with a whitelisted label but isn't a subdomain
    expect(vetOpenLink("https://notgithub.com").action).toBe("confirm");
  });

  it("allows mailto without a host check", () => {
    expect(vetOpenLink("mailto:ops@allegedly.works").action).toBe("open");
  });

  it("hard-rejects non-https/mailto schemes regardless of whitelist", () => {
    expect(vetOpenLink("javascript:alert(1)").action).toBe("reject");
    expect(vetOpenLink("data:text/html,<script>").action).toBe("reject");
    expect(vetOpenLink("file:///etc/passwd").action).toBe("reject");
    expect(vetOpenLink("http://claude.ai").action).toBe("reject"); // plain http rejected even if host is whitelisted
    expect(vetOpenLink("not a url").action).toBe("reject");
  });
});
