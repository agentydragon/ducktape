// @vitest-environment happy-dom

import { create, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { command, findThread, models, type ThreadView } from "./client";
import { CommandSchema, type Command } from "../../protocol/command_pb";
import { EventSchema, ItemKind, TurnStatus } from "../../protocol/event_pb";
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
const THREAD: ThreadView = {
  id: "10000000-0000-4000-8000-000000000001",
  sandbox: "composer-test",
  session_id: "session-test",
  harness: "HARNESS_CLAUDE",
  model: "test-model",
  cwd: "/test-work",
  created_at: "2026-01-01T00:00:00Z",
  name: null,
  archived: false,
  last_cursor: 1,
  last_event_at: null,
  harness_state: "HARNESS_STATE_RUNNING",
};

beforeEach(() => localStorage.clear());

afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
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

function admission(value: Command, cursor = 2n) {
  return create(EventEntrySchema, {
    cursor,
    origin: { sourceId: "test-runner", sequence: cursor },
    event: { observation: { case: "commandAdmitted", value: { command: value } } },
  });
}

function retry(container: HTMLDivElement): void {
  const button = [...container.querySelectorAll("button")].find((button) => button.textContent === "Retry");
  if (!button) throw new Error("Missing Retry");
  button.click();
}

it("retains input before HTTP, suppresses repeated Enter, and allows another command before admission", async () => {
  let acknowledge = (): void => {
    throw new Error("Acknowledgment is not initialized");
  };
  vi.mocked(command)
    .mockImplementationOnce((_thread, value) => {
      expect(localStorage.length).toBe(1);
      return new Promise((resolve) => {
        acknowledge = () => resolve(admission(value));
      });
    })
    .mockImplementation(async (_thread, value) => admission(value, 3n));
  const { container, composer } = await render();
  await type(composer, "hello");
  await act(async () => {
    enter(composer);
    enter(composer);
  });
  expect(command).toHaveBeenCalledOnce();
  expect(container.textContent).toContain("Awaiting saved confirmation");
  expect(composer.disabled).toBe(false);
  expect(composer.value).toBe("");
  expect(container.querySelector(".agentplane-user-bubble")).toBeNull();
  await type(composer, "second message");
  await act(async () => enter(composer));
  expect(command).toHaveBeenCalledTimes(2);
  await act(async () => acknowledge());
  expect(container.textContent).toContain("Saved · replay catching up");
  expect(composer.disabled).toBe(false);
  expect(composer.value).toBe("");
});

it("restores unconfirmed input after remount and retries the exact command", async () => {
  vi.mocked(command)
    .mockRejectedValueOnce(new Error("response lost"))
    .mockImplementation(async (_thread, value) => admission(value));
  const { container, composer } = await render();
  await type(composer, "hello");
  await act(async () => enter(composer));
  expect(container.textContent).toContain("response lost");
  expect(composer.value).toBe("");
  expect(composer.disabled).toBe(false);
  const old = mounted.pop();
  if (!old) throw new Error("Missing mounted page");
  await act(async () => old.root.unmount());
  old.container.remove();
  const restored = await render();
  expect(restored.container.textContent).toContain("hello");
  expect(restored.container.textContent).toContain("Awaiting saved confirmation");
  expect(command).toHaveBeenCalledOnce();
  await act(async () => retry(restored.container));
  expect(vi.mocked(command).mock.calls[1]).toEqual(vi.mocked(command).mock.calls[0]);
  await type(restored.composer, "hello");
  await act(async () => enter(restored.composer));
  expect(vi.mocked(command).mock.calls[2]?.[1].commandId).not.toBe(vi.mocked(command).mock.calls[0]?.[1].commandId);
});

it("gives edited text a new input id after a failure", async () => {
  vi.mocked(command)
    .mockRejectedValueOnce(new Error("response lost"))
    .mockImplementation(async (_thread, value) => admission(value));
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

it("preserves draft and sends nothing if local retention fails", async () => {
  const { container, composer } = await render();
  vi.spyOn(localStorage, "setItem").mockImplementationOnce(() => {
    throw new Error("storage full");
  });
  await type(composer, "do not lose this");
  await act(async () => enter(composer));
  expect(command).not.toHaveBeenCalled();
  expect(composer.value).toBe("do not lose this");
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("storage full");
});

it("does not regress streamed admission when the HTTP reply is lost", async () => {
  let fail = (_error: Error): void => {
    throw new Error("Missing request");
  };
  vi.mocked(command).mockImplementation(
    () =>
      new Promise((_resolve, reject) => {
        fail = reject;
      })
  );
  const { container, composer, stream } = await render();
  await type(composer, "saved input");
  await act(async () => enter(composer));
  const value = vi.mocked(command).mock.calls[0][1];
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "commandAdmitted", value: { command: value } }) })
    );
  });
  expect(container.textContent).toContain("Saved · awaiting effect");
  await act(async () => fail(new Error("lost reply")));
  expect(container.textContent).not.toContain("lost reply");
  expect(container.textContent).not.toContain("Awaiting saved confirmation");
  expect(localStorage.length).toBe(0);
  expect(container.querySelector(".agentplane-user-bubble")).toBeNull();
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(3, { case: "turnStarted", value: { turnId: "turn" } }) })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(4, {
          case: "harnessUserMessageConfirmed",
          value: {
            harnessMessageId: "native",
            turnId: "turn",
            text: "saved input",
            originCommandIds: [value.commandId],
          },
        }),
      })
    );
  });
  expect(container.querySelectorAll(".agentplane-user-bubble")).toHaveLength(1);
  expect(container.querySelector('[aria-label="Pending commands"]')).toBeNull();
});

it("keeps replayed admission authoritative when local cleanup fails and storage changes", async () => {
  vi.mocked(command).mockRejectedValue(new Error("lost reply"));
  const { container, composer, stream } = await render();
  await type(composer, "already saved");
  await act(async () => enter(composer));
  const value = vi.mocked(command).mock.calls[0][1];
  vi.spyOn(localStorage, "removeItem").mockImplementation(() => {
    throw new Error("local cleanup failed");
  });
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "commandAdmitted", value: { command: value } }) })
    );
  });
  await act(async () => window.dispatchEvent(new StorageEvent("storage", { key: null })));
  expect(localStorage.length).toBe(1);
  expect(container.textContent).toContain("Saved · awaiting effect");
  expect(container.textContent).not.toContain("Awaiting saved confirmation");
  expect(container.textContent).not.toContain("lost reply");
  expect([...container.querySelectorAll("button")].some((button) => button.textContent === "Retry")).toBe(false);
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("local cleanup failed");
});

it("does not advance replay from a later HTTP admission or skip intervening assistant output", async () => {
  vi.mocked(command).mockImplementation(async (_thread, value) => admission(value, 5n));
  const { container, composer, stream } = await render();
  await type(composer, "queued input");
  await act(async () => enter(composer));
  const value = vi.mocked(command).mock.calls[0][1];
  expect(container.textContent).toContain("Saved · replay catching up");
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "turnStarted", value: { turnId: "turn" } }) })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(3, { case: "itemStarted", value: { itemId: "answer", kind: ItemKind.ASSISTANT_TEXT } }),
      })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(4, { case: "textDelta", value: { itemId: "answer", text: "intervening output" } }),
      })
    );
    stream.dispatchEvent(
      new MessageEvent("event", { data: JSON.stringify(toJson(EventEntrySchema, admission(value, 5n))) })
    );
  });
  expect(container.textContent).toContain("intervening output");
  expect(container.textContent).toContain("Saved · awaiting effect");
  expect(container.querySelector('[role="alert"]')).toBeNull();
});

it("keeps targeted interrupt pending after admission and removes it only on the causal stopped turn", async () => {
  vi.mocked(command).mockImplementation(async (_thread, value) => admission(value, 3n));
  const { container, composer, stream } = await render();
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "turnStarted", value: { turnId: "clicked-turn" } }) })
    );
  });
  const interrupt = container.querySelector<HTMLButtonElement>('button[aria-label="Interrupt"]');
  if (!interrupt) throw new Error("Missing Interrupt");
  await act(async () => interrupt.click());
  const value = vi.mocked(command).mock.calls[0][1];
  expect(value.operation).toMatchObject({ case: "interruptTurn", value: { turnId: "clicked-turn" } });
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: JSON.stringify(toJson(EventEntrySchema, admission(value, 3n))) })
    );
  });
  expect(container.textContent).toContain("Interrupt turn clicked-turn");
  expect(container.textContent).toContain("Saved · awaiting effect");
  expect(composer.disabled).toBe(false);
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(4, {
          case: "turnCompleted",
          value: { turnId: "clicked-turn", status: TurnStatus.INTERRUPTED, interruptedByCommandId: value.commandId },
        }),
      })
    );
  });
  expect(container.querySelector('[aria-label="Pending commands"]')).toBeNull();
  expect(container.textContent).toContain("INTERRUPTED");
});

it("keeps the applied model until its causal effect while allowing the next input", async () => {
  vi.mocked(command).mockImplementation(async (_thread, value) =>
    admission(value, value.operation.case === "changeModel" ? 2n : 3n)
  );
  const { container, composer, stream } = await render(THREAD, {
    HARNESS_CLAUDE: ["test-model", "next"],
    HARNESS_CODEX: [],
  });
  const picker = container.querySelector<HTMLInputElement>('input[aria-label="Model"]');
  if (!picker) throw new Error("Missing Model");
  await act(async () => picker.click());
  const option = [...document.querySelectorAll<HTMLElement>('[role="option"]')].find(
    (value) => value.textContent === "next"
  );
  if (!option) throw new Error("Missing next model option");
  await act(async () => option.click());
  const model = vi.mocked(command).mock.calls[0][1];
  expect(model.operation).toMatchObject({ case: "changeModel", value: { model: "next" } });
  await act(async () =>
    stream.dispatchEvent(
      new MessageEvent("event", { data: JSON.stringify(toJson(EventEntrySchema, admission(model))) })
    )
  );
  expect(picker.value).toBe("test-model");
  expect(container.textContent).toContain("Change model to next");
  expect(container.textContent).toContain("Saved · awaiting effect");
  await type(composer, "apply it on this turn");
  await act(async () => enter(composer));
  expect(command).toHaveBeenCalledTimes(2);
  const input = vi.mocked(command).mock.calls[1][1];
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: JSON.stringify(toJson(EventEntrySchema, admission(input, 3n))) })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(4, { case: "modelChanged", value: { model: "next", commandId: model.commandId } }),
      })
    );
  });
  expect(picker.value).toBe("next");
  expect(container.textContent).not.toContain("Change model to next");
  expect(container.textContent).toContain("apply it on this turn");
});

it("shows a failed model command as a command outcome, not a user message", async () => {
  const { container, stream } = await render();
  const model = create(CommandSchema, {
    commandId: "model",
    operation: { case: "changeModel", value: { model: "unsupported" } },
  });
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: JSON.stringify(toJson(EventEntrySchema, admission(model))) })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(3, { case: "commandFailed", value: { commandId: "model", reason: "model unavailable" } }),
      })
    );
  });
  expect(container.querySelector('[aria-label="Command outcomes"]')?.textContent).toContain(
    "Change model to unsupported"
  );
  expect(container.textContent).toContain("Failed: model unavailable");
  expect(container.querySelector(".agentplane-user-bubble")).toBeNull();
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
