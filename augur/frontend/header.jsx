import React from "react";
import { NumberInput } from "@mantine/core";

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

export function AugurHeader({ rightSlot = null, nav = null, rolloutCount, onChangeRolloutCount, maxRolloutCount }) {
  const rightGroup = onChangeRolloutCount || rightSlot;
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
            {onChangeRolloutCount && (
              <RolloutCountControl value={rolloutCount} onChange={onChangeRolloutCount} max={maxRolloutCount} />
            )}
            {rightSlot}
          </div>
        )}
      </div>
    </header>
  );
}
