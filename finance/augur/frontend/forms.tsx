import React, { useState } from "react";
import { Button, Checkbox } from "@mantine/core";
import { NativeSelectField, NumberField } from "./lib/controls";
import {
  clampInteger,
  currencyQuantaAdd,
  currencyQuantaIsPositive,
  fmtNumber,
  fmtQuanta,
  fmtUsd,
  fmtQuantity,
} from "./lib/format";
import { LIFECYCLE_KINDS, defaultLifecycleEvent, resolveSleeveWeights, sleeveKey } from "./input_helpers";
import { sellableSleeves, isPrivateSecurityPosition } from "./data_helpers";

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
  const meta = `${fmtUsd(property.price)}` + (Number.isFinite(sqft) && sqft > 0 ? ` · ${fmtNumber(sqft)} sqft` : "");
  return `${head} — ${meta}`;
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
        value={event.amount}
        min={0}
        step={1000}
        prefix="$"
        onChange={(amount) => onChange({ amount })}
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

export function SleeveWeightsControl({
  sleeveWeights,
  portfolio,
  onChange,
  label = "Target allocation (relative weights)",
  compact = false,
}) {
  // One row per sellable sleeve (a held security, or a TLH portfolio) with an integer weight. Only RATIOS matter, so the row also
  // shows each weight as a percentage of their sum — that is the number a person actually reasons
  // about, while the stored value stays an integer and needs no sum-to-one validator to defend it.
  //
  // `sleeveWeights == null` means "not edited yet" and seeds from what the owner currently holds,
  // so opening this and changing nothing leaves the target matching today's portfolio. Weight 0 is
  // meaningful rather than empty: it puts the holding OUTSIDE the target, never sold to fund the
  // band and not counted when measuring what is overweight.
  const sellable = sellableSleeves(portfolio);
  if (sellable.length === 0) return null;
  const resolved = resolveSleeveWeights(sleeveWeights, sellable);
  // The type argument is load-bearing. `resolveSleeveWeights` is untyped, so `resolved` is `any`
  // and the callback's return type is discarded — leaving `Map<unknown, unknown>`, which makes
  // every arithmetic use of a weight below a compile error.
  const weightByKey = new Map<string, number>(resolved.map((sleeve) => [sleeveKey(sleeve), sleeve.weight]));
  const total = sellable.reduce((sum, row) => sum + (weightByKey.get(row.key) ?? 0), 0);

  const emit = (key, weight) =>
    onChange(
      sellable.map((row) => ({
        ...row.sleeve,
        weight: row.key === key ? weight : (weightByKey.get(row.key) ?? 0),
      }))
    );

  return (
    <div className={compact ? "" : "mt-3"}>
      {label && <div className="augur-field-label mb-2">{label}</div>}
      <ul className="overflow-hidden rounded border border-slate-200 divide-y divide-slate-200 dark:border-slate-700 dark:divide-slate-700">
        {sellable.map((row) => {
          const weight = weightByKey.get(row.key) ?? 0;
          const share = total > 0 ? Math.round((100 * weight) / total) : 0;
          return (
            <li
              key={row.key}
              className={`flex items-center gap-2 px-2 py-1 ${weight > 0 ? "" : "bg-slate-50 opacity-80 dark:bg-slate-900/40"}`}
            >
              <span className="flex-1 text-sm font-semibold augur-strong">{row.label}</span>
              <input
                type="number"
                min={0}
                step={1}
                value={weight}
                aria-label={`Target weight for ${row.label}`}
                onChange={(event) => emit(row.key, Math.max(0, Math.trunc(Number(event.target.value) || 0)))}
                className="augur-input w-20 text-right augur-tabular"
              />
              <span className="w-12 text-right text-xs augur-muted augur-tabular">
                {weight > 0 ? `${share}%` : "—"}
              </span>
            </li>
          );
        })}
      </ul>
      {total === 0 && (
        <div className="mt-1 text-xs augur-muted">
          Every weight is zero, so nothing is ever sold to refill cash — an unaffordable month is ruin.
        </div>
      )}
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

function PortfolioPositionRow({ position, currency }) {
  return (
    <tr className="border-t border-slate-100 dark:border-slate-800">
      <td className="py-1 pl-3">
        <div className="truncate font-semibold augur-strong">{position.label || position.symbol}</div>
        <div className="truncate text-xs augur-muted">
          {position.symbol} · {position.accountLabel || position.accountId}
        </div>
      </td>
      <td className="py-1 text-right augur-tabular">{fmtQuantity(position.quantity)}</td>
      <td className="py-1 text-right augur-tabular">{fmtQuanta(position.unitValueQuanta, currency)}</td>
      <td className="py-1 text-right augur-tabular">{fmtQuanta(position.totalCostBasisQuanta, currency)}</td>
      <td className="py-1 text-right font-semibold augur-tabular">
        {fmtQuanta(position.currentValueQuanta, currency)}
      </td>
    </tr>
  );
}

// Money and basis by cohort, not units at a price: the columns a holding fills with a unit count
// and a unit value carry the cohort count instead.
function PortfolioTlhRow({ portfolio, currency }) {
  const cohorts = portfolio.cohorts.length;
  return (
    <tr className="border-t border-slate-100 dark:border-slate-800">
      <td className="py-1 pl-3">
        <div className="truncate font-semibold augur-strong">{portfolio.label}</div>
        <div className="truncate text-xs augur-muted">
          {portfolio.asset.symbol} · {portfolio.accountLabel || portfolio.accountId}
        </div>
      </td>
      <td colSpan={2} className="py-1 text-right augur-tabular augur-muted">
        {cohorts} {cohorts === 1 ? "cohort" : "cohorts"}
      </td>
      <td className="py-1 text-right augur-tabular">{fmtQuanta(portfolio.totalCostBasisQuanta, currency)}</td>
      <td className="py-1 text-right font-semibold augur-tabular">
        {fmtQuanta(portfolio.currentValueQuanta, currency)}
      </td>
    </tr>
  );
}

function PortfolioBondRow({ bond, currency }) {
  const periodsPerYear = 12 / bond.couponPeriodMonths;
  return (
    <tr className="border-t border-slate-100 dark:border-slate-800">
      <td className="py-1 pl-3">
        <div className="augur-strong">{bond.label ?? bond.bondId}</div>
        <div className="text-xs augur-muted">
          {bond.inflationIndexed ? "TIPS \u00b7 " : ""}
          {bond.issuerJurisdictionId ?? "corporate"}
        </div>
      </td>
      <td className="py-1 text-right augur-tabular">{(100 * bond.annualCouponRate).toFixed(2)}%</td>
      <td className="py-1 text-right augur-tabular augur-muted">{periodsPerYear}x/yr</td>
      <td className="py-1 text-right augur-tabular augur-muted">{bond.monthsToMaturityAtStart} mo</td>
      {/* Face, not a mark: a held-to-maturity bond is never priced, so this column is what it
          redeems for rather than what it would fetch today. */}
      <td className="py-1 text-right augur-tabular">{fmtQuanta(bond.faceValueQuanta, currency)}</td>
    </tr>
  );
}

function PortfolioSubtotalRow({ label, valueQuanta, dataKey, currency }) {
  return (
    <tr className="border-t border-slate-100 dark:border-slate-800">
      <td colSpan={4} className="py-1 pl-3 text-xs augur-muted">
        {label}
      </td>
      <td
        className="py-1 text-right text-xs font-semibold augur-tabular augur-muted"
        data-product-portfolio-subtotal={dataKey}
      >
        {fmtQuanta(valueQuanta, currency)}
      </td>
    </tr>
  );
}

export function ProductPortfolioPanel({ portfolio, error }) {
  const [collapsed, setCollapsed] = useState(true);
  const holdings = portfolio?.holdings ?? [];
  const currency = { currencyCode: portfolio?.currencyCode, currencyQuantum: portfolio?.currencyQuantum };
  const publicHoldings = holdings.filter((position) => !isPrivateSecurityPosition(position));
  const privateSecurityHoldings = holdings.filter(isPrivateSecurityPosition);
  const publicHoldingsValueQuanta = sumCurrentValueQuanta(publicHoldings);
  const privateSecurityValueQuanta = sumCurrentValueQuanta(privateSecurityHoldings);
  // Managed portfolios are not holdings: `totalHoldingsValueQuanta` leaves them out.
  const tlhPortfolios = portfolio?.tlhPortfolios ?? [];
  const tlhValueQuanta = sumCurrentValueQuanta(tlhPortfolios);
  const cashQuanta = portfolio?.cashQuanta ?? "0";
  const bonds = portfolio?.bonds ?? [];
  const bondFaceQuanta = portfolio?.totalBondFaceValueQuanta ?? "0";
  const totalQuanta = currencyQuantaAdd(
    cashQuanta,
    portfolio?.totalHoldingsValueQuanta ?? "0",
    tlhValueQuanta,
    bondFaceQuanta
  );
  const hasAnything =
    currencyQuantaIsPositive(cashQuanta) || holdings.length > 0 || tlhPortfolios.length > 0 || bonds.length > 0;
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
            {fmtQuanta(totalQuanta, currency)}
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
              <td className="py-1 text-right font-semibold augur-tabular">{fmtQuanta(cashQuanta, currency)}</td>
            </tr>
            {publicHoldings.length > 0 && (
              <>
                <PortfolioGroupHeaderRow label="Public securities" />
                {publicHoldings.map((position) => (
                  <PortfolioPositionRow key={position.positionId} position={position} currency={currency} />
                ))}
                <PortfolioSubtotalRow
                  label="Public subtotal"
                  valueQuanta={publicHoldingsValueQuanta}
                  dataKey="public-securities"
                  currency={currency}
                />
              </>
            )}
            {privateSecurityHoldings.length > 0 && (
              <>
                <PortfolioGroupHeaderRow label="Private securities" />
                {privateSecurityHoldings.map((position) => (
                  <PortfolioPositionRow key={position.positionId} position={position} currency={currency} />
                ))}
                <PortfolioSubtotalRow
                  label="Private subtotal"
                  valueQuanta={privateSecurityValueQuanta}
                  dataKey="private-securities"
                  currency={currency}
                />
              </>
            )}
            {tlhPortfolios.length > 0 && (
              <>
                <PortfolioGroupHeaderRow label="Managed (TLH)" />
                {tlhPortfolios.map((managed) => (
                  <PortfolioTlhRow key={managed.portfolioId} portfolio={managed} currency={currency} />
                ))}
                <PortfolioSubtotalRow
                  label="Managed subtotal"
                  valueQuanta={tlhValueQuanta}
                  dataKey="tlh-portfolios"
                  currency={currency}
                />
              </>
            )}
            {bonds.length > 0 && (
              <>
                <PortfolioGroupHeaderRow label="Bonds (held to maturity)" />
                {bonds.map((bond) => (
                  <PortfolioBondRow key={bond.bondId} bond={bond} currency={currency} />
                ))}
                <PortfolioSubtotalRow
                  label="Bond face subtotal"
                  valueQuanta={bondFaceQuanta}
                  dataKey="bonds"
                  currency={currency}
                />
              </>
            )}
            {holdings.length === 0 && tlhPortfolios.length === 0 && bonds.length === 0 && (
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
                <td className="py-1 text-right font-semibold augur-tabular">{fmtQuanta(totalQuanta, currency)}</td>
              </tr>
            </tfoot>
          )}
        </table>
      ) : null}
    </div>
  );
}

function sumCurrentValueQuanta(positions) {
  return currencyQuantaAdd(...positions.map((position) => position.currentValueQuanta ?? "0"));
}
