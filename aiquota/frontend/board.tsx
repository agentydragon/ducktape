/**
 * The quota board: pure rendering of one `/v1/quotas` payload at one instant.
 *
 * Kept apart from the page around it because the Haku Console renders this same board inside
 * its own shell, from the payload its aiquota proxy already fetches
 * (`haku/console/frontend/aiquota_panel.tsx`) — one renderer, so the two surfaces cannot drift
 * the way `aiquota/AGENTS.md` warns about. Time enters only as the `now` prop and nothing here
 * fetches, so a host owns its own refresh and a screenshot of a scene is the same picture on
 * every run.
 *
 * Each window renders as the two-marker bar the design settled on
 * (`aiquota/gnome/DESIGN.md`): the fill is how much quota is gone, the tick is how much of
 * the window is gone, and the shaded span between them is the pace deviation — the thing a
 * plain percentage cannot tell you, since "80% used" is comfortable at day six of seven and
 * alarming at day two.
 */

import { type JSX } from "react";

import type { AllQuotasView, BurnStatus, ExtraSpend, PaidCredits, ProviderView, WindowView } from "./schema";
import { formatUsd, formatUsdWhole } from "./format";
import {
  formatAge,
  formatClock,
  formatDuration,
  formatPeakInterval,
  formatPrepaidBalance,
  formatMultiplier,
  formatResetCreditSummary,
  roundHalfToEven,
  type Tint,
} from "../shared/presentation.js";
import { effectiveQuota, type EffectiveQuota, type PrepaidBalance } from "./quotas";

// The payload carries provider ids; these are how the vendors write their names, matching the
// GNOME panel's labels. An id with no entry shows as itself rather than being guessed at.
const PROVIDER_NAMES: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
  openai_platform: "OpenAI API",
  anthropic_platform: "Anthropic API",
  zai: "z.ai",
};

/**
 * One card per provider. `aiquota-scope` carries the palette (board.css), so a host page's own
 * tokens are neither read nor overwritten.
 */
export function QuotaBoard({ quotas, now }: { quotas: AllQuotasView; now: number }): JSX.Element {
  return (
    <section className="aiquota-scope aiquota-board" aria-live="polite">
      {quotas.providers.map((provider) => (
        <ProviderCard key={provider.provider} provider={provider} now={now} />
      ))}
    </section>
  );
}

function ProviderCard({ provider, now }: { provider: ProviderView; now: number }): JSX.Element {
  const quota = effectiveQuota(provider);
  const tint = providerTint(provider, quota);
  const overPlan = provider.currently_over_plan;
  const paidCreditsActive = provider.paid_credits_active;
  return (
    <article className={`aiquota-card aiquota-tint-${tint}`} aria-label={`${provider.provider} quota`}>
      <header className="aiquota-card-head">
        <h2 className="aiquota-card-title">
          <span className={`aiquota-dot aiquota-tint-${tint}`} aria-hidden="true" />
          {PROVIDER_NAMES[provider.provider] ?? provider.provider}
          {provider.provider === "openai_platform" && quota.openaiPlatformCredits
            ? ` · ${formatUsd(Number(quota.openaiPlatformCredits.total_available_usd))} API credit`
            : null}
          {provider.provider === "claude" && quota.prepaidBalance
            ? ` · ${formatClaudePrepaidHeading(quota.prepaidBalance, quota.prepaidStaleSince, quota.staleSince, now)}`
            : null}
        </h2>
        <div className="aiquota-card-status">
          {quota.staleSince !== null && (
            <span className="aiquota-badge aiquota-badge-stale">
              Stale · {formatAge((now - Date.parse(quota.staleSince)) / 1000)}
            </span>
          )}
          {quota.error !== null && <span className="aiquota-badge aiquota-badge-error">Error</span>}
        </div>
      </header>

      {quota.error !== null && <p className="aiquota-error">{quota.error}</p>}
      {overPlan && <OverPlanStrip extra={quota.extraSpend} windows={quota.windows} />}
      {quota.paidCredits && (
        <PaidCreditsStrip credits={quota.paidCredits} active={paidCreditsActive} windows={quota.windows} />
      )}
      {quota.openaiPlatformCredits?.grants.length ? (
        <p className="aiquota-aside">
          API credit grants: {formatOpenaiPlatformGrantSummary(quota.openaiPlatformCredits.grants)}
        </p>
      ) : null}
      {quota.anthropicPlatformCredits && <AnthropicPlatformCreditsStrip credits={quota.anthropicPlatformCredits} />}
      {quota.resetCredits !== null && (
        <ResetCreditsStrip
          count={quota.resetCredits}
          expiries={quota.resetCreditExpiries}
          expiryLookupFailed={quota.resetCreditExpiryLookupFailed}
        />
      )}
      {provider.burn && <BurnStrip burn={provider.burn} now={now} />}

      {overPlan || paidCreditsActive
        ? null
        : quota.windows.length === 0
          ? quota.error === null &&
            quota.prepaidBalance === null &&
            quota.openaiPlatformCredits == null &&
            quota.anthropicPlatformCredits == null && <p className="aiquota-empty">No quota data.</p>
          : quota.windows.map((window) => (
              <WindowRow
                key={`${window.name ?? ""}-${window.window_seconds}`}
                window={window}
                stale={quota.staleSince !== null}
              />
            ))}

      {!overPlan && provider.extra_status === "informational" && quota.extraSpend && (
        <p className="aiquota-aside">{extraSpendText(quota.extraSpend)} spent this month</p>
      )}
    </article>
  );
}

function WindowRow({ window, stale }: { window: WindowView; stale: boolean }): JSX.Element {
  const tint = stale ? "stale" : window.tint;
  return (
    <section className="aiquota-window">
      <div className="aiquota-window-head">
        <span className="aiquota-window-label">{window.label}</span>
        <span className="aiquota-window-used">{window.used}%</span>
      </div>
      <Meter
        usedPercent={window.used_percent}
        elapsed={window.elapsed}
        tint={tint}
        label={`${window.label}: ${window.used}% used, ${roundHalfToEven(window.elapsed * 100)}% of the window elapsed`}
      />
      <div className="aiquota-window-meta">
        <span>↻ {window.reset}</span>
        {window.exhausted ? (
          <span className="aiquota-exhausted">exhausted</span>
        ) : (
          <>
            {window.pace && <span className={`aiquota-pace aiquota-tint-${tint}`}>Δ{window.pace}</span>}
            {window.forecast && <span>{window.forecast}</span>}
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
      className={`aiquota-meter aiquota-tint-${tint}`}
      role="meter"
      aria-label={label}
      aria-valuenow={roundHalfToEven(usedPercent)}
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

/**
 * While a window is exhausted and extra spend is enabled, every further call is billed. The
 * CLI and the GNOME popup both drop the bars here and show the two countdowns on one line —
 * what is left to know is when the plan starts covering work again.
 */
function OverPlanStrip({ extra, windows }: { extra: ExtraSpend | null; windows: WindowView[] }): JSX.Element {
  return (
    <div className="aiquota-strip aiquota-strip-over-plan">
      <p className="aiquota-strip-line">
        ⚡ Paying above subscription{extra ? ` — ${extraSpendText(extra)} this month` : ""}
      </p>
      <p className="aiquota-strip-note">
        {windows.map((window) => `${window.label}: ${window.used}% ↻ ${window.reset}`).join("   ")}
      </p>
    </div>
  );
}

/**
 * Earned rate-limit resets: headroom the account already has in hand, so it belongs with the
 * other facts about the future rather than in the status badges. Expiry details may be partial
 * or fail independently from the authoritative reset count.
 */
function ResetCreditsStrip({
  count,
  expiries,
  expiryLookupFailed,
}: {
  count: number;
  expiries: string[];
  expiryLookupFailed: boolean;
}): JSX.Element {
  return (
    <div className={`aiquota-strip${count > 0 ? " aiquota-strip-resets" : ""}`}>
      <p className="aiquota-strip-line">{formatResetCreditSummary(count, expiries, expiryLookupFailed)}</p>
    </div>
  );
}

function PaidCreditsStrip({
  credits,
  active,
  windows,
}: {
  credits: PaidCredits;
  active: boolean;
  windows: WindowView[];
}): JSX.Element {
  const amount = credits.unlimited ? "unlimited credits" : formatCreditBalance(credits.balance);
  return (
    <div className={`aiquota-strip${active ? " aiquota-strip-over-plan" : ""}`}>
      <p className="aiquota-strip-line">
        {active ? "Using paid credits" : "Paid credits"} · {amount}
      </p>
      {active && (
        <p className="aiquota-strip-note">
          {windows.map((window) => `${window.label}: ${window.used}% ↻ ${window.reset}`).join("   ")}
        </p>
      )}
    </div>
  );
}

function formatClaudePrepaidHeading(
  balance: PrepaidBalance,
  staleSince: string | null,
  providerStaleSince: string | null,
  now: number
): string {
  const label =
    balance.kind === "available"
      ? `${formatPrepaidBalance(balance.amount_minor, balance.exponent, balance.currency)} prepaid balance`
      : "prepaid balance unavailable";
  if (staleSince && Date.parse(staleSince) !== (providerStaleSince ? Date.parse(providerStaleSince) : null)) {
    return `${label} (stale ${formatAge((now - Date.parse(staleSince)) / 1000)})`;
  }
  return label;
}

function AnthropicPlatformCreditsStrip({
  credits,
}: {
  credits: NonNullable<EffectiveQuota["anthropicPlatformCredits"]>;
}): JSX.Element {
  if (credits.organizations.length === 0) return <p className="aiquota-aside">No Anthropic API organizations.</p>;
  return (
    <div className="aiquota-aside">
      {credits.organizations.map((organization) => {
        const balance = organization.balance?.credits;
        return (
          <section key={organization.uuid}>
            <p>
              {organization.name} · Anthropic API credit balance ·{" "}
              {balance
                ? `${formatPrepaidBalance(balance.amount_minor, balance.exponent, organization.currency ?? "USD")} available`
                : organization.error
                  ? `lookup failed: ${organization.error}`
                  : "unavailable"}
            </p>
            {anthropicTrancheLines(organization.tranches, "API credit grant")}
            {anthropicTrancheLines(organization.promo_tranches, "API promo credit grant")}
          </section>
        );
      })}
    </div>
  );
}

function formatOpenaiPlatformGrantSummary(
  grants: NonNullable<EffectiveQuota["openaiPlatformCredits"]>["grants"]
): string {
  return grants
    .map((grant) => {
      const expiry = grant.expires_at ? new Date(grant.expires_at).toLocaleString() : "unknown";
      const status = grant.is_scheduled ? " · scheduled" : grant.is_held_for_prepaid_transition ? " · held" : "";
      return `${formatUsd(Number(grant.granted_usd) - Number(grant.used_usd))} expires ${expiry}${status}`;
    })
    .join("; ");
}

function anthropicTrancheLines(
  tranches: NonNullable<EffectiveQuota["anthropicPlatformCredits"]>["organizations"][number]["tranches"],
  label: string
): JSX.Element[] {
  return (tranches ?? []).flatMap((tranche) => {
    const granted = tranche.granted.credits;
    const remaining = tranche.remaining.credits;
    if (!granted || !remaining) return [];
    const expiry = tranche.expires_at ? new Date(tranche.expires_at).toLocaleString() : "unknown";
    const products = [
      ...new Set(
        (tranche.scope.applies_to ?? []).map((item) => {
          const qualifiers = [item.model_family, item.model_version].filter(Boolean).join(", ");
          return qualifiers ? `${item.product} (${qualifiers})` : item.product;
        })
      ),
    ].sort();
    const isUnrestricted = ["unrestricted", "unscoped"].includes(tranche.scope.kind) && products.length === 0;
    const scope = isUnrestricted ? "" : ` · scope: ${products.join(", ") || tranche.scope.kind}`;
    return [
      <p key={tranche.id}>
        {label} {formatPrepaidBalance(remaining.amount_minor, remaining.exponent, tranche.currency)} /{" "}
        {formatPrepaidBalance(granted.amount_minor, granted.exponent, tranche.currency)} · expires {expiry}
        {scope}
      </p>,
    ];
  });
}

function formatCreditBalance(raw: string | null | undefined): string {
  if (raw == null) return "available";
  const amount = Number(raw);
  if (!Number.isFinite(amount)) return "available";
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(amount)} credits left`;
}

/** Peak hours cost a multiple per token, so they belong beside the quota they drain. */
function BurnStrip({ burn, now }: { burn: BurnStatus; now: number }): JSX.Element {
  const first = burn.upcoming[0];
  const changesAt = first ? new Date(burn.in_peak ? first.end : first.start) : null;
  const until = changesAt ? formatDuration((changesAt.getTime() - now) / 1000) : null;
  const ahead = burn.in_peak ? burn.upcoming.slice(1) : burn.upcoming;
  return (
    <div className={`aiquota-strip${burn.in_peak ? " aiquota-strip-peak" : ""}`}>
      <p className="aiquota-strip-line">
        {burn.in_peak && changesAt
          ? `🔥 ${formatMultiplier(burn.multiplier)} burn until ${formatClock(changesAt)} (${until}) — ${burn.applies_to}`
          : `${formatMultiplier(burn.multiplier)} burn — next ${formatMultiplier(burn.peak_multiplier)} window in ${until}`}
      </p>
      {ahead.length > 0 && (
        <p className="aiquota-strip-note aiquota-strip-intervals">
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

function providerTint(provider: ProviderView, quota: EffectiveQuota): Tint | "error" {
  if (quota.error !== null && quota.windows.length === 0) return "error";
  if (quota.windows.length === 0) return "unknown";
  if (provider.currently_over_plan || provider.paid_credits_active) return "hot";
  if (quota.staleSince !== null) return "stale";
  return provider.summary_tint;
}
