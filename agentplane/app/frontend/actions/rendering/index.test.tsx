// @vitest-environment happy-dom
import { describe, expect, it, vi } from "vitest";
import { create } from "@bufbuild/protobuf";
import { act, useState } from "react";

import {
  ActionIdentitySchema,
  ActionState,
  type ActionIdentity,
} from "../../../../action_service/api/contracts/action_service_pb";
import { CallToolResultView, type CallToolResult } from "../call_tool_result";
import { answerApi, button, mount, SSH_EXEC_ARGUMENTS, sshExec, toolResult } from "../testing/harness";
import { ACTION_NOTIFICATION_FORMATTERS } from "../notifications";
import { executionResult } from "../presentation";
import {
  ACTION_RENDERERS,
  actionCompactCardExpands,
  actionOffersCardDecisionButtons,
  renderActionCall,
  renderActionLabel,
  renderActionOutcome,
  renderDetailsArguments,
  renderDetailsResult,
  renderNativeDetailsResult,
  renderPaneCollapsed,
  renderPaneOpened,
  shouldRenderRequestTitle,
} from "./index";

function identity(group: string, name: string): ActionIdentity {
  return create(ActionIdentitySchema, { group, name });
}

const SSH_EXEC = identity("ssh", "exec");

async function drawDetailsResult(action: ActionIdentity, result: CallToolResult) {
  const custom = renderDetailsResult(action, result);
  return mount(custom === undefined ? <CallToolResultView result={result} /> : custom);
}

/** The CallToolResult stored for an ssh `exec` call whose `ExecResult` has `returned`'s fields. */
function stored(returned: Record<string, unknown> = {}): CallToolResult {
  return toolResult(executionResult(sshExec(ActionState.SUCCEEDED, returned).execution));
}

describe("Action presentation slots", () => {
  it("gives every custom browser Action a push notification format", () => {
    for (const [group, renderers] of ACTION_RENDERERS) {
      const formatters = ACTION_NOTIFICATION_FORMATTERS.get(group);
      expect(
        [...renderers.keys()].filter((name) => !formatters?.has(name)),
        group
      ).toEqual([]);
    }
  });

  it("summarizes supported Home Assistant service calls using the target and effect", async () => {
    const action = identity("home_assistant", "ha_call_service");
    const fan = { domain: "fan", service: "set_percentage", entity_id: "fan.greenhouse", data: { percentage: 35 } };
    expect((await mount(renderActionLabel(action, fan))).textContent).toContain("Set fan.greenhouse to 35%");
    expect((await mount(renderPaneOpened(action, fan))).textContent).toContain("fan.greenhouse");
    expect(actionOffersCardDecisionButtons(action, fan)).toBe(false);
    const generic = { domain: "fan", service: "set_percentage", entity_id: ["fan.greenhouse", "fan.study"] };
    expect((await mount(renderActionLabel(action, generic))).textContent).toContain("Call fan · set percentage");
    expect((await mount(renderActionLabel(action, { ...fan, future: true }))).textContent).toContain(
      "Call Home Assistant service"
    );
  });

  it("folds pod filters without repeating the operation or namespace", async () => {
    const args = {
      namespace: "prod",
      fieldSelector: "status.phase=Running",
      labelSelector: "app=web",
    };
    const action = identity("kubernetes_admin", "pods_list_in_namespace");
    const details = await mount(renderDetailsArguments(action, args));

    expect(renderPaneCollapsed(action, args)).toBeNull();
    expect(details.textContent).toContain("field selector status.phase=Running");
    expect(details.textContent).not.toContain("List pods in namespace");
    expect(details.textContent).not.toContain("namespace prod");
  });

  it("uses a human-facing Action label in place of the technical identity", async () => {
    const action = identity("kubernetes_admin", "pods_list_in_namespace");
    const args = { namespace: "tofu-controller" };
    const heading = renderActionLabel(action, args);
    const label = await mount(heading);

    expect(label.textContent).toContain("List pods in namespace tofu-controller");
    expect(label.textContent).not.toContain("kubernetes_admin");
    expect(label.textContent).not.toContain("pods_list_in_namespace");
    const fallback = await mount(renderActionLabel(action, { namespace: "tofu-controller", hidden: true }));
    expect(fallback.textContent).toContain("List pods in namespace");
    expect(shouldRenderRequestTitle(action, args, "List pods in namespace tofu-controller", heading)).toBe(false);
    expect(shouldRenderRequestTitle(action, args, "Inspect the running demo pods", heading)).toBe(true);
  });

  it("suppresses a caller title matching the displayed text label", () => {
    const action = identity("gmail", "threads_list");
    const args = {};
    const heading = renderActionLabel(action, args);

    expect(heading).toBe("Gmail: Search threads");
    expect(shouldRenderRequestTitle(action, args, "Gmail: Search threads", heading)).toBe(false);
    expect(shouldRenderRequestTitle(action, args, "Search for the release email", heading)).toBe(true);
  });

  it.each([
    ["resources_get", { apiVersion: "v1", kind: "Pod", name: "web-0", namespace: "apps" }, "Get Pod apps/web-0"],
    ["resources_delete", { apiVersion: "v1", kind: "Pod", name: "web-0", namespace: "apps" }, "Delete Pod apps/web-0"],
    ["pods_delete", { name: "web-0", namespace: "apps" }, "Delete Pod apps/web-0"],
    ["pods_exec", { name: "web-0", namespace: "apps", command: ["true"] }, "Run command in Pod apps/web-0"],
    ["pods_get", { name: "web-0", namespace: "apps" }, "Get Pod apps/web-0"],
    ["pods_log", { name: "web-0", namespace: "apps" }, "View logs for Pod apps/web-0"],
  ] as const)("uses the concise Kubernetes identity for %s", async (name, args, expected) => {
    const label = await mount(renderActionLabel(identity("kubernetes_admin", name), args));
    expect(label.textContent).toContain(expected);
    expect(label.textContent).not.toContain("in namespace apps");
  });

  it("keeps a Kubernetes target in its heading and shows only additional details below it", async () => {
    const action = identity("kubernetes_admin", "resources_get");
    const args = { apiVersion: "apps/v1", kind: "Deployment", name: "web", namespace: "apps" };
    const label = await mount(renderActionLabel(action, args));
    const opened = await mount(renderPaneOpened(action, args));
    const details = await mount(renderDetailsArguments(action, args));

    expect(label.textContent).toContain("Get Deployment apps/web");
    expect(opened.textContent).toContain("Read with API version apps/v1");
    expect(details.textContent).toContain("Read with API version apps/v1");
    expect(details.textContent).not.toContain("Get Deployment");
    expect(details.textContent).not.toContain("apps/web");
  });

  it("keeps the target in the label without adding namespace boilerplate", async () => {
    const action = identity("kubernetes_admin", "resources_get");
    const args = { apiVersion: "v1", kind: "Pod", name: "web-0" };
    const opened = await mount(renderPaneOpened(action, args));
    const details = await mount(renderDetailsArguments(action, args));
    const label = await mount(renderActionLabel(action, { ...args, namespace: "apps" }));
    const suppliedNamespace = await mount(renderPaneOpened(action, { ...args, namespace: "apps" }));

    expect(opened.textContent).toContain("Read with API version v1");
    expect(details.textContent).toContain("Read with API version v1");
    expect(opened.textContent).not.toContain("Namespace");
    expect(details.textContent).not.toContain("Namespace");
    expect(suppliedNamespace.textContent).not.toContain("Namespace");
    expect(label.textContent).toContain("apps/web-0");
    expect(actionOffersCardDecisionButtons(action, { ...args, namespace: "" })).toBe(false);
  });

  it("keeps Kubernetes list scope visible and folds narrowing selectors", async () => {
    const listArgs = { apiVersion: "v1", kind: "Pod", namespace: "apps", labelSelector: "app=web" };
    const listAction = identity("kubernetes_admin", "resources_list");
    const listLabel = await mount(renderActionLabel(listAction, listArgs));
    const listCollapsed = await mount(renderPaneCollapsed(listAction, listArgs));
    const listPane = await mount(renderPaneOpened(listAction, listArgs));
    expect(listLabel.textContent).toContain("List Pod resources in namespace apps");
    expect(listCollapsed.textContent).not.toContain("label selector app=web");
    expect(listCollapsed.textContent).toContain("Using API version v1");
    expect(listCollapsed.textContent).not.toContain("apps");
    expect(listPane.textContent).toContain("label selector app=web");
    expect(listPane.textContent).toContain("Using API version v1");
    expect(listPane.textContent).not.toContain("apps");

    const eventsArgs = { namespace: "apps", fieldSelector: "type=Warning" };
    const eventsAction = identity("kubernetes_admin", "events_list");
    const eventsLabel = await mount(renderActionLabel(eventsAction, eventsArgs));
    const eventsOpened = await mount(renderPaneOpened(eventsAction, eventsArgs));
    expect(eventsLabel.textContent).toContain("List events in namespace apps");
    expect(renderPaneCollapsed(eventsAction, eventsArgs)).toBeNull();
    expect(eventsOpened.textContent).toContain("type=Warning");
    expect(eventsOpened.textContent).not.toContain("apps");
    expect(renderPaneCollapsed(eventsAction, { namespace: "apps" })).toBeNull();
  });

  it("names a cluster-scoped delete by its kind and name", async () => {
    const action = identity("kubernetes_admin", "resources_delete");
    const args = { apiVersion: "v1", kind: "PersistentVolume", name: "test-volume" };
    expect((await mount(renderActionLabel(action, args))).textContent).toContain("Delete PersistentVolume test-volume");
    expect((await mount(renderPaneOpened(action, args))).textContent).toContain("Use API version v1");
    expect(actionOffersCardDecisionButtons(action, args)).toBe(true);
  });

  it.each([
    [{ container: "sidecar", tail: 10 }, "View logs for Pod apps/web-0, container sidecar", "Read last 10 lines"],
    [{ previous: true, tail: 60 }, "View logs for Pod apps/web-0, previous run", "Read last 60 lines"],
    [{ previous: false, tail: -1 }, "View logs for Pod apps/web-0", "Read last 100 lines (default)"],
  ])(
    "keeps the log source in the label and folds the line limit: %o",
    async (options, expectedLabel, expectedLimit) => {
      const action = identity("kubernetes_admin", "pods_log");
      const args = { name: "web-0", namespace: "apps", ...options };
      const label = await mount(renderActionLabel(action, args));
      expect(label.textContent).toContain(expectedLabel);
      expect(label.textContent).not.toContain("last");
      expect((await mount(renderPaneOpened(action, args))).textContent).toContain(expectedLimit);
      expect(actionCompactCardExpands(action, args)).toBe(true);
    }
  );

  it("shows calendar context and explicit delete grace in the collapsed pane", async () => {
    const calendarAction = identity("tana", "get_or_create_calendar_node");
    const args = { workspaceId: "workspace-123", granularity: "week", date: "2026-10-12" };
    const calendar = await mount(renderPaneCollapsed(calendarAction, args));
    expect(calendar.textContent).toContain("week containing");
    expect(calendar.textContent).toContain("2026-10-12");
    expect(calendar.textContent).toContain("workspace-123");
    expect(actionOffersCardDecisionButtons(calendarAction, args)).toBe(true);
    const unknown = { ...args, mode: "unknown" };
    expect(actionOffersCardDecisionButtons(calendarAction, unknown)).toBe(false);
    expect(actionCompactCardExpands(calendarAction, unknown)).toBe(true);

    const deletion = await mount(
      renderPaneCollapsed(identity("kubernetes_admin", "resources_delete"), {
        apiVersion: "v1",
        kind: "Pod",
        name: "web-0",
        namespace: "apps",
        gracePeriodSeconds: 0,
      })
    );
    expect(deletion.textContent).toContain("Request a 0s grace period");
    expect(deletion.textContent).not.toContain("Use API version v1");
    expect(deletion.textContent).not.toContain("apps/web-0");
  });

  it("puts the SSH target in the label and the command in the pane", async () => {
    const label = await mount(renderActionLabel(SSH_EXEC, SSH_EXEC_ARGUMENTS));
    const collapsed = await mount(renderPaneCollapsed(SSH_EXEC, SSH_EXEC_ARGUMENTS));
    const opened = await mount(renderPaneOpened(SSH_EXEC, SSH_EXEC_ARGUMENTS));
    const details = await mount(renderDetailsArguments(SSH_EXEC, SSH_EXEC_ARGUMENTS));

    expect(label.textContent).toContain("Run command on test-user@test-host.example");
    expect(collapsed.textContent).toContain("echo test-output");
    expect(collapsed.textContent).not.toContain("test-host.example");
    expect(opened.textContent).toContain("echo test-output");
    expect(details.textContent).toContain("echo test-output");
    expect(details.textContent).not.toContain("test-host.example");
  });

  it("marks an SSH command that runs as root", async () => {
    const label = await mount(renderActionLabel(SSH_EXEC, { ...SSH_EXEC_ARGUMENTS, user: "root" }));
    expect(label.querySelector('[title="Runs as root"]')?.textContent).toBe("root@test-host.example");
    const unprivileged = await mount(renderActionLabel(SSH_EXEC, SSH_EXEC_ARGUMENTS));
    expect(unprivileged.querySelector('[title="Runs as root"]')).toBeNull();
  });

  it("renders Gmail drafts from submitted data without repeating their subject", async () => {
    const action = identity("gmail", "drafts_create");
    const args = {
      to: ["reader@example.com"],
      cc: ["copy@example.com"],
      subject: "Release notes",
      body: "The release is ready.\nPlease review it.",
      thread_id: "thread-123",
    };
    const label = await mount(renderActionLabel(action, args));
    const collapsed = await mount(renderPaneCollapsed(action, args));
    const opened = await mount(renderPaneOpened(action, args));
    const details = await mount(renderDetailsArguments(action, args));

    expect(label.textContent).toContain("Gmail: Create unsent draft");
    expect(collapsed.textContent).toContain("reader@example.com");
    expect(collapsed.textContent).toContain("Release notes");
    expect(collapsed.textContent).toContain("The release is ready.");
    expect(opened.textContent).toContain("copy@example.com");
    expect(opened.textContent).toContain("The release is ready.");
    expect(details.textContent).toContain("Please review it.");
    expect(details.textContent).toContain("Release notes");

    const result = toolResult({ content: [], structuredContent: { id: "draft-123" }, isError: false });
    const renderedResult = await mount(renderDetailsResult(action, result));
    expect(renderedResult.textContent).toContain("Saved as an unsent draft");
    expect(
      renderedResult.querySelector('a[href="https://mail.google.com/mail/u/0/#drafts?compose=draft-123"]')
    ).not.toBeNull();
  });

  it("keeps a Gmail draft's request and created draft in one call view", async () => {
    const action = identity("gmail", "drafts_create");
    const args = {
      to: ["reader@example.com"],
      cc: ["copy@example.com"],
      bcc: ["blind-copy@example.com"],
      subject: "Release notes",
      body: "The release is ready.\nPlease review it.",
      thread_id: "thread-123",
    };
    const pending = renderActionCall(action, args);
    expect(pending?.includesResult).toBe(false);
    const pendingView = await mount(pending?.content ?? null);
    expect(pendingView.textContent).toContain("Release notes");
    expect(pendingView.textContent).toContain("reader@example.com");
    expect(pendingView.textContent).toContain("blind-copy@example.com");
    expect(pendingView.textContent).toContain("Please review it.");
    expect(pendingView.querySelector('a[href="https://mail.google.com/mail/u/0/#all/thread-123"]')).not.toBeNull();

    const result = toolResult({
      content: [],
      structuredContent: {
        id: "draft-123",
        message: { payload: { headers: [{ name: "Subject", value: "Release notes saved" }] } },
      },
      isError: false,
    });
    const completed = renderActionCall(action, args, result);
    expect(completed?.includesResult).toBe(true);
    const completedView = await mount(completed?.content ?? null);
    expect(completedView.textContent).toContain("Release notes saved");
    expect(completedView.textContent).toContain("reader@example.com");
    expect(completedView.textContent).toContain("The release is ready.");
    expect(completedView.textContent).toContain("Saved as an unsent draft");
    expect(completedView.textContent).not.toContain("draft-123");
    expect(
      completedView.querySelector('a[href="https://mail.google.com/mail/u/0/#drafts?compose=draft-123"]')
    ).not.toBeNull();
    expect(renderActionCall(action, { ...args, unexpected: true })).toBeUndefined();

    const unexpectedResult = toolResult({
      content: [],
      structuredContent: { id: 123 },
      isError: false,
    });
    expect(renderActionCall(action, args, unexpectedResult)).toBeUndefined();
  });

  it("shows Gmail search scope and continuation without exposing the cursor", async () => {
    const action = identity("gmail", "threads_list");
    const args = { q: "from:alerts@example.com", labelIds: ["INBOX"], maxResults: 5, includeSpamTrash: false };
    const label = await mount(renderActionLabel(action, args));
    const collapsed = await mount(renderPaneCollapsed(action, args));
    const details = await mount(renderDetailsArguments(action, args));

    expect(label.textContent).toContain("Gmail: Search threads");
    expect(collapsed.textContent).toContain("from:alerts@example.com");
    expect(collapsed.textContent).not.toContain("Up to 5 threads");
    expect(collapsed.textContent).not.toContain("Inbox");
    expect(collapsed.textContent).not.toContain("Includes spam and trash");
    expect(details.textContent).toContain("Up to 5 threads");
    expect(details.textContent).toContain("Inbox");
    expect(actionCompactCardExpands(action, args)).toBe(true);

    const includingSpamAndTrash = await mount(
      renderPaneCollapsed(action, { q: "from:alerts@example.com", includeSpamTrash: true })
    );
    expect(includingSpamAndTrash.textContent).toContain("Includes spam and trash");
    const continuationPage = await mount(
      renderPaneCollapsed(action, { q: "from:alerts@example.com", pageToken: "secret-token" })
    );
    expect(continuationPage.textContent).toContain("Continues a previous search");
    expect(continuationPage.textContent).not.toContain("secret-token");

    const result = toolResult({
      content: [],
      structuredContent: {
        threads: [{ id: "thread-1", snippet: "Your build completed." }],
        nextPageToken: "next",
      },
      isError: false,
    });
    const renderedResult = await mount(renderDetailsResult(action, result));
    expect(renderedResult.textContent).toContain("Your build completed.");
    expect(renderedResult.textContent).toContain("1 thread returned");
    expect(renderedResult.textContent).toContain("More threads available");
    expect(renderedResult.querySelector('a[href="https://mail.google.com/mail/u/0/#all/thread-1"]')).not.toBeNull();
  });

  it.each(["messages_get", "threads_get"])(
    "resolves custom labels in the combined and standalone Gmail %s results",
    async (name) => {
      const action = identity("gmail", name);
      const requestId = "70000000-0000-4000-8000-000000000007";
      const path = `/upstream/actions/v1/operator/action-requests/${requestId}/gmail-resource-summaries`;
      const sent = answerApi((request) =>
        request.path === path
          ? Response.json({ labels: [{ id: "Label_19", title: "Deployments/Reviewed" }] })
          : undefined
      );
      const message = { id: "message-1", labelIds: ["INBOX", "Label_19", "Label_99"] };
      const value = name === "messages_get" ? message : { id: "thread-1", messages: [message] };
      const args = { id: value.id };
      const receipt: CallToolResult = { content: [], structuredContent: value, isError: false };

      for (const preview of [
        renderActionCall(action, args, receipt, requestId)?.content,
        renderDetailsResult(action, receipt, { requestId, arguments: args }),
      ]) {
        const view = await mount(preview);
        await vi.waitFor(() => expect(view.textContent).toContain("Inbox, Deployments/Reviewed, Label_99"));
        expect(view.textContent).not.toContain("Label_19");
      }
      expect(sent.map((request) => [request.method, request.path, request.query.toString(), request.body])).toEqual([
        ["GET", path, "", undefined],
        ["GET", path, "", undefined],
      ]);
    }
  );

  it("fetches saved Gmail labels after a cached pending message review becomes a receipt", async () => {
    const action = identity("gmail", "messages_get");
    const requestId = "70000000-0000-4000-8000-000000000007";
    const path = `/upstream/actions/v1/operator/action-requests/${requestId}/gmail-resource-summaries`;
    let completed = false;
    const sent = answerApi((request) =>
      request.path === path
        ? Response.json(
            completed
              ? { labels: [{ id: "Label_19", title: "Deployments/Reviewed" }] }
              : { messages: [{ id: "message-1", title: "Deployment approval" }] }
          )
        : undefined
    );
    const args = { id: "message-1" };
    const receipt: CallToolResult = {
      content: [],
      structuredContent: { id: "message-1", labelIds: ["Label_19"] },
      isError: false,
    };
    function Review() {
      const [result, setResult] = useState<CallToolResult>();
      return (
        <>
          <button onClick={() => setResult(receipt)}>Show receipt</button>
          {renderActionCall(action, args, result, requestId)?.content}
        </>
      );
    }

    const view = await mount(<Review />);
    await vi.waitFor(() => expect(view.textContent).toContain("Deployment approval"));
    completed = true;
    await act(async () => button(view, "Show receipt").click());
    await vi.waitFor(() => expect(view.textContent).toContain("Deployments/Reviewed"));
    expect(sent.map((request) => request.path)).toEqual([path, path]);
  });

  it("shows a Gmail filter's exceptions and forwarding destination before approval", async () => {
    const action = identity("gmail", "filters_create");
    const args = {
      criteria: { subject: "Staging", negatedQuery: "is:starred", hasAttachment: true },
      action: { addLabelIds: ["STARRED"], removeLabelIds: ["INBOX"], forward: "reviewer@example.com" },
    };
    const pane = await mount(renderPaneOpened(action, args));
    expect(pane.textContent).toContain("Excludes query is:starred");
    expect(pane.textContent).toContain("Forward to reviewer@example.com");
    expect(pane.textContent).toContain("Add labels Starred");
    expect(pane.textContent).toContain("Remove labels Inbox");
    expect(actionOffersCardDecisionButtons(action, args)).toBe(false);
  });

  it("acknowledges a Gmail deletion only for a successful empty result", async () => {
    const action = identity("gmail", "labels_delete");
    const empty = { content: [], isError: false };
    const receipt = await mount(renderDetailsResult(action, empty));
    expect(receipt.textContent).toContain("Label deleted.");
    expect(renderDetailsResult(action, { ...empty, isError: true })).toBeUndefined();
    expect(
      renderDetailsResult(action, { ...empty, structuredContent: { error: "unexpected variant" } })
    ).toBeUndefined();
  });

  it("shows the provider's partial relabel failure without an applied receipt", async () => {
    const action = identity("gmail", "threads_modify_labels");
    const failure = toolResult({
      content: [{ type: "text", text: "failed to modify 1/2 thread(s) (1 sub-exception)" }],
      isError: true,
    });
    const receipt = await drawDetailsResult(action, failure);
    expect(receipt.textContent).toContain("failed to modify 1/2 thread(s)");
    expect(receipt.textContent).not.toContain("Updated labels");
  });

  it("shows named Grocy list links without duplicate numeric IDs or detail-mode copy", async () => {
    for (const [name, args, label, article, path] of [
      ["products_list", { detail: "full" }, "List Grocy products", "Pumpkin", "product"],
      ["quantity_units_list", {}, "List quantity units", "Can", "quantityunit"],
    ] as const) {
      const action = identity("grocy_sf", name);
      expect((await mount(renderPaneOpened(action, args))).querySelector("p")).toBeNull();
      expect((await mount(renderDetailsArguments(action, args))).querySelector("p")).toBeNull();
      expect((await mount(renderActionLabel(action, args))).textContent).toContain(label);
      const receipt = await drawDetailsResult(action, {
        content: [],
        structuredContent: [{ id: 734, name: article }],
        isError: false,
      });
      expect(receipt.querySelector(`a[href="https://grocy-sf.allegedly.works/${path}/734"]`)?.textContent).toBe(
        article
      );
      expect(receipt.textContent).not.toContain("#734");
    }
  });

  it("shows submitted Grocy edit changes, explicit clears, and each recorded outcome", async () => {
    const action = identity("grocy_sf", "products_edit");
    const args = {
      items: [
        { product: "Oats", name: "Rolled oats", location: "Pantry", clear_fields: ["description"] },
        { product: "Muesli", min_stock_amount: 0 },
      ],
    };
    const result: CallToolResult = {
      content: [],
      structuredContent: [
        { kind: "ok", object_id: 2 },
        { kind: "error", error: "Product not found" },
      ],
      isError: false,
    };
    expect(renderActionOutcome(action, result)).toEqual({ label: "Partially completed", failed: true });
    const rendered = await mount(renderActionCall(action, args, result)?.content);
    expect(rendered.textContent).toContain("Description · Clear");
    expect(rendered.textContent).toContain("Default location · Pantry");
    expect(rendered.textContent).toContain("Disable low-stock threshold");
    expect(rendered.textContent).toContain("Failed · Muesli · Product not found");
    expect(rendered.querySelector('a[href="https://grocy-sf.allegedly.works/product/2"]')?.textContent).toBe(
      "Rolled oats"
    );
  });

  it("names removed Grocy items only when the recorded successful result matches the requested ID", async () => {
    const action = identity("grocy_sf", "shopping_list_items_remove");
    const result = {
      content: [],
      structuredContent: [
        { kind: "ok", item_id: 52, product_name: "Kefir", amount: 2, qu_name: "Bottle" },
        { kind: "error", error: "Item missing" },
        { kind: "ok", item_id: 88, product_name: "Bananas", amount: 3, qu_name: null },
      ],
      isError: false,
    };
    const args = { item_ids: [51, 52, 53] };
    expect(renderActionCall(action, args)?.content).toBeNull();
    const rendered = await mount(renderActionCall(action, args, result)?.content);
    expect(rendered.textContent).toContain("Kefir · 2 Bottle");
    expect(rendered.textContent).not.toContain("Shopping item #52");
    expect(rendered.textContent).toContain("Shopping item #51");
    expect(rendered.textContent).toContain("Shopping item #53");
    expect(rendered.textContent).toContain("Failed · Item missing");
  });

  it("shows total inventory stock with the opened subset and matched location", async () => {
    const action = identity("grocy_sf", "stock_get");
    const args = { products: ["Kefir"], locations: ["Fridge"] };
    expect((await mount(renderDetailsArguments(action, args))).textContent).toContain("Locations · Fridge");
    const view = await drawDetailsResult(action, {
      content: [],
      structuredContent: [
        {
          product_id: 21,
          product_name: "Kefir",
          amount: 3,
          amount_opened: 1,
          qu_name: "Bottle",
          location_name: "Fridge",
          best_before_date: "2026-11-25",
        },
      ],
      isError: false,
    });
    expect(view.textContent).toContain("Kefir · 3 Bottle in Fridge");
    expect(view.textContent).toContain("1 opened");
    expect(view.textContent).toContain("Best before 2026-11-25");
  });

  it("shows unexpected Grocy entity types and fields as recorded text", async () => {
    const action = identity("grocy_sf", "entity_update");
    const view = await mount(
      renderDetailsArguments(action, {
        entity: "constructor",
        objectId: 7,
        body: { constructor: "Material value" },
      })
    );
    expect(view.textContent).toContain("constructor #7");
    expect(view.textContent).toContain("constructor · Material value");
    expect(view.querySelector("a")).toBeNull();
  });

  it("resolves pending Grocy targets by saved request and keeps an unresolved ID link", async () => {
    const id = "70000000-0000-4000-8000-000000000007";
    const path = `/upstream/actions/v1/operator/action-requests/${id}/grocy-reference-summaries`;
    const sent = answerApi((request) =>
      request.path === path ? Response.json({ references: [{ key: "products/11", name: "Coffee" }] }) : undefined
    );
    const action = identity("grocy_sf", "products_merge");
    const args = { productIdToKeep: 11, productIdToRemove: 22 };
    const receipt = await mount(renderDetailsArguments(action, args, id));
    expect(receipt.textContent).toContain("Product #11");
    expect(sent).toEqual([]);

    const view = await mount(renderDetailsArguments(action, args, id, true));
    await vi.waitFor(() => {
      expect(view.querySelector('a[href="https://grocy-sf.allegedly.works/product/11"]')?.textContent).toBe("Coffee");
    });
    expect(view.querySelector('a[href="https://grocy-sf.allegedly.works/product/22"]')?.textContent).toBe(
      "Product #22"
    );
    expect(view.textContent).not.toContain("Product #11");
    expect(sent.map((request) => [request.method, request.path, request.query.toString(), request.body])).toEqual([
      ["GET", path, "", undefined],
    ]);
  });

  it("shows Grocy 204 acknowledgements only for successful empty receipts", async () => {
    const action = identity("grocy_sf", "transaction_undo");
    const empty: CallToolResult = { content: [], isError: false };
    expect((await mount(renderDetailsResult(action, empty))).textContent).toContain("Transaction undone");
    expect(renderDetailsResult(action, { ...empty, isError: true })).toBeUndefined();
    expect(
      renderDetailsResult(action, { content: [{ type: "text", text: "Unknown outcome" }], isError: false })
    ).toBeUndefined();
  });

  it("falls back to the recorded Tana workspace ID when a name is unavailable", async () => {
    const action = identity("tana", "get_or_create_calendar_node");
    const args = { workspaceId: "workspace-123", granularity: "week", date: "2026-10-12" };
    const collapsed = await mount(renderPaneCollapsed(action, args));
    const details = await mount(renderDetailsArguments(action, args));
    const label = await mount(renderActionLabel(action, args));

    expect(label.textContent).toContain("Get or create Tana calendar node");

    for (const rendered of [collapsed, details]) {
      expect(rendered.textContent).toContain("week");
      expect(rendered.textContent).toContain("2026-10-12");
      expect(rendered.textContent).toContain("workspace-123");
    }
  });

  it("preserves recorded data for a newly discovered Action", async () => {
    const excluded = [["new_provider", "new_tool"]] as const;
    const result = toolResult({
      content: [],
      structuredContent: { lookup_dependent_value: "preserve raw response" },
      isError: false,
    });

    for (const [group, name] of excluded) {
      const action = identity(group, name);
      expect(renderActionLabel(action, { id: "opaque-id" })).toBeUndefined();
      expect(renderPaneCollapsed(action, { id: "opaque-id" })).toBeUndefined();
      expect(renderPaneOpened(action, { id: "opaque-id" })).toBeUndefined();
      expect(renderDetailsArguments(action, { id: "opaque-id" })).toBeUndefined();

      const raw = await drawDetailsResult(action, result);
      expect(raw.textContent).toContain("Structured content");
      expect(raw.textContent).toContain('"lookup_dependent_value": "preserve raw response"');
    }
  });

  it("shows Gmail label changes and recorded thread IDs while live subjects are unavailable", async () => {
    const action = identity("gmail", "threads_modify_labels");
    const details = await mount(
      renderDetailsArguments(action, { thread_ids: ["thread-1", "thread-2"], add: ["Reviewed"], remove: ["Inbox"] })
    );
    expect(details.textContent).toContain("Add Reviewed");
    expect(details.textContent).toContain("Remove Inbox");
    expect(details.textContent).toContain("thread-1");
    expect(details.textContent).toContain("thread-2");
  });

  it("shows Tana placement and recorded node IDs while live names are unavailable", async () => {
    const action = identity("tana", "move_node");
    const details = await mount(
      renderDetailsArguments(action, {
        nodeId: "draft",
        targetNodeId: "project",
        sourceParentId: "inbox",
        position: "after",
        referenceNodeId: "checklist",
        keepSourceReference: true,
      })
    );
    expect(details.textContent).toContain(
      "Move draft from inbox after checklist in project, keeping the source reference."
    );
  });

  it("renders confirmed Tana move links from recorded text without printing IDs twice", async () => {
    const action = identity("tana", "move_node");
    const text =
      'Moved "Draft notes" from "Inbox" to "Website project" (start)\n\n' +
      "Node ID: draft-123\nPrevious parent: Inbox (inbox-456)\nNew parent: Website project (project-789)\nPosition: start";
    const content = [{ type: "text", text }] as const;
    const result = await drawDetailsResult(action, { content: [...content], isError: false });
    expect(result.textContent).toContain("Moved Draft notes from Inbox to Website project (start).");
    expect(result.textContent).not.toContain("project-789");
    expect(result.querySelector('a[href="https://app.tana.inc?nodeid=draft-123"]')?.textContent).toBe("Draft notes");
    expect(result.querySelector('a[href="https://app.tana.inc?nodeid=inbox-456"]')?.textContent).toBe("Inbox");
    expect(result.querySelector('a[href="https://app.tana.inc?nodeid=project-789"]')?.textContent).toBe(
      "Website project"
    );

    expect(
      renderDetailsResult(action, { content: [...content, { type: "text", text: "Extra output" }], isError: false })
    ).toBeUndefined();
    expect(renderDetailsResult(action, { content: [...content], isError: true })).toBeUndefined();
    expect(
      renderDetailsResult(action, { content: [{ type: "text", text: "New receipt format" }], isError: false })
    ).toBeUndefined();
  });

  it("links the returned Tana calendar node without claiming it was created", async () => {
    const action = identity("tana", "get_or_create_calendar_node");
    const result: CallToolResult = {
      content: [{ type: "text", text: '{"nodeId":"calendar-week-42"}' }],
      isError: false,
    };
    const receipt = await mount(
      renderDetailsResult(action, result, {
        requestId: "70000000-0000-4000-8000-000000000007",
        arguments: { workspaceId: "workspace-123", granularity: "week", date: "2026-10-12" },
      })
    );
    expect(receipt.querySelector("a")?.textContent).toBe("Calendar node for week containing 2026-10-12");
    expect(receipt.querySelector("a")?.getAttribute("href")).toBe("https://app.tana.inc?nodeid=calendar-week-42");
  });

  it("uses the provider's per-tag outcomes instead of treating a partial update as success", async () => {
    const action = identity("tana", "tag");
    const result: CallToolResult = {
      content: [
        {
          type: "text",
          text: 'Adding tags on "Plan review" (task-1):\n✓ Task: Added tag "Task"\n✗ missing: Tag not found: missing',
        },
      ],
      isError: false,
    };
    expect(renderActionOutcome(action, result)).toEqual({ label: "Partially applied", failed: true });
    const view = await mount(renderDetailsResult(action, result));
    expect(view.textContent).toContain('Added tag "Task"');
    expect(view.textContent).toContain("Tag not found: missing");
    expect(view.querySelector('a[href="https://app.tana.inc?nodeid=task-1"]')?.textContent).toBe("Plan review");
  });

  it("links a returned workspace by its home node and represents out-of-range child pages", async () => {
    const workspaces = await drawDetailsResult(identity("tana", "list_workspaces"), {
      content: [{ type: "text", text: '[{"id":"workspace-1","name":"Product","homeNodeId":"home-1"}]' }],
      isError: false,
    });
    expect(workspaces.querySelector('a[href="https://app.tana.inc?nodeid=home-1"]')?.textContent).toBe("Product");
    expect(workspaces.textContent).not.toContain("workspace-1");

    const children = await drawDetailsResult(identity("tana", "get_children"), {
      content: [{ type: "text", text: '{"children":[],"total":2,"hasMore":false}' }],
      isError: false,
    });
    expect(children.textContent).toContain("No child nodes on this page");
  });

  it("leaves a rejected Tana edit in the tool error view and preserves unexpected receipts", async () => {
    const action = identity("tana", "edit_node");
    const error: CallToolResult = {
      content: [{ type: "text", text: "Name edit failed: old_string not found in current name" }],
      isError: true,
    };
    expect(renderDetailsResult(action, error)).toBeUndefined();
    const failed = await drawDetailsResult(action, error);
    expect(failed.textContent).toContain("old_string not found in current name");

    const unknown: CallToolResult = {
      content: [{ type: "text", text: "Future edit result shape" }],
      isError: false,
    };
    expect(renderDetailsResult(action, unknown)).toBeUndefined();
    expect((await drawDetailsResult(action, unknown)).textContent).toContain("Future edit result shape");
  });

  it("summarizes a Gmail draft with its recipient, subject, and body preview", async () => {
    const collapsed = await mount(
      renderPaneCollapsed(identity("gmail", "drafts_create"), {
        to: ["user@example.com"],
        subject: "Deploy",
        body: "Ready",
      })
    );
    expect(collapsed.textContent).toContain("To user@example.com");
    expect(collapsed.textContent).toContain("Deploy");
    expect(collapsed.textContent).toContain("Ready");
  });

  it("leaves unregistered slots to the host and preserves arguments rejected by a strict schema", async () => {
    expect(renderPaneCollapsed(SSH_EXEC, { ...SSH_EXEC_ARGUMENTS, test_extra: true })).toBeUndefined();
    expect(renderPaneOpened(identity("test_group", "exec"), SSH_EXEC_ARGUMENTS)).toBeUndefined();
    const recorded = await mount(renderDetailsArguments(SSH_EXEC, { ...SSH_EXEC_ARGUMENTS, test_extra: true }));
    expect(recorded.textContent).toContain("Details shown as recorded data");
    expect(renderDetailsArguments(identity("__proto__", "constructor"), {})).toBeUndefined();
  });

  it("shows recorded Kubernetes arguments when the strict schema does not accept them", async () => {
    const action = identity("kubernetes_admin", "resources_get");
    const args = { apiVersion: "v1", kind: "Pod", name: "web-0", namespace: "apps", futureField: true };
    const label = await mount(renderActionLabel(action, args));
    const details = await mount(renderDetailsArguments(action, args));

    expect(label.textContent).toContain("Get Kubernetes resource");
    expect(details.textContent).toContain("Details shown as recorded data");
    expect(details.textContent).toContain('"futureField": true');
    expect(actionOffersCardDecisionButtons(action, args)).toBe(false);
  });

  it("shows pod-deletion scope before approval and keeps unknown arguments visible", async () => {
    const action = identity("kubernetes_admin", "pods_delete");
    const args = { name: "web-0", namespace: "apps" };
    expect((await mount(renderDetailsArguments(action, args))).textContent).toContain(
      "matching Services and OpenShift Routes"
    );
    const collapsed = await mount(renderPaneCollapsed(action, args));
    expect(collapsed.textContent).toContain("matching Services");
    expect(collapsed.textContent).not.toContain("OpenShift");

    const recorded = await mount(
      renderDetailsArguments(action, { name: "web-0", namespace: "apps", futureField: true })
    );
    expect(recorded.textContent).toContain("Details shown as recorded data");
    expect(recorded.textContent).toContain('"futureField": true');
  });

  it("uses the opened-pane argument widget for resources_get when optional fields are omitted", async () => {
    const container = await mount(
      renderPaneOpened(identity("kubernetes_admin", "resources_get"), {
        apiVersion: "apps/v1",
        kind: "Deployment",
        name: "api",
      })
    );
    expect(container.textContent).toContain("Read with API version apps/v1");
  });
});

describe("details result rendering", () => {
  it("draws the tool's value with the Action's widget", async () => {
    const container = await mount(renderDetailsResult(SSH_EXEC, stored()));
    expect(container.textContent).toContain("Exit 0");
    expect(container.textContent).not.toContain("Structured content");
  });

  it("draws the result as the tool answered when the widget does not take its value", async () => {
    const container = await drawDetailsResult(SSH_EXEC, stored({ exit_code: "test-not-a-number" }));
    expect(container.textContent).toContain("Structured content");
    expect(container.textContent).toContain('"exit_code": "test-not-a-number"');
    expect(container.textContent).not.toContain("Exit");
  });

  it("draws an error result as the tool answered", async () => {
    const failure = { kind: "target_not_configured", message: "SSH host/user target is not configured" };
    const result = toolResult({ content: [{ type: "text", text: JSON.stringify(failure) }], isError: true });
    const container = await drawDetailsResult(SSH_EXEC, result);
    expect(container.textContent).toContain("Tool error");
    expect(container.textContent).toContain('"kind": "target_not_configured"');
  });

  it("draws an unregistered Action's result as the tool answered", async () => {
    const container = await drawDetailsResult(identity("test_group", "exec"), stored());
    expect(container.textContent).toContain("Structured content");
    expect(container.textContent).not.toContain("Exit 0");
  });

  it("formats Kubernetes YAML result text with its structured value only once", async () => {
    const action = identity("kubernetes_admin", "resources_get");
    const resource = {
      apiVersion: "v1",
      kind: "ConfigMap",
      metadata: { name: "settings", namespace: "apps" },
      data: { mode: "safe" },
    };
    const result = toolResult({
      content: [
        {
          type: "text",
          text: "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\n  namespace: apps\ndata:\n  mode: safe",
        },
      ],
      structuredContent: resource,
      isError: false,
    });

    const container = await mount(renderDetailsResult(action, result));
    expect(container.textContent).toContain("ConfigMap apps/settings");
    expect(container.querySelector(".cm-editor")).toBeNull();
    await act(async () => button(container, "Resource YAML").click());
    expect(container.textContent?.match(/kind: ConfigMap/g)).toHaveLength(1);
    expect(container.textContent).not.toContain("Structured content");
    expect(container.querySelector(".cm-editor")).not.toBeNull();
  });

  it("formats a single-line YAML apply argument and shows malformed input verbatim", async () => {
    const action = identity("kubernetes_admin", "resources_create_or_update");
    const manifest =
      "{apiVersion: v1, kind: ConfigMap, metadata: {name: settings, namespace: apps}, data: {mode: safe}}";
    const formatted = await mount(renderDetailsArguments(action, { resource: manifest }));
    expect(formatted.textContent).toContain("Manifest · ConfigMap apps/settings");
    expect(formatted.textContent).toContain("apiVersion: v1");
    expect(formatted.querySelector(".cm-editor")).not.toBeNull();

    const malformed = await mount(renderDetailsArguments(action, { resource: "kind: [broken" }));
    expect(malformed.textContent).toContain("Manifest · showing submitted text");
    expect(malformed.textContent).toContain("kind: [broken");
  });

  it("names applied resources in the label", async () => {
    const action = identity("kubernetes_admin", "resources_create_or_update");
    const label = async (resource: string) => (await mount(renderActionLabel(action, { resource }))).textContent;

    expect(await label("{apiVersion: v1, kind: ConfigMap, metadata: {name: settings, namespace: apps}}")).toContain(
      "Apply ConfigMap apps/settings"
    );
    expect(
      await label(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\n  namespace: apps\n---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: token\n  namespace: apps\n"
      )
    ).toContain("Apply 2 Kubernetes resources: ConfigMap apps/settings, Secret apps/token");
    expect(await label("apiVersion: v1\nkind: Service\n")).toContain("Apply Service");
    expect(await label("kind: [broken")).toContain("Apply Kubernetes resource");
  });

  it.each([
    [
      "lists a few short fields",
      "{apiVersion: v1, kind: PersistentVolume, metadata: {name: test-volume}, spec: {persistentVolumeReclaimPolicy: Delete}}",
      "spec.persistentVolumeReclaimPolicy: Delete",
    ],
    [
      "counts many fields by group",
      "{apiVersion: v1, kind: ConfigMap, metadata: {name: settings, labels: {a: x, b: y}}, data: {c: z, d: w}}",
      "Sets 4 fields under metadata.labels, data",
    ],
    [
      "counts a multi-line field",
      '{apiVersion: v1, kind: ConfigMap, metadata: {name: settings}, data: {script: "line one\\nline two"}}',
      "Sets 1 field under data",
    ],
  ])("summarizes what an apply sets: %s", async (_, resource, expected) => {
    const action = identity("kubernetes_admin", "resources_create_or_update");
    const collapsed = await mount(renderPaneCollapsed(action, { resource }));
    expect(collapsed.textContent).toContain(expected);
    expect(collapsed.textContent).not.toContain("settings");
    expect(collapsed.textContent).not.toContain("test-volume");
  });

  it("leaves the collapsed row to the label for several resources or bare identities", () => {
    const action = identity("kubernetes_admin", "resources_create_or_update");
    expect(
      renderPaneCollapsed(action, {
        resource: "apiVersion: v1\nkind: ConfigMap\n---\napiVersion: v1\nkind: Secret\ndata: {a: b}\n",
      })
    ).toBeNull();
    expect(
      renderPaneCollapsed(action, { resource: "{apiVersion: v1, kind: Namespace, metadata: {name: x}}" })
    ).toBeNull();
    expect(renderPaneCollapsed(action, { resource: "kind: [broken" })).toBeUndefined();
  });

  it("renders a returned YAML sequence as separate Kubernetes resources", async () => {
    const action = identity("kubernetes_admin", "resources_create_or_update");
    const result = toolResult({
      content: [
        {
          type: "text",
          text: [
            "- apiVersion: v1",
            "  kind: ConfigMap",
            "  metadata:",
            "    name: settings",
            "    namespace: apps",
            "- apiVersion: v1",
            "  kind: Secret",
            "  metadata:",
            "    name: token",
            "    namespace: apps",
          ].join("\n"),
        },
      ],
      isError: false,
    });

    const container = await mount(renderDetailsResult(action, result));
    expect(container.textContent).toContain("ConfigMap apps/settings");
    expect(container.textContent).toContain("Secret apps/token");
    expect(renderActionOutcome(action, result)?.label).toBe("Resources applied");
    await act(async () => button(container, "Resource YAML").click());
    expect(container.querySelectorAll(".cm-editor")).toHaveLength(2);
  });

  it("renders the real resource-table envelope and keeps the extra provider columns readable", async () => {
    const action = identity("kubernetes_admin", "resources_list");
    const result = toolResult({
      structuredContent: {
        items: [{ Name: "web-0", Namespace: "apps", Ready: "1/2", Status: "Running", Restarts: "1 (2m ago)" }],
      },
      content: [
        {
          type: "text",
          text: "NAMESPACE   NAME    READY   STATUS    RESTARTS       LABELS\napps        web-0   1/2     Running   1 (2m ago)     app=web\n",
        },
      ],
    });
    const view = await mount(renderDetailsResult(action, result));
    expect(view.textContent).toContain("apps/web-0");
    expect(view.textContent).toContain("1/2 ready");
    await act(async () => button(view, "Provider output").click());
    expect(view.textContent).toContain("app=web");
    expect(
      renderDetailsResult(action, {
        ...result,
        content: [...result.content, { type: "text", text: "separate notice" }],
      })
    ).toBeUndefined();
  });

  it("shows events from the provider's YAML report and handles known empty results", async () => {
    const action = identity("kubernetes_admin", "events_list");
    const report = [
      "# The following events (YAML format) were found:",
      "- InvolvedObject: {apiVersion: v1, Kind: Pod, Name: web-0}",
      "  Message: Back-off restarting failed container",
      "  Namespace: apps",
      "  Reason: BackOff",
      "  Timestamp: 2026-10-12 03:00:00 +0000 UTC",
      "  Type: Warning",
    ].join("\n");
    const view = await mount(
      renderDetailsResult(action, { isError: false, content: [{ type: "text", text: report }] })
    );
    expect(view.textContent).toContain("Pod apps/web-0");
    expect(view.textContent).toContain("BackOff");
    expect(view.textContent).toContain("Warning");
    const empty = await mount(
      renderDetailsResult(action, { isError: false, content: [{ type: "text", text: "# No events found" }] })
    );
    expect(empty.textContent).toContain("No events found.");
  });

  it("keeps plain Pod logs intact even when the contents are JSON", async () => {
    const action = identity("kubernetes_admin", "pods_log");
    const text = '{"level":"info","msg":"Ready"}\n';
    const view = await mount(renderDetailsResult(action, { isError: false, content: [{ type: "text", text }] }));
    expect(view.textContent).toContain("Container logs");
    const lines = [...view.querySelectorAll(".ui-code-block .cm-line")].map((line) => line.textContent);
    expect(lines).toEqual([text.trimEnd(), ""]);
  });

  it("shows an exact delete success as an outcome with no fabricated result body", async () => {
    const action = identity("kubernetes_admin", "resources_delete");
    const args = {
      apiVersion: "v1",
      kind: "Pod",
      name: "web-0",
      namespace: "apps",
      gracePeriodSeconds: 0,
    };
    const success = toolResult({
      content: [{ type: "text", text: "Resource deleted successfully" }],
      isError: false,
    });

    expect(renderDetailsResult(action, success)).toBeNull();
    const call = renderActionCall(action, args, success);
    expect(call?.includesResult).toBe(true);
    expect(renderActionOutcome(action, success)?.label).toBe("Deletion accepted");
    expect((await mount(call?.content)).textContent).toContain("request a 0s grace period");
    const argumentsView = await mount(renderDetailsArguments(action, args));
    expect(argumentsView.textContent).toContain("Use API version v1");
    expect(argumentsView.textContent).toContain("request a 0s grace period");

    const warning = toolResult({
      content: [{ type: "text", text: "Resource deleted successfully, but finalizers are still running" }],
      isError: false,
    });
    expect(renderDetailsResult(action, warning)).toBeUndefined();
    expect(renderActionOutcome(action, warning)).toBeUndefined();
    const warningView = await drawDetailsResult(action, warning);
    expect(warningView.textContent).toContain("finalizers are still running");
    expect(renderActionCall(action, args, warning)).toBeUndefined();
  });

  it("shows an exact pod-delete success as accepted and preserves non-success envelopes", async () => {
    const action = identity("kubernetes_admin", "pods_delete");
    const success = toolResult({
      content: [{ type: "text", text: "Pod deleted successfully" }],
      isError: false,
    });

    expect(renderDetailsResult(action, success)).toBeNull();
    expect(renderActionOutcome(action, success)).toEqual({ label: "Deletion accepted", failed: false });

    const warning = toolResult({
      content: [
        { type: "text", text: "Pod deleted successfully" },
        { type: "text", text: "Warning: cleanup is still running" },
      ],
      isError: false,
    });
    expect(renderDetailsResult(action, warning)).toBeUndefined();
    expect(renderActionOutcome(action, warning)).toBeUndefined();
    const warningView = await drawDetailsResult(action, warning);
    expect(warningView.textContent).toContain("Pod deleted successfully");
    expect(warningView.textContent).toContain("Warning: cleanup is still running");

    const additionalContent = toolResult({
      content: [
        { type: "text", text: "Pod deleted successfully" },
        { type: "future", payload: "keep this content" },
      ],
      structuredContent: "Pod deleted successfully",
      isError: false,
    });
    expect(renderDetailsResult(action, additionalContent)).toBeUndefined();
    expect(renderActionOutcome(action, additionalContent)).toEqual({ label: "Deletion accepted", failed: false });
    const additionalContentView = await drawDetailsResult(action, additionalContent);
    expect(additionalContentView.textContent).toContain("Pod deleted successfully");
    expect(additionalContentView.textContent).toContain("keep this content");

    const mismatch = toolResult({
      content: [{ type: "text", text: "Pod not found" }],
      isError: false,
    });
    expect(renderDetailsResult(action, mismatch)).toBeUndefined();
    expect(renderActionOutcome(action, mismatch)).toBeUndefined();
    expect((await drawDetailsResult(action, mismatch)).textContent).toContain("Pod not found");

    const error = toolResult({
      content: [{ type: "text", text: "Pod deleted successfully" }],
      isError: true,
    });
    expect(renderDetailsResult(action, error)).toBeUndefined();
    expect(renderActionOutcome(action, error)).toBeUndefined();
    const errorView = await drawDetailsResult(action, error);
    expect(errorView.textContent).toContain("Tool error");
    expect(errorView.textContent).toContain("Pod deleted successfully");
  });
});

describe("expanded Kubernetes Action coverage", () => {
  it("distinguishes reading scale from requesting zero replicas, then shows desired and observed counts separately", async () => {
    const action = identity("kubernetes_admin", "resources_scale");
    const args = { apiVersion: "apps/v1", kind: "Deployment", name: "web", namespace: "apps" };
    expect((await mount(renderActionLabel(action, args))).textContent).toContain("Read scale for Deployment apps/web");
    const write = { ...args, scale: 0 };
    expect((await mount(renderActionLabel(action, write))).textContent).toContain("Set desired replicas");
    expect((await mount(renderDetailsArguments(action, write))).textContent).toContain("Desired replicas · 0");
    const text =
      "# Current resource scale (YAML) is below\napiVersion: autoscaling/v1\nkind: Scale\n" +
      "spec: {replicas: 0}\nstatus: {replicas: 2}";
    const receipt = await mount(renderDetailsResult(action, { isError: false, content: [{ type: "text", text }] }));
    expect(receipt.textContent).toContain("Desired replicas · 0");
    expect(receipt.textContent).toContain("Observed replicas · 2");
    expect(receipt.textContent).not.toContain("Ready");
  });

  it("shows Pod run networking scope and waits for returned readiness evidence", async () => {
    const action = identity("kubernetes_admin", "pods_run");
    const details = await mount(renderDetailsArguments(action, { image: "web:v1", port: 8080 }));
    expect(details.textContent).toContain("Expose TCP port 8080 through a ClusterIP Service.");
    expect(details.textContent).not.toContain("OpenShift Route");
    const result: CallToolResult = {
      isError: false,
      content: [
        {
          type: "text",
          text:
            "# The following resources (YAML) have been created or updated successfully\n" +
            "- apiVersion: v1\n  kind: Pod\n  metadata: {name: generated-web}\n  status: {phase: Pending}",
        },
      ],
    };
    expect(renderActionOutcome(action, result)?.label).toBe("Pod applied");
    const receipt = await mount(renderDetailsResult(action, result));
    expect(receipt.textContent).toContain("Pending");
    expect(receipt.textContent).not.toContain("Ready");
  });

  it("shows the effective metrics scope and keeps absent data distinct from zero", async () => {
    const pod = identity("kubernetes_admin", "pods_top");
    expect((await mount(renderActionLabel(pod, { namespace: "apps", all_namespaces: true }))).textContent).toContain(
      "namespace apps"
    );
    expect((await mount(renderActionLabel(pod, {}))).textContent).toContain("all namespaces");
    const receipt = await mount(
      renderDetailsResult(identity("kubernetes_admin", "nodes_stats_summary"), {
        isError: false,
        content: [{ type: "text", text: '{"node":{"nodeName":"worker","cpu":{"usageNanoCores":0}}}' }],
      })
    );
    expect(receipt.textContent).toContain("CPU · 0 cores");
    expect(receipt.textContent).not.toContain("Memory");
  });

  it("keeps credentials and API endpoints out of the friendly configuration summary", async () => {
    const text = [
      "apiVersion: v1",
      "kind: Config",
      "current-context: staging",
      "contexts:",
      "- name: staging",
      "  context: {cluster: staging-cluster, namespace: apps}",
      "clusters: [{name: staging-cluster, cluster: {server: 'https://private.example.test'}}]",
      "users: [{name: operator, user: {token: test-token}}]",
    ].join("\n");
    const receipt = await mount(
      renderDetailsResult(identity("kubernetes_admin", "configuration_view"), {
        isError: false,
        content: [{ type: "text", text }],
      })
    );
    expect(receipt.textContent).toContain("Current context · staging");
    expect(receipt.textContent).toContain("staging-cluster");
    expect(receipt.textContent).not.toContain("private.example.test");
    expect(receipt.textContent).not.toContain("test-token");
  });
});

describe("Home Assistant provider outcomes", () => {
  it("shows recurring calendar deletion scope and preserves unrecognized request fields", async () => {
    const action = identity("home_assistant", "ha_config_remove_calendar_event");
    const args = {
      entity_id: "calendar.service",
      uid: "item-1",
      recurrence_id: "2026-10-12",
      recurrence_range: "THIS_AND_FUTURE",
    };
    expect((await mount(renderActionLabel(action, args))).textContent).toContain("Delete this and future occurrences");
    expect((await mount(renderDetailsArguments(action, args))).textContent).toContain("2026-10-12");
    const future = await mount(renderDetailsArguments(action, { ...args, unknown: "preserve" }));
    expect(future.textContent).toContain("Details shown as recorded data");
    expect(future.textContent).toContain('"unknown": "preserve"');
  });

  it("distinguishes a pending operation from a completed action", () => {
    const action = identity("home_assistant", "ha_get_operation_status");
    const pending = {
      isError: false,
      content: [],
      structuredContent: { status: "pending", operation_id: "operation-1" },
    };
    expect(renderActionOutcome(action, pending)).toEqual({ label: "Pending", failed: false });
    expect(renderActionOutcome(action, { ...pending, isError: true })).toBeUndefined();
  });

  it("surfaces an energy preference dry run and explicit restore scope before approval", async () => {
    const energy = identity("home_assistant", "ha_manage_energy_prefs");
    const args = { mode: "set", dry_run: true, config: { device_consumption: [] } };
    expect((await mount(renderActionLabel(energy, args))).textContent).toContain(
      "Preview energy preferences replacement"
    );
    const backup = identity("home_assistant", "ha_manage_backup");
    const restore = { scope: "snapshot", action: "restore", backup_id: "snapshot-1", restore_database: true };
    const view = await mount(renderDetailsArguments(backup, restore));
    expect(view.textContent).toContain("Restore database · Yes");
  });
});

const cardDecisionButtonCases: Array<{
  group: string;
  name: string;
  args: Record<string, unknown>;
  visible: string[];
}> = [
  {
    group: "kubernetes_admin",
    name: "resources_get",
    args: { apiVersion: "apps/v1", kind: "Deployment", name: "api" },
    visible: ["Read with API version apps/v1"],
  },
  {
    group: "kubernetes_admin",
    name: "pods_list_in_namespace",
    args: { namespace: "prod", fieldSelector: "status.phase=Running", labelSelector: "app=web" },
    visible: ["Matching pods where", "status.phase=Running", "app=web"],
  },
  {
    group: "kubernetes_admin",
    name: "resources_list",
    args: { apiVersion: "v1", kind: "Pod", fieldSelector: "status.phase=Running", labelSelector: "app=web" },
    visible: ["Using API version v1", "status.phase=Running", "app=web"],
  },
  {
    group: "kubernetes_admin",
    name: "pods_log",
    args: { name: "api-0", container: "sidecar", previous: true, tail: -1 },
    visible: ["container sidecar", "previous run", "last 100 lines (default)"],
  },
  {
    group: "kubernetes_admin",
    name: "resources_delete",
    args: { apiVersion: "v1", kind: "Pod", name: "api-0", namespace: "prod", gracePeriodSeconds: 0 },
    visible: ["Delete Pod prod/api-0", "request a 0s grace period"],
  },
  {
    group: "kubernetes_admin",
    name: "pods_delete",
    args: { name: "api-0", namespace: "prod" },
    visible: ["Delete Pod prod/api-0"],
  },
  {
    group: "kubernetes_admin",
    name: "events_list",
    args: { namespace: "prod", fieldSelector: "type=Warning" },
    visible: ["Show events matching", "type=Warning"],
  },
  {
    group: "sandbox",
    name: "get",
    args: { name: "inspected-sandbox" },
    visible: ["Inspect sandbox inspected-sandbox"],
  },
  {
    group: "sandbox",
    name: "dispose",
    args: { name: "disposed-sandbox" },
    visible: ["Dispose sandbox disposed-sandbox"],
  },
  { group: "sandbox", name: "list", args: {}, visible: ["List sandboxes"] },
  { group: "grocy_sf", name: "products_list", args: { detail: "full" }, visible: ["List Grocy products"] },
  { group: "grocy_sf", name: "quantity_units_list", args: {}, visible: ["List quantity units"] },
  {
    group: "gmail",
    name: "threads_list",
    args: { q: "from:deployments@example.com", maxResults: 5 },
    visible: ["from:deployments@example.com", "Up to 5 threads"],
  },
  {
    group: "github",
    name: "update_pull_request",
    args: {
      owner: "example",
      repo: "service",
      pullNumber: 42,
      title: "Update documentation",
      body: "Explain deployment checks and rollback steps.",
      state: "closed",
      base: "devel",
      draft: true,
      maintainer_can_modify: false,
      reviewers: ["reviewer"],
    },
    visible: [
      "Close example/service#42",
      "replace description",
      "change base to devel",
      "mark as draft",
      "disallow maintainer edits",
      "review from reviewer",
      "New title: Update documentation",
      "Explain deployment checks and rollback steps.",
    ],
  },
  {
    group: "github",
    name: "push_files",
    args: {
      owner: "example",
      repo: "service",
      branch: "docs",
      message: "docs: update guide",
      files: [{ path: "docs/guide.md", content: "Reviewable documentation content." }],
    },
    visible: ["Push 1 file to docs in example/service", "docs: update guide", "docs/guide.md"],
  },
  {
    group: "github",
    name: "delete_file",
    args: {
      owner: "example",
      repo: "service",
      path: "docs/old-guide.md",
      message: "docs: remove old guide",
      branch: "docs",
    },
    visible: ["Delete docs/old-guide.md in example/service", "docs", "docs: remove old guide"],
  },
  {
    group: "github",
    name: "create_branch",
    args: { owner: "example", repo: "service", branch: "docs", from_branch: "devel" },
    visible: ["Create branch docs from devel in example/service"],
  },
  {
    group: "github",
    name: "create_pull_request",
    args: {
      owner: "example",
      repo: "repo",
      title: "Update docs",
      head: "feature",
      base: "devel",
      body: "",
      draft: true,
      maintainer_can_modify: false,
      reviewers: ["reviewer1"],
    },
    visible: ["from feature", "draft", "maintainers cannot edit", "review from reviewer1"],
  },
];

describe("compact card renderers", () => {
  // Card decision buttons sit on the compact card, so what it shows is the label, the collapsed
  // preview and, where the card has an expanded version, the opened preview.
  it.each(cardDecisionButtonCases)(
    "draws all displayed request fields for $group/$name",
    async ({ group, name, args, visible }) => {
      const action = identity(group, name);
      const container = await mount(
        <>
          {renderActionLabel(action, args)}
          {renderPaneCollapsed(action, args)}
          {actionCompactCardExpands(action, args) && renderPaneOpened(action, args)}
        </>
      );
      for (const value of visible) expect(container.textContent).toContain(value);
      // Arguments the card cannot draw always open to their recorded data.
      expect(actionCompactCardExpands(action, { ...args, invisible: "must review" })).toBe(true);
    }
  );

  it.each([
    ["kubernetes_admin", "pods_get", { name: "web-0", namespace: "apps" }, false],
    ["kubernetes_admin", "events_list", { namespace: "apps" }, false],
    ["sandbox", "list", {}, false],
    // The opened card adds the API version.
    ["kubernetes_admin", "resources_get", { apiVersion: "v1", kind: "Pod", name: "web-0" }, true],
    ["ssh", "exec", SSH_EXEC_ARGUMENTS, true],
    ["__proto__", "constructor", {}, true],
  ] as const)("%s/%s with %o expands: %s", (group, name, args, expands) => {
    expect(actionCompactCardExpands(identity(group, name), args)).toBe(expands);
  });

  it.each(["search_code", "search_commits", "search_pull_requests", "search_repositories", "search_users"])(
    "folds GitHub %s pagination while preserving scope and validation",
    async (name) => {
      const action = identity("github", name);
      const args = { query: "repo:example/service rollout", page: 3, perPage: 10 };
      const compact = await mount(renderPaneCollapsed(action, args));
      expect(compact.textContent).toContain(args.query);
      expect(compact.textContent).not.toContain("Page");
      const details = await mount(renderPaneOpened(action, args));
      expect(details.textContent).toContain("Page 3");
      expect(details.textContent).toContain("Page size 10");
      expect(actionCompactCardExpands(action, args)).toBe(true);
      expect(actionCompactCardExpands(action, { query: args.query })).toBe(false);
      expect(actionOffersCardDecisionButtons(action, args)).toBe(true);
      expect(actionOffersCardDecisionButtons(action, { ...args, unknownScope: true })).toBe(false);
    }
  );

  it("abbreviates the branch-update condition and keeps its exact SHA in details", async () => {
    const action = identity("github", "update_pull_request_branch");
    const args = { owner: "example", repo: "service", pullNumber: 42, expectedHeadSha: "0123456789abcdef" };
    const label = await mount(renderActionLabel(action, args));
    expect(label.textContent).toContain("example/service#42");
    expect(label.textContent).toContain("latest base changes");
    expect(label.textContent).toContain("If unchanged");
    expect(label.textContent).not.toContain(args.expectedHeadSha);
    expect((await mount(renderPaneOpened(action, args))).textContent).toContain(args.expectedHeadSha);
    expect((await mount(renderDetailsArguments(action, args))).textContent).toContain(args.expectedHeadSha);
    expect(actionCompactCardExpands(action, args)).toBe(true);
    expect(actionOffersCardDecisionButtons(action, args)).toBe(true);
  });

  it("shows an explicitly supplied maintainer edit permission in compact and opened PR summaries", async () => {
    const action = identity("github", "create_pull_request");
    const args = {
      owner: "example",
      repo: "repo",
      title: "Update docs",
      head: "feature",
      base: "devel",
      body: "",
      maintainer_can_modify: true,
    };
    const collapsed = await mount(renderPaneCollapsed(action, args));
    const opened = await mount(renderPaneOpened(action, args));

    expect(collapsed.textContent).toContain("maintainers can edit");
    expect(opened.textContent).toContain("maintainers can edit");
  });
});

describe("card decision buttons capability", () => {
  it.each(cardDecisionButtonCases)(
    "validates the whole $group/$name request independently",
    ({ group, name, args }) => {
      expect(actionOffersCardDecisionButtons(identity(group, name), args)).toBe(true);
      expect(actionOffersCardDecisionButtons(identity(group, name), { ...args, invisible: "must review" })).toBe(false);
    }
  );

  it("keeps compact Gmail search approvals for visible scope and sends custom labels to review", () => {
    const search = identity("gmail", "threads_list");
    expect(actionOffersCardDecisionButtons(search, { q: "label:inbox" })).toBe(true);
    expect(actionOffersCardDecisionButtons(search, { q: "label:inbox", pageToken: "next" })).toBe(true);
    expect(actionOffersCardDecisionButtons(search, { q: "label:inbox", includeSpamTrash: true })).toBe(true);
    expect(actionOffersCardDecisionButtons(search, { labelIds: ["Label_19"] })).toBe(false);
  });

  it("limits compact Grocy creation to the one fully displayed record", async () => {
    const action = identity("grocy_sf", "locations_create");
    const location = { name: "Chest freezer", is_freezer: true, description: "Basement" };
    const args = { items: [location] };
    const collapsed = await mount(renderPaneCollapsed(action, args));
    expect(collapsed.textContent).toContain("Chest freezer");
    expect(collapsed.textContent).toContain("Adjusts due dates");
    expect(collapsed.textContent).toContain("Basement");
    expect(actionOffersCardDecisionButtons(action, args)).toBe(true);
    expect(actionCompactCardExpands(action, args)).toBe(false);
    for (const items of [[location, { name: "Another location" }], [{ ...location, inherited_permissions: true }]]) {
      expect(actionOffersCardDecisionButtons(action, { items })).toBe(false);
      expect(actionCompactCardExpands(action, { items })).toBe(true);
    }
  });

  it("keeps Grocy stock transfers in full review", () => {
    const transfer = identity("grocy_sf", "stock_transfer");
    expect(
      actionOffersCardDecisionButtons(transfer, {
        product: 24,
        amount: 2,
        qu: 7,
        from_location: 3,
        to_location: 4,
      })
    ).toBe(false);
  });

  it("offers card decisions for complete PR calls and rejects unknown actions", () => {
    const pr = cardDecisionButtonCases.find((candidate) => candidate.name === "create_pull_request");
    if (pr === undefined) throw new Error("missing pull request fixture");
    expect(actionOffersCardDecisionButtons(identity(pr.group, pr.name), { ...pr.args, body: "important text" })).toBe(
      true
    );
    expect(actionOffersCardDecisionButtons(identity(pr.group, pr.name), { ...pr.args, title: "x".repeat(121) })).toBe(
      true
    );
    expect(
      actionOffersCardDecisionButtons(identity(pr.group, pr.name), { ...pr.args, reviewers: Array(5).fill("reviewer") })
    ).toBe(true);
    expect(actionOffersCardDecisionButtons(SSH_EXEC, SSH_EXEC_ARGUMENTS)).toBe(false);
    expect(actionOffersCardDecisionButtons(identity("__proto__", "constructor"), {})).toBe(false);
  });

  it("renders a long PR description while offering card decision buttons", async () => {
    const pr = cardDecisionButtonCases.find((candidate) => candidate.name === "create_pull_request");
    if (pr === undefined) throw new Error("missing pull request fixture");
    const args = { ...pr.args, body: "important text" };

    expect(actionOffersCardDecisionButtons(identity(pr.group, pr.name), args)).toBe(true);
    const container = await mount(renderPaneOpened(identity(pr.group, pr.name), args));
    expect(container.textContent).toContain("important text");
  });
});

describe("sandbox rendering", () => {
  const action = (name: string) => identity("sandbox", name);
  const info = {
    name: "waiting-sandbox",
    template: "python",
    phase: "Pending",
    conditions: [{ type: "Ready", status: "False", reason: "ContainersNotReady", message: "Image pulling" }],
  };
  const result = (value: unknown): unknown => value;

  it("renders create and authorizes compact decisions only for schema-valid arguments", async () => {
    const args = { name: "new-sandbox", template: "python" };
    expect((await mount(renderActionLabel(action("create"), args))).textContent).toContain("Create sandbox");
    expect((await mount(renderPaneOpened(action("create"), args))).textContent).toContain("Template: python");
    expect((await mount(renderPaneCollapsed(action("create"), args))).textContent).toContain("Template: python");
    expect(actionOffersCardDecisionButtons(action("create"), args)).toBe(true);
    expect(actionOffersCardDecisionButtons(action("create"), { ...args, hidden: true })).toBe(false);
    const recorded = await mount(renderDetailsArguments(action("create"), { ...args, hidden: true }));
    expect(recorded.textContent).toContain("Details shown as recorded data");
  });

  it("renders exec without authorizing compact decisions", async () => {
    const args = {
      name: "script-sandbox",
      script: 'echo "$HOME"',
      timeout_seconds: 60,
      max_output_bytes: 0,
      cwd: "/workspace",
    };
    expect((await mount(renderActionLabel(action("exec"), args))).textContent).toContain(
      "Run command in sandbox script-sandbox"
    );
    expect((await mount(renderPaneOpened(action("exec"), args))).textContent).toContain(
      "Working directory /workspace · Timeout 60 s · Output limit 0 bytes per stream"
    );
    expect(actionOffersCardDecisionButtons(action("exec"), args)).toBe(false);
    const recorded = await mount(renderDetailsArguments(action("exec"), { ...args, hidden: true }));
    expect(recorded.textContent).toContain("Details shown as recorded data");
  });

  it("summarizes a sandbox script on the collapsed row", async () => {
    const args = { name: "script-sandbox", script: 'echo "$HOME"', timeout_seconds: 60, max_output_bytes: 0 };
    expect((await mount(renderPaneCollapsed(action("exec"), args))).textContent).toContain('echo "$HOME"');
  });

  it("shows readiness conditions and empty lists", async () => {
    const node = await mount(renderNativeDetailsResult(action("get"), result(info)));
    expect(node.textContent).toContain("ContainersNotReady");
    expect(node.textContent).toContain("Image pulling");
    expect((await mount(renderNativeDetailsResult(action("create"), result(info)))).textContent).toContain("Pending");
    expect((await mount(renderNativeDetailsResult(action("list"), result({ sandboxes: [] })))).textContent).toContain(
      "No sandboxes."
    );
    expect(
      (await mount(renderNativeDetailsResult(action("list"), result({ sandboxes: [info] })))).textContent
    ).toContain("waiting-sandbox");
  });

  it.each([true, false])("distinguishes disposal existed=%s", async (existed) => {
    expect(
      (await mount(renderNativeDetailsResult(action("dispose"), result({ name: "box", existed })))).textContent
    ).toContain(existed ? "disposed" : "already absent");
  });

  it.each([
    [{ kind: "exited", exit_code: 2 }, "Exit 2"],
    [{ kind: "killed", signal: 9 }, "Killed by signal 9"],
    [{ kind: "timed_out" }, "Timed out"],
  ])("preserves execution outcomes and truncation", async (exit, label) => {
    const node = await mount(
      renderNativeDetailsResult(
        action("exec"),
        result({
          exit,
          stdout: { truncated_text: "", total_bytes: 123 },
          stderr: "error",
          duration_seconds: 1.5,
        })
      )
    );
    expect(node.textContent).toContain(label);
    expect(node.textContent).toContain("truncated by the server; 123 total bytes");
    expect(node.textContent).toContain("error");
  });

  it("renders pod argv with safe argument quoting, highlighted as the shell words it is displayed as", async () => {
    const node = await mount(
      renderPaneCollapsed(identity("kubernetes_admin", "pods_exec"), {
        name: "pod",
        command: ["printf", "%s", "$HOME", "a'b", ""],
      })
    );
    expect(node.querySelector(".ui-code-inline")?.textContent).toBe("printf '%s' '$HOME' 'a'\\''b' ''");
    expect(node.querySelector(".ui-tok-string")?.textContent).toBe("'%s'");

    const opened = await mount(
      renderPaneOpened(identity("kubernetes_admin", "pods_exec"), {
        name: "pod",
        namespace: "apps",
        container: "worker",
        command: ["printf", "%s", "$HOME"],
      })
    );
    expect(opened.textContent).not.toContain("Pod apps/pod");
    expect(opened.textContent).toContain("Container worker");
    expect(opened.querySelector(".ui-code-block .cm-line")?.textContent).toBe("printf '%s' '$HOME'");
  });
});
