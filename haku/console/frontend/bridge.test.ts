import { describe, expect, it } from "vitest";

import { parseInbound, vetOpenLink } from "./bridge.ts";

describe("parseInbound", () => {
  it("accepts a well-formed openLink", () => {
    expect(parseInbound({ type: "openLink", url: "https://x" })).toEqual({ type: "openLink", url: "https://x" });
  });

  it("rejects malformed / unknown payloads", () => {
    expect(parseInbound(null)).toBeNull();
    expect(parseInbound("nope")).toBeNull();
    expect(parseInbound({ type: "openLink" })).toBeNull(); // missing url
    expect(parseInbound({ type: "openLink", url: 42 })).toBeNull(); // wrong type
    expect(parseInbound({ type: "requestCapability", id: "x" })).toBeNull(); // not wired yet
    expect(parseInbound({ type: "evalThis", code: "x" })).toBeNull(); // unknown verb
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
