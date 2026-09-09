// @vitest-environment happy-dom

import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConnectionConsent } from "./consent";
import type { ConsentPreview, ConsentService } from "./consent_client";

const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];
(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function preview(): ConsentPreview {
  return {
    enrollment: {
      client_id: "registered-client-id",
      client_name: "Claude on wyrm2",
      redirect_uri: "https://client.test/oauth/callback",
      expires_at: "2026-09-09T12:00:00Z",
      version: 1,
    },
    identities: { public_coder: { enabled: true }, disabled: { enabled: false } },
    csrf_token: "test-only-csrf",
    attempted_decision: null,
  };
}

async function render(service: ConsentService, navigate = vi.fn()): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () => {
    root.render(
      <MantineProvider>
        <ConnectionConsent handle="opaque-handle" service={service} navigate={navigate} />
      </MantineProvider>
    );
  });
  return container;
}

function button(container: HTMLElement, label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent === label);
  if (!(found instanceof HTMLButtonElement)) throw new Error(`missing ${label} button`);
  return found;
}

async function chooseIdentity(container: HTMLElement): Promise<void> {
  const select = container.querySelector("select");
  if (!select) throw new Error("missing Identity select");
  await act(async () => {
    select.value = "public_coder";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
});

describe("ConnectionConsent", () => {
  it("requires an explicit enabled Identity and continues only after authorization", async () => {
    const navigate = vi.fn();
    const service: ConsentService = {
      preview: vi.fn(async () => preview()),
      decide: vi.fn<ConsentService["decide"]>(async () => ({
        verdict: "allow",
        redirect_url: "https://idp.test/held-authorization",
      })),
    };
    const container = await render(service, navigate);
    expect(service.preview).toHaveBeenCalledWith("opaque-handle");
    expect(container.textContent).toContain("registered-client-id");
    expect(container.textContent).toContain("https://client.test/oauth/callback");
    expect(container.querySelector('option[value="disabled"]')).toBeNull();
    expect(button(container, "Authorize").disabled).toBe(true);
    await chooseIdentity(container);
    await act(async () => button(container, "Authorize").click());
    expect(service.decide).toHaveBeenCalledWith("opaque-handle", {
      verdict: "allow",
      csrf_token: "test-only-csrf",
      display_name: "Claude on wyrm2",
      identity_id: "public_coder",
    });
    expect(navigate).toHaveBeenCalledOnce();
    expect(navigate).toHaveBeenCalledWith("https://idp.test/held-authorization");
  });

  it("can deny without a configured Identity and never follows the client redirect", async () => {
    const navigate = vi.fn();
    const service: ConsentService = {
      preview: async () => ({ ...preview(), identities: {} }),
      decide: vi.fn<ConsentService["decide"]>(async () => ({ verdict: "deny", redirect_url: null })),
    };
    const container = await render(service, navigate);
    expect(container.textContent).toContain("No enabled identities");
    await act(async () => button(container, "Deny").click());
    expect(service.decide).toHaveBeenCalledWith("opaque-handle", { verdict: "deny", csrf_token: "test-only-csrf" });
    expect(container.textContent).toContain("Connection denied");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("renders client metadata as text, not trusted markup or navigation", async () => {
    const service: ConsentService = {
      preview: async () => ({
        ...preview(),
        enrollment: { ...preview().enrollment, client_name: '<img src=x onerror="alert(1)">' },
      }),
      decide: vi.fn(),
    };
    const container = await render(service);
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
  });

  it("keeps a failed decision immutable and retries its exact payload", async () => {
    const decide = vi
      .fn<ConsentService["decide"]>()
      .mockRejectedValueOnce(new Error("service unavailable"))
      .mockResolvedValue({ verdict: "allow", redirect_url: "https://idp.test/resume" });
    const container = await render({ preview: async () => preview(), decide });
    await chooseIdentity(container);
    await act(async () => button(container, "Authorize").click());
    expect(container.textContent).toContain("service unavailable");
    expect(container.querySelector("input")?.disabled).toBe(true);
    expect(container.querySelector("select")?.disabled).toBe(true);
    await act(async () => button(container, "Retry authorization").click());
    expect(decide.mock.calls[1]).toEqual(decide.mock.calls[0]);
  });

  it("restores a retry-only decision after reload", async () => {
    const selected = {
      verdict: "allow" as const,
      csrf_token: "test-only-csrf",
      display_name: "Saved connection",
      identity_id: "public_coder",
    };
    const decide = vi
      .fn<ConsentService["decide"]>()
      .mockResolvedValue({ verdict: "allow", redirect_url: "https://idp.test/resume" });
    const container = await render({ preview: async () => ({ ...preview(), attempted_decision: selected }), decide });
    expect(container.querySelector("input")?.value).toBe("Saved connection");
    await act(async () => button(container, "Retry authorization").click());
    expect(decide).toHaveBeenCalledWith("opaque-handle", selected);
  });

  it("shows expired or refused previews without authorization controls", async () => {
    const container = await render({
      preview: async () => {
        throw new Error("authorization expired");
      },
      decide: vi.fn(),
    });
    expect(container.textContent).toContain("authorization expired");
    expect(container.querySelector("button")).toBeNull();
  });
});
