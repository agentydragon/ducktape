// @vitest-environment happy-dom

import { MantineProvider } from "@mantine/core";
import { act, type JSX } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SessionEvent } from "./api";
import { buildEventTimeline, EventTimelineStrip, type EventStrip, type EventTimelineItem } from "./event-timeline";

type FixtureItem = EventTimelineItem & { label: string };

function event(sequence: string | number, eventType = "system", marker = `fixture marker ${sequence}`): SessionEvent {
  const sequenceNumber = String(sequence);
  return {
    event_id: `fixture-event-${sequenceNumber}`,
    sequence_num: sequenceNumber,
    event_type: eventType,
    source: "worker",
    created_at: "2026-01-01T00:00:00Z",
    received_at: null,
    processing_at: null,
    processed_at: null,
    device_attestation_status: "DEVICE_ATTESTATION_STATUS_UNSPECIFIED",
    sent_by_account_id: null,
    payload: { subtype: "hook_response", marker },
  };
}

function item(label: string, events: SessionEvent[]): FixtureItem {
  return { id: label, label, events };
}

function isStrip(value: FixtureItem | EventStrip): value is EventStrip {
  return "kind" in value && value.kind === "event-strip";
}

function sequences(events: SessionEvent[]): string[] {
  return events.map((source) => source.sequence_num);
}

describe("buildEventTimeline", () => {
  it("places each raw event once around same-sequence and spanning presentation items", () => {
    const raw = Array.from({ length: 7 }, (_, index) => event(index + 1));
    const first = item("tool-run-starting-at-2", [raw[1]!, raw[4]!]);
    const sameAnchor = item("row-also-starting-at-2", [raw[1]!]);
    const later = item("row-starting-at-6", [raw[5]!]);

    const timeline = buildEventTimeline([first, sameAnchor, later], raw);
    expect(timeline.map((entry) => (isStrip(entry) ? `strip:${sequences(entry.events).join(",")}` : entry.id))).toEqual(
      ["strip:1", first.id, sameAnchor.id, "strip:2,3,4,5", later.id, "strip:6,7"]
    );
    const emitted = timeline.filter(isStrip).flatMap((strip) => strip.events);
    expect(emitted).toEqual(raw);
    expect(new Set(emitted.map((source) => source.event_id)).size).toBe(raw.length);
  });

  it("keeps item order and chronological exact event coverage when item anchors are nonmonotonic", () => {
    const raw = Array.from({ length: 6 }, (_, index) => event(index + 1));
    const sequenceFive = item("presentation-first-sequence-5", [raw[4]!]);
    const sequenceTwo = item("presentation-second-sequence-2", [raw[1]!]);
    const noEvents = item("presentation-row-without-events", []);
    const sequenceFour = item("presentation-last-sequence-4", [raw[3]!]);

    const timeline = buildEventTimeline([sequenceFive, sequenceTwo, noEvents, sequenceFour], raw);
    expect(timeline.filter((entry): entry is FixtureItem => !isStrip(entry)).map((entry) => entry.label)).toEqual([
      sequenceFive.label,
      sequenceTwo.label,
      noEvents.label,
      sequenceFour.label,
    ]);
    expect(timeline.filter(isStrip).map((strip) => sequences(strip.events))).toEqual([
      ["1", "2", "3", "4"],
      ["5", "6"],
    ]);
    const emitted = timeline.filter(isStrip).flatMap((strip) => strip.events);
    expect(sequences(emitted)).toEqual(["1", "2", "3", "4", "5", "6"]);
    expect(emitted.map((source) => source.event_id).sort()).toEqual(raw.map((source) => source.event_id).sort());
    expect(new Set(emitted.map((source) => source.event_id)).size).toBe(raw.length);
  });

  it("handles hook-only pages, empty history, large sequence numbers, and stable strip IDs", () => {
    const hookOnly = [event("9007199254740993"), event("9007199254740992")];
    const hookTimeline = buildEventTimeline([], hookOnly);
    expect(hookTimeline).toHaveLength(1);
    expect(isStrip(hookTimeline[0]!)).toBe(true);
    if (isStrip(hookTimeline[0]!)) {
      expect(sequences(hookTimeline[0]!.events)).toEqual(["9007199254740992", "9007199254740993"]);
    }

    expect(buildEventTimeline([], [])).toEqual([]);
    expect(buildEventTimeline([item("eventless", [])], [])).toEqual([item("eventless", [])]);
    expect(() => buildEventTimeline([], [event("not-a-sequence")])).toThrow();

    const initial = buildEventTimeline([], [event(1)]);
    const grown = buildEventTimeline([], [event(1), event(2)]);
    expect(initial[0]?.id).toBe(grown[0]?.id);
  });
});

let root: ReturnType<typeof createRoot> | null = null;
let container: HTMLDivElement | null = null;

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  container?.remove();
  root = null;
  container = null;
  vi.restoreAllMocks();
});

async function render(events: SessionEvent[]): Promise<void> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root?.render(
      <MantineProvider env="test">
        <EventTimelineStrip events={events} />
      </MantineProvider>
    )
  );
}

it("caps dots, opens the selected event batch below the strip, closes it on repeat, and opens all events", async () => {
  const raw = Array.from({ length: 15 }, (_, index) => event(index + 1));
  await render(raw);

  const strip = container?.querySelector<HTMLElement>("[data-event-strip]");
  expect(strip?.getAttribute("data-event-count")).toBe("15");
  expect(strip?.getAttribute("data-history-sequences")).toBe(raw.map((source) => source.sequence_num).join(" "));
  expect(strip?.querySelectorAll("button[data-event-dot]")).toHaveLength(8);
  expect(strip?.querySelectorAll("button[data-event-dot]").length).toBeLessThanOrEqual(12);
  expect(strip?.querySelector("[data-event-strip-details]")).toBeNull();

  const firstDot = strip?.querySelector<HTMLButtonElement>('button[data-event-dot][data-event-dot-sequences="1"]');
  expect(firstDot?.getAttribute("aria-expanded")).toBe("false");
  await act(async () => firstDot?.click());
  const details = strip?.querySelector<HTMLElement>("[data-event-strip-details]");
  expect(details).not.toBeNull();
  expect(details?.previousElementSibling?.getAttribute("aria-label")).toBe("Session activity events");
  expect(details?.textContent).toContain("1 of 1 loaded events");
  expect(details?.querySelector("[data-event-json]")).toBeNull();
  expect(firstDot?.getAttribute("aria-expanded")).toBe("true");

  await act(async () => firstDot?.click());
  expect(strip?.querySelector("[data-event-strip-details]")).toBeNull();
  expect(firstDot?.getAttribute("aria-expanded")).toBe("false");

  const allButton = strip?.querySelector<HTMLButtonElement>("button[data-event-strip-toggle]");
  expect(allButton?.getAttribute("aria-label")).toBe("Show all 15 events");
  await act(async () => allButton?.click());
  expect(strip?.querySelector("[data-event-strip-details]")?.textContent).toContain("15 of 15 loaded events");
  expect(allButton?.getAttribute("aria-expanded")).toBe("true");
});

it("filters accessible event details and mounts raw JSON only after a row is expanded", async () => {
  const raw = [
    event(1, "system", "fixture marker needle"),
    event(2, "assistant", "fixture marker haystack"),
    event(3, "system", "fixture marker other"),
  ];
  await render(raw);
  await act(async () => container?.querySelector<HTMLButtonElement>("[data-event-strip-toggle]")?.click());

  const kind = container?.querySelector<HTMLSelectElement>('select[aria-label="Event kind"]');
  expect(kind).not.toBeNull();
  if (kind !== null && kind !== undefined) {
    kind.value = "system · hook_response";
    await act(async () => kind.dispatchEvent(new Event("change", { bubbles: true })));
  }
  expect(container?.querySelector("[data-event-strip-details]")?.textContent).toContain("2 of 3 loaded events");

  const search = container?.querySelector<HTMLInputElement>('input[aria-label="Search event data"]');
  expect(search).not.toBeNull();
  if (search !== null && search !== undefined) {
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setValue?.call(search, "needle");
    await act(async () => search.dispatchEvent(new Event("input", { bubbles: true })));
  }
  expect(container?.querySelector("[data-event-strip-details]")?.textContent).toContain("1 of 3 loaded events");

  const rawRow = container?.querySelector<HTMLElement>('[data-raw-event][data-sequence="1"]');
  expect(rawRow?.querySelector("[data-event-json]")).toBeNull();
  await act(async () => rawRow?.querySelector("summary")?.click());
  await vi.waitFor(() => expect(rawRow?.querySelector("[data-event-json]")?.textContent).toContain("needle"));
});

it("keeps the selected inspector batch and filter state when strip growth rebuckets it", async () => {
  function TimelineHarness({ events }: { events: SessionEvent[] }): JSX.Element {
    const timeline = buildEventTimeline([], events);
    return (
      <>
        {timeline.map((entry) => (isStrip(entry) ? <EventTimelineStrip key={entry.id} events={entry.events} /> : null))}
      </>
    );
  }

  const raw = Array.from({ length: 17 }, (_, index) =>
    event(index + 1, "system", index === 12 ? "distinctive fixture text" : `fixture marker ${index + 1}`)
  );
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root?.render(
      <MantineProvider env="test">
        <TimelineHarness events={raw} />
      </MantineProvider>
    )
  );
  const selectedDot = [...(container?.querySelectorAll<HTMLButtonElement>("[data-event-dot]") ?? [])].find((dot) =>
    dot.dataset.eventDotSequences?.split(" ").includes("13")
  );
  expect(selectedDot).toBeDefined();
  await act(async () => selectedDot?.click());

  const search = container?.querySelector<HTMLInputElement>('input[aria-label="Search event data"]');
  expect(search).not.toBeNull();
  if (search !== null && search !== undefined) {
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setValue?.call(search, "distinctive fixture text");
    await act(async () => search.dispatchEvent(new Event("input", { bubbles: true })));
  }
  expect(container?.querySelector("[data-event-strip-details]")?.textContent).toContain("1 of 2 loaded events");

  await act(async () =>
    root?.render(
      <MantineProvider env="test">
        <TimelineHarness events={[...raw, event(18), event(19)]} />
      </MantineProvider>
    )
  );
  const retainedSearch = container?.querySelector<HTMLInputElement>('input[aria-label="Search event data"]');
  expect(retainedSearch?.value).toBe("distinctive fixture text");
  expect(container?.querySelector("[data-event-strip-details]")?.textContent).toContain("1 of 2 loaded events");
  if (retainedSearch !== null && retainedSearch !== undefined) {
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setValue?.call(retainedSearch, "");
    await act(async () => retainedSearch.dispatchEvent(new Event("input", { bubbles: true })));
  }
  expect(container?.querySelector("[data-event-strip-details]")?.textContent).toContain("2 of 2 loaded events");
  expect(container?.querySelector('[data-raw-event][data-sequence="13"]')).not.toBeNull();
  expect(container?.querySelector('[data-raw-event][data-sequence="14"]')).not.toBeNull();
  expect(container?.querySelector('[data-raw-event][data-sequence="12"]')).toBeNull();
  expect(container?.querySelector('[data-raw-event][data-sequence="15"]')).toBeNull();
});
