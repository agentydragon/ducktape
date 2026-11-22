import { routes } from "./fetch_mock";
import "@mantine/core/styles.css";

import type { JsonValue } from "@bufbuild/protobuf";
import type { ActionRequest } from "../../../action_service/api/contracts/action_service_pb";
import { ACTION_RENDERERS } from "../actions/rendering/index";
import { ActionPresentationsVisual } from "./action_presentations_visual";
// Action decision buttons use the shell stylesheet at desktop widths.
import "../shell/shell.css";
import {
  actionIntegrationSummaryRoute,
  actionPreview,
  actionReceipt,
  SSH_COMPLETED_ACTION,
  type ActionArguments,
  type ActionKey,
} from "./action_fixtures";
import { mount } from "./mount";

let pending: ActionRequest | undefined;
let receipt: ActionRequest | undefined;
let integrationSummaries: Record<string, JsonValue> | null = null;
routes.push(actionIntegrationSummaryRoute(() => integrationSummaries));

Object.assign(window, {
  agentplaneVisual: {
    actionRendererKeys(): string[] {
      return Array.from(ACTION_RENDERERS, ([group, renderers]) =>
        Array.from(renderers.keys(), (name) => `${group}.${name}`)
      ).flat();
    },
    showActionPreview(action: ActionKey, args: ActionArguments, summaries: Record<string, JsonValue> | null): void {
      pending = actionPreview(action, args);
      integrationSummaries = summaries;
    },
    showActionReceipt(action: ActionKey, args: ActionArguments, result: JsonValue): void {
      receipt = actionReceipt(SSH_COMPLETED_ACTION, action, args, result);
    },
    mountActionPresentations(sidebarWidth: number, detailWidth: number, mcp: boolean): void {
      if (pending === undefined || receipt?.execution === undefined) {
        throw new Error("Seed a pending Action and a receipt before mounting their presentations.");
      }
      mount(
        <ActionPresentationsVisual
          pending={pending}
          receipt={receipt}
          sidebarWidth={sidebarWidth}
          detailWidth={detailWidth}
          mcp={mcp}
        />
      );
    },
  },
});
