// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AllQuotasView, WindowView } from "./schema";
import { QuotaBoard } from "./board";

let root: Root;
let container: HTMLDivElement;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

function render(quotas: AllQuotasView, now = Date.parse("2026-01-15T12:00:00Z")): void {
  act(() =>
    root.render(
      <MantineProvider env="test">
        <QuotaBoard quotas={quotas} now={now} />
      </MantineProvider>
    )
  );
}

function sampleQuotas(): AllQuotasView {
  const window: WindowView = {
    name: null,
    display: true,
    used_percent: 60,
    reset_seconds: 9000,
    window_seconds: 18000,
    reset_at: "2026-01-15T14:30:00Z",
    label: "5h",
    used: 60,
    reset: "2h 30m",
    pace: "+10 pp",
    forecast: "On pace",
    tint: "warn",
    elapsed: 0.5,
    exhausted: false,
    reset_pending: false,
  };
  return {
    fetched_at: "2026-01-15T12:00:00Z",
    presentation_at: "2026-01-15T12:00:00Z",
    connection_error: "quota transport timed out",
    providers: [
      {
        provider: "codex",
        label: "Codex",
        balances: [{ label: "Paid credits", value: "450 credits" }],
        grants: [
          {
            label: "Promo grant",
            remaining: "$26",
            exact_remaining: "$25.75",
            original: "$100.00",
            expires_at: "2026-02-01T10:30:00Z",
            scope: "Codex API",
            status: "scheduled",
            group: null,
          },
        ],
        stale_since: null,
        stale_at: "2026-01-15T14:45:00Z",
        status: null,
        error_summary: "Authentication failed",
        error_detail: "401 invalid_token returned from the provider",
        display: {
          source: "current",
          observed_at: "2026-01-15T12:00:00Z",
          result: {
            kind: "success",
            windows: [],
            extra_spend: { is_enabled: true, used_usd: 25.5, monthly_limit_usd: 100, utilization: 25.5 },
            available_reset_credits: 2,
            available_reset_credit_expiries: ["2026-01-20T12:00:00Z", "2026-02-01T12:00:00Z"],
            reset_credit_expiry_lookup_failed: false,
          },
        },
        last_output: {
          fetched_at: "2026-01-15T12:00:00Z",
          result: { kind: "error", error: "401 invalid_token returned from the provider" },
        },
        last_success: null,
        currently_over_plan: false,
        extra_status: "informational",
        monthly_extra_spend: { is_enabled: true, used_usd: 25.5, monthly_limit_usd: 100, utilization: 25.5 },
        paid_credits_active: false,
        burn: null,
        display_windows: [window],
        display_openai_platform_credits: null,
        display_anthropic_platform_credits: null,
        summary_tint: "hot",
        summary_index: 0,
      },
      {
        provider: "anthropic_platform",
        label: "Anthropic API",
        balances: [{ label: "Available balance", value: "$75.00", group: "Research org" }],
        grants: [
          {
            label: "Promo grant",
            remaining: "$25.00",
            original: "$50.00",
            expires_at: null,
            scope: "Claude API (Sonnet)",
            status: null,
            group: "Research org",
          },
        ],
        stale_since: null,
        stale_at: null,
        status: null,
        error_summary: null,
        error_detail: null,
        display: { source: "none", observed_at: null, result: null },
        last_output: { fetched_at: "2026-01-15T12:00:00Z", result: { kind: "success", windows: [] } },
        last_success: null,
        currently_over_plan: false,
        extra_status: "none",
        paid_credits_active: false,
        burn: null,
        display_windows: [],
        display_openai_platform_credits: null,
        display_anthropic_platform_credits: null,
        summary_tint: "unknown",
        summary_index: null,
      },
    ],
  } as unknown as AllQuotasView;
}

describe("QuotaBoard", () => {
  it("keeps exhaustion visible without repeating it in status; pending resets remain warnings", () => {
    const quotas = sampleQuotas();
    const exhausted = quotas.providers[0]!.display_windows[0]!;
    exhausted.exhausted = true;
    exhausted.used_percent = 100;
    exhausted.used = 100;
    render(quotas);
    let card = container.querySelector('.aiquota-card[aria-label="Codex quota"]')!;
    expect(card.querySelector(".aiquota-status-row")).toBeNull();
    expect(card.textContent).toContain("100%");
    expect(card.textContent).toContain("exhausted");

    render(quotas, Date.parse("2026-01-15T14:31:00Z"));
    card = container.querySelector('.aiquota-card[aria-label="Codex quota"]')!;
    const status = card.querySelector(".aiquota-status-row")!;
    expect(status.textContent).toContain("awaiting updated usage");
    expect(status.classList.contains("aiquota-status-urgent")).toBe(false);
  });

  it("puts current extra enablement on the monthly row instead of duplicating exhausted status", () => {
    const quotas = sampleQuotas();
    const codex = quotas.providers[0]!;
    codex.display_windows[0]!.exhausted = true;
    codex.display_windows[0]!.used = 100;
    codex.display_windows[0]!.used_percent = 100;
    codex.currently_over_plan = true;
    codex.extra_status = "active";
    render(quotas);

    const card = container.querySelector('.aiquota-card[aria-label="Codex quota"]')!;
    expect(card.querySelector(".aiquota-status-row")).toBeNull();
    expect(card.textContent).toContain("Extra spend this month: $26 of $100 limit · extra usage enabled");
    expect(card.textContent).toContain("exhausted");
    expect(card.textContent).not.toContain("Included quota exhausted");
  });

  it("keeps a status fallback when active extra usage has no monthly-spend row", () => {
    const quotas = sampleQuotas();
    const codex = quotas.providers[0]!;
    codex.display_windows[0]!.exhausted = true;
    codex.currently_over_plan = true;
    codex.monthly_extra_spend = null;
    render(quotas);

    const card = container.querySelector('.aiquota-card[aria-label="Codex quota"]')!;
    expect(card.querySelector(".aiquota-status-row")?.textContent).toBe("Extra usage enabled");
    expect(card.textContent).not.toContain("Extra spend this month");
  });

  it.each([
    { raw: -5, displayed: -5, value: 0 },
    { raw: 0, displayed: 0, value: 0 },
    { raw: 99.9, displayed: 99, value: 99 },
    { raw: 100, displayed: 100, value: 100 },
    { raw: 105, displayed: 105, value: 100 },
  ])("exposes displayed usage $displayed within the meter range", ({ raw, displayed, value }) => {
    const quotas = sampleQuotas();
    const window = quotas.providers[0]!.display_windows[0]!;
    window.used_percent = raw;
    window.used = displayed;
    render(quotas);

    const meter = container.querySelector('[role="meter"]');
    expect(meter?.getAttribute("aria-valuenow")).toBe(String(value));
    expect(meter?.getAttribute("aria-valuemin")).toBe("0");
    expect(meter?.getAttribute("aria-valuemax")).toBe("100");
    expect(meter?.getAttribute("aria-valuetext")).toBe(`${displayed}% used, 50% of the window elapsed`);
    expect(meter?.getAttribute("aria-label")).toBe("5h quota usage");
    expect(container.querySelector(".aiquota-window-used")?.textContent).toBe(`${displayed}%`);
    expect(container.querySelector<HTMLElement>(".aiquota-meter-fill")?.style.width).toBe(
      `${Math.max(0, Math.min(100, raw))}%`
    );
  });

  it("groups providers, renders normalized balances and grants, and keeps paid windows visible", () => {
    const quotas = sampleQuotas();
    render(quotas);

    expect(container.querySelector(".aiquota-board")?.hasAttribute("aria-live")).toBe(false);
    expect(container.querySelectorAll(".aiquota-provider-group")).toHaveLength(2);
    expect(container.querySelectorAll(".aiquota-card")).toHaveLength(2);
    expect(container.textContent).toContain("Meter: fill = quota used; tick = time elapsed.");
    expect(container.textContent).toContain("Forecasts use average usage since the window started.");
    expect(container.querySelector(".aiquota-board-help")?.hasAttribute("open")).toBe(false);
    expect(container.textContent).toContain("Paid credits");
    expect(container.textContent).toContain("5h");
    expect(container.textContent).toContain("On pace");
    expect(container.textContent).toContain("Numeric pace: +10 pp");
    expect(container.textContent).toContain("$26 remaining");
    expect(container.textContent).toContain("Scope: Codex API");
    expect(container.textContent).toContain("scheduled");
    expect(container.textContent).toContain("Extra spend this month: $26 of $100 limit");
    expect(container.textContent).toContain("Research org");

    const grantDetails = container.querySelector(".aiquota-reading-row .aiquota-details");
    expect(grantDetails?.hasAttribute("open")).toBe(false);
    expect(grantDetails?.textContent).toContain("Original amount");
    expect(grantDetails?.textContent).toContain("$100.00");
    expect(grantDetails?.textContent).toContain("Exact remaining$25.75");
    expect(container.querySelector(".aiquota-source-error .aiquota-error-details pre")?.textContent).toBe(
      "quota transport timed out"
    );
    expect(container.querySelector(".aiquota-card .aiquota-error-details pre")?.textContent).toBe(
      "401 invalid_token returned from the provider"
    );
  });

  it("places subscription usage before balances and reset details", () => {
    render(sampleQuotas());
    const card = container.querySelector('.aiquota-card[aria-label="Codex quota"]')!;
    const window = card.querySelector(".aiquota-window")!;
    const readings = card.querySelector(".aiquota-readings")!;
    const resets = card.querySelector(".aiquota-strip")!;
    expect(window.compareDocumentPosition(readings) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(window.compareDocumentPosition(resets) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("uses the optional grant hint while keeping older payloads compatible", () => {
    const quotas = sampleQuotas();
    const grant = quotas.providers[0]!.grants![0]!;
    grant.remaining = "$100";
    grant.original = "$100.00";
    render(quotas);
    expect(container.querySelector(".aiquota-reading-row .aiquota-details")?.textContent).toContain("Original amount");

    grant.original_repeats_remaining = true;
    render(quotas);
    expect(container.querySelector(".aiquota-reading-row .aiquota-details")?.textContent).not.toContain(
      "Original amount"
    );
  });

  it("does not add exact spend details when whole-dollar values are already exact", () => {
    const quotas = sampleQuotas();
    quotas.providers[0]!.monthly_extra_spend!.used_usd = 20;
    quotas.providers[0]!.monthly_extra_spend!.monthly_limit_usd = 100;
    render(quotas);
    expect(container.textContent).toContain("Extra spend this month: $20 of $100 limit");
    expect(container.textContent).not.toContain("Extra spend details");
  });

  it("updates only reset countdown and pending state as time advances without a new payload", () => {
    const quotas = sampleQuotas();
    render(quotas, Date.parse("2026-01-15T12:00:00Z"));
    expect(container.querySelector('.aiquota-card[aria-label="Codex quota"]')?.textContent).toContain(
      "Resets in 2h 30m"
    );
    expect(container.querySelector('.aiquota-card[aria-label="Codex quota"]')?.textContent).toContain("On pace");

    render(quotas, Date.parse("2026-01-15T14:31:00Z"));

    const codex = container.querySelector('.aiquota-card[aria-label="Codex quota"]');
    expect(codex?.textContent).not.toContain("paid credits available");
    expect(codex?.textContent).not.toContain("extra usage enabled");
    expect(codex?.textContent).toContain("Awaiting updated usage");
    expect(codex?.textContent).toContain("Reset time passed · awaiting updated usage");
    expect(codex?.textContent).not.toContain("Numeric pace: +10 pp");
    expect(codex?.textContent).not.toContain("On pace");
    expect(codex?.querySelector(".aiquota-badge-stale")).toBeNull();
  });

  it("renders an explicitly stale observation even with no provider fetch error", () => {
    const quotas = sampleQuotas();
    const codex = quotas.providers[0]!;
    codex.display_windows[0]!.exhausted = true;
    codex.paid_credits_active = true;
    codex.stale_at = "2026-01-15T12:10:00Z";
    codex.stale_since = null;
    codex.error_summary = null;
    codex.error_detail = null;
    codex.last_output = {
      fetched_at: "2026-01-15T12:00:00Z",
      result: { kind: "success", windows: [], reset_credit_expiry_lookup_failed: false },
    };
    render(quotas);
    expect(container.textContent).not.toContain("Included quota exhausted");
    expect(container.textContent).not.toContain("paid credits available");
    expect(container.textContent).toContain("Paid credits");
    expect(container.textContent).toContain("exhausted");
    render(quotas, Date.parse("2026-01-15T12:11:00Z"));

    expect(container.querySelector('.aiquota-card[aria-label="Codex quota"] .aiquota-badge-stale')?.textContent).toBe(
      "Data stale · 11m"
    );
    const card = container.querySelector('.aiquota-card[aria-label="Codex quota"]');
    expect(card?.querySelector(".aiquota-error-details")).toBeNull();
    expect(card?.textContent).not.toContain("paid credits available");
    expect(card?.textContent).not.toContain("extra usage enabled");
    expect(card?.textContent).toContain("Extra spend this month: $26");
    expect(card?.textContent).not.toContain("On pace");
    expect(card?.textContent).not.toContain("Numeric pace");
  });

  it("moves known zero resets into details after any displayed reset deadline passes", () => {
    const quotas = sampleQuotas();
    const codex = quotas.providers[0]!;
    const exhausted = codex.display_windows[0]!;
    exhausted.exhausted = true;
    const other = { ...exhausted, exhausted: false, reset_at: "2026-01-15T12:01:00Z" };
    codex.display_windows.push(other);
    const result = codex.display.result!;
    result.available_reset_credits = 0;
    render(quotas);
    const card = container.querySelector('.aiquota-card[aria-label="Codex quota"]')!;
    expect(card.querySelector(".aiquota-strip-line")?.textContent).toBe("Banked resets: 0");

    render(quotas, Date.parse("2026-01-15T12:01:00Z"));
    expect(card.querySelector(".aiquota-strip-line")).toBeNull();
    expect(card.querySelector(".aiquota-strip details p")?.textContent).toBe("Banked resets: 0");

    render(quotas, Date.parse("2026-01-15T14:45:00Z"));
    expect(card.querySelector(".aiquota-badge-stale")).not.toBeNull();
    expect(card.querySelector(".aiquota-strip details p")?.textContent).toBe("Banked resets: 0");
  });
});
