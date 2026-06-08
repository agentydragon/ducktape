import React, { useState } from "react";
import { Button, Checkbox } from "@mantine/core";
import { NativeSelectField, NumberField } from "./lib/controls.tsx";
import { clampInteger, fmtNumber, fmtQuantity, fmtUsd } from "./lib/format.ts";
import { LIFECYCLE_KINDS, SELL_BUCKETS, SELL_BUCKET_BY_CODE, defaultLifecycleEvent } from "./input_helpers.ts";
import { portfolioHasBucket, isPrivateSecurityPosition } from "./data_helpers.ts";

function firstSaleMonth(events) {
  let earliest = null;
  for (const event of events) {
    if (event.kind === "property_sale" && (earliest == null || event.month < earliest)) {
      earliest = event.month;
    }
  }
  return earliest;
}

// True for any event the wire validator rejects as a post-sale residual: events strictly
// after `saleMonth`, plus same-month non-sale events (a SetRentedFraction in the same month
// as the sale is also illegal). `saleMonth == null` means no sale on the timeline → nothing
// is post-sale.
function isEventPostSale(event, saleMonth) {
  if (saleMonth == null) return false;
  if (event.month > saleMonth) return true;
  return event.month === saleMonth && event.kind !== "property_sale";
}

export function DisclosureArrow({ collapsed, className = "" }) {
  return (
    <span
      aria-hidden="true"
      className={`text-[8px] transition-transform ${collapsed ? "" : "rotate-90"} ${className}`.trim()}
    >
      ▶
    </span>
  );
}

export function propertyLabel(property) {
  const sqft = Number(property.sqft);
  const head = property.address || property.id;
  const meta = `${fmtUsd(property.priceUsd)}` + (Number.isFinite(sqft) && sqft > 0 ? ` · ${fmtNumber(sqft)} sqft` : "");
  return `${head} — ${meta}`;
}

// Read-only context for a selected property: a neighborhood · beds/baths · HOA summary, the photo
// thumbnail (links to the listing image), the source-listing link, and free-form notes. Only the
// fields the deployment actually provides render (public fixtures carry none of the rich ones).
export function PropertyDetails({ property }) {
  if (!property) return null;
  const summary = [
    property.neighborhood,
    `${fmtNumber(property.beds)} bd / ${fmtNumber(property.baths)} ba`,
    Number(property.hoaMonthlyUsd) > 0 ? `HOA ${fmtUsd(property.hoaMonthlyUsd)}/mo` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="grid gap-2">
      <div className="text-xs augur-muted">{summary}</div>
      {property.imageUrl && (
        <a
          href={property.imageUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="group inline-block overflow-hidden rounded border border-slate-300 bg-slate-100 dark:border-slate-700 dark:bg-slate-900"
          aria-label={`Open property image for ${property.address || property.id}`}
        >
          <img
            src={property.imageUrl}
            alt={property.address || property.id}
            className="h-24 w-40 object-cover transition-transform duration-200 group-hover:scale-[1.02]"
          />
        </a>
      )}
      {property.sourceUrl && (
        <a
          href={property.sourceUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs font-semibold text-blue-700 hover:text-blue-900 dark:text-blue-300 dark:hover:text-blue-200"
        >
          Source listing ↗
        </a>
      )}
      {property.notes && <div className="text-xs augur-muted whitespace-pre-line">{property.notes}</div>}
    </div>
  );
}

export function LifecycleEventsEditor({ events, horizonMonths, onChange, showLabel = true, className = "" }) {
  const maxMonth = Math.max(1, Number(horizonMonths) - 1);
  const saleMonth = firstSaleMonth(events);
  // After a sale, the property is frozen: the wire validator rejects any other lifecycle event
  // at or after that month. Clamp new events to <sale_month so adding via the button is always
  // a legal placement.
  const addCeiling = saleMonth != null ? saleMonth - 1 : maxMonth;
  const canAdd = addCeiling >= 1;
  const addEvent = () => {
    const last = events[events.length - 1];
    const suggested = last ? Math.min(addCeiling, last.month + 12) : Math.min(addCeiling, 12);
    const kind = last ? last.kind : LIFECYCLE_KINDS[0].value;
    onChange([...events, defaultLifecycleEvent(kind, Math.max(1, suggested))]);
  };
  const updateEvent = (index, patch) => {
    onChange(events.map((event, idx) => (idx === index ? { ...event, ...patch } : event)));
  };
  const removeEvent = (index) => {
    onChange(events.filter((_, idx) => idx !== index));
  };
  return (
    <div className={`grid gap-2 ${className}`.trim()}>
      {showLabel && <div className="augur-field-label">Timeline (mid-horizon changes)</div>}
      {events.length > 0 && (
        <div className="overflow-hidden rounded border border-slate-300 divide-y divide-slate-300 dark:border-slate-600 dark:divide-slate-600">
          {events.map((event, index) => (
            <LifecycleEventRow
              key={event._id ?? index}
              event={event}
              maxMonth={maxMonth}
              postSale={isEventPostSale(event, saleMonth)}
              onChange={(patch) => updateEvent(index, patch)}
              onReplaceKind={(kind) => updateEvent(index, defaultLifecycleEvent(kind, event.month))}
              onRemove={() => removeEvent(index)}
            />
          ))}
        </div>
      )}
      <div>
        <Button size="xs" variant="default" disabled={!canAdd} onClick={addEvent}>
          + Add event
        </Button>
        {!canAdd && (
          <span className="ml-2 text-xs augur-muted">No room before the existing sale at month {saleMonth}.</span>
        )}
      </div>
    </div>
  );
}

function LifecycleEventRow({ event, maxMonth, postSale, onChange, onReplaceKind, onRemove }) {
  // Borderless row — the outer editor frame provides the pill outline; `divide-y` draws the
  // horizontal separator between consecutive rows. The post-sale state tints the row's
  // background so the warning is still obvious without breaking the shared frame.
  return (
    <div
      className={`grid items-end gap-2 p-2 sm:grid-cols-[7rem_10rem_1fr_auto] ${
        postSale ? "bg-rose-50 dark:bg-rose-950/30" : ""
      }`}
    >
      <NumberField
        label="Month"
        value={event.month}
        min={1}
        max={maxMonth}
        step={1}
        suffix="mo"
        onChange={(month) => onChange({ month: clampInteger(month, 1, maxMonth) })}
      />
      <NativeSelectField
        label="Kind"
        aria-label="Lifecycle event kind"
        value={event.kind}
        data={LIFECYCLE_KINDS}
        onChange={(domEvent) => onReplaceKind(domEvent.target.value)}
      />
      <LifecycleEventValueField event={event} onChange={onChange} />
      <Button size="xs" variant="outline" color="red" onClick={onRemove} aria-label="Remove event">
        Remove
      </Button>
      {postSale && (
        <div className="col-span-full text-xs text-rose-700 dark:text-rose-300">
          This event fires after the property is sold — the backend will reject the scenario.
        </div>
      )}
    </div>
  );
}

function LifecycleEventValueField({ event, onChange }) {
  if (event.kind === "set_rented_fraction") {
    return (
      <NumberField
        label="Rented"
        value={event.rentedFractionPct}
        min={0}
        max={100}
        step={5}
        suffix="%"
        onChange={(rentedFractionPct) => onChange({ rentedFractionPct })}
      />
    );
  }
  if (event.kind === "set_primary_residence") {
    return (
      <Checkbox
        label="Primary home"
        aria-label="Primary home after this event"
        checked={Boolean(event.livesHere)}
        classNames={{ label: "text-sm font-semibold augur-strong" }}
        onChange={(domEvent) => onChange({ livesHere: domEvent.currentTarget.checked })}
      />
    );
  }
  if (event.kind === "capital_improvement") {
    return (
      <NumberField
        label="Amount"
        value={event.amountUsd}
        min={0}
        step={1000}
        prefix="$"
        onChange={(amountUsd) => onChange({ amountUsd })}
      />
    );
  }
  if (event.kind === "property_sale") {
    return (
      <NumberField
        label="Closing cost"
        description="% of sale price."
        value={event.closingCostPct}
        min={0}
        max={100}
        step={0.5}
        suffix="%"
        onChange={(closingCostPct) => onChange({ closingCostPct })}
      />
    );
  }
  return null;
}

export function SellOrderControl({
  sellOrder,
  portfolio,
  onChange,
  label = "Sell preference (top first)",
  compact = false,
}) {
  // Render one row per bucket. Enabled rows appear in priority order at the top with up/down
  // controls; disabled rows trail at the bottom, dimmed. Reorder mutates a string of bucket
  // codes (e.g. "pc") so it slots into the URL encoder without an array-equality dance.
  const codes = String(sellOrder ?? "");
  const enabledCodes = [];
  const seen = new Set();
  for (const code of codes) {
    if (SELL_BUCKET_BY_CODE.has(code) && !seen.has(code)) {
      enabledCodes.push(code);
      seen.add(code);
    }
  }
  const disabledBuckets = SELL_BUCKETS.filter((bucket) => !seen.has(bucket.code));
  const enabledBuckets = enabledCodes.map((code) => SELL_BUCKET_BY_CODE.get(code));
  const visibleBuckets = [...enabledBuckets, ...disabledBuckets].filter((bucket) =>
    portfolioHasBucket(portfolio, bucket.name)
  );
  if (visibleBuckets.length === 0) return null;

  const setEnabled = (bucketCode, enabled) => {
    const next = enabledCodes.filter((code) => code !== bucketCode);
    if (enabled) next.push(bucketCode);
    onChange(next.join(""));
  };
  const moveUp = (bucketCode) => {
    const idx = enabledCodes.indexOf(bucketCode);
    if (idx <= 0) return;
    const next = enabledCodes.slice();
    [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]];
    onChange(next.join(""));
  };
  const moveDown = (bucketCode) => {
    const idx = enabledCodes.indexOf(bucketCode);
    if (idx < 0 || idx >= enabledCodes.length - 1) return;
    const next = enabledCodes.slice();
    [next[idx], next[idx + 1]] = [next[idx + 1], next[idx]];
    onChange(next.join(""));
  };

  const firstDisabledCode = visibleBuckets.find((bucket) => enabledCodes.indexOf(bucket.code) < 0)?.code ?? null;
  return (
    <div className={compact ? "" : "mt-3"}>
      {label && <div className="augur-field-label mb-2">{label}</div>}
      <ul className="overflow-hidden rounded border border-slate-200 divide-y divide-slate-200 dark:border-slate-700 dark:divide-slate-700">
        {visibleBuckets.map((bucket) => {
          const enabledIdx = enabledCodes.indexOf(bucket.code);
          const isEnabled = enabledIdx >= 0;
          const canMoveUp = isEnabled && enabledIdx > 0;
          const canMoveDown = isEnabled && enabledIdx < enabledCodes.length - 1;
          // Visual separator between "in order" and "shelved" groups.
          const shelfBoundary = bucket.code === firstDisabledCode && enabledCodes.length > 0;
          return (
            <li
              key={bucket.code}
              className={`flex items-center gap-2 px-2 py-1 ${isEnabled ? "" : "bg-slate-50 opacity-80 dark:bg-slate-900/40"} ${
                shelfBoundary ? "border-t-2 border-t-slate-300 dark:border-t-slate-600" : ""
              }`}
            >
              <span className="w-6 text-right text-sm font-semibold augur-tabular augur-muted">
                {isEnabled ? `${enabledIdx + 1}.` : ""}
              </span>
              <span className="flex-1 text-sm font-semibold augur-strong">{bucket.label}</span>
              {isEnabled ? (
                <>
                  <button
                    type="button"
                    aria-label={`Move ${bucket.label} up`}
                    disabled={!canMoveUp}
                    onClick={() => moveUp(bucket.code)}
                    className="px-1 text-xs augur-muted disabled:opacity-30"
                  >
                    ▲
                  </button>
                  <button
                    type="button"
                    aria-label={`Move ${bucket.label} down`}
                    disabled={!canMoveDown}
                    onClick={() => moveDown(bucket.code)}
                    className="px-1 text-xs augur-muted disabled:opacity-30"
                  >
                    ▼
                  </button>
                  <button
                    type="button"
                    aria-label={`Remove ${bucket.label} from sell order`}
                    onClick={() => setEnabled(bucket.code, false)}
                    className="ml-1 rounded px-1.5 text-sm font-semibold text-rose-600 hover:bg-rose-50 dark:text-rose-300 dark:hover:bg-rose-950/30"
                  >
                    ×
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  aria-label={`Add ${bucket.label} to sell order`}
                  onClick={() => setEnabled(bucket.code, true)}
                  className="rounded px-1.5 text-sm font-semibold text-emerald-600 hover:bg-emerald-50 dark:text-emerald-300 dark:hover:bg-emerald-950/30"
                >
                  +
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function PortfolioGroupHeaderRow({ label }) {
  return (
    <tr className="border-t border-slate-200 dark:border-slate-700">
      <td colSpan={5} className="pt-2 pb-1 text-[11px] uppercase tracking-wide augur-muted">
        {label}
      </td>
    </tr>
  );
}

function PortfolioPositionRow({ position }) {
  return (
    <tr className="border-t border-slate-100 dark:border-slate-800">
      <td className="py-1 pl-3">
        <div className="truncate font-semibold augur-strong">{position.label || position.symbol}</div>
        <div className="truncate text-xs augur-muted">
          {position.symbol} · {position.accountLabel || position.accountId}
        </div>
      </td>
      <td className="py-1 text-right augur-tabular">{fmtQuantity(position.quantity)}</td>
      <td className="py-1 text-right augur-tabular">{fmtUsd(position.unitValueUsd)}</td>
      <td className="py-1 text-right augur-tabular">{fmtUsd(position.totalCostBasisUsd)}</td>
      <td className="py-1 text-right font-semibold augur-tabular">{fmtUsd(position.currentValueUsd)}</td>
    </tr>
  );
}

function PortfolioSubtotalRow({ label, valueUsd, dataKey }) {
  return (
    <tr className="border-t border-slate-100 dark:border-slate-800">
      <td colSpan={4} className="py-1 pl-3 text-xs augur-muted">
        {label}
      </td>
      <td
        className="py-1 text-right text-xs font-semibold augur-tabular augur-muted"
        data-product-portfolio-subtotal={dataKey}
      >
        {fmtUsd(valueUsd)}
      </td>
    </tr>
  );
}

export function ProductPortfolioPanel({ portfolio, error }) {
  const [collapsed, setCollapsed] = useState(true);
  const holdings = portfolio?.holdings ?? [];
  const publicHoldings = holdings.filter((position) => !isPrivateSecurityPosition(position));
  const privateSecurityHoldings = holdings.filter(isPrivateSecurityPosition);
  const publicHoldingsValueUsd = sumCurrentValueUsd(publicHoldings);
  const privateSecurityValueUsd = sumCurrentValueUsd(privateSecurityHoldings);
  const cashUsd = portfolio?.cashUsd ?? 0;
  const totalUsd = cashUsd + (portfolio?.totalHoldingsValueUsd ?? 0);
  const hasAnything = cashUsd > 0 || holdings.length > 0;
  return (
    <div className="px-4 py-3">
      <button
        type="button"
        className="augur-eyebrow flex w-full cursor-pointer items-baseline justify-between gap-2 text-left"
        aria-expanded={!collapsed}
        onClick={() => setCollapsed((previous) => !previous)}
      >
        <span className="inline-flex items-center gap-1">
          <DisclosureArrow collapsed={collapsed} />
          Initial portfolio
        </span>
        {portfolio && !error && (
          <span
            className="text-xs font-normal normal-case tracking-normal augur-tabular augur-muted"
            data-product-portfolio-subtotal="total"
          >
            {fmtUsd(totalUsd)}
          </span>
        )}
      </button>
      {!collapsed && error ? (
        <div className="mt-3 augur-note-danger text-sm">Portfolio failed to load: {error}</div>
      ) : null}
      {!collapsed && !error ? (
        <table className="mt-3 w-full text-sm">
          <thead>
            <tr className="text-left text-[11px] uppercase tracking-wide augur-muted">
              <th className="py-1 font-normal">Holding</th>
              <th className="py-1 text-right font-normal">Units</th>
              <th className="py-1 text-right font-normal">Unit value</th>
              <th className="py-1 text-right font-normal">Basis</th>
              <th className="py-1 text-right font-normal">Value</th>
            </tr>
          </thead>
          <tbody>
            <tr className="border-t border-slate-100 dark:border-slate-800">
              <td className="py-1 font-semibold augur-strong">Cash</td>
              <td colSpan={3} />
              <td className="py-1 text-right font-semibold augur-tabular">{fmtUsd(cashUsd)}</td>
            </tr>
            {publicHoldings.length > 0 && (
              <>
                <PortfolioGroupHeaderRow label="Public securities" />
                {publicHoldings.map((position) => (
                  <PortfolioPositionRow key={position.positionId} position={position} />
                ))}
                <PortfolioSubtotalRow
                  label="Public subtotal"
                  valueUsd={publicHoldingsValueUsd}
                  dataKey="public-securities"
                />
              </>
            )}
            {privateSecurityHoldings.length > 0 && (
              <>
                <PortfolioGroupHeaderRow label="Private securities" />
                {privateSecurityHoldings.map((position) => (
                  <PortfolioPositionRow key={position.positionId} position={position} />
                ))}
                <PortfolioSubtotalRow
                  label="Private subtotal"
                  valueUsd={privateSecurityValueUsd}
                  dataKey="private-securities"
                />
              </>
            )}
            {holdings.length === 0 && (
              <tr className="border-t border-slate-100 dark:border-slate-800">
                <td colSpan={5} className="py-1 augur-muted">
                  No holdings
                </td>
              </tr>
            )}
          </tbody>
          {hasAnything && (
            <tfoot>
              <tr className="border-t-2 border-slate-300 dark:border-slate-600">
                <td colSpan={4} className="py-1 font-semibold augur-strong">
                  Total
                </td>
                <td className="py-1 text-right font-semibold augur-tabular">{fmtUsd(totalUsd)}</td>
              </tr>
            </tfoot>
          )}
        </table>
      ) : null}
    </div>
  );
}

function sumCurrentValueUsd(positions) {
  return positions.reduce((total, position) => total + (position.currentValueUsd ?? 0), 0);
}
