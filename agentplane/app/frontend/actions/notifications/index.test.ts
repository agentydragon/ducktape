import { describe, expect, it } from "vitest";

import type { ActionRequestView } from "../types";
import { formatActionNotification } from "./index";

function actionRequest(
  fields: Pick<ActionRequestView, "action" | "arguments" | "title" | "description">
): ActionRequestView {
  return {
    id: "00000000-0000-4000-8000-000000000001",
    idempotency_key: "notification-format-test",
    ...fields,
    caller: null,
    external_grant: null,
    state: "decision_pending",
    version: 1,
    created_at: "2026-10-09T12:00:00Z",
    updated_at: "2026-10-09T12:00:00Z",
    decision: null,
    execution: null,
  };
}

const kubernetesNotificationCases = [
  ["resources_create_or_update", { resource: "apiVersion: v1\nkind: Secret" }, "Apply Kubernetes resource"],
  [
    "resources_get",
    { apiVersion: "apps/v1", kind: "Deployment", name: "api", namespace: "apps" },
    "Get Deployment apps/api",
  ],
  [
    "resources_delete",
    { apiVersion: "apps/v1", kind: "Deployment", name: "api", namespace: "apps", gracePeriodSeconds: 0 },
    "⚠ Delete Deployment apps/api",
  ],
  ["pods_delete", { name: "api-0", namespace: "apps" }, "⚠ Delete Pod apps/api-0"],
  [
    "pods_list_in_namespace",
    { namespace: "apps", labelSelector: "app=api", fieldSelector: "status.phase=Running" },
    "List pods in namespace apps",
  ],
  [
    "pods_exec",
    { name: "api-0", namespace: "apps", container: "api", command: ["id"] },
    "Run command in Pod apps/api-0",
  ],
  [
    "pods_log",
    { name: "api-0", namespace: "apps", container: "api", previous: false, tail: 50 },
    "View logs for Pod apps/api-0",
  ],
] as const;

const malformedKubernetesNotificationCases = [
  ["resources_create_or_update", { resource: 123 }],
  ["resources_get", { apiVersion: "apps/v1", kind: "Deployment", name: "api", namespace: 7 }],
  ["resources_delete", { apiVersion: "apps/v1", kind: "Deployment", name: "api", gracePeriodSeconds: "0" }],
  ["pods_delete", { name: "api-0", namespace: 7 }],
  ["pods_list_in_namespace", { namespace: "apps", labelSelector: 123 }],
  ["pods_exec", { name: "api-0", namespace: "apps", command: "id" }],
  ["pods_log", { name: "api-0", namespace: "apps", previous: "false" }],
] as const;

describe("action notification formatting", () => {
  it("snapshots the fallback for an unhandled Action", () => {
    const request = actionRequest({
      action: { group: "finance", name: "transfer" },
      arguments: { recipient: "vendor", amount: 400, account: "checking" },
      title: "Transfer $400 to vendor",
      description: "Invoice 2026-04-18.",
    });

    expect(formatActionNotification(request)).toMatchInlineSnapshot(`
      {
        "text": "Invoice 2026-04-18.",
        "title": "Transfer $400 to vendor · finance / transfer",
      }
    `);
  });

  it("snapshots SSH exec with its target, command, and timeout", () => {
    const request = actionRequest({
      action: { group: "ssh", name: "exec" },
      arguments: { user: "deploy", host: "build-01", command: "df -h", timeout_seconds: 30 },
      title: "Check disk space",
      description: "Run the disk usage check on the build host.",
    });

    expect(formatActionNotification(request)).toMatchInlineSnapshot(`
      {
        "text": "$ df -h · Timeout 30 s",
        "title": "Check disk space · Run command on deploy@build-01",
      }
    `);
  });

  it("uses the human-facing Action label instead of the technical identity", () => {
    const request = actionRequest({
      action: { group: "kubernetes_admin", name: "pods_list_in_namespace" },
      arguments: { namespace: "tofu-controller" },
      title: "Check the controller pods",
      description: "Confirm the controller is ready.",
    });

    expect(formatActionNotification(request)).toEqual({
      title: "Check the controller pods · List pods in namespace tofu-controller",
      text: "Confirm the controller is ready.",
    });
  });

  it("does not duplicate an Action title that already matches the caller title", () => {
    const request = actionRequest({
      action: { group: "kubernetes_admin", name: "pods_list_in_namespace" },
      arguments: { namespace: "tofu-controller" },
      title: "List pods in namespace tofu-controller",
      description: "Confirm the controller is ready.",
    });

    expect(formatActionNotification(request).title).toBe("List pods in namespace tofu-controller");
  });

  it("uses Tana's human-facing Action description in the notification title", () => {
    const request = actionRequest({
      action: { group: "tana", name: "get_or_create_calendar_node" },
      arguments: { workspaceId: "workspace-1", granularity: "week" },
      title: "Prepare this week's planning page",
      description: "Create the planning page if it does not exist.",
    });

    expect(formatActionNotification(request)).toEqual({
      title: "Prepare this week's planning page · Get or create calendar node",
      text: "Create the planning page if it does not exist.",
    });
  });

  it("uses human-facing descriptions for Grocy's no-lookup Actions", () => {
    const cases = [
      ["products_list", "List Grocy products"],
      ["quantity_units_list", "List quantity units"],
      ["get_system_info", "Show Grocy system information"],
    ] as const;

    for (const [name, actionTitle] of cases) {
      const request = actionRequest({
        action: { group: "grocy_sf", name },
        arguments: {},
        title: "Check the pantry service",
        description: "Read-only Grocy request.",
      });

      expect(formatActionNotification(request)).toEqual({
        title: `Check the pantry service · ${actionTitle}`,
        text: "Read-only Grocy request.",
      });
    }
  });

  it("adds Gmail's draft description through the shared notification title combiner", () => {
    const request = actionRequest({
      action: { group: "gmail", name: "drafts_create" },
      arguments: { to: ["reader@example.com"], subject: "Release notes", body: "Ready to review." },
      title: "Prepare the release email",
      description: "Check the recipients and body before approving.",
    });

    expect(formatActionNotification(request)).toEqual({
      title: "Prepare the release email · Gmail: Draft email",
      text: "Check the recipients and body before approving.",
    });
  });

  it("uses Gmail's thread-search description without duplicating a matching caller title", () => {
    const request = actionRequest({
      action: { group: "gmail", name: "threads_list" },
      arguments: { q: "from:alerts@example.com" },
      title: "Gmail: Search threads",
      description: null,
    });

    expect(formatActionNotification(request)).toEqual({
      title: "Gmail: Search threads",
      text: "Action requires approval",
    });
  });

  it.each(kubernetesNotificationCases)("formats the Kubernetes notification for %s", (name, args, actionTitle) => {
    const request = actionRequest({
      action: { group: "kubernetes_admin", name },
      arguments: args,
      title: "Review the cluster operation",
      description: "The controller is waiting for this change.",
    });

    expect(formatActionNotification(request)).toEqual({
      title: `Review the cluster operation · ${actionTitle}`,
      text: "The controller is waiting for this change.",
    });
  });

  it.each(kubernetesNotificationCases)("falls back for %s when arguments contain an unknown field", (name, args) => {
    const request = actionRequest({
      action: { group: "kubernetes_admin", name },
      arguments: { ...args, unshown: true },
      title: "Review the cluster operation",
      description: "The controller is waiting for this change.",
    });

    expect(formatActionNotification(request).title).toBe(`Review the cluster operation · kubernetes_admin / ${name}`);
  });

  it.each(malformedKubernetesNotificationCases)("falls back for malformed %s arguments", (name, args) => {
    const request = actionRequest({
      action: { group: "kubernetes_admin", name },
      arguments: args,
      title: "Review the cluster operation",
      description: "The controller is waiting for this change.",
    });

    expect(formatActionNotification(request).title).toBe(`Review the cluster operation · kubernetes_admin / ${name}`);
  });

  it.each([
    ["resources_get", { name: "api" }],
    ["resources_delete", { kind: "Deployment" }],
    ["pods_delete", { namespace: "apps" }],
    ["pods_list_in_namespace", { namespace: "" }],
    ["pods_exec", { namespace: "apps" }],
    ["pods_log", { namespace: "apps" }],
  ] as const)("uses the generic identity for %s when its displayed target is missing", (name, args) => {
    const request = actionRequest({
      action: { group: "kubernetes_admin", name },
      arguments: args,
      title: "Review the cluster operation",
      description: "The controller is waiting for this change.",
    });

    expect(formatActionNotification(request)).toEqual({
      title: `Review the cluster operation · kubernetes_admin / ${name}`,
      text: "The controller is waiting for this change.",
    });
  });

  it("falls back when SSH arguments contain fields the widget cannot show", () => {
    const request = actionRequest({
      action: { group: "ssh", name: "exec" },
      arguments: { user: "deploy", host: "build-01", command: "df -h", unrecognized: "value" },
      title: "Check disk space",
      description: "Run the disk usage check on the build host.",
    });

    expect(formatActionNotification(request)).toMatchInlineSnapshot(`
      {
        "text": "Run the disk usage check on the build host.",
        "title": "Check disk space · ssh / exec",
      }
    `);
  });
});
