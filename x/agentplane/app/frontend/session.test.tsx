// @vitest-environment happy-dom

import { create, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { command, findThread, models } from "./client";
import { CommandSchema } from "../../protocol/command_pb";
import { EventSchema, ItemKind } from "../../protocol/event_pb";
import { EventEntrySchema } from "../../protocol/event_log_pb";
import { AttachedSchema, Harness } from "../../runner/protocol_pb";
import { SessionView } from "./session";

vi.mock("./client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./client")>()),
  command: vi.fn(),
  findThread: vi.fn(),
  models: vi.fn(),
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];
const THREAD = {
  id: "10000000-0000-4000-8000-000000000001",
  sandbox: "composer-test",
  session_id: "session-test",
} as never;

afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

function event(cursor: number, observation: MessageInitShape<typeof EventSchema>["observation"]): string {
  return JSON.stringify(
    toJson(
      EventEntrySchema,
      create(EventEntrySchema, {
        cursor: BigInt(cursor),
        origin: { sourceId: "test-runner", sequence: BigInt(cursor) },
        event: create(EventSchema, { observation }),
      })
    )
  );
}

async function render(
  thread: typeof THREAD | null = THREAD,
  catalog: { HARNESS_CLAUDE: string[]; HARNESS_CODEX: string[] } = {
    HARNESS_CLAUDE: ["test-model"],
    HARNESS_CODEX: ["test-model"],
  }
): Promise<{
  container: HTMLDivElement;
  composer: HTMLTextAreaElement;
  stream: EventTarget;
  streams: EventTarget[];
  rerender: (sessionId: string) => Promise<void>;
}> {
  const streams: EventTarget[] = [];
  vi.mocked(findThread).mockResolvedValue(thread);
  vi.mocked(models).mockResolvedValue(catalog as never);
  vi.stubGlobal(
    "EventSource",
    class extends EventTarget {
      constructor() {
        super();
        streams.push(this);
      }
      close(): void {}
    }
  );
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  async function rerender(sessionId: string): Promise<void> {
    await act(async () => {
      root.render(
        <MantineProvider>
          <MemoryRouter>
            <SessionView sandbox="composer-test" sessionId={sessionId} onBack={() => {}} />
          </MemoryRouter>
        </MantineProvider>
      );
    });
  }
  await rerender("session-test");
  const stream = streams[0];
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("attached", {
        data: JSON.stringify({
          sessionId: "session-test",
          spec: { harness: "HARNESS_CLAUDE", model: "test-model" },
        }),
      })
    );
    await Promise.resolve();
  });
  await act(async () => {
    stream.dispatchEvent(new MessageEvent("event", { data: event(1, { case: "harnessStarted", value: {} }) }));
  });
  const composer = container.querySelector("textarea");
  if (!composer) throw new Error("Missing composer");
  return { container, composer, stream, streams, rerender };
}

async function type(composer: HTMLTextAreaElement, text: string): Promise<void> {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(composer, text);
    composer.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function enter(composer: HTMLTextAreaElement): void {
  composer.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
}

it("shows sending immediately, suppresses repeated Enter, and clears only after acknowledgment", async () => {
  let acknowledge = (): void => {
    throw new Error("Acknowledgment is not initialized");
  };
  vi.mocked(command).mockReturnValue(new Promise((resolve) => (acknowledge = () => resolve(create(EventEntrySchema)))));
  const { container, composer } = await render();
  await type(composer, "hello");
  await act(async () => {
    enter(composer);
    enter(composer);
  });
  expect(command).toHaveBeenCalledOnce();
  expect(container.querySelector('[role="status"]')?.textContent).toBe("Sending…");
  expect(composer.disabled).toBe(true);
  expect(composer.value).toBe("hello");
  await act(async () => acknowledge());
  expect(container.querySelector('[role="status"]')).toBeNull();
  expect(composer.disabled).toBe(false);
  expect(composer.value).toBe("");
});

it("reuses the input id after an ambiguous failure, but gives a new submission its own id", async () => {
  vi.mocked(command).mockRejectedValueOnce(new Error("response lost")).mockResolvedValue(create(EventEntrySchema));
  const { container, composer } = await render();
  await type(composer, "hello");
  await act(async () => enter(composer));
  expect(container.textContent).toContain("response lost");
  expect(composer.value).toBe("hello");
  expect(composer.disabled).toBe(false);
  await act(async () => enter(composer));
  expect(vi.mocked(command).mock.calls[1]).toEqual(vi.mocked(command).mock.calls[0]);
  await type(composer, "hello");
  await act(async () => enter(composer));
  expect(vi.mocked(command).mock.calls[2]?.[1].commandId).not.toBe(vi.mocked(command).mock.calls[0]?.[1].commandId);
});

it("gives edited text a new input id after a failure", async () => {
  vi.mocked(command).mockRejectedValueOnce(new Error("response lost")).mockResolvedValue(create(EventEntrySchema));
  const { composer } = await render();
  await type(composer, "hello");
  await act(async () => enter(composer));
  await type(composer, "different message");
  await act(async () => enter(composer));
  expect(vi.mocked(command).mock.calls[1]?.[1].commandId).not.toBe(vi.mocked(command).mock.calls[0]?.[1].commandId);
  expect(toJson(CommandSchema, vi.mocked(command).mock.calls[1]?.[1] as never)).toMatchObject({
    submitInput: { text: "different message" },
  });
});

it("collapses a lone tool call behind its run disclosure", async () => {
  const { container, stream } = await render();
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "turnStarted", value: { turnId: "t1" } }) })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(3, {
          case: "itemStarted",
          value: { itemId: "tool#0", kind: ItemKind.TOOL_CALL, toolName: "Bash" },
        }),
      })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(4, {
          case: "itemCompleted",
          value: { itemId: "tool#0", outcome: { case: "tool", value: { output: "ok", succeeded: true } } },
        }),
      })
    );
  });
  const control = [...container.querySelectorAll("button")].find((button) =>
    button.textContent?.includes("1 tool call")
  );
  expect(control?.getAttribute("aria-expanded")).toBe("false");
});

it("isolates transcript, draft, and late transport callbacks when the target changes", async () => {
  const { container, composer, stream, streams, rerender } = await render();
  await type(composer, "draft for the old target");
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "turnStarted", value: { turnId: "old-turn" } }) })
    );
  });
  await rerender("next-session");
  const nextComposer = container.querySelector("textarea");
  expect(nextComposer?.value).toBe("");
  expect(nextComposer?.disabled).toBe(true);
  expect(container.textContent).not.toContain("old-turn");
  await act(async () => {
    stream.dispatchEvent(new MessageEvent("error", { data: "old target failure" }));
    streams[1].dispatchEvent(
      new MessageEvent("attached", {
        data: JSON.stringify({
          sessionId: "next-session",
          spec: { harness: "HARNESS_CLAUDE", model: "test-model" },
        }),
      })
    );
    streams[1].dispatchEvent(new MessageEvent("event", { data: event(1, { case: "harnessStarted", value: {} }) }));
    await Promise.resolve();
  });
  expect(container.textContent).not.toContain("old target failure");
  expect(nextComposer?.disabled).toBe(false);
  expect(container.querySelector('[role="alert"]')).toBeNull();
});

it("shows a replay integrity failure and stops controls at the verified prefix", async () => {
  const { container, composer, stream } = await render();
  await act(async () => {
    stream.dispatchEvent(new MessageEvent("event", { data: event(3, { case: "harnessStarted", value: {} }) }));
  });
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("Event gap: expected 2, received 3");
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("through event 1");
  expect(composer.disabled).toBe(true);
  expect(container.querySelector<HTMLInputElement>('input[aria-label="Model"]')?.placeholder).toBe("Model unavailable");
});

it("keeps the model unknown during catch-up instead of showing an older replayed model", async () => {
  const { container, composer, stream } = await render(null, {
    HARNESS_CLAUDE: ["old", "current", "next"],
    HARNESS_CODEX: [],
  });
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("attached", {
        data: JSON.stringify(
          toJson(
            AttachedSchema,
            create(AttachedSchema, { lastCursor: 3n, spec: { harness: Harness.CLAUDE, model: "current" } })
          )
        ),
      })
    );
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "modelChanged", value: { model: "old" } }) })
    );
  });
  const picker = container.querySelector<HTMLInputElement>('input[aria-label="Model"]');
  expect(picker?.value).toBe("");
  expect(composer.disabled).toBe(true);
  expect(container.querySelector('[role="status"]')?.textContent).toBe("Catching up: 2 / 3 events");
  await act(async () => {
    stream.dispatchEvent(new MessageEvent("event", { data: event(3, { case: "harnessStarted", value: {} }) }));
  });
  expect(picker?.value).toBe("current");
  // Replay has caught up, but a command needs its durable Thread target before it can be sent.
  expect(composer.disabled).toBe(true);
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(4, { case: "modelChanged", value: { model: "next" } }) })
    );
  });
  expect(picker?.value).toBe("next");
});
