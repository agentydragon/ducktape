// @vitest-environment jsdom
import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Choice, Choices, Feedback, Handoff, Launch, SignalToggle } from "./affordances.tsx";
import { openLink, requestLaunch } from "./bridge.ts";
import { clearResponse, readResponse, sendFeedback, setResponse } from "./client.ts";

vi.mock("./bridge.ts", () => ({ openLink: vi.fn(), requestLaunch: vi.fn() }));
vi.mock("./client.ts", () => ({
  sendFeedback: vi.fn(),
  setResponse: vi.fn(),
  clearResponse: vi.fn(),
  readResponse: vi.fn().mockResolvedValue(null),
}));

window.matchMedia ??= ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener: () => {},
  removeListener: () => {},
  addEventListener: () => {},
  removeEventListener: () => {},
  dispatchEvent: () => false,
})) as typeof window.matchMedia;

function render(ui: ReactElement) {
  return rtlRender(<MantineProvider>{ui}</MantineProvider>);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Handoff", () => {
  it("opens a claude.ai/new deep-link with the encoded prompt via the gated openLink", () => {
    render(<Handoff prompt="review the $695 fee" label="Send to Claude" />);
    fireEvent.click(screen.getByRole("button", { name: /Send to Claude/ }));
    expect(openLink).toHaveBeenCalledWith("https://claude.ai/new?q=review%20the%20%24695%20fee");
  });

  it("defaults the label", () => {
    render(<Handoff prompt="x" />);
    expect(screen.getByRole("button", { name: /Send to Claude/ })).toBeTruthy();
  });

  it("shows the Claude logomark alongside the imperative label", () => {
    const { container } = render(<Handoff prompt="x" label="Debug test_foo flakiness" />);
    expect(container.querySelector("svg")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Debug test_foo flakiness/ })).toBeTruthy();
  });
});

describe("Choices", () => {
  it("records the picked choice as a feedback note scoped to the item, prefixed by the question", async () => {
    vi.mocked(sendFeedback).mockResolvedValue(undefined);
    render(
      <Choices item="i1" prompt="How did the dentist visit go?">
        <Choice value="Missed it" />
        <Choice value="Went, as expected" />
      </Choices>
    );
    fireEvent.click(screen.getByRole("button", { name: "Went, as expected" }));
    expect(sendFeedback).toHaveBeenCalledWith("How did the dentist visit go? → Went, as expected", "i1");
    expect(await screen.findByText(/recorded: Went, as expected/)).toBeTruthy();
  });

  it("records a choice's value while showing its child label (like <option value>)", async () => {
    vi.mocked(sendFeedback).mockResolvedValue(undefined);
    render(
      <Choices prompt="Booked?">
        <Choice value="yes">Yes, all set</Choice>
      </Choices>
    );
    fireEvent.click(screen.getByRole("button", { name: "Yes, all set" }));
    expect(sendFeedback).toHaveBeenCalledWith("Booked? → yes", undefined);
  });

  it("captures a free-text answer via Other…", async () => {
    vi.mocked(sendFeedback).mockResolvedValue(undefined);
    render(
      <Choices prompt="How did it go?">
        <Choice value="A" />
      </Choices>
    );
    fireEvent.click(screen.getByRole("button", { name: "Other…" }));
    fireEvent.change(screen.getByPlaceholderText(/Describe how it went/), {
      target: { value: "rescheduled to Tuesday" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(sendFeedback).toHaveBeenCalledWith("How did it go? → other: rescheduled to Tuesday", undefined);
    expect(await screen.findByText(/recorded: other — rescheduled to Tuesday/)).toBeTruthy();
  });

  it("renders nothing for a <choice> outside a <choices>", () => {
    const { container } = render(<Choice value="orphan" />);
    expect(container.querySelector("button")).toBeNull();
  });
});

describe("SignalToggle", () => {
  it("prefills the current answer (pressed) and records a new pick to the responses log", async () => {
    vi.mocked(readResponse).mockResolvedValue("went");
    vi.mocked(setResponse).mockResolvedValue(undefined);
    render(
      <SignalToggle scope="dentist-appt" field="status">
        <Choice value="went">Went</Choice>
        <Choice value="missed">Missed</Choice>
      </SignalToggle>
    );
    expect(readResponse).toHaveBeenCalledWith("dentist-appt", "status");
    // Prefill marks the current answer pressed…
    await screen.findByRole("button", { name: "Went", pressed: true });
    // …and picking another records it.
    fireEvent.click(screen.getByRole("button", { name: "Missed" }));
    expect(setResponse).toHaveBeenCalledWith("dentist-appt", "status", "missed");
  });

  it("clears the slot when the active answer is re-picked (radio toggle-off)", async () => {
    vi.mocked(readResponse).mockResolvedValue("yes");
    vi.mocked(clearResponse).mockResolvedValue(undefined);
    render(
      <SignalToggle scope="s" field="booked">
        <Choice value="yes">Yes</Choice>
      </SignalToggle>
    );
    fireEvent.click(await screen.findByRole("button", { name: "Yes", pressed: true }));
    expect(clearResponse).toHaveBeenCalledWith("s", "booked");
    expect(setResponse).not.toHaveBeenCalled();
  });
});

describe("Launch", () => {
  it("asks the shell to launch the prompt and surfaces the session link on success", async () => {
    vi.mocked(requestLaunch).mockResolvedValue({ type: "launchResult", id: "1", ok: true, sessionUrl: "https://s/x" });
    render(<Launch prompt="run the backup" label="Launch" />);
    fireEvent.click(screen.getByRole("button", { name: /Launch/ }));
    expect(requestLaunch).toHaveBeenCalledWith("run the backup");
    const link = await screen.findByText("open session →");
    fireEvent.click(link);
    expect(openLink).toHaveBeenCalledWith("https://s/x");
  });

  it("shows the shell's reason and lets the operator retry when the launch is declined", async () => {
    vi.mocked(requestLaunch).mockResolvedValue({ type: "launchResult", id: "1", ok: false, reason: "cancelled" });
    render(<Launch prompt="x" />);
    fireEvent.click(screen.getByRole("button", { name: /Launch run/ }));
    expect(await screen.findByText("cancelled")).toBeTruthy();
    // The button comes back so the operator can try again.
    expect(screen.getByRole("button", { name: /Launch run/ })).toBeTruthy();
  });
});

describe("Feedback", () => {
  it("sends the canned text (scoped to the item) and confirms", async () => {
    vi.mocked(sendFeedback).mockResolvedValue(undefined);
    render(<Feedback text="not useful" label="👎 not useful" item="i1" />);
    fireEvent.click(screen.getByRole("button", { name: /not useful/ }));
    expect(sendFeedback).toHaveBeenCalledWith("not useful", "i1");
    expect(await screen.findByText("✓ sent")).toBeTruthy();
  });

  it("surfaces a send failure instead of a false confirmation", async () => {
    vi.mocked(sendFeedback).mockRejectedValue(new Error("offline"));
    render(<Feedback text="x" />);
    fireEvent.click(screen.getByRole("button", { name: /Send feedback/ }));
    await waitFor(() => expect(screen.getByText("offline")).toBeTruthy());
    expect(screen.queryByText("✓ sent")).toBeNull();
  });
});
