import React from "react";
import { NativeSelect, NumberInput, SegmentedControl } from "@mantine/core";

const SCALE_OPTIONS = [
  { value: "linear", label: "Linear" },
  { value: "log", label: "Log" },
];

// Tab-shared rollout count control. It lives in the header — the only region common to both the
// product and calibration surfaces — so a single control drives the rollout count on every page.
function RolloutCountControl({ value, onChange, max }) {
  return (
    <label className="flex items-center gap-1.5 whitespace-nowrap" data-augur-rollout-count-control="">
      <span className="augur-eyebrow">Rollouts</span>
      <NumberInput
        aria-label="Rollouts"
        size="xs"
        min={1}
        max={max}
        step={1}
        value={value ?? ""}
        hideControls
        thousandSeparator=","
        classNames={{ input: "augur-tabular w-24 text-right" }}
        onChange={(next) => {
          const number = typeof next === "number" ? next : Number(next);
          onChange(Number.isFinite(number) ? number : null);
        }}
      />
    </label>
  );
}

// Tab-shared horizon control (months). Drives both the product projection and the calibration run.
function HorizonControl({ value, onChange, max }) {
  return (
    <label className="flex items-center gap-1.5 whitespace-nowrap" data-augur-horizon-control="">
      <span className="augur-eyebrow">Horizon</span>
      <NumberInput
        aria-label="Horizon"
        size="xs"
        min={1}
        max={max}
        step={12}
        value={value ?? ""}
        hideControls
        suffix=" mo"
        classNames={{ input: "augur-tabular w-20 text-right" }}
        onChange={(next) => {
          const number = typeof next === "number" ? next : Number(next);
          onChange(Number.isFinite(number) ? number : null);
        }}
      />
    </label>
  );
}

// Tab-shared chart scale (linear/log). Both the product metric fan and the calibration mark fan
// honor it, so the toggle lives once in the header rather than per-chart on each page.
function ScaleControl({ value, onChange }) {
  return (
    <div data-augur-scale-control="">
      <SegmentedControl aria-label="Chart scale" size="xs" value={value} data={SCALE_OPTIONS} onChange={onChange} />
    </div>
  );
}

// Tab-shared exogenous-model picker. Like the rollout count, it lives in the header so a single
// control drives the model on both the product and calibration pages. Rendered only when the
// deployment exposes more than one preset (a single preset means there's nothing to choose).
function ExogenousModelControl({ value, onChange, presets }) {
  return (
    <label className="flex items-center gap-1.5 whitespace-nowrap" data-augur-exogenous-model-control="">
      <span className="augur-eyebrow">Model</span>
      <NativeSelect
        aria-label="Exogenous model"
        size="xs"
        value={value ?? ""}
        data={presets.map((preset) => ({ value: preset, label: preset }))}
        classNames={{ input: "augur-tabular" }}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

export function AugurHeader({
  rightSlot = null,
  nav = null,
  rolloutCount,
  onChangeRolloutCount,
  maxRolloutCount,
  exogenousModel,
  onChangeExogenousModel,
  exogenousPresets = [],
  horizonMonths,
  onChangeHorizonMonths,
  maxHorizonMonths,
  metricScale,
  onChangeMetricScale,
}) {
  const showExogenousControl = onChangeExogenousModel && exogenousPresets.length > 1;
  const rightGroup =
    onChangeRolloutCount || onChangeHorizonMonths || onChangeMetricScale || showExogenousControl || rightSlot;
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
        {rightGroup && (
          <div className="flex min-w-[min(100%,28rem)] flex-1 flex-wrap items-center justify-end gap-3 text-xs augur-muted sm:flex-none">
            {showExogenousControl && (
              <ExogenousModelControl
                value={exogenousModel}
                onChange={onChangeExogenousModel}
                presets={exogenousPresets}
              />
            )}
            {onChangeHorizonMonths && (
              <HorizonControl value={horizonMonths} onChange={onChangeHorizonMonths} max={maxHorizonMonths} />
            )}
            {onChangeRolloutCount && (
              <RolloutCountControl value={rolloutCount} onChange={onChangeRolloutCount} max={maxRolloutCount} />
            )}
            {onChangeMetricScale && <ScaleControl value={metricScale} onChange={onChangeMetricScale} />}
            {rightSlot}
          </div>
        )}
      </div>
    </header>
  );
}
