import React from "react";
import { NativeSelect, NumberInput, SegmentedControl, Text } from "@mantine/core";

const SCALE_OPTIONS = [
  { value: "linear", label: "Linear" },
  { value: "log", label: "Log" },
];

const CURRENCY_DISPLAY_OPTIONS = [
  { value: "exact", label: "Exact" },
  { value: "compact", label: "Compact" },
];

// Width for a NumberInput unit chip (e.g. "mo"), matching the sidebar `NumberField` sizing so the
// control's unit suffix reads the same as the product tab's.
function unitSectionWidth(section) {
  return Math.max(34, String(section).length * 8 + 24);
}

function numberInputOnChange(onChange) {
  return (next) => {
    const number = typeof next === "number" ? next : Number(next);
    onChange(Number.isFinite(number) ? number : null);
  };
}

export function SharedControls({
  rolloutCount,
  onChangeRolloutCount,
  maxRolloutCount,
  firstSeed,
  onChangeFirstSeed,
  model,
  onChangeModel,
  models,
  horizonMonths,
  onChangeHorizonMonths,
  maxHorizonMonths,
  metricScale,
  onChangeMetricScale,
  currencyDisplay,
  onChangeCurrencyDisplay,
  settingsOpen,
  onChangeSettingsOpen,
}) {
  const showModelControl = onChangeModel && models.length > 1;
  const rows = [
    onChangeHorizonMonths && {
      label: "Horizon",
      input: (
        <NumberInput
          aria-label="Horizon"
          size="xs"
          min={1}
          max={maxHorizonMonths}
          step={12}
          value={horizonMonths ?? ""}
          hideControls
          rightSection={<Text className="augur-number-section">mo</Text>}
          rightSectionPointerEvents="none"
          rightSectionWidth={unitSectionWidth("mo")}
          classNames={{ input: "augur-tabular w-16 text-right" }}
          onChange={numberInputOnChange(onChangeHorizonMonths)}
        />
      ),
    },
    onChangeRolloutCount && {
      label: "Rollouts",
      input: (
        <NumberInput
          aria-label="Rollouts"
          size="xs"
          min={1}
          max={maxRolloutCount}
          step={1}
          value={rolloutCount ?? ""}
          hideControls
          thousandSeparator=","
          classNames={{ input: "augur-tabular w-16 text-right" }}
          onChange={numberInputOnChange(onChangeRolloutCount)}
        />
      ),
    },
    onChangeFirstSeed && {
      label: "Seed",
      input: (
        <NumberInput
          aria-label="First seed"
          size="xs"
          min={0}
          max={2 ** 31 - 1}
          step={1}
          value={firstSeed ?? ""}
          hideControls
          thousandSeparator=","
          classNames={{ input: "augur-tabular w-16 text-right" }}
          onChange={numberInputOnChange(onChangeFirstSeed)}
        />
      ),
    },
    showModelControl && {
      label: "Model",
      input: (
        <NativeSelect
          aria-label="Exogenous model"
          size="xs"
          value={model ?? ""}
          data={models.map((preset) => ({ value: preset, label: preset }))}
          classNames={{ input: "augur-tabular" }}
          onChange={(event) => onChangeModel(event.target.value)}
        />
      ),
    },
    onChangeMetricScale && {
      label: "Scale",
      input: (
        <SegmentedControl
          aria-label="Chart scale"
          size="xs"
          value={metricScale}
          data={SCALE_OPTIONS}
          onChange={onChangeMetricScale}
        />
      ),
    },
    onChangeCurrencyDisplay && {
      label: "Currency",
      input: (
        <SegmentedControl
          aria-label="Currency display"
          size="xs"
          value={currencyDisplay}
          data={CURRENCY_DISPLAY_OPTIONS}
          onChange={onChangeCurrencyDisplay}
        />
      ),
    },
  ].filter(Boolean);

  return (
    <details
      open={settingsOpen}
      onToggle={(e) => onChangeSettingsOpen(e.currentTarget.open)}
      className="augur-card [&_summary::-webkit-details-marker]:hidden"
    >
      <summary className="augur-eyebrow flex cursor-pointer list-none items-baseline justify-between gap-2 px-4 py-3">
        <span className="inline-flex items-center gap-1">
          <span aria-hidden="true" className="transition-transform [details[open]_&]:rotate-90">
            ▸
          </span>
          Simulation settings
        </span>
      </summary>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3 border-t border-slate-200 px-4 py-3 text-xs augur-muted dark:border-slate-700">
        {rows.map(({ label, input }) => (
          <div key={label} className="inline-flex items-center gap-2">
            <span className="augur-eyebrow whitespace-nowrap">{label}</span>
            {input}
          </div>
        ))}
      </div>
    </details>
  );
}

export function AugurHeader({ rightSlot = null, nav = null }) {
  return (
    <header className="sticky top-0 z-40 border-b border-slate-200 bg-white/95 px-4 py-3 shadow-sm backdrop-blur dark:border-slate-800 dark:bg-slate-950/95 sm:px-6 lg:px-8">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex min-w-0 items-center gap-3">
          <img src="/favicon.svg" alt="" className="h-9 w-9 shrink-0" aria-hidden="true" />
          <div className="min-w-0">
            <h1 className="text-base font-semibold text-slate-950 dark:text-slate-50">Augur</h1>
          </div>
          {nav}
        </div>
        {rightSlot && <div className="text-xs augur-muted">{rightSlot}</div>}
      </div>
    </header>
  );
}
