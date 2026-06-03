import React, { useEffect, useMemo, useState } from "react";
import { NativeSelect } from "@mantine/core";

import { fetchBudgetSnapshot, fetchBudgetTransactions } from "./client.ts";
import { buildSummaryCsv, buildTransactionsCsv } from "./budget_csv.ts";
import { parseAdjustments, adjustmentsToParams, effectiveSignedAvg, computeTotals } from "./budget_adjustments.ts";
import { fmtUsd, fmtNumber } from "./lib/format.ts";
import { toastFetchError } from "./lib/toast.ts";

// Only expense buckets stack into the "monthly spend" outflow chart. Inflow / transfer /
// income render in their own panels (or, for inflow, alongside their family's expenses).
const STACKABLE_SPEND_KIND = "expense";

// Window selector options. Trailing-N options send a `trailing_months` window spec;
// "max" sends a `since_coverage_start` window, asking the server for the full available
// gap-free history (anchored at the deployment's `coverage_starts` config). The latter
// only appears in the dropdown once we know `coverage_starts` from a snapshot response.
const TRAILING_CHOICES = [
  { value: "trailing:3", label: "Trailing 3 months" },
  { value: "trailing:6", label: "Trailing 6 months" },
  { value: "trailing:12", label: "Trailing 12 months" },
  { value: "trailing:24", label: "Trailing 24 months" },
];
const MAX_CHOICE_VALUE = "max";

function windowSpecFromChoice(value) {
  if (value === MAX_CHOICE_VALUE) return { kind: "since_coverage_start" };
  const months = Number(value.split(":")[1]);
  return { kind: "trailing_months", months };
}

function maxChoiceLabel(coverageStarts) {
  return `Max (since ${coverageStarts})`;
}

// `windowChoice` values are "trailing:N" / "max"; the colon is awkward in a download filename.
function windowSlug(windowChoice) {
  return windowChoice.replace(":", "-");
}

function downloadCsv(filename, content) {
  const blob = new Blob([content], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  // Defer cleanup: revoking the blob URL synchronously after click() can cancel the download in
  // some browsers before it starts. Release on the next macrotask, once the download has begun.
  setTimeout(() => {
    anchor.remove();
    URL.revokeObjectURL(url);
  }, 0);
}

const EXPORT_BUTTON_CLASS = "augur-icon-button gap-1.5 px-2.5 py-1.5 text-xs font-medium";

// Mirror the product/calibration shell's URL-state pattern: rewrite only our two budget params
// (preserving everything else) so a hidden-rent / overridden view round-trips through the URL.
function writeAdjustmentsToSearch(adjustments) {
  const params = new URLSearchParams(window.location.search);
  const { bhide, bset } = adjustmentsToParams(adjustments);
  if (bhide) params.set("bhide", bhide);
  else params.delete("bhide");
  if (bset) params.set("bset", bset);
  else params.delete("bset");
  const search = params.toString();
  const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
  if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState(null, "", newUrl);
  }
}

const UNGROUPED_FAMILY = "_ungrouped";

function fmtMonth(iso) {
  // iso: "YYYY-MM-DD" — render as "Jul '25" so 12 months fit on a row of pills.
  const [yearStr, monthStr] = iso.split("-");
  const month = Number(monthStr);
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${names[month - 1]} '${yearStr.slice(2)}`;
}

function fmtFamily(family) {
  if (family === UNGROUPED_FAMILY) return "Ungrouped";
  return family.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function KindBadge({ kind }) {
  const tone =
    {
      expense: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
      inflow: "bg-sky-50 text-sky-800 dark:bg-sky-950/40 dark:text-sky-300",
      transfer: "bg-slate-50 text-slate-500 italic dark:bg-slate-900 dark:text-slate-500",
      income: "bg-emerald-50 text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300",
    }[kind] || "bg-slate-100 text-slate-700";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${tone}`}
    >
      {kind}
    </span>
  );
}

function Sparkline({ amounts, width = 90, height = 24 }) {
  if (!amounts.length) return null;
  const max = Math.max(...amounts.map((value) => Math.abs(value)));
  if (max === 0) return <svg width={width} height={height} aria-hidden="true" />;
  const step = width / Math.max(amounts.length - 1, 1);
  const points = amounts
    .map((value, index) => {
      const x = index * step;
      const y = height / 2 - (value / max) * (height / 2 - 1);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <line x1="0" x2={width} y1={height / 2} y2={height / 2} stroke="currentColor" strokeOpacity="0.15" />
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

// "Nice" tick values: 1/2/5 × 10^n bracketing the data so axis labels read $5k/$10k/$15k.
function niceTicks(max, target = 5) {
  if (max <= 0) return { ticks: [0], ceiling: 1 };
  const rawStep = max / target;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const candidates = [1, 2, 5, 10].map((m) => m * magnitude);
  const step = candidates.find((c) => c >= rawStep) ?? candidates[candidates.length - 1];
  const ceiling = Math.ceil(max / step) * step;
  const ticks = [];
  for (let t = 0; t <= ceiling + 1e-9; t += step) ticks.push(t);
  return { ticks, ceiling };
}

function StackedMonthlyChart({ months, bucketSeries }) {
  const spendSeries = bucketSeries.filter((series) => series.kind === STACKABLE_SPEND_KIND);
  if (!months.length || !spendSeries.length) {
    return <div className="px-4 py-6 text-sm augur-muted">No spend data in window.</div>;
  }
  const monthlyTotals = months.map((_, i) =>
    spendSeries.reduce((acc, series) => acc + Math.max(series.monthlyAmounts[i], 0), 0)
  );
  const max = Math.max(...monthlyTotals);
  const { ticks, ceiling } = niceTicks(max);
  const palette = [
    "#0ea5e9",
    "#f97316",
    "#10b981",
    "#a855f7",
    "#ef4444",
    "#eab308",
    "#06b6d4",
    "#ec4899",
    "#84cc16",
    "#6366f1",
    "#14b8a6",
    "#f43f5e",
    "#22d3ee",
    "#fb923c",
    "#a3e635",
    "#c084fc",
    "#fbbf24",
    "#94a3b8",
  ];
  const yAxisWidthPx = 56;
  const innerHeightPx = 220;
  return (
    <div className="overflow-hidden">
      <div className="flex" style={{ height: innerHeightPx }}>
        <div
          className="flex flex-col justify-between text-right text-[10px] augur-muted"
          style={{ width: yAxisWidthPx, paddingRight: 6 }}
        >
          {ticks
            .slice()
            .reverse()
            .map((value) => (
              <span key={value} className="leading-none augur-tabular">
                {fmtUsd(value)}
              </span>
            ))}
        </div>
        <div className="relative flex-1">
          <div className="absolute inset-0 flex flex-col justify-between">
            {ticks
              .slice()
              .reverse()
              .map((value) => (
                <div key={value} className="border-t border-slate-200 dark:border-slate-700/60" style={{ height: 0 }} />
              ))}
          </div>
          <svg
            viewBox={`0 0 100 ${ceiling}`}
            preserveAspectRatio="none"
            width="100%"
            height={innerHeightPx}
            role="img"
            aria-label="Monthly stacked spend"
          >
            {months.map((monthIso, monthIdx) => {
              const barWidth = 100 / months.length;
              let cursor = ceiling;
              const segments = spendSeries.map((series, seriesIdx) => {
                const value = Math.max(series.monthlyAmounts[monthIdx], 0);
                if (value <= 0) return null;
                const y = cursor - value;
                cursor = y;
                return (
                  <rect
                    key={series.bucketId}
                    x={monthIdx * barWidth + barWidth * 0.1}
                    y={y}
                    width={barWidth * 0.8}
                    height={value}
                    fill={palette[seriesIdx % palette.length]}
                    opacity="0.9"
                  >
                    <title>{`${series.label} · ${fmtMonth(monthIso)}: ${fmtUsd(value)}`}</title>
                  </rect>
                );
              });
              return <g key={monthIso}>{segments}</g>;
            })}
          </svg>
        </div>
      </div>
      <div className="flex text-[10px] augur-muted" style={{ paddingLeft: yAxisWidthPx }}>
        {months.map((monthIso) => (
          <span key={monthIso} className="flex-1 text-center">
            {fmtMonth(monthIso)}
          </span>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[11px]" style={{ paddingLeft: yAxisWidthPx }}>
        {spendSeries.map((series, idx) => (
          <span key={series.bucketId} className="inline-flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-sm" style={{ background: palette[idx % palette.length] }} />
            {series.label}
          </span>
        ))}
      </div>
    </div>
  );
}

// Inline editor for a per-bucket override. Prefills with the bucket's current planned magnitude
// (the override if set, else the rounded historical average) so "Set" is a one-keystroke tweak.
function OverrideEditor({ entry, onAdjust, onClose }) {
  const [draft, setDraft] = useState(() =>
    entry.overridden ? String(entry.adjustment.monthly) : String(Math.round(Math.abs(entry.windowAvg)))
  );
  const commit = () => {
    const value = Number(draft);
    if (draft.trim() !== "" && Number.isFinite(value) && value >= 0) {
      onAdjust(entry.bucketId, { kind: "override", monthly: value });
    }
    onClose();
  };
  return (
    <div className="flex items-center justify-end gap-1" onClick={(event) => event.stopPropagation()}>
      <input
        type="number"
        min={0}
        autoFocus
        aria-label={`Planned monthly amount for ${entry.label}`}
        className="augur-input w-24 text-right augur-tabular"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") commit();
          else if (event.key === "Escape") onClose();
        }}
      />
      <button type="button" className="text-[11px] augur-link" onClick={commit}>
        Save
      </button>
      <button type="button" className="text-[11px] augur-link" onClick={onClose}>
        Cancel
      </button>
    </div>
  );
}

function BucketRow({ entry, onSelect, selected, onAdjust }) {
  const [editing, setEditing] = useState(false);
  const stop = (event) => event.stopPropagation();
  // Effective figure shown in the $/mo cell: the override when set, otherwise the historical
  // average. Overridden rows render in accent + carry a "was …" reset line; hidden rows are
  // dimmed and struck through (they're dropped from totals/chart but stay visible to bring back).
  const valueClass = entry.overridden
    ? "font-semibold augur-accent-text"
    : entry.hidden
      ? "line-through opacity-70"
      : "";
  return (
    <tr
      className={`cursor-pointer transition-colors ${selected ? "bg-sky-50 dark:bg-sky-950/30" : "hover:bg-slate-50 dark:hover:bg-slate-900"} ${entry.hidden ? "opacity-50" : ""}`}
      onClick={() => onSelect(entry.bucketId)}
      data-budget-bucket-row={entry.bucketId}
    >
      <th className="px-3 py-2 text-left text-sm font-semibold text-slate-700 dark:text-slate-200">
        {entry.label}
        <span className="ml-2">
          <KindBadge kind={entry.kind} />
        </span>
      </th>
      <td className="px-3 py-2 text-right text-sm augur-tabular" onClick={stop}>
        {editing ? (
          <OverrideEditor entry={entry} onAdjust={onAdjust} onClose={() => setEditing(false)} />
        ) : (
          <>
            <span className={valueClass}>{fmtUsd(entry.effectiveAvg)}</span>
            {entry.overridden && (
              <div className="text-[10px] augur-muted">
                was {fmtUsd(entry.windowAvg)} ·{" "}
                <button type="button" className="augur-link" onClick={() => onAdjust(entry.bucketId, null)}>
                  reset
                </button>
              </div>
            )}
          </>
        )}
      </td>
      <td className="px-3 py-2 text-right text-sm augur-tabular augur-muted">{fmtNumber(entry.transactionCount)}</td>
      <td className="px-3 py-2 text-right">
        <Sparkline amounts={entry.monthlyAmounts} />
      </td>
      <td
        className="px-3 py-2 text-right whitespace-nowrap text-[11px]"
        onClick={stop}
        data-budget-bucket-plan={entry.bucketId}
      >
        {!entry.hidden && !editing && (
          <button type="button" className="augur-link" onClick={() => setEditing(true)}>
            {entry.overridden ? "Edit" : "Set"}
          </button>
        )}
        <button
          type="button"
          className="ml-2 augur-link"
          onClick={() => {
            // Close the override editor first so a hidden row never keeps a stray open input.
            setEditing(false);
            onAdjust(entry.bucketId, entry.hidden ? null : { kind: "hidden" });
          }}
        >
          {entry.hidden ? "Show" : "Hide"}
        </button>
      </td>
    </tr>
  );
}

function HeadlineCards({ totals, historical, windowMonths, adjustmentsActive }) {
  // Always show "Monthly burn" (net of inflows + income). Show "Inflows" / "Income"
  // cards only when nonzero so we don't render a wall of $0 cards on simple flows.
  // When inflows == income == 0, "spend" and "netBurn" are identical -- skip the
  // duplicate card. With planning adjustments active the figures are the *adjusted*
  // totals (hidden buckets dropped, overrides applied); the burn card then anchors
  // them against the unadjusted history so the tweak's effect is legible.
  const subtitle = adjustmentsActive ? "planned $/mo" : `${windowMonths}-month average`;
  const cards = [];
  const hasOffsets = totals.inflow > 0 || totals.income > 0;
  if (hasOffsets) {
    cards.push({ label: "Gross spend", value: totals.spend, note: "All expense buckets, before inflows." });
  }
  if (totals.inflow > 0) {
    cards.push({
      label: "Inflows",
      value: totals.inflow,
      note: "Refunds, insurance, etc.",
      tone: "text-sky-700 dark:text-sky-400",
    });
  }
  if (totals.income > 0) {
    cards.push({
      label: "Income",
      value: totals.income,
      tone: "text-emerald-700 dark:text-emerald-400",
    });
  }
  cards.push({
    label: adjustmentsActive ? "Planned monthly burn" : hasOffsets ? "Net monthly burn" : "Monthly burn",
    value: totals.netBurn,
    note: adjustmentsActive
      ? `Historical: ${fmtUsd(historical.netBurn)}/mo. Positive = drawing down savings.`
      : "Positive = drawing down savings.",
  });
  const cols = cards.length === 4 ? "sm:grid-cols-4" : cards.length === 3 ? "sm:grid-cols-3" : "sm:grid-cols-2";
  return (
    <section className={`grid gap-3 ${cols}`}>
      {cards.map((card) => (
        <div key={card.label} className="augur-card p-4">
          <div className="augur-eyebrow">
            {card.label} <span className="font-normal opacity-60">({subtitle})</span>
          </div>
          <div className={`mt-2 text-2xl font-semibold augur-tabular ${card.tone || ""}`}>{fmtUsd(card.value)}</div>
          {card.note && <div className="text-[11px] augur-muted">{card.note}</div>}
        </div>
      ))}
    </section>
  );
}

// A running summary of the active planning adjustments with a one-click reset. Renders nothing
// until something is hidden or overridden, so the default view stays uncluttered.
function AdjustmentsBar({ rows, onReset }) {
  const hidden = rows.filter((row) => row.hidden);
  const overridden = rows.filter((row) => row.overridden);
  if (!hidden.length && !overridden.length) return null;
  return (
    <section
      className="augur-note-info flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg p-3 text-xs"
      data-budget-adjustments=""
    >
      <span className="augur-eyebrow text-[10px]">Planning adjustments</span>
      {hidden.length > 0 && (
        <span>
          <span className="font-semibold">Hidden:</span> {hidden.map((row) => row.label).join(", ")}
        </span>
      )}
      {overridden.length > 0 && (
        <span>
          <span className="font-semibold">Set:</span>{" "}
          {overridden.map((row) => `${row.label} → ${fmtUsd(Math.abs(row.effectiveAvg))}/mo`).join(", ")}
        </span>
      )}
      <button type="button" className="augur-link ml-auto" onClick={onReset}>
        Reset all
      </button>
    </section>
  );
}

function FamilyPanel({ family, rows, onSelectBucket, selectedBucketId, onAdjust, windowMonths }) {
  // Roll up the family-level totals from each row's effective average (override when set, else
  // historical) so the summary lines up with the row figures. Hidden buckets are excluded -- the
  // panel total tracks the same adjusted view the headline cards show.
  let grossOut = 0;
  let grossIn = 0;
  for (const row of rows) {
    if (row.hidden) continue;
    if (row.kind === "expense") grossOut += row.effectiveAvg;
    else if (row.kind === "inflow") grossIn += Math.abs(row.effectiveAvg);
    else if (row.kind === "income") grossIn += Math.abs(row.effectiveAvg);
  }
  // `transfer` buckets are direction-agnostic -- their sign is real (negative = net inflow).
  // Add their signed average to the relevant side so net stays honest.
  for (const row of rows) {
    if (row.hidden || row.kind !== "transfer") continue;
    if (row.effectiveAvg >= 0) grossOut += row.effectiveAvg;
    else grossIn += -row.effectiveAvg;
  }
  const net = grossOut - grossIn;
  // Hide the In/Net columns when the family is purely expense-side -- no signal there.
  const hasInflowSide = grossIn > 0;
  const sortedRows = rows.slice().sort((l, r) => Math.abs(r.effectiveAvg) - Math.abs(l.effectiveAvg));
  return (
    <section className="augur-panel overflow-hidden" data-budget-family={family}>
      <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <div className="flex items-baseline justify-between gap-3 flex-wrap">
          <div>
            <div className="augur-eyebrow">{fmtFamily(family)}</div>
            <div className="mt-1 text-[11px] augur-muted">
              {rows.length} bucket{rows.length === 1 ? "" : "s"} · {windowMonths}-month average shown for each side.
            </div>
          </div>
          <div className="flex gap-4 text-right">
            <div>
              <div className="augur-eyebrow text-[10px]">{hasInflowSide ? "Out" : "Spend"}</div>
              <div className="augur-tabular text-sm font-semibold">{fmtUsd(grossOut)}</div>
            </div>
            {hasInflowSide && (
              <>
                <div>
                  <div className="augur-eyebrow text-[10px]">In</div>
                  <div className="augur-tabular text-sm font-semibold text-emerald-700 dark:text-emerald-400">
                    −{fmtUsd(grossIn)}
                  </div>
                </div>
                <div>
                  <div className="augur-eyebrow text-[10px]">Net</div>
                  <div className="augur-tabular text-sm font-semibold">{fmtUsd(net)}</div>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead>
            <tr className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
              <th className="px-3 py-2 font-semibold">Bucket</th>
              <th className="px-3 py-2 text-right font-semibold">{windowMonths}-month avg $/mo</th>
              <th className="px-3 py-2 text-right font-semibold">Tx count</th>
              <th className="px-3 py-2 text-right font-semibold">Trend</th>
              <th className="px-3 py-2 text-right font-semibold">Plan</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {sortedRows.map((row) => (
              <BucketRow
                key={row.bucketId}
                entry={row}
                onSelect={onSelectBucket}
                selected={selectedBucketId === row.bucketId}
                onAdjust={onAdjust}
              />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function LumpyPanel({ items, bucketsById }) {
  if (!items.length) {
    return <div className="px-4 py-6 text-sm augur-muted">No lumpy spends in window above threshold.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full border-t border-slate-200 text-sm dark:border-slate-700">
        <thead>
          <tr className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
            <th className="px-3 py-2 font-semibold">Date</th>
            <th className="px-3 py-2 font-semibold">Merchant</th>
            <th className="px-3 py-2 font-semibold">Bucket</th>
            <th className="px-3 py-2 text-right font-semibold">Amount</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
          {items.map((item) => (
            <tr key={item.transactionId} data-budget-lumpy-row={item.transactionId}>
              <td className="px-3 py-2 augur-tabular text-xs">{item.date}</td>
              <th className="px-3 py-2 text-left font-medium text-slate-700 dark:text-slate-200">
                {item.merchantName || item.name}
              </th>
              <td className="px-3 py-2 text-xs">{bucketsById.get(item.bucketId)?.label || item.bucketId}</td>
              <td className="px-3 py-2 text-right augur-tabular">{fmtUsd(item.amount)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TransactionsPanel({ transactions }) {
  if (!transactions) return <div className="px-4 py-6 text-sm augur-muted">Loading…</div>;
  if (!transactions.length) {
    return <div className="px-4 py-6 text-sm augur-muted">No transactions in this bucket.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
            <th className="px-3 py-2 font-semibold">Date</th>
            <th className="px-3 py-2 font-semibold">Merchant / Descriptor</th>
            <th className="px-3 py-2 font-semibold">Plaid PFC</th>
            <th className="px-3 py-2 font-semibold">Account</th>
            <th className="px-3 py-2 text-right font-semibold">Amount</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
          {transactions.map((tx) => (
            <tr key={tx.transactionId}>
              <td className="px-3 py-2 augur-tabular text-xs">{tx.date}</td>
              <th className="px-3 py-2 text-left text-slate-700 dark:text-slate-200">
                <div className="font-medium">{tx.merchantName || tx.name}</div>
                {tx.merchantName && tx.merchantName !== tx.name && (
                  <div className="text-[10px] augur-muted truncate max-w-md">{tx.name}</div>
                )}
              </th>
              <td className="px-3 py-2 text-[11px] augur-muted">{tx.pfcDetailed || tx.pfcPrimary || "—"}</td>
              <td className="px-3 py-2 text-xs">{tx.accountName}</td>
              <td className="px-3 py-2 text-right augur-tabular">{fmtUsd(tx.amount)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function BudgetWorkspace() {
  // Default to full coverage history. When `coverage_starts` is unset on the deployment
  // the server 400s; switch the default to a trailing window if that's ever a concern.
  const [windowChoice, setWindowChoice] = useState(MAX_CHOICE_VALUE);
  const [snapshot, setSnapshot] = useState(null);
  const [snapshotError, setSnapshotError] = useState(null);
  const [selectedBucketId, setSelectedBucketId] = useState(null);
  const [bucketTx, setBucketTx] = useState(null);
  const [bucketTxError, setBucketTxError] = useState(null);
  // Planning adjustments (hidden buckets / per-bucket overrides) initialized from the URL so a
  // shared link reopens the same tweaked view.
  const [adjustments, setAdjustments] = useState(() => parseAdjustments(window.location.search));

  // Mirror adjustments to the URL whenever they change (replaceState; preserves other params). On
  // mount this re-emits the params just parsed in, so the no-change guard makes it a no-op.
  useEffect(() => {
    writeAdjustmentsToSearch(adjustments);
  }, [adjustments]);

  const windowSpec = useMemo(() => windowSpecFromChoice(windowChoice), [windowChoice]);

  useEffect(() => {
    const controller = new AbortController();
    setSnapshot(null);
    setSnapshotError(null);
    fetchBudgetSnapshot({ window: windowSpec }, { signal: controller.signal })
      .then((payload) => setSnapshot(payload))
      .catch((error) => {
        if (error?.name === "AbortError") return;
        setSnapshotError(error?.message || String(error));
        toastFetchError("budget-snapshot", "Budget snapshot failed", error);
      });
    return () => controller.abort();
  }, [windowSpec]);

  useEffect(() => {
    if (!selectedBucketId) return undefined;
    const controller = new AbortController();
    setBucketTx(null);
    setBucketTxError(null);
    fetchBudgetTransactions({ bucketId: selectedBucketId, window: windowSpec }, { signal: controller.signal })
      .then((payload) => setBucketTx(payload.transactions))
      .catch((error) => {
        if (error?.name === "AbortError") return;
        setBucketTxError(error?.message || String(error));
        toastFetchError("budget-transactions", "Transactions failed", error);
      });
    return () => controller.abort();
  }, [selectedBucketId, windowSpec]);

  const bucketsById = useMemo(() => {
    const out = new Map();
    if (snapshot) for (const bucket of snapshot.buckets) out.set(bucket.id, bucket);
    return out;
  }, [snapshot]);

  const rows = useMemo(() => {
    if (!snapshot) return [];
    return snapshot.monthlyByBucket.map((series) => {
      const bucket = bucketsById.get(series.bucketId);
      return {
        bucketId: series.bucketId,
        label: bucket?.label ?? series.bucketId,
        kind: bucket?.kind ?? "expense",
        family: bucket?.family ?? null,
        monthlyAmounts: series.monthlyAmounts,
        // Day-normalized $/mo from the backend (signed window total / days covered × avg
        // days/mo). Not sum/months, which counted a partial current month as a full one.
        windowAvg: series.windowMonthlyAvg,
        transactionCount: series.transactionCount,
      };
    });
  }, [snapshot, bucketsById]);

  // Overlay the planning adjustments onto each row: `hidden`/`overridden` flags + the `effectiveAvg`
  // (override re-signed into the bucket's direction, else the historical window average) the
  // headline, family rollups, and per-row display all read from.
  const adjustedRows = useMemo(
    () =>
      rows.map((row) => {
        const adjustment = adjustments.get(row.bucketId);
        return {
          ...row,
          adjustment,
          hidden: adjustment?.kind === "hidden",
          overridden: adjustment?.kind === "override",
          effectiveAvg: effectiveSignedAvg(row.kind, row.windowAvg, adjustment),
        };
      }),
    [rows, adjustments]
  );

  // Hidden buckets drop out of the stacked chart entirely (the "natural spending without rent" view).
  const visibleRows = useMemo(() => adjustedRows.filter((row) => !row.hidden), [adjustedRows]);

  const rowsByFamily = useMemo(() => {
    // Group rows by family. Buckets without a declared family share a synthetic
    // "_ungrouped" key so they still render -- just below the named families.
    const grouped = new Map();
    for (const row of adjustedRows) {
      const key = row.family ?? UNGROUPED_FAMILY;
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(row);
    }
    // Stable ordering: named families first, alphabetically; ungrouped last.
    const families = Array.from(grouped.keys()).sort((l, r) => {
      if (l === UNGROUPED_FAMILY) return 1;
      if (r === UNGROUPED_FAMILY) return -1;
      return l.localeCompare(r);
    });
    return families.map((family) => ({ family, rows: grouped.get(family) }));
  }, [adjustedRows]);

  // Adjusted totals power the headline cards; the unadjusted version anchors the "Historical: …"
  // note so the effect of hiding/overriding is legible. With no adjustments the two coincide.
  const totals = useMemo(() => (snapshot ? computeTotals(rows, adjustments) : null), [snapshot, rows, adjustments]);
  const historicalTotals = useMemo(() => (snapshot ? computeTotals(rows, new Map()) : null), [snapshot, rows]);

  const exportSummary = () => {
    if (!snapshot) return;
    // Pass the adjusted rows so the export carries the planning overlay (Planned $/mo + Hidden
    // columns) alongside the historical actuals when any bucket is hidden/overridden.
    downloadCsv(`budget-summary-${windowSlug(windowChoice)}.csv`, buildSummaryCsv(snapshot.months, adjustedRows));
  };

  const exportTransactions = () => {
    if (!selectedBucketId || !bucketTx) return;
    downloadCsv(`budget-transactions-${selectedBucketId}.csv`, buildTransactionsCsv(bucketTx));
  };

  // null clears the bucket back to "actual"; otherwise set hidden / override. One adjustment per
  // bucket (hide and override are mutually exclusive). Functional update so rapid successive
  // changes each compose on the latest state; the effect above mirrors the result to the URL.
  const applyAdjustment = (bucketId, adjustment) => {
    setAdjustments((previous) => {
      const next = new Map(previous);
      if (adjustment) next.set(bucketId, adjustment);
      else next.delete(bucketId);
      return next;
    });
  };

  const resetAdjustments = () => setAdjustments(new Map());

  return (
    <main className="px-4 py-6 sm:px-6 lg:px-8 space-y-5">
      <section className="augur-panel p-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <div className="augur-eyebrow">Budget planner</div>
            <div className="mt-1 text-xs augur-muted">
              Live spend from Plaid mirror. Buckets are grouped by family (medical, etc.); the family panel shows gross
              expense, gross inflow, and net separately rather than auto-netting per bucket -- reimbursement timing is
              too lumpy and (for some providers) actually arrives before the matching charge.
            </div>
          </div>
          <div className="flex items-center gap-2">
            {snapshot && (
              <button
                type="button"
                className={EXPORT_BUTTON_CLASS}
                onClick={exportSummary}
                data-budget-export="summary"
              >
                ↓ Export CSV
              </button>
            )}
            <NativeSelect
              aria-label="Window"
              data={
                snapshot?.coverageStarts
                  ? [...TRAILING_CHOICES, { value: MAX_CHOICE_VALUE, label: maxChoiceLabel(snapshot.coverageStarts) }]
                  : TRAILING_CHOICES
              }
              value={windowChoice}
              onChange={(event) => setWindowChoice(event.target.value)}
              classNames={{ input: "augur-tabular min-w-[12rem]" }}
            />
          </div>
        </div>
        {snapshot?.coverageStarts && (
          <div className="mt-2 text-[11px] augur-muted">
            Spend before <span className="font-semibold">{snapshot.coverageStarts}</span> is partial -- one or more
            linked accounts didn&apos;t return earlier transactions. The selected window is clamped to that date for
            consistency.
          </div>
        )}
      </section>

      {snapshotError && <div className="augur-note-danger p-4 text-sm">Budget snapshot failed: {snapshotError}</div>}

      {!snapshot && !snapshotError && (
        <div className="augur-panel p-8 text-center text-sm augur-muted">Loading budget snapshot…</div>
      )}

      {snapshot && (
        <>
          <HeadlineCards
            totals={totals}
            historical={historicalTotals}
            windowMonths={snapshot.months.length}
            adjustmentsActive={adjustments.size > 0}
          />

          <AdjustmentsBar rows={adjustedRows} onReset={resetAdjustments} />

          <section className="augur-panel overflow-hidden">
            <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
              <div className="augur-eyebrow">Monthly spend by bucket</div>
              <div className="mt-1 text-[11px] augur-muted">
                Stacked expense outflows only. Inflows are shown per-family below, not netted here. Hidden buckets are
                excluded.
              </div>
            </div>
            <div className="p-4">
              <StackedMonthlyChart months={snapshot.months} bucketSeries={visibleRows} />
            </div>
          </section>

          {rowsByFamily.map(({ family, rows: familyRows }) => (
            <FamilyPanel
              key={family}
              family={family}
              rows={familyRows}
              onSelectBucket={setSelectedBucketId}
              selectedBucketId={selectedBucketId}
              onAdjust={applyAdjustment}
              windowMonths={snapshot.months.length}
            />
          ))}

          {selectedBucketId && (
            <section className="augur-panel overflow-hidden">
              <div className="flex items-start justify-between gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
                <div>
                  <div className="augur-eyebrow">Transactions — {bucketsById.get(selectedBucketId)?.label}</div>
                  <div className="mt-1 text-xs augur-muted">All transactions in the window for this bucket.</div>
                </div>
                <div className="flex items-center gap-3">
                  {bucketTx && bucketTx.length > 0 && (
                    <button
                      type="button"
                      className={EXPORT_BUTTON_CLASS}
                      onClick={exportTransactions}
                      data-budget-export="transactions"
                    >
                      ↓ Export CSV
                    </button>
                  )}
                  <button type="button" className="text-xs augur-link" onClick={() => setSelectedBucketId(null)}>
                    Close
                  </button>
                </div>
              </div>
              {bucketTxError ? (
                <div className="augur-note-danger p-4 text-sm">Transactions failed: {bucketTxError}</div>
              ) : (
                <TransactionsPanel transactions={bucketTx} />
              )}
            </section>
          )}

          <section className="augur-panel overflow-hidden">
            <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
              <div className="augur-eyebrow">Lumpy spends (≥ {fmtUsd(snapshot.lumpyThresholdUsd)})</div>
              <div className="mt-1 text-xs augur-muted">
                Single large outflows in the window. Click into a row above to see the full transaction list for that
                bucket; this panel surfaces just the headline-grabbing items.
              </div>
            </div>
            <LumpyPanel items={snapshot.lumpy} bucketsById={bucketsById} />
          </section>
        </>
      )}
    </main>
  );
}
