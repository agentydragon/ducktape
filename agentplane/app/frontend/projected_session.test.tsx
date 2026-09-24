// @vitest-environment happy-dom

import { create, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it } from "vitest";

import { EventSchema, TurnStatus } from "../../protocol/event_pb";
import { EntityCard, pruneCommandErrors } from "./projected_session";
import { RetainedDisclosureProvider } from "./retained_disclosures";
import type { ThreadEntity } from "./thread_sync";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];

afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
});

type Observation = MessageInitShape<typeof EventSchema>["observation"];

async function renderLifecycle(observation: string, event: Observation): Promise<HTMLDivElement> {
  const entity: ThreadEntity = {
    threadId: "test-thread",
    projectionEpoch: "test-epoch",
    entityKind: "lifecycle",
    entityId: "7",
    entityIndex: "7",
    cursor: "7",
    revisionCursor: "7",
    pending: false,
    turnId: null,
    state: { observation, event: toJson(EventSchema, create(EventSchema, { observation: event })) },
    textRef: null,
    argumentsRef: null,
    outputRef: null,
    inputRef: null,
  };
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () =>
    root.render(
      <MantineProvider>
        <RetainedDisclosureProvider>
          <EntityCard threadId="test-thread" entity={entity} live={false} />
        </RetainedDisclosureProvider>
      </MantineProvider>
    )
  );
  return container;
}

/** The row's own text: everything but its Evidence disclosure. */
function textBesideEvidence(row: Element): string {
  const copy = row.cloneNode(true) as Element;
  copy.querySelectorAll("details").forEach((details) => details.remove());
  return copy.textContent ?? "";
}

it("drops request errors after their local commands are dismissed", () => {
  const errors = new Map([
    ["dismissed", "connection lost"],
    ["pending", "request timed out"],
  ]);

  expect(pruneCommandErrors(errors, new Set(["pending"]))).toEqual(new Map([["pending", "request timed out"]]));
  expect(pruneCommandErrors(errors, new Set(errors.keys()))).toBe(errors);
});

it.each<[string, Observation, string]>([
  [
    "turn_completed",
    { case: "turnCompleted", value: { turnId: "test-turn", status: TurnStatus.COMPLETED } },
    "Turn completed",
  ],
  ["turn_started", { case: "turnStarted", value: { turnId: "test-turn", model: "test-model" } }, "Turn started"],
  [
    "model_changed",
    { case: "modelChanged", value: { previousModel: "test-model", model: "test-model-next" } },
    "Model changed to test-model-next",
  ],
  ["harness_exited", { case: "harnessExited", value: { exitCode: 3 } }, "Harness exited with code 3"],
])("shows an ordinary %s as one line with no disclosure of its own", async (observation, event, line) => {
  const container = await renderLifecycle(observation, event);
  const row = container.querySelector('[data-thread-anchor="7"]')!;
  // The entity's Evidence is the row's only disclosure; the raw event is reached through it.
  expect([...row.querySelectorAll("summary")].map((summary) => summary.textContent)).toEqual(["Evidence"]);
  expect(textBesideEvidence(row)).toBe(line);
  expect(container.querySelector('[role="alert"]')).toBeNull();
});

it.each(['Test API failure: HTTP 429\n<img src="x" onerror="throw new Error()">', ""])(
  "shows a failed turn's error as a plain-text alert: %j",
  async (error) => {
    const container = await renderLifecycle("turn_completed", {
      case: "turnCompleted",
      value: { turnId: "test-failed-turn", status: TurnStatus.FAILED, error },
    });
    const alert = container.querySelector('[role="alert"][data-thread-anchor="7"]')!;
    expect(textBesideEvidence(alert)).toBe(`Turn failed${error || "The harness reported no error details."}`);
    expect(alert.querySelector("img")).toBeNull();
  }
);

it.each<[string, string, Observation]>([
  [
    "Turn interrupted",
    "turn_completed",
    { case: "turnCompleted", value: { turnId: "test-turn", status: TurnStatus.INTERRUPTED } },
  ],
  [
    "Turn losttest harness exited during the turn",
    "turn_completed",
    {
      case: "turnCompleted",
      value: { turnId: "test-turn", status: TurnStatus.PROCESS_LOST, error: "test harness exited during the turn" },
    },
  ],
  ["Harness lost", "harness_lost", { case: "harnessLost", value: {} }],
])("keeps an abnormal ending prominent: %s", async (text, observation, event) => {
  const container = await renderLifecycle(observation, event);
  expect(textBesideEvidence(container.querySelector('[role="alert"][data-thread-anchor="7"]')!)).toBe(text);
});
