import { act, createElement } from "react";
import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";
// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from "vitest";

import * as client from "./client";
import { AgentEnrollmentPanel } from "./agent_enrollment_panel";
import { OAuthResultPage } from "./oauth_result_page";
import type { EnrollmentView, OAuthConnectionResult } from "./client";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason?: unknown) => void = () => undefined;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

function enrollment(overrides: Partial<EnrollmentView> = {}): EnrollmentView {
  return {
    operator_display_name: "Operator",
    client_software: "Example client",
    redirect_host: "example.test",
    requested_scopes: [],
    suggested_agent_name: "Suggested Agent",
    reconnectable_agents: [],
    access_profiles: ["default"],
    default_access_profile_id: "default",
    form_token: "form-token",
    ...overrides,
  };
}

function oauthResult(overrides: Partial<OAuthConnectionResult> = {}): OAuthConnectionResult {
  return {
    status: "success",
    title: "Connected",
    message: "The connection is ready.",
    ...overrides,
  } as OAuthConnectionResult;
}

function withProvider(element: ReturnType<typeof createElement>) {
  return createElement(MantineProvider, null, element);
}

function mounted() {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  return {
    container,
    root,
    unmount: () => {
      act(() => root.unmount());
      container.remove();
    },
  };
}

function renderAgent(
  view: ReturnType<typeof mounted>,
  interactionId: string,
  initialChoice: "create" | "reconnect" = "create"
) {
  act(() =>
    view.root.render(
      withProvider(
        createElement(AgentEnrollmentPanel, {
          key: `${interactionId}:${initialChoice}`,
          interactionId,
          initialChoice,
          onReturnToSettings: vi.fn(),
        })
      )
    )
  );
}

function renderOAuth(view: ReturnType<typeof mounted>, resultId: string) {
  act(() => view.root.render(withProvider(createElement(OAuthResultPage, { key: resultId, resultId }))));
}

function agentNameValue(view: ReturnType<typeof mounted>): string | undefined {
  return Array.from(view.container.querySelectorAll<HTMLInputElement>("input")).find((input) => input.type === "text")
    ?.value;
}

afterEach(() => {
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe("route resource identity", () => {
  it("remounts Agent enrollment state when the interaction or initial choice changes", async () => {
    const first = deferred<EnrollmentView>();
    const second = deferred<EnrollmentView>();
    const load = vi
      .spyOn(client, "getAgentEnrollment")
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const view = mounted();

    renderAgent(view, "interaction-one", "reconnect");
    await vi.waitFor(() => expect(load).toHaveBeenCalledWith("interaction-one"));

    // The same interaction can be revisited with a different initial choice. The old
    // selection/form state and a late response must not survive that route transition.
    renderAgent(view, "interaction-one", "create");
    expect(view.container.textContent).not.toContain("Reconnect an existing Agent");
    await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(2));

    await act(async () => {
      first.resolve(
        enrollment({
          suggested_agent_name: "First Agent",
          reconnectable_agents: [{ agent_id: "agent-one", display_name: "First Agent", access_profile_id: "default" }],
        })
      );
      await first.promise;
      await Promise.resolve();
    });
    expect(agentNameValue(view)).not.toBe("First Agent");
    expect(view.container.textContent).not.toContain("Reconnect an existing Agent");

    await act(async () => {
      second.resolve(enrollment({ suggested_agent_name: "Second Agent" }));
      await second.promise;
    });
    expect(agentNameValue(view)).toBe("Second Agent");
    expect(view.container.textContent).toContain("Continue");
    expect(view.container.textContent).not.toContain("Reconnect an existing Agent");

    view.unmount();
  });

  it("does not carry an old enrollment error into a new interaction", async () => {
    const first = deferred<EnrollmentView>();
    const second = deferred<EnrollmentView>();
    const load = vi
      .spyOn(client, "getAgentEnrollment")
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const view = mounted();

    renderAgent(view, "interaction-one");
    await vi.waitFor(() => expect(load).toHaveBeenCalledWith("interaction-one"));

    renderAgent(view, "interaction-two");
    expect(view.container.textContent).not.toContain("first interaction failed");
    await vi.waitFor(() => expect(load).toHaveBeenCalledWith("interaction-two"));
    expect(load.mock.results[1]?.value).toBe(second.promise);

    await act(async () => {
      first.reject(new Error("first interaction failed"));
      await first.promise.catch(() => undefined);
      await Promise.resolve();
    });
    expect(view.container.textContent).not.toContain("first interaction failed");

    await act(async () => {
      second.resolve(enrollment({ suggested_agent_name: "New interaction" }));
      await second.promise;
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(agentNameValue(view)).toBe("New interaction");

    view.unmount();
  });

  it("remounts the OAuth result resource when resultId changes", async () => {
    const first = deferred<OAuthConnectionResult>();
    const second = deferred<OAuthConnectionResult>();
    const consume = vi
      .spyOn(client, "consumeOAuthConnectionResult")
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const view = mounted();

    renderOAuth(view, "result-one");
    await vi.waitFor(() => expect(consume).toHaveBeenCalledWith("result-one"));

    renderOAuth(view, "result-two");
    await vi.waitFor(() => expect(consume).toHaveBeenCalledWith("result-two"));

    await act(async () => {
      first.resolve(oauthResult({ title: "First result", message: "First message" }));
      await first.promise;
      await Promise.resolve();
    });
    expect(view.container.textContent).not.toContain("First result");
    expect(view.container.textContent).not.toContain("First message");

    await act(async () => {
      second.resolve(oauthResult({ title: "Second result", message: "Second message" }));
      await second.promise;
    });
    expect(view.container.textContent).toContain("Second result");
    expect(view.container.textContent).not.toContain("First result");

    view.unmount();
  });

  it("does not carry an old OAuth error into a new result", async () => {
    const first = deferred<OAuthConnectionResult>();
    const second = deferred<OAuthConnectionResult>();
    const consume = vi
      .spyOn(client, "consumeOAuthConnectionResult")
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const view = mounted();

    renderOAuth(view, "result-one");
    await vi.waitFor(() => expect(consume).toHaveBeenCalledWith("result-one"));

    renderOAuth(view, "result-two");
    expect(view.container.textContent).not.toContain("first result failed");
    await vi.waitFor(() => expect(consume).toHaveBeenCalledWith("result-two"));

    await act(async () => {
      first.reject(new Error("first result failed"));
      await first.promise.catch(() => undefined);
      await Promise.resolve();
    });
    expect(view.container.textContent).not.toContain("first result failed");

    await act(async () => {
      second.resolve(oauthResult({ title: "Recovered result" }));
      await second.promise;
    });
    expect(view.container.textContent).toContain("Recovered result");

    view.unmount();
  });
});
