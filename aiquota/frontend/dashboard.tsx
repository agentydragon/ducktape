/**
 * The dashboard itself: pure rendering of one `/v1/quotas` payload at one instant.
 *
 * Time enters only as the `now` prop, so a screenshot of a scene is the same picture every
 * run and the visual harness needs no fetch (`screenshots/harness.tsx`).
 *
 * Each window renders as the two-marker bar the design settled on
 * (`aiquota/gnome/DESIGN.md`): the fill is how much quota is gone, the tick is how much of
 * the window is gone, and the shaded span between them is the pace deviation — the thing a
 * plain percentage cannot tell you, since "80% used" is comfortable at day six of seven and
 * alarming at day two.
 */

import { type JSX } from "react";

import {
  displayUsedPercent,
  formatAge,
  formatClock,
  formatDuration,
  formatKnownExpiries,
  formatMultiplier,
  formatPace,
  formatPaceForecast,
  formatPeakInterval,
  formatUsd,
  formatUsdWhole,
  formatWindowLabel,
  roundHalfToEven,
} from "./format";
import { bindingTint, computePace, elapsedFraction, isExhausted, tintFor, type Tint, type WindowMath } from "./pace";
import {
  effectiveQuota,
  isShortWindow,
  resetSeconds,
  type BurnStatus,
  type EffectiveQuota,
  type ExtraSpend,
  type ProviderView,
  type QuotasView,
  type QuotaWindow,
} from "./quotas";

// The payload carries provider ids; these are how the vendors write their names, matching the
// GNOME panel's labels. An id with no entry shows as itself rather than being guessed at.
const PROVIDER_NAMES: Record<string, string> = { claude: "Claude", codex: "Codex", zai: "z.ai" };

export function Dashboard({
  quotas,
  now,
  error,
  refreshing,
  onRefresh,
}: {
  quotas: QuotasView | null;
  now: number;
  error: string | null;
  refreshing: boolean;
  onRefresh: () => void;
}): JSX.Element {
  return (
    <main id="app">
      <header className="page-heading">
        <h1>AI quota</h1>
        <button
          type="button"
          className="refresh"
          onClick={onRefresh}
          disabled={refreshing}
          aria-busy={refreshing}
          aria-label="Refresh"
          title="Refresh"
        >
          <RefreshIcon />
        </button>
      </header>
      {error && <p className="notice">{error}</p>}
      <section className="providers" aria-live="polite">
        {quotas ? (
          quotas.providers.map((provider) => <ProviderCard key={provider.provider} provider={provider} now={now} />)
        ) : (
          <p className="empty">{error ? "No snapshot loaded." : "Loading quota data…"}</p>
        )}
      </section>
      {quotas && (
        <footer>
          Snapshot {new Date(quotas.fetched_at).toLocaleString()} ·{" "}
          {formatAge((now - Date.parse(quotas.fetched_at)) / 1000)} ago
        </footer>
      )}
    </main>
  );
}

/** The conventional circular arrow; the button is icon-only, so its label lives in aria-label. */
function RefreshIcon(): JSX.Element {
  return (
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
      <path d="M21 3v6h-6" />
    </svg>
  );
}

function ProviderCard({ provider, now }: { provider: ProviderView; now: number }): JSX.Element {
  const quota = effectiveQuota(provider);
  const tint = providerTint(provider, quota, now);
  const overPlan = provider.currently_over_plan;
  return (
    <article className={`provider-card tint-${tint}`} aria-label={`${provider.provider} quota`}>
      <header>
        <h2>
          <span className={`dot tint-${tint}`} aria-hidden="true" />
          {PROVIDER_NAMES[provider.provider] ?? provider.provider}
        </h2>
        <div className="card-status">
          {quota.staleSince !== null && (
            <span className="badge stale">Stale · {formatAge((now - Date.parse(quota.staleSince)) / 1000)}</span>
          )}
          {quota.error !== null && <span className="badge error">Error</span>}
          <span className="freshness">
            checked {formatAge((now - Date.parse(provider.last_output.fetched_at)) / 1000)} ago
          </span>
        </div>
      </header>

      {quota.error !== null && <p className="error-copy">{quota.error}</p>}
      {overPlan && <OverPlanStrip extra={quota.extraSpend} windows={quota.windows} now={now} />}
      {quota.resetCredits !== null && (
        <ResetCreditsStrip count={quota.resetCredits} expiries={quota.resetCreditExpiries} />
      )}
      {provider.burn && <BurnStrip burn={provider.burn} now={now} />}

      {overPlan
        ? null
        : quota.windows.length === 0
          ? quota.error === null && <p className="empty">No quota data.</p>
          : quota.windows.map((window) => (
              <WindowRow
                key={`${window.name ?? ""}-${window.window_seconds}`}
                window={window}
                isShort={isShortWindow(window, quota.windows)}
                stale={quota.staleSince !== null}
                now={now}
              />
            ))}

      {!overPlan && provider.extra_status === "informational" && quota.extraSpend && (
        <p className="aside">{extraSpendText(quota.extraSpend)} spent this month</p>
      )}
    </article>
  );
}

function WindowRow({
  window,
  isShort,
  stale,
  now,
}: {
  window: QuotaWindow;
  isShort: boolean;
  stale: boolean;
  now: number;
}): JSX.Element {
  const math: WindowMath = {
    usedPercent: window.used_percent,
    resetSeconds: resetSeconds(window, now),
    windowSeconds: window.window_seconds,
  };
  const exhausted = isExhausted(math);
  const pace = exhausted ? null : computePace(math);
  const tint = stale ? "stale" : tintFor(pace, window.used_percent, { isShort });
  const label = formatWindowLabel(window);
  const used = displayUsedPercent(window);
  const paceText = formatPace(pace);
  const forecast = formatPaceForecast(pace, math.resetSeconds);
  return (
    <section className="window">
      <div className="window-head">
        <span className="window-label">{label}</span>
        <span className="window-used">{used}%</span>
      </div>
      <Meter
        usedPercent={window.used_percent}
        elapsed={elapsedFraction(math)}
        tint={tint}
        label={`${label}: ${used}% used, ${roundHalfToEven(elapsedFraction(math) * 100)}% of the window elapsed`}
      />
      <div className="window-meta">
        <span className="reset">↻ {formatDuration(math.resetSeconds)}</span>
        {exhausted ? (
          <span className="exhausted">exhausted</span>
        ) : (
          <>
            {paceText && <span className={`pace tint-${tint}`}>Δ{paceText}</span>}
            {forecast && <span className="forecast">{forecast}</span>}
          </>
        )}
      </div>
    </section>
  );
}

function Meter({
  usedPercent,
  elapsed,
  tint,
  label,
}: {
  usedPercent: number;
  elapsed: number;
  tint: Tint;
  label: string;
}): JSX.Element {
  const fill = Math.max(0, Math.min(100, usedPercent));
  const tick = elapsed * 100;
  return (
    <div
      className={`meter tint-${tint}`}
      role="meter"
      aria-label={label}
      aria-valuenow={roundHalfToEven(usedPercent)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div className="meter-fill" style={{ width: `${fill}%` }} />
      <div
        className={`meter-deviation ${fill >= tick ? "ahead" : "behind"}`}
        style={{ left: `${Math.min(fill, tick)}%`, width: `${Math.abs(fill - tick)}%` }}
      />
      <div className="meter-tick" style={{ left: `${tick}%` }} />
    </div>
  );
}

/**
 * While a window is exhausted and extra spend is enabled, every further call is billed. The
 * CLI and the GNOME popup both drop the bars here and show the two countdowns on one line —
 * what is left to know is when the plan starts covering work again.
 */
function OverPlanStrip({
  extra,
  windows,
  now,
}: {
  extra: ExtraSpend | null;
  windows: QuotaWindow[];
  now: number;
}): JSX.Element {
  return (
    <div className="strip over-plan">
      <p className="strip-line">⚡ Paying above subscription{extra ? ` — ${extraSpendText(extra)} this month` : ""}</p>
      <p className="strip-note">
        {windows
          .map(
            (window) =>
              `${formatWindowLabel(window)}: ${displayUsedPercent(window)}% ↻ ${formatDuration(resetSeconds(window, now))}`
          )
          .join("   ")}
      </p>
    </div>
  );
}

/**
 * Earned rate-limit resets: headroom the account already has in hand, so it belongs with the
 * other facts about the future rather than in the status badges. The expiries come from a
 * best-effort detail endpoint that can name fewer credits than the count — hence "known".
 */
function ResetCreditsStrip({ count, expiries }: { count: number; expiries: string[] }): JSX.Element {
  return (
    <div className={`strip${count > 0 ? " resets" : ""}`}>
      <p className="strip-line">
        ↻ {count} banked reset{count === 1 ? "" : "s"}
      </p>
      {expiries.length > 0 && <p className="strip-note">Known expiries: {formatKnownExpiries(expiries)}</p>}
    </div>
  );
}

/** Peak hours cost a multiple per token, so they belong beside the quota they drain. */
function BurnStrip({ burn, now }: { burn: BurnStatus; now: number }): JSX.Element {
  const first = burn.upcoming[0];
  const changesAt = first ? new Date(burn.in_peak ? first.end : first.start) : null;
  const until = changesAt ? formatDuration((changesAt.getTime() - now) / 1000) : null;
  const ahead = burn.in_peak ? burn.upcoming.slice(1) : burn.upcoming;
  return (
    <div className={`strip${burn.in_peak ? " in-peak" : ""}`}>
      <p className="strip-line">
        {burn.in_peak && changesAt
          ? `🔥 ${formatMultiplier(burn.multiplier)} burn until ${formatClock(changesAt)} (${until}) — ${burn.applies_to}`
          : `${formatMultiplier(burn.multiplier)} burn — next ${formatMultiplier(burn.peak_multiplier)} window in ${until}`}
      </p>
      {ahead.length > 0 && (
        <p className="strip-note intervals">
          <span>{formatMultiplier(burn.peak_multiplier)} windows (local):</span>
          {ahead.map((interval) => (
            <span key={interval.start}>{formatPeakInterval(interval)}</span>
          ))}
        </p>
      )}
    </div>
  );
}

function extraSpendText(extra: ExtraSpend): string {
  return `extra ${formatUsd(extra.used_usd)}/${formatUsdWhole(extra.monthly_limit_usd)} (${roundHalfToEven(extra.utilization)}%)`;
}

function providerTint(provider: ProviderView, quota: EffectiveQuota, now: number): Tint | "error" {
  if (quota.error !== null && quota.windows.length === 0) return "error";
  if (quota.windows.length === 0) return "unknown";
  if (provider.currently_over_plan) return "hot";
  if (quota.staleSince !== null) return "stale";
  return bindingTint(
    quota.windows.map((window) => {
      const math: WindowMath = {
        usedPercent: window.used_percent,
        resetSeconds: resetSeconds(window, now),
        windowSeconds: window.window_seconds,
      };
      const exhausted = isExhausted(math);
      return exhausted
        ? "hot"
        : tintFor(computePace(math), window.used_percent, { isShort: isShortWindow(window, quota.windows) });
    })
  );
}
