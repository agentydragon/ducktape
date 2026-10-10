import { describe, expect, it } from "vitest";

import { ACTION_PRESENTATION_CATALOG } from "../presentation_catalog";
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

  it("formats every migrated action without exposing its technical group or tool name", () => {
    for (const { group, name } of ACTION_PRESENTATION_CATALOG) {
      const args =
        group === "kubernetes_admin" && name === "pods_list_in_namespace"
          ? { namespace: "demo" }
          : group === "ssh" && name === "exec"
            ? { user: "operator", host: "host.example", command: "echo ok" }
            : {};
      const formatted = formatActionNotification(
        actionRequest({
          action: { group, name },
          arguments: args,
          title: "Review this request",
          description: "Caller context",
        })
      );
      expect(formatted.title).not.toContain(`${group} / ${name}`);
      expect(formatted.title).toContain("Review this request");
      expect(formatted.text).toBe(group === "ssh" && name === "exec" ? "$ echo ok" : "Caller context");
    }
  });

  it("does not repeat a caller title that is exactly the custom Action label", () => {
    const request = actionRequest({
      action: { group: "kubernetes_admin", name: "pods_list_in_namespace" },
      arguments: { namespace: "tofu-controller" },
      title: "List pods in namespace tofu-controller",
      description: "Review the result.",
    });

    expect(formatActionNotification(request).title).toBe("List pods in namespace tofu-controller");
  });

  it("composes the request context and action-provided title through the shared boundary", () => {
    const request = actionRequest({
      action: { group: "gmail", name: "drafts_create" },
      arguments: { to: ["reader@example.com"], subject: "Release notes", body: "Ready" },
      title: "Compose a release email",
      description: "Review before saving the draft.",
    });

    expect(formatActionNotification(request)).toEqual({
      title: "Compose a release email · Draft email: Release notes",
      text: "Review before saving the draft.",
    });
    expect(formatActionNotification({ ...request, title: "Draft email: Release notes" }).title).toBe(
      "Draft email: Release notes"
    );
  });
});
