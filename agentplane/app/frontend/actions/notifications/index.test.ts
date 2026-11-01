import { describe, expect, it } from "vitest";

import type { ActionRequestView, JsonValue } from "../types";
import { formatActionNotification } from "./index";

function actionRequest(
  fields: Pick<ActionRequestView, "action" | "arguments" | "title" | "description">
): ActionRequestView {
  return {
    id: "00000000-0000-4000-8000-000000000001",
    idempotency_key: "notification-format-test",
    ...fields,
    caller: { namespace: "test-namespace", name: "test-account" },
    caller_uid: null,
    state: "decision_pending",
    version: 1,
    created_at: "2026-10-09T12:00:00Z",
    updated_at: "2026-10-09T12:00:00Z",
    decision: null,
    execution: null,
  };
}

// [Action name, arguments, notification's Action title, notification text when it is not the description]
const kubernetesNotificationCases: [string, { [key: string]: JsonValue }, string, string?][] = [
  ["resources_create_or_update", { resource: "apiVersion: v1\nkind: Secret" }, "Apply Secret"],
  [
    "resources_create_or_update",
    {
      resource:
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: b\n---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n",
    },
    "Apply 3 Kubernetes resources: ConfigMap a, ConfigMap b, and 1 more",
  ],
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
  [
    "resources_delete",
    { apiVersion: "v1", kind: "PersistentVolume", name: "test-volume" },
    "⚠ Delete PersistentVolume test-volume",
  ],
  ["resources_list", { apiVersion: "v1", kind: "Node" }, "List Node resources"],
  [
    "resources_list",
    { apiVersion: "v1", kind: "PersistentVolumeClaim", namespace: "apps", labelSelector: "app=api" },
    "List PersistentVolumeClaim resources in namespace apps",
  ],
  ["events_list", { namespace: "apps", fieldSelector: "type=Warning" }, "List events in namespace apps"],
  ["pods_delete", { name: "api-0", namespace: "apps" }, "⚠ Delete Pod apps/api-0"],
  [
    "pods_list_in_namespace",
    { namespace: "apps", labelSelector: "app=api", fieldSelector: "status.phase=Running" },
    "List pods in namespace apps",
  ],
  [
    "pods_exec",
    { name: "api-0", namespace: "apps", container: "api", command: ["printf", "%s\n", "hello world"] },
    "Run command in Pod apps/api-0",
    "$ printf '%s\n' 'hello world' · Container api",
  ],
  ["pods_get", { name: "api-0", namespace: "apps" }, "Get Pod apps/api-0"],
  [
    "pods_log",
    { name: "api-0", namespace: "apps", container: "api", previous: false, tail: 50 },
    "View logs for Pod apps/api-0, container api, last 50 lines",
  ],
];

const malformedKubernetesNotificationCases: [string, { [key: string]: JsonValue }][] = [
  ["resources_create_or_update", { resource: 123 }],
  ["resources_get", { apiVersion: "apps/v1", kind: "Deployment", name: "api", namespace: 7 }],
  ["resources_delete", { apiVersion: "apps/v1", kind: "Deployment", name: "api", gracePeriodSeconds: "0" }],
  ["pods_delete", { name: "api-0", namespace: 7 }],
  ["pods_list_in_namespace", { namespace: "apps", labelSelector: 123 }],
  ["pods_exec", { name: "api-0", namespace: "apps", command: "id" }],
  ["pods_get", { name: "api-0", namespace: 7 }],
  ["pods_log", { name: "api-0", namespace: "apps", previous: "false" }],
  ["resources_list", { apiVersion: "v1", kind: 7 }],
  ["events_list", { namespace: 7 }],
];

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

  it("marks an SSH command that runs as root", () => {
    const request = actionRequest({
      action: { group: "ssh", name: "exec" },
      arguments: { user: "root", host: "build-01", command: "df -h" },
      title: "Check disk space",
      description: null,
    });

    expect(formatActionNotification(request).title).toBe("Check disk space · ⚠ Run command on root@build-01");
  });

  it("snapshots sandbox exec with its target, script, and settings", () => {
    const request = actionRequest({
      action: { group: "sandbox", name: "exec" },
      arguments: {
        name: "test-sandbox",
        script: "git status",
        timeout_seconds: 60,
        max_output_bytes: 4096,
        cwd: "/workspace",
      },
      title: "Check the checkout",
      description: null,
    });

    expect(formatActionNotification(request)).toMatchInlineSnapshot(`
      {
        "text": "$ git status · Working directory /workspace · Timeout 60 s · Output limit 4096 bytes per stream",
        "title": "Check the checkout · Run command in sandbox test-sandbox",
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

  it.each(kubernetesNotificationCases)(
    "formats the Kubernetes notification for %s",
    (name, args, actionTitle, text = "The controller is waiting for this change.") => {
      const request = actionRequest({
        action: { group: "kubernetes_admin", name },
        arguments: args,
        title: "Review the cluster operation",
        description: "The controller is waiting for this change.",
      });

      expect(formatActionNotification(request)).toEqual({
        title: `Review the cluster operation · ${actionTitle}`,
        text,
      });
    }
  );

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
    ["pods_get", { namespace: "apps" }],
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

  it("names a GitHub push's target and lists its commit message and first files by UTF-8 size", () => {
    const request = actionRequest({
      action: { group: "github", name: "push_files" },
      arguments: {
        owner: "test-owner",
        repo: "test-repo",
        branch: "test-branch",
        message: "test: update fixtures",
        files: [
          { path: "test/accented.txt", content: "é".repeat(406) },
          { path: "test/small.txt", content: "x".repeat(1906) },
          { path: "test/large.txt", content: "x".repeat(72047) },
          { path: "test/extra-1.txt", content: "" },
          { path: "test/extra-2.txt", content: "" },
        ],
      },
      title: "Push the test fixtures",
      description: "Description the notification does not need.",
    });

    expect(formatActionNotification(request)).toEqual({
      title: "Push the test fixtures · Push 5 files to test-branch in test-owner/test-repo",
      text: [
        "test: update fixtures",
        "test/accented.txt · 812 B",
        "test/small.txt · 1.9 KB",
        "test/large.txt · 72 KB",
        "and 2 more files",
      ].join("\n"),
    });
  });

  it("names what a GitHub pull request update changes and shows the new text", () => {
    const request = actionRequest({
      action: { group: "github", name: "update_pull_request" },
      arguments: {
        owner: "test-owner",
        repo: "test-repo",
        pullNumber: 7,
        state: "closed",
        title: "Test title",
        body: "Closed pending test evidence.",
      },
      title: "Close the test PR",
      description: null,
    });

    expect(formatActionNotification(request)).toEqual({
      title: "Close the test PR · Close test-owner/test-repo#7: retitle, replace description",
      text: "New title: Test title\nClosed pending test evidence.",
    });
  });

  it("uses the caller's description when a pull request update carries no new text", () => {
    const request = actionRequest({
      action: { group: "github", name: "update_pull_request" },
      arguments: { owner: "test-owner", repo: "test-repo", pullNumber: 7, draft: false },
      title: "Ready the test PR",
      description: "CI is green.",
    });

    expect(formatActionNotification(request)).toEqual({
      title: "Ready the test PR · Update test-owner/test-repo#7: mark ready for review",
      text: "CI is green.",
    });
  });

  it("names a GitHub branch with its source and repository", () => {
    const request = actionRequest({
      action: { group: "github", name: "create_branch" },
      arguments: { owner: "test-owner", repo: "test-repo", branch: "test-branch", from_branch: "test-base" },
      title: "Create the test branch",
      description: "Branch for the test change.",
    });

    expect(formatActionNotification(request)).toEqual({
      title: "Create the test branch · Create branch test-branch from test-base in test-owner/test-repo",
      text: "Branch for the test change.",
    });
  });

  it("names a GitHub pull request's base and title, then its head, settings and description", () => {
    const request = actionRequest({
      action: { group: "github", name: "create_pull_request" },
      arguments: {
        owner: "test-owner",
        repo: "test-repo",
        title: "Test title",
        head: "test-head",
        base: "test-base",
        body: "Test description.",
        draft: true,
        maintainer_can_modify: false,
      },
      title: "Open the test PR",
      description: null,
    });

    expect(formatActionNotification(request)).toEqual({
      title: "Open the test PR · Open PR into test-base of test-owner/test-repo: Test title",
      text: "from test-head · draft · maintainers cannot edit\nTest description.",
    });
  });

  it.each(["push_files", "update_pull_request", "create_branch", "create_pull_request"])(
    "falls back for GitHub %s arguments with a field the formatter cannot show",
    (name) => {
      const request = actionRequest({
        action: { group: "github", name },
        arguments: { owner: "test-owner", repo: "test-repo", unshown: true },
        title: "Review the GitHub change",
        description: "Caller description.",
      });

      expect(formatActionNotification(request)).toEqual({
        title: `Review the GitHub change · github / ${name}`,
        text: "Caller description.",
      });
    }
  );

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
