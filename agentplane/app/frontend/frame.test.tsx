// @vitest-environment happy-dom

import { create, fromJsonString, type MessageInitShape } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";

import { EventSchema, Direction } from "../../protocol/event_pb";
import { EventEntrySchema, type EventEntry } from "../../protocol/event_log_pb";
import { FrameView } from "./frame";

function entry(observation: MessageInitShape<typeof EventSchema>["observation"]): EventEntry {
  return create(EventEntrySchema, {
    cursor: 12n,
    origin: { sourceId: "native-source", sequence: 12n },
    event: { observation, at: { seconds: 100n, nanos: 123456789 }, sourceSequences: [8n, 10n] },
  });
}

function render(value: EventEntry): HTMLDivElement {
  const container = document.createElement("div");
  container.innerHTML = renderToStaticMarkup(
    <MantineProvider env="test">
      <FrameView entry={value} />
    </MantineProvider>
  );
  return container;
}

it("keeps native evidence and the full exact protobuf envelope inspectable", () => {
  const value = entry({ case: "native", value: { direction: Direction.FROM_HARNESS, line: '{"text":"hello"}\n' } });
  const container = render(value);
  const frame = container.querySelector("details");
  expect(frame?.open).toBe(false);
  expect(frame?.dataset.eventCursor).toBe("12");
  expect(frame?.id).toBe("agentplane-event-12");
  expect(frame?.querySelector("summary")?.textContent).toContain("Native frame · FROM_HARNESS");
  expect(container.querySelector(".agentplane-hljs")?.textContent).toBe('{"text":"hello"}\n');
  expect(container.textContent).toContain("caused by native-source:8, native-source:10");
  const serialized = container.querySelector("[data-event-envelope]")?.textContent;
  if (!serialized) throw new Error("Missing generated EventEntry envelope");
  expect(fromJsonString(EventEntrySchema, serialized)).toEqual(value);
});

it("distinguishes admission from the later model effect", () => {
  const admitted = render(
    entry({
      case: "commandAdmitted",
      value: { command: { commandId: "model-1", operation: { case: "changeModel", value: { model: "next" } } } },
    })
  );
  expect(admitted.querySelector("summary")?.textContent).toContain("Command admitted · Change model to next · model-1");
  expect(admitted.querySelector("summary")?.textContent).not.toContain("Model changed");
  const changed = render(
    entry({ case: "modelChanged", value: { commandId: "model-1", previousModel: "old", model: "next" } })
  );
  expect(changed.querySelector("summary")?.textContent).toContain("Model changed · old → next · command model-1");
});

it("shows every originating command of a coalesced harness message", () => {
  const container = render(
    entry({
      case: "harnessUserMessageConfirmed",
      value: { harnessMessageId: "native-user", originCommandIds: ["input-123", "input-125"], text: "A\nC" },
    })
  );
  expect(container.querySelector("summary")?.textContent).toContain("originating commands: input-123, input-125");
});

it("treats native markup as data in both the readable payload and envelope", () => {
  const line = '<img src=x onerror="alert(1)">';
  const container = render(entry({ case: "native", value: { direction: Direction.FROM_HARNESS, line } }));
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector(".agentplane-hljs")?.textContent).toBe(line);
});
