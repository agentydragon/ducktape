import React, { useEffect, useState } from "react";
import { MantineProvider } from "@mantine/core";

import { fetchAugurCalibrationInfo, fetchAugurCatalog, fetchAugurDeployment, fetchAugurSettings } from "./client.ts";

import { ProductProjectionWorkspace } from "./product.tsx";
import { CalibrationWorkspace } from "./calibration.tsx";
import { BudgetWorkspace } from "./budget.tsx";
import {
  AugurHeader,
  SharedControls,
  AugurTabBar,
  DeploymentCommitSummary,
  tabFromSearch,
  writeTabToSearch,
} from "./header.tsx";
import { ProductProjectionLoading } from "./skeleton.tsx";
import { CurrencyDisplayProvider } from "./hooks.ts";
import {
  rolloutCountFromSearch,
  rolloutCountDefault,
  clampRolloutCount,
  firstSeedFromSearch,
  firstSeedDefault,
  clampFirstSeed,
  modelFromSearch,
  defaultModel,
  horizonMonthsFromSearch,
  horizonMonthsDefault,
  clampHorizonMonths,
  metricScaleFromSearch,
} from "./input_helpers.ts";

// The shared rollout count is a top-level concern (both tabs run this many rollouts), so it gets
// its own `?n=` param written by the shell rather than living in either tab's serialized input.
// Omitted when at the default, so a default rollout count leaves no `?n=` in the URL.
function writeRolloutCountToSearch(value, bootstrap) {
  const params = new URLSearchParams(window.location.search);
  if (value == null || value === rolloutCountDefault(bootstrap)) params.delete("n");
  else params.set("n", String(value));
  const search = params.toString();
  const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
  if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState(null, "", newUrl);
  }
}

function writeFirstSeedToSearch(value, bootstrap) {
  const params = new URLSearchParams(window.location.search);
  if (value == null || value === firstSeedDefault(bootstrap)) params.delete("seed");
  else params.set("seed", String(value));
  const search = params.toString();
  const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
  if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState(null, "", newUrl);
  }
}

// The shared exogenous model is likewise a top-level concern (both tabs run against it), so it
// gets its own `?x=` param. Omitted when at the deployment default, like the `?n=` param above.
function writeExogenousModelToSearch(value, bootstrap) {
  const params = new URLSearchParams(window.location.search);
  if (value == null || value === defaultModel(bootstrap)) params.delete("x");
  else params.set("x", value);
  const search = params.toString();
  const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
  if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState(null, "", newUrl);
  }
}

// The shared horizon (`?h=`) and chart scale (`?scale=`) are also top-level, owned by the shell.
function writeHorizonMonthsToSearch(value, bootstrap) {
  const params = new URLSearchParams(window.location.search);
  if (value == null || value === horizonMonthsDefault(bootstrap)) params.delete("h");
  else params.set("h", String(value));
  const search = params.toString();
  const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
  if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState(null, "", newUrl);
  }
}

function writeMetricScaleToSearch(value) {
  const params = new URLSearchParams(window.location.search);
  if (value === "log") params.set("scale", "log");
  else params.delete("scale");
  const search = params.toString();
  const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
  if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState(null, "", newUrl);
  }
}

function currencyDisplayFromSearch(searchString) {
  return new URLSearchParams(searchString).get("fmt") === "exact" ? "exact" : "compact";
}

function BudgetAppSurface({ deployment, tab, onSelectTab }) {
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

function CalibrationAppSurface({
  bootstrap,
  deployment,
  tab,
  onSelectTab,
  rolloutCount,
  onChangeRolloutCount,
  firstSeed,
  onChangeFirstSeed,
  model,
  onChangeModel,
  horizonMonths,
  onChangeHorizonMonths,
  metricScale,
  onChangeMetricScale,
  currencyDisplay,
  onChangeCurrencyDisplay,
  settingsOpen,
  onChangeSettingsOpen,
}) {
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
          horizonMonths={horizonMonths}
          metricScale={metricScale}
          sharedControlsSlot={
            <SharedControls
              rolloutCount={rolloutCount}
              onChangeRolloutCount={onChangeRolloutCount}
              maxRolloutCount={bootstrap.maxRolloutSamples}
              firstSeed={firstSeed}
              onChangeFirstSeed={onChangeFirstSeed}
              model={model}
              onChangeModel={onChangeModel}
              models={bootstrap.models}
              horizonMonths={horizonMonths}
              onChangeHorizonMonths={onChangeHorizonMonths}
              maxHorizonMonths={bootstrap.maxHorizonMonths}
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
function LoadedAppShell({ bootstrap, deployment }) {
  const [tab, setTab] = useState(() => tabFromSearch(window.location.search));
  const [rolloutCount, setRolloutCount] = useState(() => rolloutCountFromSearch(window.location.search, bootstrap));
  const [firstSeed, setFirstSeed] = useState(() => firstSeedFromSearch(window.location.search, bootstrap));
  const [model, setModel] = useState(() => modelFromSearch(window.location.search, bootstrap));
  const [horizonMonths, setHorizonMonths] = useState(() => horizonMonthsFromSearch(window.location.search, bootstrap));
  const [metricScale, setMetricScale] = useState(() => metricScaleFromSearch(window.location.search));
  const [currencyDisplay, setCurrencyDisplay] = useState(() => currencyDisplayFromSearch(window.location.search));
  const [settingsOpen, setSettingsOpen] = useState(true);

  const onSelectTab = (next) => {
    setTab(next);
    writeTabToSearch(next);
  };

  const onChangeRolloutCount = (value) => {
    const next = value == null ? value : clampRolloutCount(value, bootstrap);
    setRolloutCount(next);
    writeRolloutCountToSearch(next, bootstrap);
  };

  const onChangeFirstSeed = (value) => {
    const next = value == null ? value : clampFirstSeed(value);
    setFirstSeed(next);
    writeFirstSeedToSearch(next, bootstrap);
  };

  const onChangeModel = (value) => {
    setModel(value);
    writeExogenousModelToSearch(value, bootstrap);
  };

  const onChangeHorizonMonths = (value) => {
    const next = value == null ? value : clampHorizonMonths(value, bootstrap);
    setHorizonMonths(next);
    writeHorizonMonthsToSearch(next, bootstrap);
  };

  const onChangeMetricScale = (value) => {
    setMetricScale(value);
    writeMetricScaleToSearch(value);
  };

  const onChangeCurrencyDisplay = (value) => {
    setCurrencyDisplay(value);
    const params = new URLSearchParams(window.location.search);
    if (value !== "compact") params.set("fmt", "exact");
    else params.delete("fmt");
    const search = params.toString();
    const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
    window.history.replaceState(null, "", newUrl);
  };

  const sharedProps = {
    bootstrap,
    deployment,
    tab,
    onSelectTab,
    rolloutCount,
    onChangeRolloutCount,
    firstSeed,
    onChangeFirstSeed,
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
  let surface;
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
  const [bootstrap, setBootstrap] = useState(null);
  const [bootstrapError, setBootstrapError] = useState(null);
  const [deployment, setDeployment] = useState(null);

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
        if (error?.name === "AbortError") return;
        setBootstrap(null);
        setBootstrapError(error?.message || String(error));
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetchAugurDeployment({ signal: controller.signal })
      .then((payload) => setDeployment(payload))
      .catch((error) => {
        if (error?.name === "AbortError") return;
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
      <ProductProjectionAppShell />
    </MantineProvider>
  );
}
