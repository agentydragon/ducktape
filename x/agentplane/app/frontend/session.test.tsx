// @vitest-environment happy-dom

import { create, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { sendInput } from "./client";
import { EventSchema, ItemKind } from "./protocol_pb";
import { SessionView } from "./session";

vi.mock("./client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./client")>()),
  sendInput: vi.fn(),
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];

afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

function event(sequence: number, observation: MessageInitShape<typeof EventSchema>["observation"]): string {
  return JSON.stringify(toJson(EventSchema, create(EventSchema, { sequence: BigInt(sequence), observation })));
}

async function render(): Promise<{ container: HTMLDivElement; composer: HTMLTextAreaElement; stream: EventTarget }> {
  const stream = new EventTarget();
  vi.stubGlobal(
    "EventSource",
    class {
      addEventListener = stream.addEventListener.bind(stream);
      close(): void {}
    }
  );
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () => {
    root.render(
      <MantineProvider>
        <MemoryRouter>
          <SessionView sandbox="composer-test" sessionId="session-test" onBack={() => {}} />
        </MemoryRouter>
      </MantineProvider>
    );
  });
  await act(async () => {
    stream.dispatchEvent(new MessageEvent("event", { data: JSON.stringify({ sequence: "1", harnessStarted: {} }) }));
  });
  const composer = container.querySelector("textarea");
  if (!composer) throw new Error("Missing composer");
  return { container, composer, stream };
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
  vi.mocked(sendInput).mockReturnValue(new Promise<void>((resolve) => (acknowledge = resolve)));
  const { container, composer } = await render();
  await type(composer, "hello");
  await act(async () => {
    enter(composer);
    enter(composer);
  });
  expect(sendInput).toHaveBeenCalledOnce();
  expect(container.querySelector('[role="status"]')?.textContent).toBe("Sending…");
  expect(composer.disabled).toBe(true);
  expect(composer.value).toBe("hello");
  await act(async () => acknowledge());
  expect(container.querySelector('[role="status"]')).toBeNull();
  expect(composer.disabled).toBe(false);
  expect(composer.value).toBe("");
});

it("reuses the input id after an ambiguous failure, but gives a new submission its own id", async () => {
  vi.mocked(sendInput).mockRejectedValueOnce(new Error("response lost")).mockResolvedValue(undefined);
  const { container, composer } = await render();
  await type(composer, "hello");
  await act(async () => enter(composer));
  expect(container.textContent).toContain("response lost");
  expect(composer.value).toBe("hello");
  expect(composer.disabled).toBe(false);
  await act(async () => enter(composer));
  expect(vi.mocked(sendInput).mock.calls[1]).toEqual(vi.mocked(sendInput).mock.calls[0]);
  await type(composer, "hello");
  await act(async () => enter(composer));
  expect(vi.mocked(sendInput).mock.calls[2]?.[2]).not.toBe(vi.mocked(sendInput).mock.calls[0]?.[2]);
});

it("gives edited text a new input id after a failure", async () => {
  vi.mocked(sendInput).mockRejectedValueOnce(new Error("response lost")).mockResolvedValue(undefined);
  const { composer } = await render();
  await type(composer, "hello");
  await act(async () => enter(composer));
  await type(composer, "different message");
  await act(async () => enter(composer));
  expect(vi.mocked(sendInput).mock.calls[1]?.[2]).not.toBe(vi.mocked(sendInput).mock.calls[0]?.[2]);
  expect(vi.mocked(sendInput).mock.calls[1]?.[3]).toBe("different message");
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
