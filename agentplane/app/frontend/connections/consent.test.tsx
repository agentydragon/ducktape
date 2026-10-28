// @vitest-environment happy-dom

import { act } from "react";
import { describe, expect, it, vi } from "vitest";

import { answerConnectionFacade, mountConnectionService, type Sent } from "./testing/test_support";
import { ConnectionConsent } from "./consent";
import type { ConsentPreview, ConsentResult } from "./connection_service_client";
import { sampleConnection } from "./testing/fixture";

function preview(): ConsentPreview {
  return {
    enrollment: {
      client_id: "registered-client-id",
      client_name: "Claude on wyrm2",
      redirect_uri: "https://client.test/oauth/callback",
      expires_at: "2026-09-09T12:00:00Z",
      version: "1",
    },
    service_accounts: [{ namespace: "agentplane-test", name: "test-coder" }],
    connections: [],
  };
}

const ROOT = "/upstream/connections/v1/operator";
const PREVIEW = `${ROOT}/connection-enrollments/opaque-handle/preview`;
const DECISION = `${ROOT}/connection-enrollments/opaque-handle/decision`;
const CALLERS = `${ROOT}/caller-service-accounts`;
const CONNECTIONS = `${ROOT}/connections`;

function connectionJson(row: ReturnType<typeof sampleConnection>): Record<string, unknown> {
  return {
    id: row.id,
    displayName: row.display_name,
    version: row.version,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    grants: row.grants.map((grant) => ({
      id: grant.id,
      connectionId: grant.connection_id,
      revision: grant.revision,
      caller: grant.caller,
      issuer: grant.issuer,
      clientId: grant.client_id,
      status: `GRANT_STATUS_${grant.status.toUpperCase()}`,
      createdAt: grant.created_at,
    })),
    boundCaller: row.bound_caller,
  };
}

/** Answers the preview with `previewed` and a decision with `decided`; a `Response` answers as an
 * HTTP failure. */
function serve(previewed: ConsentPreview | Response, decided?: ConsentResult | Response): Sent[] {
  return answerConnectionFacade(({ path }) => {
    if (path === PREVIEW) {
      if (previewed instanceof Response) return previewed;
      return {
        clientId: previewed.enrollment.client_id,
        clientName: previewed.enrollment.client_name,
        redirectUri: previewed.enrollment.redirect_uri,
        expiresAt: previewed.enrollment.expires_at,
        version: previewed.enrollment.version,
      };
    }
    if (path === CALLERS) return { serviceAccounts: previewed instanceof Response ? [] : previewed.service_accounts };
    if (path === CONNECTIONS)
      return { connections: previewed instanceof Response ? [] : previewed.connections.map(connectionJson) };
    if (path === DECISION) {
      if (decided instanceof Response || decided === undefined) return decided;
      return {
        verdict: decided.verdict === "allow" ? "VERDICT_ALLOW" : "VERDICT_DENY",
        ...(decided.redirect_url === null ? {} : { redirectUrl: decided.redirect_url }),
      };
    }
  });
}

const decisions = (sent: Sent[]): unknown[] => sent.filter(({ path }) => path === DECISION).map(({ body }) => body);

/** Mounts the consent page for `opaque-handle` and waits for its preview to arrive or fail. */
async function render(navigate = vi.fn()): Promise<HTMLDivElement> {
  const container = await mountConnectionService(<ConnectionConsent handle="opaque-handle" navigate={navigate} />);
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

const TEST_CODER = { namespace: "agentplane-test", name: "test-coder" };

async function chooseServiceAccount(container: HTMLElement): Promise<void> {
  const select = container.querySelector<HTMLSelectElement>('select[name="service_account"]');
  if (!select) throw new Error("missing ServiceAccount select");
  await act(async () => {
    select.value = "agentplane-test/test-coder";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

describe("ConnectionConsent", () => {
  it("requires a fresh explicit confirmation for reconnect and sends the reviewed version", async () => {
    const connection = sampleConnection();
    const sent = serve(
      { ...preview(), connections: [connection] },
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
        expectedVersion: "1",
        allow: {
          serviceAccount: TEST_CODER,
          reconnect: {
            connectionId: connection.id,
            expectedVersion: connection.version,
            authorityChangeConfirmed: true,
          },
        },
      },
    ]);
    await vi.waitFor(() => expect(container.textContent).toContain("restart authorization"));
    expect(container.textContent).toContain("start a new authorization from your MCP client");
  });

  it("requires an explicit labeled ServiceAccount and continues only after authorization", async () => {
    const navigate = vi.fn();
    const sent = serve(preview(), { verdict: "allow", redirect_url: "https://idp.test/held-authorization" });
    const container = await render(navigate);
    expect(sent.map(({ path }) => path)).toEqual(expect.arrayContaining([PREVIEW, CALLERS, CONNECTIONS]));
    expect(sent).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ method: "POST", path: PREVIEW, body: {} }),
        expect.objectContaining({ method: "GET", path: CALLERS }),
        expect.objectContaining({ method: "GET", path: CONNECTIONS }),
      ])
    );
    expect(sent).toHaveLength(3);
    expect(container.textContent).toContain("registered-client-id");
    expect(container.textContent).toContain("https://client.test/oauth/callback");
    expect(container.querySelector('option[value="agentplane-test/test-coder"]')).not.toBeNull();
    expect(button(container, "Authorize").disabled).toBe(true);
    await chooseServiceAccount(container);
    await act(async () => button(container, "Authorize").click());
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledExactlyOnceWith("https://idp.test/held-authorization"));
    expect(decisions(sent)).toEqual([
      {
        expectedVersion: "1",
        allow: {
          serviceAccount: TEST_CODER,
          newConnection: { displayName: "Claude on wyrm2" },
        },
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
    expect(decisions(sent)).toEqual([{ expectedVersion: "1", deny: {} }]);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("renders client metadata as text, not trusted markup or navigation", async () => {
    serve({ ...preview(), enrollment: { ...preview().enrollment, client_name: '<img src=x onerror="alert(1)">' } });
    const container = await render();
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
  });

  it("shows expired or refused previews without authorization controls", async () => {
    serve(Response.json({ detail: "authorization expired" }, { status: 410 }));
    const container = await render();
    expect(container.textContent).toContain("authorization expired");
    expect(container.querySelector("button")).toBeNull();
  });
});
