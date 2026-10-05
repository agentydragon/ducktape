// @vitest-environment happy-dom

import { screen, waitForElementToBeRemoved } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ConnectionConsent } from "./consent";
import type { ConsentPreview, ConsentService } from "./consent_client";
import { sampleConnection } from "./connections_fixture";
import { renderInMantine } from "./testing_library";

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

async function render(service: ConsentService, navigate = vi.fn()): Promise<ReturnType<typeof renderInMantine>> {
  const rendered = renderInMantine(<ConnectionConsent handle="opaque-handle" service={service} navigate={navigate} />);
  await waitForElementToBeRemoved(() => screen.queryByText("Loading authorization request…"));
  return rendered;
}

// Mantine's required marker makes the accessible name "ServiceAccount *".
const serviceAccount = (): HTMLElement => screen.getByRole("combobox", { name: /^ServiceAccount/ });
const connectionName = (): HTMLElement => screen.getByRole("textbox", { name: /^Connection name/ });

const PUBLIC_CODER = { namespace: "agentplane-test", name: "public-coder" };

describe("ConnectionConsent", () => {
  it("requires a fresh explicit confirmation for reconnect and pins the reviewed version on retry", async () => {
    const connection = sampleConnection();
    const decide = vi
      .fn<ConsentService["decide"]>()
      .mockRejectedValue(new Error("Connection changed; restart authorization"));
    const { container, user } = await render({
      preview: async () => ({ ...preview(), connections: [connection] }),
      decide,
    });
    const selection = screen.getByRole("combobox", { name: "Connection" });
    await user.selectOptions(selection, connection.id);
    await user.selectOptions(serviceAccount(), "agentplane-test/public-coder");
    expect(container).toHaveTextContent("Old tokens never switch ServiceAccount");
    expect(container).toHaveTextContent("registered-client-123");
    expect(container).toHaveTextContent("Acts as agentplane-test/personal");
    expect(screen.getByRole("button", { name: "Authorize" })).toBeDisabled();
    const confirmation = screen.getByRole("checkbox", { name: "I confirm replacing this Connection’s authority" });
    await user.click(confirmation);
    expect(screen.getByRole("button", { name: "Authorize" })).toBeEnabled();
    // Changing the selected ServiceAccount requires reviewing the warning again.
    await user.selectOptions(serviceAccount(), "agentplane-test/public-coder");
    expect(screen.getByRole("button", { name: "Authorize" })).toBeDisabled();
    await user.click(confirmation);
    await user.click(screen.getByRole("button", { name: "Authorize" }));
    expect(decide).toHaveBeenCalledExactlyOnceWith("opaque-handle", {
      verdict: "allow",
      csrf_token: "test-only-csrf",
      service_account: PUBLIC_CODER,
      connection: {
        kind: "reconnect",
        connection_id: connection.id,
        expected_version: connection.version,
        authority_change_confirmed: true,
      },
    });
    expect(await screen.findByText(/restart authorization/)).toBeInTheDocument();
    expect(selection).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Retry authorization" }));
    expect(decide.mock.calls[1]).toEqual(decide.mock.calls[0]);
  });

  it("requires an explicit labeled ServiceAccount and continues only after authorization", async () => {
    const navigate = vi.fn();
    const service: ConsentService = {
      preview: vi.fn(async () => preview()),
      decide: vi.fn<ConsentService["decide"]>(async () => ({
        verdict: "allow",
        redirect_url: "https://idp.test/held-authorization",
      })),
    };
    const { container, user } = await render(service, navigate);
    expect(service.preview).toHaveBeenCalledWith("opaque-handle");
    expect(container).toHaveTextContent("registered-client-id");
    expect(container).toHaveTextContent("https://client.test/oauth/callback");
    expect(screen.getByRole("option", { name: "agentplane-test/public-coder" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Authorize" })).toBeDisabled();
    await user.selectOptions(serviceAccount(), "agentplane-test/public-coder");
    await user.click(screen.getByRole("button", { name: "Authorize" }));
    expect(service.decide).toHaveBeenCalledWith("opaque-handle", {
      verdict: "allow",
      csrf_token: "test-only-csrf",
      connection: { kind: "new", display_name: "Claude on wyrm2" },
      service_account: PUBLIC_CODER,
    });
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledExactlyOnceWith("https://idp.test/held-authorization"));
  });

  it("can deny without a labeled ServiceAccount and never follows the client redirect", async () => {
    const navigate = vi.fn();
    const service: ConsentService = {
      preview: async () => ({ ...preview(), service_accounts: [] }),
      decide: vi.fn<ConsentService["decide"]>(async () => ({ verdict: "deny", redirect_url: null })),
    };
    const { container, user } = await render(service, navigate);
    expect(container).toHaveTextContent("No labeled caller ServiceAccounts");
    await user.click(screen.getByRole("button", { name: "Deny" }));
    expect(service.decide).toHaveBeenCalledWith("opaque-handle", { verdict: "deny", csrf_token: "test-only-csrf" });
    expect(await screen.findByText("Connection denied")).toBeInTheDocument();
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
    const { container } = await render(service);
    expect(screen.getByText('<img src=x onerror="alert(1)">')).toBeInTheDocument();
    // By tag: the `img` and `link` roles skip an `alt=""` image and an `<a>` without `href`.
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
  });

  it("keeps a failed decision immutable and retries its exact payload", async () => {
    const decide = vi
      .fn<ConsentService["decide"]>()
      .mockRejectedValueOnce(new Error("service unavailable"))
      .mockResolvedValue({ verdict: "allow", redirect_url: "https://idp.test/resume" });
    const { user } = await render({ preview: async () => preview(), decide });
    await user.selectOptions(serviceAccount(), "agentplane-test/public-coder");
    await user.click(screen.getByRole("button", { name: "Authorize" }));
    expect(await screen.findByText("service unavailable")).toBeInTheDocument();
    expect(connectionName()).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Connection" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Retry authorization" }));
    expect(decide.mock.calls[1]).toEqual(decide.mock.calls[0]);
  });

  it("restores a retry-only decision after reload", async () => {
    const selected = {
      verdict: "allow" as const,
      csrf_token: "test-only-csrf",
      connection: { kind: "new" as const, display_name: "Saved connection" },
      service_account: PUBLIC_CODER,
    };
    const decide = vi
      .fn<ConsentService["decide"]>()
      .mockResolvedValue({ verdict: "allow", redirect_url: "https://idp.test/resume" });
    const { user } = await render({ preview: async () => ({ ...preview(), attempted_decision: selected }), decide });
    expect(connectionName()).toHaveValue("Saved connection");
    await user.click(screen.getByRole("button", { name: "Retry authorization" }));
    expect(decide).toHaveBeenCalledWith("opaque-handle", selected);
  });

  it("shows expired or refused previews without authorization controls", async () => {
    const { container } = await render({
      preview: async () => {
        throw new Error("authorization expired");
      },
      decide: vi.fn(),
    });
    expect(container).toHaveTextContent("authorization expired");
    expect(screen.queryByRole("button")).toBeNull();
  });
});
