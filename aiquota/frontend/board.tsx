/**
 * Pure rendering of one `/v1/quotas` payload at one instant. Time enters through `now`;
 * the host owns fetching.
 */

import { Badge, Paper } from "@mantine/core";
import { type JSX } from "react";

import type { AllQuotasView, BalanceView, BurnStatus, GrantView, ProviderView, WindowView } from "./schema";
import {
  formatAge,
  formatExtraSpend,
  formatExtraSpendExact,
  extraSpendHasHiddenFraction,
  refreshWindowTiming,
  showResetCredits,
  quotaActivity,
  staleObservationAt,
  formatClock,
  formatDeadline,
  formatDuration,
  formatMultiplier,
  formatPeakInterval,
  formatResetCreditSummary,
  roundHalfToEven,
  type Tint,
} from "../shared/presentation.js";
import { effectiveQuota, type EffectiveQuota } from "./quotas";

const API_PROVIDERS = new Set(["openai_platform", "anthropic_platform"]);
const PROVIDER_LABELS: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
  openai_platform: "OpenAI API",
  anthropic_platform: "Anthropic API",
  zai: "z.ai",
};

/** One card per provider, grouped into subscription quotas and API billing balances. */
export function QuotaBoard({ quotas, now }: { quotas: AllQuotasView; now: number }): JSX.Element {
  const subscriptions = quotas.providers.filter((provider) => !API_PROVIDERS.has(provider.provider));
  const apiBalances = quotas.providers.filter((provider) => API_PROVIDERS.has(provider.provider));
  const connectionError = quotas.connection_error ?? null;

  return (
    <section className="aiquota-scope aiquota-board">
      <p className="aiquota-board-help">
        Meter: fill = quota used; tick = time elapsed. Forecasts use average usage since the window started.
      </p>
      {connectionError && <ConnectionError error={connectionError} />}
      {subscriptions.length > 0 && <ProviderGroup label="Subscriptions" providers={subscriptions} now={now} />}
      {apiBalances.length > 0 && <ProviderGroup label="API balances" providers={apiBalances} now={now} />}
      {quotas.providers.length === 0 && <p className="aiquota-empty">No quota providers are available.</p>}
    </section>
  );
}

function providerLabel(provider: ProviderView): string {
  return provider.label || PROVIDER_LABELS[provider.provider] || provider.provider;
}

function ProviderGroup({
  label,
  providers,
  now,
}: {
  label: string;
  providers: ProviderView[];
  now: number;
}): JSX.Element {
  const headingId = `aiquota-group-${label.toLowerCase().replaceAll(" ", "-")}`;
  return (
    <section className="aiquota-provider-group" aria-labelledby={headingId}>
      <h2 className="aiquota-group-title" id={headingId}>
        {label}
      </h2>
      <div className="aiquota-provider-grid">
        {providers.map((provider) => (
          <ProviderCard key={provider.provider} provider={provider} now={now} />
        ))}
      </div>
    </section>
  );
}

function ProviderCard({ provider, now }: { provider: ProviderView; now: number }): JSX.Element {
  const quota = effectiveQuota(provider);
  const tint = providerTint(provider, quota, now);
  const balances = provider.balances ?? [];
  const grants = provider.grants ?? [];
  const label = providerLabel(provider);
  const errorSummary = provider.error_summary ?? (quota.error === null ? null : "Provider update failed");
  const errorDetail = provider.error_detail ?? quota.error;
  const staleSince = providerStaleSince(provider, quota, now);
  const isStale = staleSince !== null;
  const windows = quota.windows.map((window) => presentWindow(window, now, isStale));
  const { resetPending, overPlan, status } = quotaActivity(
    windows.map(({ window, resetPending }) => ({ exhausted: window.exhausted, resetPending })),
    isStale,
    provider.currently_over_plan,
    provider.paid_credits_active,
    quota.extraSpend !== null
  );
  const hasReadings =
    quota.windows.length > 0 ||
    balances.length > 0 ||
    grants.length > 0 ||
    quota.extraSpend !== null ||
    (quota.resetCredits ?? 0) > 0;
  const showResetSummary = showResetCredits(
    quota.resetCredits,
    windows.map(({ window, resetPending: pending }) => ({ exhausted: window.exhausted, resetPending: pending }))
  );

  return (
    <Paper
      component="article"
      withBorder
      radius="md"
      p="md"
      className={`aiquota-card aiquota-tint-${tint}`}
      aria-label={`${label} quota`}
    >
      <header className="aiquota-card-head">
        <h3 className="aiquota-card-title">
          <span className={`aiquota-dot aiquota-tint-${tint}`} aria-hidden="true" />
          {label}
        </h3>
        <div className="aiquota-card-status">
          {staleSince !== null ? (
            <Badge color="yellow" variant="light" className="aiquota-badge-stale">
              Data stale · {formatAge((now - staleSince) / 1000)}
            </Badge>
          ) : null}
          {errorSummary !== null && (
            <Badge color="red" variant="light" className="aiquota-badge-error">
              {errorSummary}
            </Badge>
          )}
        </div>
      </header>

      {errorSummary !== null && errorDetail !== null && <ErrorDetails summary="Error details" detail={errorDetail} />}
      {status && (
        <p className={`aiquota-status-row${!isStale && !resetPending ? " aiquota-status-urgent" : ""}`}>{status}</p>
      )}
      {(balances.length > 0 || grants.length > 0) && <Readings balances={balances} grants={grants} now={now} />}
      {quota.resetCredits !== null && (
        <ResetCredits count={quota.resetCredits} quota={quota} now={new Date(now)} showSummary={showResetSummary} />
      )}
      {provider.burn && hasReadings && <BurnSummary burn={provider.burn} now={now} />}
      {quota.extraSpend && (
        <>
          <p className="aiquota-extra-usage">
            {formatExtraSpend(quota.extraSpend.used_usd, quota.extraSpend.monthly_limit_usd, overPlan)}
          </p>
          {extraSpendHasHiddenFraction(quota.extraSpend.used_usd, quota.extraSpend.monthly_limit_usd) && (
            <details className="aiquota-details">
              <summary>Extra spend details</summary>
              <p>Exact: {formatExtraSpendExact(quota.extraSpend.used_usd, quota.extraSpend.monthly_limit_usd)}</p>
            </details>
          )}
        </>
      )}

      {windows.length > 0
        ? windows.map(({ window, resetPending: windowResetPending }) => (
            <WindowRow
              key={`${window.name ?? ""}-${window.window_seconds}`}
              window={window}
              stale={isStale || windowResetPending}
              resetPending={windowResetPending}
            />
          ))
        : !hasReadings && (
            <p className="aiquota-empty">
              {errorSummary !== null
                ? "No current or cached quota readings are available."
                : "No quota, balance, or grant readings are available for this provider."}
            </p>
          )}
    </Paper>
  );
}

function Readings({
  balances,
  grants,
  now,
}: {
  balances: BalanceView[];
  grants: GrantView[];
  now: number;
}): JSX.Element {
  const groups = new Map<string, { balances: BalanceView[]; grants: GrantView[] }>();
  for (const balance of balances) {
    const group = balance.group ?? "";
    const rows = groups.get(group) ?? { balances: [], grants: [] };
    rows.balances.push(balance);
    groups.set(group, rows);
  }
  for (const grant of grants) {
    const group = grant.group ?? "";
    const rows = groups.get(group) ?? { balances: [], grants: [] };
    rows.grants.push(grant);
    groups.set(group, rows);
  }

  return (
    <div className="aiquota-readings">
      {[...groups].map(([group, rows]) => (
        <section className="aiquota-reading-group" key={group || "default"}>
          {group && <h4 className="aiquota-reading-group-title">{group}</h4>}
          <dl className="aiquota-reading-list">
            {rows.balances.map((balance) => (
              <BalanceRow key={`${balance.label}-${balance.value}`} balance={balance} now={now} />
            ))}
            {rows.grants.map((grant, index) => (
              <GrantRow key={`${grant.label}-${grant.expires_at ?? "unknown"}-${index}`} grant={grant} now={now} />
            ))}
          </dl>
        </section>
      ))}
    </div>
  );
}

function BalanceRow({ balance, now }: { balance: BalanceView; now: number }): JSX.Element {
  return (
    <div className="aiquota-reading-row">
      <dt>{balance.label}</dt>
      <dd>{balance.value}</dd>
      {balance.stale_since && (
        <span className="aiquota-reading-note">
          Balance stale · {formatAge((now - Date.parse(balance.stale_since)) / 1000)}
        </span>
      )}
      {balance.error && <ErrorDetails summary="Balance lookup failed" detail={balance.error} />}
      {(balance.exact_value || balance.pool_detail) && (
        <details className="aiquota-details">
          <summary>Balance details</summary>
          <dl className="aiquota-detail-list">
            {balance.pool_detail && (
              <>
                <dt>Pool</dt>
                <dd>{balance.pool_detail}</dd>
              </>
            )}
            {balance.exact_value && (
              <>
                <dt>Exact balance</dt>
                <dd>{balance.exact_value}</dd>
              </>
            )}
          </dl>
        </details>
      )}
    </div>
  );
}

function GrantRow({ grant, now }: { grant: GrantView; now: number }): JSX.Element {
  const expiry = grant.expires_at ? formatDeadline(grant.expires_at, new Date(now)) : null;
  const hasDetails =
    !grant.original_repeats_remaining || grant.exact_remaining || grant.scope_detail || grant.expires_at;
  const notes = [
    grant.status ?? null,
    grant.scope ? `Scope: ${grant.scope}` : null,
    `Expires ${expiry ?? "unknown"}`,
  ].filter((note): note is string => note !== null);
  return (
    <div className="aiquota-reading-row">
      <dt>{grant.label}</dt>
      <dd>{grant.remaining} remaining</dd>
      <span className="aiquota-reading-note">{notes.join(" · ")}</span>
      {hasDetails && (
        <details className="aiquota-details">
          <summary>Grant details</summary>
          <dl className="aiquota-detail-list">
            {!grant.original_repeats_remaining && (
              <>
                <dt>Original amount</dt>
                <dd>{grant.original}</dd>
              </>
            )}
            {grant.exact_remaining && (
              <>
                <dt>Exact remaining</dt>
                <dd>{grant.exact_remaining}</dd>
              </>
            )}
            {grant.scope_detail && (
              <>
                <dt>Scope</dt>
                <dd>{grant.scope_detail}</dd>
              </>
            )}
            {grant.expires_at && (
              <>
                <dt>Expiry time</dt>
                <dd>{formatDeadline(grant.expires_at, new Date(now), true)}</dd>
              </>
            )}
          </dl>
        </details>
      )}
    </div>
  );
}

function ErrorDetails({ summary, detail }: { summary: string; detail: string | null }): JSX.Element {
  return (
    <details className="aiquota-error-details">
      <summary>{summary}</summary>
      {detail && <pre>{detail}</pre>}
    </details>
  );
}

function presentWindow(window: WindowView, now: number, stale = false): { window: WindowView; resetPending: boolean } {
  const timing = refreshWindowTiming(
    {
      resetAtMs: window.reset_at ? Date.parse(window.reset_at) : null,
      resetSeconds: window.reset_seconds,
      resetPending: window.reset_pending ?? false,
      reset: window.reset,
      pace: window.pace,
      forecast: window.forecast,
    },
    now,
    stale
  );
  return {
    window: {
      ...window,
      reset: timing.reset,
      pace: timing.pace,
      forecast: timing.forecast,
      tint: timing.resetPending || stale ? "stale" : window.tint,
    },
    resetPending: timing.resetPending,
  };
}

function providerStaleSince(provider: ProviderView, quota: EffectiveQuota, now: number): number | null {
  const since = provider.stale_since ?? quota.staleSince;
  return staleObservationAt(
    provider.display.observed_at ? Date.parse(provider.display.observed_at) : null,
    since ? Date.parse(since) : null,
    provider.stale_at ? Date.parse(provider.stale_at) : null,
    now
  );
}

function WindowRow({
  window,
  stale,
  resetPending,
}: {
  window: WindowView;
  stale: boolean;
  resetPending: boolean;
}): JSX.Element {
  const tint = stale ? "stale" : window.tint;
  return (
    <section className="aiquota-window">
      <div className="aiquota-window-head">
        <span className="aiquota-window-label">{window.label}</span>
        <span className="aiquota-window-used">{window.used}%</span>
      </div>
      <Meter
        usedPercent={window.used_percent}
        displayedPercent={window.used}
        elapsed={window.elapsed}
        tint={tint}
        label={`${window.label} quota usage`}
      />
      <div className="aiquota-window-meta">
        <span>{resetPending ? "Awaiting updated usage" : `Resets in ${window.reset}`}</span>
        {window.exhausted && !resetPending && <span className="aiquota-exhausted">exhausted</span>}
        {window.forecast && !window.exhausted && <span>{window.forecast}</span>}
      </div>
      {window.pace && (
        <details className="aiquota-details">
          <summary>Window details</summary>
          <p>Numeric pace: {window.pace}</p>
        </details>
      )}
    </section>
  );
}

function Meter({
  usedPercent,
  displayedPercent,
  elapsed,
  tint,
  label,
}: {
  usedPercent: number;
  displayedPercent: number;
  elapsed: number;
  tint: Tint;
  label: string;
}): JSX.Element {
  const fill = Math.max(0, Math.min(100, usedPercent));
  const tick = elapsed * 100;
  return (
    <div
      className={`aiquota-meter aiquota-tint-${tint}`}
      role="meter"
      aria-label={label}
      aria-valuenow={Math.max(0, Math.min(100, displayedPercent))}
      aria-valuetext={`${displayedPercent}% used, ${roundHalfToEven(elapsed * 100)}% of the window elapsed`}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className={`aiquota-meter-fill${fill >= 100 ? " aiquota-meter-fill-end" : ""}`}
        style={{ width: `${fill}%` }}
      />
      <div
        className={`aiquota-meter-deviation ${fill >= tick ? "aiquota-ahead" : "aiquota-behind"}`}
        style={{ left: `${Math.min(fill, tick)}%`, width: `${Math.abs(fill - tick)}%` }}
      />
      <div className="aiquota-meter-tick" style={{ left: `${tick}%` }} />
    </div>
  );
}

function ResetCredits({
  count,
  quota,
  now,
  showSummary,
}: {
  count: number;
  quota: EffectiveQuota;
  now: Date;
  showSummary: boolean;
}): JSX.Element {
  return (
    <div className={`aiquota-strip${count > 0 ? " aiquota-strip-resets" : ""}`}>
      {showSummary && (
        <p className="aiquota-strip-line">
          Banked resets:{" "}
          {formatResetCreditSummary(count, quota.resetCreditExpiries, quota.resetCreditExpiryLookupFailed, now)}
        </p>
      )}
      <details className="aiquota-details">
        <summary>{count > 0 ? "Reset expiry details" : "Details"}</summary>
        <p>
          {count === 0 && "Banked resets: "}
          {formatResetCreditSummary(count, quota.resetCreditExpiries, quota.resetCreditExpiryLookupFailed, now, true)}
        </p>
      </details>
    </div>
  );
}

function BurnSummary({ burn, now }: { burn: BurnStatus; now: number }): JSX.Element {
  const next = burn.upcoming[0];
  const changesAt = next ? new Date(burn.in_peak ? next.end : next.start) : null;
  const until = changesAt ? formatDuration((changesAt.getTime() - now) / 1000) : null;
  return (
    <div className={`aiquota-strip${burn.in_peak ? " aiquota-strip-peak" : ""}`}>
      <p className="aiquota-strip-line">
        {burn.in_peak && changesAt
          ? `${formatMultiplier(burn.multiplier)} burn until ${formatClock(changesAt)} (${until}) · ${burn.applies_to}`
          : `${formatMultiplier(burn.multiplier)} burn · ${formatMultiplier(burn.peak_multiplier)} window in ${until ?? "unknown"}`}
      </p>
      {burn.upcoming.length > 0 && (
        <details className="aiquota-details">
          <summary>Peak schedule</summary>
          <p className="aiquota-strip-note aiquota-strip-intervals">
            {burn.upcoming.map((interval) => formatPeakInterval(interval)).join(" · ")}
          </p>
        </details>
      )}
    </div>
  );
}

function ConnectionError({ error }: { error: string }): JSX.Element {
  return (
    <div className="aiquota-source-error">
      <p>Quota data source connection failed.</p>
      <ErrorDetails summary="Source error details" detail={error} />
    </div>
  );
}

function providerTint(provider: ProviderView, quota: EffectiveQuota, now: number): Tint | "error" {
  if (
    quota.windows.some((window) => presentWindow(window, now).resetPending) ||
    providerStaleSince(provider, quota, now) !== null
  ) {
    return "stale";
  }
  if (quota.error !== null && quota.windows.length === 0) return "error";
  if (quota.windows.length === 0) return "unknown";
  if (provider.currently_over_plan || provider.paid_credits_active) return "hot";
  return provider.summary_tint;
}
