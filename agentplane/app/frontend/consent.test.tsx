// @vitest-environment happy-dom

import { act } from "react";
import { describe, expect, it, vi } from "vitest";

import { answerApi, mount, type Sent } from "./actions/testing";
import { ConnectionConsent } from "./consent";
import type { ConsentPreview, ConsentResult } from "./consent_client";
import { sampleConnection } from "./connections_fixture";

function preview(): ConsentPreview {
  return {
    enrollment: {
      client_id: "registered-client-id",
      client_name: "Claude on wyrm2",
      redirect_uri: "https://client.test/oauth/callback",
      expires_at: "2026-09-09T12:00:00Z",
      version: 1,
    },
    service_accounts: [{ namespace: "agentplane-test", name: "public-coder" }],
    connections: [],
    csrf_token: "test-only-csrf",
    attempted_decision: null,
  };
}

const PREVIEW = "/connection-enrollments/opaque-handle/preview";
const DECISION = "/connection-enrollments/opaque-handle/decision";

/** Answers the preview with `previewed` and each decision with the next of `decided`, the last one
 * repeating; a `Response` answers as an HTTP failure. */
function serve(previewed: ConsentPreview | Response, ...decided: (ConsentResult | Response)[]): Sent[] {
  return answerApi(({ path }) => {
    if (path === PREVIEW) return previewed;
    if (path === DECISION) return decided.length > 1 ? decided.shift() : decided[0];
  });
}

const decisions = (sent: Sent[]): unknown[] => sent.filter(({ path }) => path === DECISION).map(({ body }) => body);

/** Mounts the consent page for `opaque-handle` and waits for its preview to arrive or fail. */
async function render(navigate = vi.fn()): Promise<HTMLDivElement> {
  const container = await mount(<ConnectionConsent handle="opaque-handle" navigate={navigate} />);
  await vi.waitFor(() => expect(container.textContent).not.toContain("Loading authorization request"));
  return container;
}

/** Clicks `label`, then waits for the decision it sends. */
async function decide(container: HTMLElement, label: string, sent: Sent[]): Promise<void> {
  const before = decisions(sent).length;
  await act(async () => button(container, label).click());
  await vi.waitFor(() => expect(decisions(sent)).toHaveLength(before + 1));
}

const refused = (detail: string): Response => Response.json({ detail }, { status: 409 });

function button(container: HTMLElement, label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent === label);
  if (!(found instanceof HTMLButtonElement)) throw new Error(`missing ${label} button`);
  return found;
}

const PUBLIC_CODER = { namespace: "agentplane-test", name: "public-coder" };

async function chooseServiceAccount(container: HTMLElement): Promise<void> {
  const select = container.querySelector<HTMLSelectElement>('select[name="service_account"]');
  if (!select) throw new Error("missing ServiceAccount select");
  await act(async () => {
    select.value = "agentplane-test/public-coder";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

describe("ConnectionConsent", () => {
  it("requires a fresh explicit confirmation for reconnect and pins the reviewed version on retry", async () => {
    const connection = sampleConnection();
    const sent = serve(
      { ...preview(), connections: [connection] },
      refused("Connection changed; restart authorization"),
      refused("Connection changed; restart authorization")
    );
    const container = await render();
    const selection = container.querySelector<HTMLSelectElement>('select[name="connection"]');
    if (!selection) throw new Error("missing Connection select");
    await act(async () => {
      selection.value = connection.id;
      selection.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await chooseServiceAccount(container);
    expect(container.textContent).toContain("Old tokens never switch ServiceAccount");
    expect(container.textContent).toContain("registered-client-123");
    expect(container.textContent).toContain("Acts as agentplane-test/personal");
    expect(button(container, "Authorize").disabled).toBe(true);
    const confirmation = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    if (!confirmation) throw new Error("missing authority confirmation");
    await act(async () => confirmation.click());
    expect(button(container, "Authorize").disabled).toBe(false);
    // Changing the selected ServiceAccount requires reviewing the warning again.
    await chooseServiceAccount(container);
    expect(button(container, "Authorize").disabled).toBe(true);
    await act(async () => confirmation.click());
    await decide(container, "Authorize", sent);
    expect(decisions(sent)).toEqual([
      {
        verdict: "allow",
        csrf_token: "test-only-csrf",
        service_account: PUBLIC_CODER,
        connection: {
          kind: "reconnect",
          connection_id: connection.id,
          expected_version: connection.version,
          authority_change_confirmed: true,
        },
      },
    ]);
    await vi.waitFor(() => expect(container.textContent).toContain("restart authorization"));
    expect(selection.disabled).toBe(true);
    await decide(container, "Retry authorization", sent);
    const [first, retried] = decisions(sent);
    expect(retried).toEqual(first);
  });

  it("requires an explicit labeled ServiceAccount and continues only after authorization", async () => {
    const navigate = vi.fn();
    const sent = serve(preview(), { verdict: "allow", redirect_url: "https://idp.test/held-authorization" });
    const container = await render(navigate);
    expect(sent.map(({ path }) => path)).toEqual([PREVIEW]);
    expect(container.textContent).toContain("registered-client-id");
    expect(container.textContent).toContain("https://client.test/oauth/callback");
    expect(container.querySelector('option[value="agentplane-test/public-coder"]')).not.toBeNull();
    expect(button(container, "Authorize").disabled).toBe(true);
    await chooseServiceAccount(container);
    await act(async () => button(container, "Authorize").click());
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledExactlyOnceWith("https://idp.test/held-authorization"));
    expect(decisions(sent)).toEqual([
      {
        verdict: "allow",
        csrf_token: "test-only-csrf",
        connection: { kind: "new", display_name: "Claude on wyrm2" },
        service_account: PUBLIC_CODER,
      },
    ]);
  });

  it("can deny without a labeled ServiceAccount and never follows the client redirect", async () => {
    const navigate = vi.fn();
    const sent = serve({ ...preview(), service_accounts: [] }, { verdict: "deny", redirect_url: null });
    const container = await render(navigate);
    expect(container.textContent).toContain("No labeled caller ServiceAccounts");
    await act(async () => button(container, "Deny").click());
    await vi.waitFor(() => expect(container.textContent).toContain("Connection denied"));
    expect(decisions(sent)).toEqual([{ verdict: "deny", csrf_token: "test-only-csrf" }]);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("renders client metadata as text, not trusted markup or navigation", async () => {
    serve({ ...preview(), enrollment: { ...preview().enrollment, client_name: '<img src=x onerror="alert(1)">' } });
    const container = await render();
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
  });

  it("keeps a failed decision immutable and retries its exact payload", async () => {
    const sent = serve(preview(), Response.json({ detail: "service unavailable" }, { status: 503 }), {
      verdict: "allow",
      redirect_url: "https://idp.test/resume",
    });
    const container = await render();
    await chooseServiceAccount(container);
    await decide(container, "Authorize", sent);
    await vi.waitFor(() => expect(container.textContent).toContain("service unavailable"));
    expect(container.querySelector("input")?.disabled).toBe(true);
    expect(container.querySelector("select")?.disabled).toBe(true);
    await act(async () => button(container, "Retry authorization").click());
    await vi.waitFor(() => expect(decisions(sent)).toHaveLength(2));
    const [first, retried] = decisions(sent);
    expect(retried).toEqual(first);
  });

  it("restores a retry-only decision after reload", async () => {
    const selected = {
      verdict: "allow" as const,
      csrf_token: "test-only-csrf",
      connection: { kind: "new" as const, display_name: "Saved connection" },
      service_account: PUBLIC_CODER,
    };
    const sent = serve(
      { ...preview(), attempted_decision: selected },
      { verdict: "allow", redirect_url: "https://idp.test/resume" }
    );
    const container = await render();
    expect(container.querySelector("input")?.value).toBe("Saved connection");
    await act(async () => button(container, "Retry authorization").click());
    await vi.waitFor(() => expect(decisions(sent)).toEqual([selected]));
  });

  it("shows expired or refused previews without authorization controls", async () => {
    serve(Response.json({ detail: "authorization expired" }, { status: 410 }));
    const container = await render();
    expect(container.textContent).toContain("authorization expired");
    expect(container.querySelector("button")).toBeNull();
  });
});
