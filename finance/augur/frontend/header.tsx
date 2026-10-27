import { NativeSelect, NumberInput, SegmentedControl } from "@mantine/core";
import type { ReactNode } from "react";

import type { AugurDeployment } from "./client";
import { replaceSearchParams } from "./url_state";

const SCALE_OPTIONS = [
  { value: "linear" as const, label: "Linear" },
  { value: "log" as const, label: "Log" },
];

const CURRENCY_DISPLAY_OPTIONS = [
  { value: "exact" as const, label: "Exact" },
  { value: "compact" as const, label: "Compact" },
];

type MetricScale = (typeof SCALE_OPTIONS)[number]["value"];
export type CurrencyDisplay = (typeof CURRENCY_DISPLAY_OPTIONS)[number]["value"];

function isMetricScale(value: string): value is MetricScale {
  return SCALE_OPTIONS.some((option) => option.value === value);
}

function isCurrencyDisplay(value: string): value is CurrencyDisplay {
  return CURRENCY_DISPLAY_OPTIONS.some((option) => option.value === value);
}

type SharedControlsProps = {
  rolloutCount: number | null;
  onChangeRolloutCount?: (value: number | null) => void;
  maxRolloutCount: number;
  model: string | null;
  onChangeModel?: (value: string) => void;
  models: readonly string[];
  metricScale: MetricScale;
  onChangeMetricScale?: (value: MetricScale) => void;
  currencyDisplay: CurrencyDisplay;
  onChangeCurrencyDisplay?: (value: CurrencyDisplay) => void;
  settingsOpen: boolean;
  onChangeSettingsOpen: (open: boolean) => void;
};
type ControlRow = { label: string; input: ReactNode };

function numberInputOnChange(onChange: (value: number | null) => void) {
  return (next: string | number) => {
    const number = typeof next === "number" ? next : Number(next);
    onChange(Number.isFinite(number) ? number : null);
  };
}

export function SharedControls({
  rolloutCount,
  onChangeRolloutCount,
  maxRolloutCount,
  model,
  onChangeModel,
  models,
  metricScale,
  onChangeMetricScale,
  currencyDisplay,
  onChangeCurrencyDisplay,
  settingsOpen,
  onChangeSettingsOpen,
}: SharedControlsProps) {
  const showModelControl = onChangeModel != null && models.length > 1;
  const rows: ControlRow[] = [];
  if (onChangeRolloutCount) {
    rows.push({
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
    });
  }
  if (showModelControl) {
    rows.push({
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
    });
  }
  if (onChangeMetricScale) {
    rows.push({
      label: "Scale",
      input: (
        <SegmentedControl
          aria-label="Chart scale"
          size="xs"
          value={metricScale}
          data={SCALE_OPTIONS}
          onChange={(value) => {
            if (isMetricScale(value)) onChangeMetricScale(value);
          }}
        />
      ),
    });
  }
  if (onChangeCurrencyDisplay) {
    rows.push({
      label: "Currency",
      input: (
        <SegmentedControl
          aria-label="Currency display"
          size="xs"
          value={currencyDisplay}
          data={CURRENCY_DISPLAY_OPTIONS}
          onChange={(value) => {
            if (isCurrencyDisplay(value)) onChangeCurrencyDisplay(value);
          }}
        />
      ),
    });
  }

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
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-slate-200 px-4 py-2 text-xs augur-muted dark:border-slate-700">
        {rows.map(({ label, input }) => (
          <div key={label} className="inline-flex items-center gap-1.5">
            <span className="augur-eyebrow whitespace-nowrap">{label}</span>
            {input}
          </div>
        ))}
      </div>
    </details>
  );
}

export function AugurHeader({
  rightSlot = null,
  nav = null,
}: {
  rightSlot?: ReactNode | null;
  nav?: ReactNode | null;
}) {
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

// -- Tab navigation ------------------------------------------------------------
//
// Top-level views. "product" is the default; the active tab is mirrored to the URL `?tab=`
// (omitted for the default), following the same replaceState pattern as the product `?scenarios=` state.
const TABS = [
  { value: "product", label: "Product" },
  { value: "calibration", label: "Calibration" },
  { value: "budget", label: "Budget" },
] as const;
export type AugurTab = (typeof TABS)[number]["value"];
function isAugurTab(value: string): value is AugurTab {
  return TABS.some((tab) => tab.value === value);
}

const DEFAULT_TAB: AugurTab = "product";

export function tabFromSearch(searchString: string): AugurTab {
  const requested = new URLSearchParams(searchString).get("tab");
  return requested && isAugurTab(requested) ? requested : DEFAULT_TAB;
}

export function writeTabToSearch(tab: AugurTab) {
  replaceSearchParams({ tab: tab === DEFAULT_TAB ? null : tab });
}

export function AugurTabBar({ tab, onSelectTab }: { tab: AugurTab; onSelectTab: (tab: AugurTab) => void }) {
  return (
    <nav className="flex items-center gap-1" aria-label="Augur views" data-augur-tab-bar="">
      {TABS.map((entry) => (
        <button
          key={entry.value}
          type="button"
          className="augur-view-tab"
          data-active={tab === entry.value ? "" : undefined}
          aria-current={tab === entry.value ? "page" : undefined}
          data-augur-tab={entry.value}
          onClick={() => onSelectTab(entry.value)}
        >
          {entry.label}
        </button>
      ))}
    </nav>
  );
}

// -- Deployment status chip (header rightSlot) ---------------------------------

function shortCommit(commit: string | null | undefined) {
  if (!commit) return null;
  return commit.slice(0, 12);
}

function DeploymentCommitItem({
  label,
  image,
}: {
  label: string;
  image: NonNullable<AugurDeployment["api"]> | null | undefined;
}) {
  const commit = shortCommit(image?.sourceCommit);
  if (!commit) return null;
  const className = "mono font-semibold text-slate-700 dark:text-slate-200";
  const title = image?.imageTag ? `${label}: ${image.imageTag}` : label;
  return (
    <span className="whitespace-nowrap" title={title}>
      {label}{" "}
      {image?.sourceCommitUrl ? (
        <a className={`${className} augur-link`} href={image.sourceCommitUrl}>
          {commit}
        </a>
      ) : (
        <span className={className}>{commit}</span>
      )}
    </span>
  );
}

export function DeploymentCommitSummary({ deployment }: { deployment: AugurDeployment | null }) {
  const apiCommit = deployment?.api?.sourceCommit ?? null;
  const frontendCommit = deployment?.frontend?.sourceCommit ?? null;
  if (!apiCommit && !frontendCommit) return null;
  if (deployment != null && apiCommit && frontendCommit && apiCommit === frontendCommit) {
    return <DeploymentCommitItem label="Deployed" image={deployment.api} />;
  }
  return (
    <>
      <DeploymentCommitItem label="API" image={deployment?.api} />
      <DeploymentCommitItem label="UI" image={deployment?.frontend} />
    </>
  );
}
