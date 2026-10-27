import { useEffect, useState, type ReactNode } from "react";
import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";

import { fetchAugurCalibrationInfo, fetchAugurCatalog, fetchAugurDeployment, fetchAugurSettings } from "./client";

import { ProductProjectionWorkspace } from "./product";
import { CalibrationWorkspace } from "./calibration";
import { BudgetWorkspace } from "./budget";
import {
  AugurHeader,
  SharedControls,
  AugurTabBar,
  DeploymentCommitSummary,
  tabFromSearch,
  writeTabToSearch,
  type AugurTab,
} from "./header";
import { ProductProjectionLoading } from "./skeleton";
import { CurrencyDisplayProvider } from "./hooks";
import {
  rolloutCountFromSearch,
  rolloutCountDefault,
  clampRolloutCount,
  firstSeedDefault,
  modelFromSearch,
  defaultModel,
  horizonMonthsFromSearch,
  horizonMonthsDefault,
  clampHorizonMonths,
  metricScaleFromSearch,
} from "./input_helpers";
import { replaceSearchParams } from "./url_state";

// The shared rollout count is a top-level concern (both tabs run this many rollouts), so it gets
// its own `?n=` param written by the shell rather than living in either tab's serialized input.
// Omitted when at the default, so a default rollout count leaves no `?n=` in the URL.
type Catalog = Awaited<ReturnType<typeof fetchAugurCatalog>>;
type Settings = Awaited<ReturnType<typeof fetchAugurSettings>>;
type CalibrationInfo = Awaited<ReturnType<typeof fetchAugurCalibrationInfo>>;
type Bootstrap = Omit<Catalog, keyof Settings> & Settings & { calibration: CalibrationInfo };
type Deployment = Awaited<ReturnType<typeof fetchAugurDeployment>>;
type MetricScale = ReturnType<typeof metricScaleFromSearch>;

function errorMessage(error: unknown): string {
  if (error && typeof error === "object" && "message" in error && error.message) return String(error.message);
  return String(error);
}

function isAbortError(error: unknown): boolean {
  return error != null && typeof error === "object" && "name" in error && error.name === "AbortError";
}

function writeRolloutCountToSearch(value: number | null, bootstrap: Bootstrap) {
  replaceSearchParams({ n: value == null || value === rolloutCountDefault(bootstrap) ? null : value });
}

// The shared exogenous model is likewise a top-level concern (both tabs run against it), so it
// gets its own `?x=` param. Omitted when at the deployment default, like the `?n=` param above.
function writeExogenousModelToSearch(value: string | null, bootstrap: Bootstrap) {
  replaceSearchParams({ x: value == null || value === defaultModel(bootstrap) ? null : value });
}

// The shared horizon (`?h=`) and chart scale (`?scale=`) are also top-level, owned by the shell.
function writeHorizonMonthsToSearch(value: number | null, bootstrap: Bootstrap) {
  replaceSearchParams({ h: value == null || value === horizonMonthsDefault(bootstrap) ? null : value });
}

function writeMetricScaleToSearch(value: MetricScale) {
  replaceSearchParams({ scale: value === "log" ? "log" : null });
}

function currencyDisplayFromSearch(searchString: string): "exact" | "compact" {
  return new URLSearchParams(searchString).get("fmt") === "exact" ? "exact" : "compact";
}

function BudgetAppSurface({
  deployment,
  tab,
  onSelectTab,
}: {
  deployment: Deployment | null;
  tab: AugurTab;
  onSelectTab: (tab: AugurTab) => void;
}) {
  return (
    <div
      data-augur-surface="budget"
      className="min-h-screen bg-slate-100 text-slate-800 dark:bg-slate-950 dark:text-slate-100"
    >
      <AugurHeader
        nav={<AugurTabBar tab={tab} onSelectTab={onSelectTab} />}
        rightSlot={<DeploymentCommitSummary deployment={deployment} />}
      />
      <BudgetWorkspace />
    </div>
  );
}

type SharedAppProps = {
  bootstrap: Bootstrap;
  deployment: Deployment | null;
  tab: AugurTab;
  onSelectTab: (tab: AugurTab) => void;
  rolloutCount: number | null;
  onChangeRolloutCount: (value: number | null) => void;
  firstSeed: number;
  model: string;
  onChangeModel: (value: string) => void;
  horizonMonths: number;
  onChangeHorizonMonths: (value: number | null) => void;
  metricScale: MetricScale;
  onChangeMetricScale: (value: MetricScale) => void;
  currencyDisplay: "exact" | "compact";
  onChangeCurrencyDisplay: (value: "exact" | "compact") => void;
  settingsOpen: boolean;
  onChangeSettingsOpen: (open: boolean) => void;
};

function CalibrationAppSurface({
  bootstrap,
  deployment,
  tab,
  onSelectTab,
  rolloutCount,
  onChangeRolloutCount,
  firstSeed,
  model,
  onChangeModel,
  metricScale,
  onChangeMetricScale,
  currencyDisplay,
  onChangeCurrencyDisplay,
  settingsOpen,
  onChangeSettingsOpen,
}: SharedAppProps) {
  return (
    <div
      data-augur-surface="calibration"
      className="min-h-screen bg-slate-100 text-slate-800 dark:bg-slate-950 dark:text-slate-100"
    >
      <AugurHeader
        nav={<AugurTabBar tab={tab} onSelectTab={onSelectTab} />}
        rightSlot={<DeploymentCommitSummary deployment={deployment} />}
      />
      <main className="px-4 py-6 sm:px-6 lg:px-8">
        <CalibrationWorkspace
          bootstrap={bootstrap}
          rolloutCount={rolloutCount}
          firstSeed={firstSeed}
          model={model}
          metricScale={metricScale}
          sharedControlsSlot={
            <SharedControls
              rolloutCount={rolloutCount}
              onChangeRolloutCount={onChangeRolloutCount}
              maxRolloutCount={bootstrap.maxRolloutSamples}
              model={model}
              onChangeModel={onChangeModel}
              models={bootstrap.models}
              metricScale={metricScale}
              onChangeMetricScale={onChangeMetricScale}
              currencyDisplay={currencyDisplay}
              onChangeCurrencyDisplay={onChangeCurrencyDisplay}
              settingsOpen={settingsOpen}
              onChangeSettingsOpen={onChangeSettingsOpen}
            />
          }
        />
      </main>
    </div>
  );
}

// Mounted once bootstrap has loaded so the shared defaults (which depend on bootstrap fields like
// `maxRolloutSamples` and `models`) are available at state-init time. Owns the cross-tab
// state — the active tab plus shared controls — and hands it to whichever workspace is active.
function LoadedAppShell({ bootstrap, deployment }: { bootstrap: Bootstrap; deployment: Deployment | null }) {
  const [tab, setTab] = useState(() => tabFromSearch(window.location.search));
  const [rolloutCount, setRolloutCount] = useState<number | null>(() =>
    rolloutCountFromSearch(window.location.search, bootstrap)
  );
  // First seed is a fixed default (no UI control) so runs stay reproducible.
  const firstSeed = firstSeedDefault(bootstrap);
  const [model, setModel] = useState(() => modelFromSearch(window.location.search, bootstrap));
  const [horizonMonths, setHorizonMonths] = useState(() => horizonMonthsFromSearch(window.location.search, bootstrap));
  const [metricScale, setMetricScale] = useState(() => metricScaleFromSearch(window.location.search));
  const [currencyDisplay, setCurrencyDisplay] = useState(() => currencyDisplayFromSearch(window.location.search));
  const [settingsOpen, setSettingsOpen] = useState(true);

  const onSelectTab = (next: AugurTab) => {
    setTab(next);
    writeTabToSearch(next);
  };

  const onChangeRolloutCount = (value: number | null) => {
    const next = value == null ? value : clampRolloutCount(value, bootstrap);
    setRolloutCount(next);
    writeRolloutCountToSearch(next, bootstrap);
  };

  const onChangeModel = (value: string) => {
    setModel(value);
    writeExogenousModelToSearch(value, bootstrap);
  };

  const onChangeHorizonMonths = (value: number | null) => {
    const next = value == null ? value : clampHorizonMonths(value, bootstrap);
    setHorizonMonths(next);
    writeHorizonMonthsToSearch(next, bootstrap);
  };

  const onChangeMetricScale = (value: MetricScale) => {
    setMetricScale(value);
    writeMetricScaleToSearch(value);
  };

  const onChangeCurrencyDisplay = (value: "exact" | "compact") => {
    setCurrencyDisplay(value);
    replaceSearchParams({ fmt: value !== "compact" ? "exact" : null });
  };

  const sharedProps = {
    bootstrap,
    deployment,
    tab,
    onSelectTab,
    rolloutCount,
    onChangeRolloutCount,
    firstSeed,
    model,
    onChangeModel,
    horizonMonths,
    onChangeHorizonMonths,
    metricScale,
    onChangeMetricScale,
    currencyDisplay,
    onChangeCurrencyDisplay,
    settingsOpen,
    onChangeSettingsOpen: setSettingsOpen,
  };
  let surface: ReactNode;
  if (tab === "calibration") {
    surface = <CalibrationAppSurface {...sharedProps} />;
  } else if (tab === "budget") {
    surface = <BudgetAppSurface {...sharedProps} />;
  } else {
    surface = <ProductProjectionWorkspace {...sharedProps} />;
  }
  return (
    <CurrencyDisplayProvider value={{ display: currencyDisplay, setDisplay: onChangeCurrencyDisplay }}>
      {surface}
    </CurrencyDisplayProvider>
  );
}

function ProductProjectionAppShell() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);
  const [deployment, setDeployment] = useState<Deployment | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    // The bootstrap endpoint is split into cohesive resources (`/api/catalog`, `/api/settings`,
    // `/api/calibration`). The shell needs catalog + settings together to mount (shared-control
    // defaults read off both), so fetch them in parallel and merge back into the single view
    // model the workspaces consume.
    Promise.all([
      fetchAugurCatalog({ signal: controller.signal }),
      fetchAugurSettings({ signal: controller.signal }),
      fetchAugurCalibrationInfo({ signal: controller.signal }),
    ])
      .then(([catalog, settings, calibration]) => {
        setBootstrap({ ...catalog, ...settings, calibration });
        setBootstrapError(null);
      })
      .catch((error) => {
        if (isAbortError(error)) return;
        setBootstrap(null);
        setBootstrapError(errorMessage(error));
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetchAugurDeployment({ signal: controller.signal })
      .then((payload) => setDeployment(payload))
      .catch((error) => {
        if (isAbortError(error)) return;
        setDeployment(null);
      });
    return () => controller.abort();
  }, []);

  if (!bootstrap) return <ProductProjectionLoading error={bootstrapError} />;

  return <LoadedAppShell bootstrap={bootstrap} deployment={deployment} />;
}

export default function AugurApp() {
  return (
    <MantineProvider defaultColorScheme="auto">
      <Notifications position="top-right" />
      <ProductProjectionAppShell />
    </MantineProvider>
  );
}
