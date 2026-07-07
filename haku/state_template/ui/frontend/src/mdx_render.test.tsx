// @vitest-environment jsdom
// Render-level coverage for Mdx, on top of the pure-function tests in mdx.test.ts. This is the
// regression test for the 2026-07-02 bug: the gateway's CSP (`script-src 'self'`, no
// `unsafe-eval`; base #2711) blocked the old `@mdx-js/mdx` runtime `evaluate()` outright ("Couldn't
// render this content: ... unsafe-eval ..."), so every run note failed to render. Mdx no longer
// evaluates anything (see mdx.tsx's domToReact) — these tests exercise the paths that used to go
// through eval: internal-link interception, widget embedding, and GFM task lists.

import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { openLink } from "./bridge.ts";
import { callToolRequest, sendFeedback, setResponse } from "./client.ts";
import { Mdx } from "./mdx.tsx";

vi.mock("./bridge.ts", () => ({ openLink: vi.fn(), requestLaunch: vi.fn(), notifyRouteChanged: vi.fn() }));
vi.mock("./client.ts", () => ({
  sendFeedback: vi.fn().mockResolvedValue(undefined),
  setResponse: vi.fn().mockResolvedValue(undefined),
  clearResponse: vi.fn().mockResolvedValue(undefined),
  readResponse: vi.fn().mockResolvedValue(null),
  callToolRequest: vi.fn().mockResolvedValue({
    tool_call_id: "tc_1",
    server_id: "grocy-sf",
    status: "pending_approval",
  }),
}));

// jsdom has no matchMedia; MantineProvider's color-scheme detection needs it.
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

// Widgets (Callout/StatusBadge) are Mantine components and need a MantineProvider ancestor.
function render(ui: ReactElement) {
  return rtlRender(<MantineProvider>{ui}</MantineProvider>);
}

describe("Mdx", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders plain markdown", () => {
    render(<Mdx source={"# Title\n\nSome **bold** text."} />);
    expect(screen.getByRole("heading", { name: "Title" })).toBeTruthy();
    expect(screen.getByText("bold").tagName).toBe("STRONG");
  });

  it("intercepts an internal .md link via onNavigate instead of following href", () => {
    const onNavigate = vi.fn();
    render(
      <Mdx
        source="[items checklist](../../procedures/propagation/items.md)"
        basePath="runs/2026-07-02/x.md"
        onNavigate={onNavigate}
      />
    );
    const link = screen.getByRole("link", { name: "items checklist" });
    link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(onNavigate).toHaveBeenCalledWith("procedures/propagation/items.md");
  });

  it("opens an external link in a new tab, unintercepted", () => {
    const onNavigate = vi.fn();
    render(<Mdx source="[docs](https://example.com/x)" onNavigate={onNavigate} />);
    const link = screen.getByRole("link", { name: "docs" }) as HTMLAnchorElement;
    expect(link.target).toBe("_blank");
    expect(link.rel).toBe("noreferrer");
  });

  it("embeds the Callout and StatusBadge widgets from literal HTML-attribute syntax", () => {
    // CommonMark only treats a custom tag as a block (vs. wrapping it in a <p>) when its opening
    // tag is alone on its line, blank-line-separated — see procedures/garden.md's widget section.
    const source = [
      '<callout kind="warning" title="Heads up">',
      "",
      "watch out",
      "",
      "</callout>",
      "",
      '<statusbadge status="open" color="teal">',
      "</statusbadge>",
    ].join("\n");
    render(<Mdx source={source} />);
    expect(screen.getByText("Heads up")).toBeTruthy();
    expect(screen.getByText("watch out")).toBeTruthy();
    expect(screen.getByText("open")).toBeTruthy();
  });

  it("passes a widget's custom attributes through DOMPurify to the component", () => {
    // Regression: DOMPurify strips non-standard attributes (prompt/which/text/…) unless they're in
    // ADD_ATTR. When they were missing, an embedded <handoff prompt="…"> reached the button with an
    // empty prompt and opened a blank claude.ai/new. Rendering through Mdx (not the component
    // directly) is what exercises the sanitizer — clicking proves the prompt survived it.
    render(<Mdx source={'<handoff prompt="do the thing" label="Hand off"></handoff>'} />);
    fireEvent.click(screen.getByRole("button", { name: /Hand off/ }));
    expect(openLink).toHaveBeenCalledWith("https://claude.ai/new?q=do%20the%20thing");
  });

  it("composes <choices>/<choice> through the sanitizer, wiring the pick up via context", async () => {
    // Proves the compound-widget path end to end: DOMPurify keeps the nested tags, domToReact
    // rebuilds them, and the <choice> button reaches its parent <choices>'s context to record the
    // pick — the composition the flat options="a|b" attribute replaced.
    const source = [
      '<choices prompt="Booked?" item="i1">',
      '<choice value="yes">Yes, all set</choice>',
      "</choices>",
    ].join("\n");
    render(<Mdx source={source} />);
    fireEvent.click(screen.getByRole("button", { name: "Yes, all set" }));
    expect(sendFeedback).toHaveBeenCalledWith("Booked? → yes", "i1");
  });

  it("wires <signal-toggle> scope/field through the sanitizer to the responses log", async () => {
    // scope is a standard attr (kept by default); field needs ADD_ATTR. A stripped field would
    // silently write to responses/<scope>/.yaml — this proves both segments reach setResponse.
    const source = [
      '<signal-toggle scope="dentist-appt" field="status">',
      '<choice value="went">Went</choice>',
      "</signal-toggle>",
    ].join("\n");
    render(<Mdx source={source} />);
    fireEvent.click(await screen.findByRole("button", { name: "Went" }));
    expect(setResponse).toHaveBeenCalledWith("dentist-appt", "status", "went");
  });

  it("wires <tool-call> request through the sanitizer to the backend proxy", async () => {
    render(
      <Mdx
        source={[
          '<tool-call request="2026-07-thrive-box-grocy-stock-add" label="Add to Grocy">',
          "",
          "</tool-call>",
        ].join("\n")}
      />
    );
    const button = await screen.findByRole("button", { name: "Add to Grocy" });
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(button);
    expect(callToolRequest).toHaveBeenCalledWith("2026-07-thrive-box-grocy-stock-add");
    expect(await screen.findByText("waiting in console")).toBeTruthy();
  });

  it("reads a <handoff> prompt authored inside the tag (a fenced code block), multi-line intact", () => {
    // Long/multi-line prompts can't be a literal attribute (a multi-line tag isn't a marked HTML
    // block). Authored inside the tag as a code block, the prompt survives verbatim.
    const source = [
      '<handoff label="Start the renewal">',
      "",
      "```text",
      'Renew the "example" subscription.',
      "Step 1: call the provider.",
      "```",
      "",
      "</handoff>",
    ].join("\n");
    render(<Mdx source={source} />);
    fireEvent.click(screen.getByRole("button", { name: /Start the renewal/ }));
    const arg = vi.mocked(openLink).mock.calls[0][0];
    expect(arg.startsWith("https://claude.ai/new?q=")).toBe(true);
    const q = decodeURIComponent(arg.slice("https://claude.ai/new?q=".length));
    expect(q).toContain('Renew the "example" subscription.');
    expect(q).toContain("Step 1: call the provider.");
  });

  it("renders a GFM task-list checkbox as disabled and checked/unchecked", () => {
    render(<Mdx source={"- [ ] todo\n- [x] done"} />);
    const boxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    expect(boxes).toHaveLength(2);
    expect(boxes[0].checked).toBe(false);
    expect(boxes[1].checked).toBe(true);
    expect(boxes.every((b) => b.disabled)).toBe(true);
  });

  it("shows an error instead of throwing if rendering fails", () => {
    // DOMParser input is already-sanitized HTML by construction, so this simulates a downstream
    // failure rather than a realistic markdown string — just proving the catch path renders, not
    // (as the old eval-based Mdx did) leaves the operator staring at a raw CSP violation message.
    const bad: unknown = {
      toString: () => {
        throw new Error("boom");
      },
    };
    render(<Mdx source={bad as string} />);
    expect(screen.getByText(/Couldn't render this content/)).toBeTruthy();
  });
});
