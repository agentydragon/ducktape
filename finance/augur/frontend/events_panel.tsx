import React, { useEffect, useMemo, useRef } from "react";
import { fmtNumber } from "./lib/format.ts";
import { fmtMetricValue } from "./lib/chart.ts";
import { useCurrencyDisplay } from "./hooks.ts";
import { ScenarioBadge } from "./scenario_tabs.tsx";
import {
  ROLLOUT_EVENT_COLORS,
  ROLLOUT_EVENT_KIND_ORDER,
  ROLLOUT_EVENT_KIND_LABELS,
  TABLE_NUMERIC_HEADER,
  eventGroupsByMonth,
  eventLabel,
  eventDetailText,
  eventColor,
  eventAmount,
} from "./data_helpers.ts";

export function SelectedRolloutEventsPanel({
  events,
  selectedSummary,
  activeScenario,
  loading,
  selectedEventMonthIndex,
  hoveredEventMonthIndex,
  onSelectEventMonth,
  onHoverEventMonth,
}) {
  const { display: currencyDisplay } = useCurrencyDisplay();
  const groups = useMemo(() => eventGroupsByMonth(events), [events]);
  const groupRefs = useRef(new Map());

  useEffect(() => {
    if (selectedEventMonthIndex == null) return;
    groupRefs.current.get(selectedEventMonthIndex)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selectedEventMonthIndex, groups]);

  if (!selectedSummary) return null;

  const selectMonthFromKeyboard = (keyboardEvent, monthIndex) => {
    if (keyboardEvent.key !== "Enter" && keyboardEvent.key !== " ") return;
    keyboardEvent.preventDefault();
    onSelectEventMonth?.(monthIndex);
  };

  return (
    <div className="border-t border-slate-200 dark:border-slate-700">
      <div className="flex flex-col gap-1 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <span className="augur-eyebrow">Selected rollout events</span>
            {activeScenario && <ScenarioBadge label={activeScenario.label} color={activeScenario.color} />}
          </div>
          <div className="mt-1 text-xs augur-muted">Seed {selectedSummary.seed}</div>
        </div>
        <div className="text-xs font-semibold augur-tabular augur-muted">
          {loading ? "Loading events" : `${fmtNumber(groups.length)} months / ${fmtNumber(events.length)} events`}
        </div>
      </div>
      {loading ? (
        <div className="px-4 pb-4 text-sm augur-muted">Loading...</div>
      ) : events.length === 0 ? (
        <div className="px-4 pb-4 text-sm augur-muted">No events</div>
      ) : (
        <div className="max-h-[18rem] overflow-auto border-t border-slate-200 dark:border-slate-700">
          <table className="min-w-full text-sm">
            <thead className="sticky top-0 bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
              <tr>
                <th className="px-4 py-2 font-semibold">Event</th>
                <th className={TABLE_NUMERIC_HEADER}>Amount</th>
                <th className="px-4 py-2 font-semibold">Detail</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((group, groupIndex) => {
                const isSelected = selectedEventMonthIndex === group.monthIndex;
                const isHovered = hoveredEventMonthIndex === group.monthIndex;
                const zebra =
                  groupIndex % 2 === 1 ? "bg-slate-50/70 dark:bg-slate-900/50" : "bg-white dark:bg-slate-950/30";
                const groupTint = isSelected
                  ? "bg-teal-50 dark:bg-teal-950/30"
                  : isHovered
                    ? "bg-cyan-50 dark:bg-slate-800"
                    : zebra;
                const interactiveClassName = `cursor-pointer outline-none ${groupTint}`;
                return (
                  <React.Fragment key={group.monthIndex}>
                    <tr
                      ref={(node) => {
                        if (node) {
                          groupRefs.current.set(group.monthIndex, node);
                        } else {
                          groupRefs.current.delete(group.monthIndex);
                        }
                      }}
                      role="button"
                      tabIndex={0}
                      className={interactiveClassName}
                      data-product-rollout-event-month={group.monthIndex}
                      data-product-rollout-event-month-selected={isSelected ? "true" : "false"}
                      data-product-rollout-event-month-hovered={isHovered ? "true" : "false"}
                      onClick={() => onSelectEventMonth?.(group.monthIndex)}
                      onKeyDown={(keyboardEvent) => selectMonthFromKeyboard(keyboardEvent, group.monthIndex)}
                      onMouseEnter={() => onHoverEventMonth?.(group.monthIndex)}
                      onMouseLeave={() => onHoverEventMonth?.(null)}
                      onFocus={() => onHoverEventMonth?.(group.monthIndex)}
                      onBlur={() => onHoverEventMonth?.(null)}
                    >
                      <td
                        className="px-4 pb-1 pt-2 text-xs font-semibold uppercase tracking-wide augur-muted"
                        colSpan={3}
                      >
                        <div className="flex min-w-0 items-center justify-between gap-3">
                          <span>Month {group.monthIndex + 1}</span>
                          <span className="shrink-0">{fmtNumber(group.events.length)} events</span>
                        </div>
                      </td>
                    </tr>
                    {group.events.map((event, index) => (
                      <tr
                        key={`${event.kind}-${event.monthIndex}-${index}`}
                        role="button"
                        tabIndex={0}
                        className={interactiveClassName}
                        onClick={() => onSelectEventMonth?.(group.monthIndex)}
                        onKeyDown={(keyboardEvent) => selectMonthFromKeyboard(keyboardEvent, group.monthIndex)}
                        onMouseEnter={() => onHoverEventMonth?.(group.monthIndex)}
                        onMouseLeave={() => onHoverEventMonth?.(null)}
                        onFocus={() => onHoverEventMonth?.(group.monthIndex)}
                        onBlur={() => onHoverEventMonth?.(null)}
                      >
                        <td className="px-4 py-1">
                          <div className="flex min-w-0 items-start gap-2">
                            <span
                              className="mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full"
                              style={{ backgroundColor: eventColor(event) }}
                              aria-hidden="true"
                            />
                            <span className="min-w-0">{eventLabel(event)}</span>
                          </div>
                        </td>
                        <td className="whitespace-nowrap px-3 py-1 text-right augur-tabular">
                          {fmtMetricValue("amountUsd", eventAmount(event), currencyDisplay)}
                        </td>
                        <td className="min-w-[12rem] px-4 py-1 text-xs augur-muted">
                          {eventDetailText(event, currencyDisplay)}
                        </td>
                      </tr>
                    ))}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function EventKindLegend({ events, visibility }) {
  const countsByKind = new Map();
  for (const event of events) {
    const kind = event?.kind;
    if (kind == null) continue;
    countsByKind.set(kind, (countsByKind.get(kind) ?? 0) + 1);
  }
  const presentKinds = ROLLOUT_EVENT_KIND_ORDER.filter((kind) => countsByKind.has(kind));
  if (presentKinds.length === 0) return null;
  return (
    <div className="border-t border-slate-200 px-4 py-3 dark:border-slate-700">
      <div className="flex items-center justify-between gap-3">
        <div className="augur-eyebrow">Event kinds</div>
        <div className="flex items-center gap-2 text-[11px]">
          <button
            type="button"
            className="rounded px-1.5 py-0.5 text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
            onClick={visibility.showAll}
          >
            Show all
          </button>
          <button
            type="button"
            className="rounded px-1.5 py-0.5 text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
            onClick={visibility.hideAll}
          >
            Hide all
          </button>
        </div>
      </div>
      <ul className="mt-2 flex flex-wrap gap-1.5" role="list" aria-label="Event-kind visibility legend">
        {presentKinds.map((kind) => {
          const isVisible = visibility.visible.has(kind);
          const color = ROLLOUT_EVENT_COLORS[kind] ?? "#64748b";
          const label = ROLLOUT_EVENT_KIND_LABELS[kind] ?? kind;
          const count = countsByKind.get(kind);
          return (
            <li key={kind}>
              <button
                type="button"
                aria-pressed={isVisible}
                title={
                  isVisible ? `Hide ${label} markers (shift-click: only)` : `Show ${label} markers (shift-click: only)`
                }
                onClick={(mouseEvent) => {
                  if (mouseEvent.shiftKey) visibility.only(kind);
                  else visibility.toggle(kind);
                }}
                className={`flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-semibold transition-opacity ${
                  isVisible
                    ? "border-slate-300 bg-white text-slate-800 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100"
                    : "border-dashed border-slate-300 bg-transparent text-slate-400 opacity-60 dark:border-slate-700 dark:text-slate-500"
                }`}
              >
                <span
                  aria-hidden="true"
                  className="inline-block h-2.5 w-2.5 rounded-full"
                  style={{ backgroundColor: color, opacity: isVisible ? 1 : 0.4 }}
                />
                <span>{label}</span>
                <span className="augur-tabular augur-muted">{count}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
