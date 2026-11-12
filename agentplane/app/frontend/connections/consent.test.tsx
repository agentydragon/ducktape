// @vitest-environment happy-dom

import { act } from "react";
import { describe, expect, it, vi } from "vitest";
import { create, toJson } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";

import { Verdict } from "../../../action_service/action_service_pb";
import {
  DecideConnectionEnrollmentResponseSchema,
  ListConnectionIdentitiesResponseSchema,
  ListConnectionsResponseSchema,
  PreviewConnectionEnrollmentResponseSchema,
  type DecideConnectionEnrollmentResponse,
} from "../../../mcp_edge/connection_service_pb";
import { IdentitySchema } from "../../../proto/identity_pb";
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
    identities: [create(IdentitySchema, { name: "test-coder", uid: "30000000-0000-4000-8000-000000000002" })],
    connections: [],
  };
}

const ROOT = "/upstream/connections/v1/operator";
const PREVIEW = `${ROOT}/connection-enrollments/opaque-handle/preview`;
const DECISION = `${ROOT}/connection-enrollments/opaque-handle/decision`;
const CALLERS = `${ROOT}/connection-identities`;
const CONNECTIONS = `${ROOT}/connections`;

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
        ListConnectionIdentitiesResponseSchema,
        create(ListConnectionIdentitiesResponseSchema, {
          identities: previewed instanceof Response ? [] : previewed.identities,
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

function button(container: HTMLElement, label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent === label);
  if (!(found instanceof HTMLButtonElement)) throw new Error(`missing ${label} button`);
  return found;
}

const TEST_CODER = { name: "test-coder", uid: "30000000-0000-4000-8000-000000000002" };

async function chooseIdentity(container: HTMLElement): Promise<void> {
  const select = container.querySelector<HTMLSelectElement>('select[name="identity"]');
  if (!select) throw new Error("missing Identity select");
  await act(async () => {
    select.value = "test-coder";
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
    await chooseIdentity(container);
    expect(container.textContent).toContain("Fresh OAuth replaces this Connection");
    expect(container.textContent).toContain("registered-client-123");
    expect(container.textContent).toContain("Acts as personal");
    expect(button(container, "Authorize").disabled).toBe(true);
    const confirmation = container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    if (!confirmation) throw new Error("missing authority confirmation");
    await act(async () => confirmation.click());
    expect(button(container, "Authorize").disabled).toBe(false);
    // Changing the selected Identity requires reviewing the warning again.
    await chooseIdentity(container);
    expect(button(container, "Authorize").disabled).toBe(true);
    await act(async () => confirmation.click());
    await decide(container, "Authorize", sent);
    expect(decisions(sent)).toEqual([
      {
        expectedVersion: "1",
        allow: {
          identity: TEST_CODER,
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

  it("requires an explicit live Identity and continues only after authorization", async () => {
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
    expect(container.querySelector('option[value="test-coder"]')).not.toBeNull();
    expect(button(container, "Authorize").disabled).toBe(true);
    await chooseIdentity(container);
    await act(async () => button(container, "Authorize").click());
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledExactlyOnceWith("https://idp.test/held-authorization"));
    expect(decisions(sent)).toEqual([
      {
        expectedVersion: "1",
        allow: {
          identity: TEST_CODER,
          newConnection: { displayName: "Claude on wyrm2" },
        },
      },
    ]);
  });

  it("can deny without a live Identity and never follows the client redirect", async () => {
    const navigate = vi.fn();
    const sent = serve(
      { ...preview(), identities: [] },
      create(DecideConnectionEnrollmentResponseSchema, { verdict: Verdict.DENY })
    );
    const container = await render(navigate);
    expect(container.textContent).toContain("No live Identities exist");
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
