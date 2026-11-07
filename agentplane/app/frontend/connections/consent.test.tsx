// @vitest-environment happy-dom

import { act } from "react";
import { describe, expect, it, vi } from "vitest";
import { create, toJson, type JsonObject } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";

import { Verdict } from "../../../action_service/action_service_pb";
import {
  ConnectionSchema,
  DecideConnectionEnrollmentResponseSchema,
  ListCallerServiceAccountsResponseSchema,
  ListConnectionsResponseSchema,
  PreviewConnectionEnrollmentResponseSchema,
  type DecideConnectionEnrollmentResponse,
} from "../../../mcp_edge/connection_service_pb";
import { ServiceAccountSchema } from "../../../proto/identity_pb";
import { answerConnectionFacade, mountConnectionService, type Sent } from "./testing/test_support";
import { ConnectionConsent } from "./consent";
import type { ConsentPreview } from "./connection_service_client";
import { sampleConnection } from "./testing/fixture";

function preview(): ConsentPreview {
  return {
    enrollment: create(PreviewConnectionEnrollmentResponseSchema, {
      clientId: "registered-client-id",
      clientName: "Claude on wyrm2",
      redirectUri: "https://client.test/oauth/callback",
      expiresAt: timestampFromDate(new Date("2026-09-09T12:00:00Z")),
      version: 1n,
    }),
    serviceAccounts: [create(ServiceAccountSchema, { namespace: "agentplane-test", name: "test-coder" })],
    connections: [],
  };
}

const ROOT = "/upstream/connections/v1/operator";
const PREVIEW = `${ROOT}/connection-enrollments/opaque-handle/preview`;
const DECISION = `${ROOT}/connection-enrollments/opaque-handle/decision`;
const CALLERS = `${ROOT}/caller-service-accounts`;
const CONNECTIONS = `${ROOT}/connections`;

function connectionJson(row: ReturnType<typeof sampleConnection>): JsonObject {
  return toJson(ConnectionSchema, row) as JsonObject;
}

/** Answers the preview with `previewed` and a decision with `decided`; a `Response` answers as an
 * HTTP failure. */
function serve(previewed: ConsentPreview | Response, decided?: DecideConnectionEnrollmentResponse | Response): Sent[] {
  return answerConnectionFacade(({ path }) => {
    if (path === PREVIEW) {
      if (previewed instanceof Response) return previewed;
      return toJson(PreviewConnectionEnrollmentResponseSchema, previewed.enrollment);
    }
    if (path === CALLERS) {
      return toJson(
        ListCallerServiceAccountsResponseSchema,
        create(ListCallerServiceAccountsResponseSchema, {
          serviceAccounts: previewed instanceof Response ? [] : previewed.serviceAccounts,
        })
      );
    }
    if (path === CONNECTIONS) {
      return toJson(
        ListConnectionsResponseSchema,
        create(ListConnectionsResponseSchema, {
          connections: previewed instanceof Response ? [] : previewed.connections,
        })
      );
    }
    if (path === DECISION) {
      if (decided instanceof Response || decided === undefined) return decided;
      return toJson(DecideConnectionEnrollmentResponseSchema, decided);
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
      create(DecideConnectionEnrollmentResponseSchema, {
        verdict: Verdict.ALLOW,
        redirectUrl: "https://idp.test/held-authorization",
      })
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
            expectedVersion: "2",
            authorityChangeConfirmed: true,
          },
        },
      },
    ]);
    expect(container.textContent).toContain("Continue authorization");
  });

  it("requires an explicit labeled ServiceAccount and continues only after authorization", async () => {
    const navigate = vi.fn();
    const sent = serve(
      preview(),
      create(DecideConnectionEnrollmentResponseSchema, {
        verdict: Verdict.ALLOW,
        redirectUrl: "https://idp.test/held-authorization",
      })
    );
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
    const sent = serve(
      { ...preview(), serviceAccounts: [] },
      create(DecideConnectionEnrollmentResponseSchema, { verdict: Verdict.DENY })
    );
    const container = await render(navigate);
    expect(container.textContent).toContain("No labeled caller ServiceAccounts");
    await act(async () => button(container, "Deny").click());
    await vi.waitFor(() => expect(container.textContent).toContain("Connection denied"));
    expect(decisions(sent)).toEqual([{ expectedVersion: "1", deny: {} }]);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("renders client metadata as text, not trusted markup or navigation", async () => {
    const enrollment = create(PreviewConnectionEnrollmentResponseSchema, {
      ...preview().enrollment,
      clientName: '<img src=x onerror="alert(1)">',
    });
    serve({ ...preview(), enrollment });
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
