import React, { useState } from "react";
import { Button } from "@mantine/core";
import { NumberField, NativeSelectField } from "./lib/controls.tsx";
import { fmtUsd, fmtNumber } from "./lib/format.ts";
import { scenarioColor, resolveVariant, MAX_VARIANTS } from "./input_helpers.ts";
import {
  DisclosureArrow,
  SellOrderControl,
  ProductPortfolioPanel,
  LifecycleEventsEditor,
  propertyLabel,
} from "./forms.tsx";

const INDEX_DATA = [
  { value: "inflation", label: "Inflation" },
  { value: "none", label: "None" },
];
const YES_NO = [
  { value: "yes", label: "Yes" },
  { value: "no", label: "No" },
];
const FINANCING_DATA = [
  { value: "cash", label: "Cash" },
  { value: "mortgage", label: "Mortgage" },
];
const TERM_DATA = [
  { value: "360", label: "30 yr" },
  { value: "180", label: "15 yr" },
];

// Every per-scenario knob is a spreadsheet row (rows = knobs, columns = Base + variants), edited in
// place and individually overridable per variant. Owning knobs (`needs`) only appear once some
// scenario makes them relevant (the same "union" trick the metric list uses): `owns` once any
// scenario buys, `mortgage`/`rented`/`managed` once any buys-with-mortgage / rents-it-out / uses an
// agency. The lifecycle timeline is list-shaped, but still renders as a per-scenario table row.
// Row groups, in display order. Each is independently collapsible; the finer housing split (Mortgage
// / Rental income / Management as their own groups) lets you fold away the parts that don't matter
// for the comparison you're looking at.
const GROUPS = [
  "Property",
  "Timeline",
  "Mortgage",
  "Rental income",
  "Management",
  "Spending",
  "Outside rent",
  "Cash buffer",
  "Private equity",
];

const KNOBS = [
  { key: "propertyId", label: "Property to buy", kind: "property", group: "Property" },
  { key: "annualInsurancePct", label: "Insurance", kind: "pct", max: 10, step: 0.05, group: "Property", needs: "owns" },
  {
    key: "annualMaintenancePct",
    label: "Maintenance",
    kind: "pct",
    max: 10,
    step: 0.1,
    group: "Property",
    needs: "owns",
  },
  { key: "livesHere", label: "Owner lives here", kind: "bool", group: "Property", needs: "owns" },
  { key: "financingKind", label: "Financing", kind: "financing", group: "Property", needs: "owns" },
  {
    key: "downPaymentPct",
    label: "Down payment",
    kind: "pct",
    max: 100,
    step: 1,
    group: "Mortgage",
    needs: "mortgage",
  },
  { key: "mortgageTermMonths", label: "Term", kind: "term", group: "Mortgage", needs: "mortgage" },
  {
    key: "annualRatePct",
    label: "Annual rate",
    kind: "pct",
    max: 25,
    step: 0.125,
    group: "Mortgage",
    needs: "mortgage",
  },
  {
    key: "rentalFractionRentedPct",
    label: "Rented",
    kind: "pct",
    max: 100,
    step: 1,
    group: "Rental income",
    needs: "owns",
  },
  {
    key: "rentalVacancyPct",
    label: "Vacancy",
    kind: "pct",
    max: 100,
    step: 1,
    group: "Rental income",
    needs: "rented",
  },
  {
    key: "rentalFullPropertyMonthlyUsd",
    label: "Full-property rent",
    kind: "usd",
    step: 100,
    group: "Rental income",
    needs: "rented",
  },
  { key: "useRentalManagement", label: "Use management", kind: "bool", group: "Rental income", needs: "rented" },
  { key: "managementFeePct", label: "Mgmt fee", kind: "pct", max: 100, step: 1, group: "Management", needs: "managed" },
  {
    key: "leasingFeeMonths",
    label: "Leasing fee",
    kind: "num",
    unit: "mo",
    step: 0.5,
    group: "Management",
    needs: "managed",
  },
  {
    key: "avgTenancyMonths",
    label: "Avg tenancy",
    kind: "num",
    unit: "mo",
    step: 1,
    group: "Management",
    needs: "managed",
  },
  { key: "monthlySpendUsd", label: "Monthly spend", kind: "usd", min: 1, step: 100, group: "Spending" },
  { key: "spendIndex", label: "Spend index", kind: "index", group: "Spending" },
  { key: "monthlyRentUsd", label: "Monthly rent", kind: "usd", step: 100, group: "Outside rent" },
  { key: "rentalLocationId", label: "Rent location", kind: "location", group: "Outside rent" },
  { key: "sellOrder", label: "Sell preference", kind: "sellOrder", group: "Cash buffer" },
  { key: "cashBufferTriggerBelowUsd", label: "Trigger below", kind: "usd", step: 1000, group: "Cash buffer" },
  { key: "cashBufferSaleUsd", label: "Sell amount", kind: "usd", step: 1000, group: "Cash buffer" },
  { key: "cashBufferIndexToInflation", label: "Buffer index", kind: "boolIndex", group: "Cash buffer" },
  { key: "peLnwFloorUsd", label: "PE LNW floor", kind: "usd", step: 10000, group: "Private equity" },
  { key: "peIndexFloorToInflation", label: "PE floor index", kind: "boolIndex", group: "Private equity" },
];

// The chosen house's read-only facts, surfaced as comparison rows (one cell per scenario, "—" when a
// scenario buys nothing). They describe the property and aren't edited here, so they live in their
// own collapsible "House facts" group rather than among the editable knobs.
const usdOrDash = (value) => (value == null ? "—" : fmtUsd(value));
const numOrDash = (value) => (value == null ? "—" : fmtNumber(value));
const HOUSE_FACTS = [
  { label: "Type", value: (property) => property.type || "—" },
  {
    label: "Beds / baths",
    value: (property) =>
      property.beds == null && property.baths == null
        ? "—"
        : `${numOrDash(property.beds)} / ${numOrDash(property.baths)}`,
  },
  { label: "Sqft", value: (property) => numOrDash(property.sqft) },
  { label: "Year built", value: (property) => property.yearBuilt ?? "—" },
  { label: "Price", value: (property) => usdOrDash(property.priceUsd) },
  { label: "HOA / mo", value: (property) => usdOrDash(property.hoaMonthlyUsd) },
  { label: "Property tax / yr", value: (property) => usdOrDash(property.annualTaxOnListUsd) },
  { label: "Rent estimate", value: (property) => usdOrDash(property.rentEstimateUsd) },
];

function propertyOptions(bootstrap) {
  const properties = bootstrap.properties ?? [];
  return [
    { value: "", label: properties.length === 0 ? "(no properties)" : "(no purchase)" },
    ...properties.map((property) => ({ value: property.id, label: propertyLabel(property) })),
  ];
}

function locationOptions(bootstrap) {
  return [
    { value: "", label: "(default)" },
    ...bootstrap.locations.map((location) => ({ value: location.id, label: location.label })),
  ];
}

function findProperty(bootstrap, propertyId) {
  if (propertyId == null) return null;
  return (bootstrap.properties ?? []).find((property) => property.id === propertyId) ?? null;
}

// Whether a knob applies to a single resolved scenario. Gates both the row (shown when it applies
// to some scenario) and each cell (a non-applying scenario shows "—" instead of an inert input — so
// the mortgage rows render inputs only for the scenarios that actually buy with a mortgage, etc.).
function knobApplies(knob, input) {
  const owns = input.propertyId != null;
  const rented = owns && Number(input.rentalFractionRentedPct) > 0;
  // Owner-occupancy is moot once the property is fully rented (the wire forces is_primary_residence
  // off in that case), so don't offer the toggle.
  if (knob.key === "livesHere" && Number(input.rentalFractionRentedPct) >= 100) return false;
  switch (knob.needs) {
    case "owns":
      return owns;
    case "mortgage":
      return owns && input.financingKind === "mortgage";
    case "rented":
      return rented;
    case "managed":
      return rented && input.useRentalManagement;
    default:
      return true;
  }
}

// Placeholder text for the full-property-rent cell: left blank, it falls back to the property's rent
// estimate, so "" and "$0" mean different things. Other knobs have no placeholder.
function cellPlaceholder(knob, input, bootstrap) {
  if (knob.key !== "rentalFullPropertyMonthlyUsd") return undefined;
  const property = findProperty(bootstrap, input.propertyId);
  const estimate = Number(property?.rentEstimateUsd);
  return estimate > 0 ? `${fmtUsd(estimate)} (property default)` : "(required)";
}

// Placeholder for a cell whose knob doesn't apply to that scenario (e.g. a mortgage rate for a
// renting / all-cash scenario).
function NaCell() {
  return (
    <td className="px-3 py-1.5 text-center align-middle augur-muted" aria-label="not applicable">
      —
    </td>
  );
}

// Compact, label-less control for a spreadsheet cell. The row + column headers already name the knob
// and the scenario, so the input carries only an `aria-label`. `muted` renders inherited cells
// greyed/disabled-looking (Mantine `filled` variant + a fade) so overrides stand out — they stay
// editable (typing creates an override). `rightSection` tucks the revert ↩ inside the box (a suffix,
// replacing a select's native chevron while overridden), made clickable via pointer-events.
function KnobCell({
  knob,
  value,
  ariaLabel,
  bootstrap,
  portfolio,
  muted = false,
  rightSection = undefined,
  placeholder = undefined,
  onChange,
}) {
  const wrap = (control) => <div className={muted ? "opacity-75" : undefined}>{control}</div>;
  const controlProps = {
    ...(rightSection ? { rightSection, rightSectionPointerEvents: "auto", rightSectionWidth: 30 } : {}),
    ...(muted ? { variant: "filled" } : {}),
  };
  const number = (extra) => (
    <NumberField
      aria-label={ariaLabel}
      value={value}
      min={knob.min ?? 0}
      max={knob.max}
      step={knob.step ?? 1}
      placeholder={placeholder}
      {...extra}
      {...controlProps}
      onChange={onChange}
    />
  );
  const select = (data, current, decode) => (
    <NativeSelectField
      aria-label={ariaLabel}
      value={current}
      data={data}
      {...controlProps}
      onChange={(event) => onChange(decode(event.target.value))}
    />
  );
  switch (knob.kind) {
    case "usd":
      return wrap(number({ prefix: "$" }));
    case "pct":
      return wrap(number({ suffix: "%" }));
    case "num":
      return wrap(number({ suffix: knob.unit }));
    case "index":
      return wrap(select(INDEX_DATA, value, (next) => (next === "none" ? "none" : "inflation")));
    case "boolIndex":
      return wrap(select(INDEX_DATA, value ? "inflation" : "none", (next) => next === "inflation"));
    case "bool":
      return wrap(select(YES_NO, value ? "yes" : "no", (next) => next === "yes"));
    case "financing":
      return wrap(select(FINANCING_DATA, value, (next) => (next === "mortgage" ? "mortgage" : "cash")));
    case "term":
      return wrap(select(TERM_DATA, String(value), (next) => (Number(next) === 180 ? 180 : 360)));
    case "property":
      return wrap(select(propertyOptions(bootstrap), value ?? "", (next) => next || null));
    case "sellOrder":
      return wrap(
        <SellOrderControl sellOrder={value} portfolio={portfolio} compact label={null} onChange={onChange} />
      );
    default: // location
      return wrap(select(locationOptions(bootstrap), value ?? "", (next) => next || null));
  }
}

// Revert-to-base affordance rendered inside an overridden cell's input as a right section (suffix).
function RevertButton({ label, onClick }) {
  return (
    <button
      type="button"
      aria-label={label}
      title="Revert to base"
      onClick={onClick}
      className="augur-link text-sm leading-none"
    >
      ↩
    </button>
  );
}

function stopSummaryButton(event) {
  event.preventDefault();
  event.stopPropagation();
}

function ScenarioHeaderCells({ entries, activeId, onSelect, onDelete, onRename }) {
  const [editingId, setEditingId] = useState(null);
  return (
    <div className="contents" data-product-scenario-tabs="">
      {entries.map((entry, index) => {
        const isActive = entry.id === activeId;
        const isEditing = editingId === entry.id;
        const isBase = entry.id === "base";
        // Empty names survive poorly through chart legends and URL round-trips, so normalize on
        // commit while still allowing in-progress edits to be blank.
        const commitEdit = () => {
          if (entry.label.trim() === "") onRename(entry.id, isBase ? "Base" : `Variant ${index}`);
          setEditingId(null);
        };
        return (
          <div
            key={entry.id}
            data-product-scenario-col={entry.id}
            data-product-scenario-tab={entry.id}
            data-product-scenario-select={entry.id}
            data-active={isActive ? "" : undefined}
            title="Click to select, double-click to rename"
            className={`flex h-8 min-w-0 cursor-pointer items-center justify-center border-b-2 px-2 text-center ${
              isActive
                ? "border-blue-500 text-slate-900 dark:text-slate-50"
                : "border-transparent text-slate-500 dark:text-slate-400"
            }`}
            onClick={(event) => {
              if (isEditing) return;
              stopSummaryButton(event);
              onSelect(entry.id);
            }}
            onDoubleClick={(event) => {
              if (isEditing) return;
              stopSummaryButton(event);
              setEditingId(entry.id);
            }}
          >
            <div className="flex min-w-0 items-center justify-center gap-1.5">
              <span
                className="h-2.5 w-2.5 shrink-0 rounded-full"
                style={{ backgroundColor: scenarioColor(index) }}
                aria-hidden="true"
              />
              {isEditing ? (
                <input
                  data-product-scenario-rename={entry.id}
                  aria-label={`Rename ${entry.label}`}
                  autoFocus
                  className="min-w-0 flex-1 bg-transparent text-center text-sm font-semibold text-slate-900 focus:outline-none dark:text-slate-50"
                  value={entry.label}
                  onClick={(event) => event.stopPropagation()}
                  onChange={(event) => onRename(entry.id, event.target.value)}
                  onBlur={commitEdit}
                  onKeyDown={(event) => {
                    event.stopPropagation();
                    if (event.key === "Enter" || event.key === "Escape") {
                      event.preventDefault();
                      commitEdit();
                    }
                  }}
                />
              ) : (
                <span
                  className={`min-w-0 truncate text-sm font-semibold ${
                    isActive ? "text-slate-900 dark:text-slate-50" : "text-slate-600 dark:text-slate-300"
                  }`}
                >
                  {entry.label}
                </span>
              )}
              {!isBase && (
                <button
                  type="button"
                  data-product-scenario-delete={entry.id}
                  aria-label={`Delete ${entry.label}`}
                  className="shrink-0 text-base leading-none text-slate-400 hover:text-rose-600 dark:hover:text-rose-400"
                  onClick={(event) => {
                    stopSummaryButton(event);
                    onDelete(entry.id);
                  }}
                >
                  ×
                </button>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// One variant's cell: editable, bound to the override value when overridden or the inherited Base
// value otherwise (editing an inherited cell creates the override). The revert ↩ appears (inside the
// box) only when overridden, dropping the key so the cell re-inherits Base.
function VariantKnobCell({ knob, variant, baseInput, bootstrap, portfolio, onPatchVariant, onRevertKeys }) {
  const resolved = resolveVariant(baseInput, variant.overrides);
  if (!knobApplies(knob, resolved)) return <NaCell />;
  const overridden = knob.key in variant.overrides;
  const value = overridden ? variant.overrides[knob.key] : baseInput[knob.key];
  return (
    <td className="px-3 py-1.5 align-top">
      <KnobCell
        knob={knob}
        value={value}
        ariaLabel={`${knob.label} — ${variant.label}`}
        bootstrap={bootstrap}
        portfolio={portfolio}
        muted={!overridden}
        placeholder={cellPlaceholder(knob, resolved, bootstrap)}
        rightSection={
          overridden ? (
            <RevertButton
              label={`Revert ${knob.label} for ${variant.label} to base`}
              onClick={() => onRevertKeys(variant.id, [knob.key])}
            />
          ) : undefined
        }
        onChange={(next) => onPatchVariant(variant.id, { [knob.key]: next })}
      />
    </td>
  );
}

function TimelineCell({ entry, horizonMonths, onSetBaseField, onPatchVariant, onRevertKeys }) {
  if (entry.input.propertyId == null) return <NaCell />;
  const overridden = entry.variant != null && "propertyLifecycleEvents" in entry.variant.overrides;
  const content = (
    <LifecycleEventsEditor
      events={entry.input.propertyLifecycleEvents ?? []}
      horizonMonths={horizonMonths}
      showLabel={false}
      className="min-w-[14rem]"
      onChange={(events) =>
        entry.variant == null
          ? onSetBaseField("propertyLifecycleEvents", events)
          : onPatchVariant(entry.variant.id, { propertyLifecycleEvents: events })
      }
    />
  );
  return (
    <td className="px-3 py-2 align-top" data-product-timeline-cell={entry.id}>
      <div className={entry.variant != null && !overridden ? "opacity-75" : undefined}>
        {overridden && (
          <div className="mb-1 flex justify-end">
            <button
              type="button"
              className="augur-link text-xs font-semibold"
              onClick={() => onRevertKeys(entry.variant.id, ["propertyLifecycleEvents"])}
            >
              Revert
            </button>
          </div>
        )}
        {content}
      </div>
    </td>
  );
}

// Full-width scenario editor for the Base + per-variant-overrides model. Every per-scenario knob is
// a row (Base column + one column per variant); a Base cell edits everywhere, a variant cell
// overrides individually. List-shaped knobs like the lifecycle timeline still live inside their
// scenario cells, so base/variants remain side-by-side. The whole thing collapses so the chart/results
// below are reachable without scrolling.
export function ScenarioEditor({
  base,
  variants,
  activeId,
  bootstrap,
  portfolio,
  portfolioError,
  horizonMonths,
  onSetBaseField,
  onPatchVariant,
  onRevertKeys,
  onAddVariant,
  onSelect,
  onDeleteVariant,
  onRename,
  onResetBase,
}) {
  const [open, setOpen] = useState(true);
  // Per-group + timeline collapse — fold away the rows/sections that don't matter for the comparison
  // at hand (e.g. mortgage terms, management, the per-scenario timelines). Everything starts expanded.
  const [collapsed, setCollapsed] = useState(() => new Set());
  const toggleCollapsed = (name) =>
    setCollapsed((previous) => {
      const next = new Set(previous);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  const entries = [{ id: "base", label: base.label }, ...variants.map((v) => ({ id: v.id, label: v.label }))];
  const activeVariant = variants.find((v) => v.id === activeId) ?? null;
  const resetActive = () => {
    if (activeVariant == null) onResetBase();
    else onRevertKeys(activeVariant.id, Object.keys(activeVariant.overrides));
  };
  const settingColumnWidth = "18rem";
  const scenarioMinWidth = "12rem";
  const tableMinWidth = `${18 + entries.length * 12}rem`;
  const comparisonColumns = {
    gridTemplateColumns: `${settingColumnWidth} repeat(${entries.length}, minmax(${scenarioMinWidth}, 1fr))`,
    minWidth: tableMinWidth,
    width: "100%",
  };

  const resolvedInputs = [base.input, ...variants.map((v) => resolveVariant(base.input, v.overrides))];
  const visibleKnobs = KNOBS.filter((knob) => resolvedInputs.some((input) => knobApplies(knob, input)));

  // Scenarios that buy a property get a lifecycle timeline row inside the grid. A variant's events
  // inherit Base until edited (creating a revertable `propertyLifecycleEvents` override). The chosen
  // house's read-only facts render as their own collapsible "House facts" row group
  // (`houseFactsGroup`), inserted after the editable Property group.
  const timelineCollapsed = collapsed.has("Timeline");
  const scenarioRows = [
    { id: "base", label: base.label, input: base.input, variant: null },
    ...variants.map((v) => ({ id: v.id, label: v.label, input: resolveVariant(base.input, v.overrides), variant: v })),
  ];
  const owningScenarios = scenarioRows.filter((entry) => entry.input.propertyId != null);

  const timelineGroup =
    owningScenarios.length > 0 ? (
      <>
        <tr>
          <th colSpan={1 + entries.length} className="bg-slate-50 p-0 dark:bg-slate-900">
            <button
              type="button"
              className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-[11px] font-semibold uppercase tracking-wide text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
              aria-expanded={!timelineCollapsed}
              data-product-group-toggle="Timeline"
              onClick={() => toggleCollapsed("Timeline")}
            >
              <DisclosureArrow collapsed={timelineCollapsed} />
              Timeline
              {timelineCollapsed && <span className="ml-1 normal-case augur-muted">1 row</span>}
            </button>
          </th>
        </tr>
        {!timelineCollapsed && (
          <tr data-product-timeline="">
            <th className="whitespace-nowrap px-3 py-2 text-left font-medium augur-strong">
              Timeline (mid-horizon changes)
            </th>
            {scenarioRows.map((entry) => (
              <TimelineCell
                key={entry.id}
                entry={entry}
                horizonMonths={horizonMonths}
                onSetBaseField={onSetBaseField}
                onPatchVariant={onPatchVariant}
                onRevertKeys={onRevertKeys}
              />
            ))}
          </tr>
        )}
      </>
    ) : null;

  // Read-only "House facts" group: one row per surfaced property attribute, one cell per scenario (its
  // resolved property, or "—" when it buys nothing). Collapsible via the same group toggle as the
  // editable groups; rendered only when some scenario actually buys.
  const houseFactsCollapsed = collapsed.has("House facts");
  const houseFactsGroup =
    owningScenarios.length > 0 ? (
      <>
        <tr>
          <th colSpan={1 + entries.length} className="bg-slate-50 p-0 dark:bg-slate-900">
            <button
              type="button"
              className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-[11px] font-semibold uppercase tracking-wide text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
              aria-expanded={!houseFactsCollapsed}
              data-product-group-toggle="House facts"
              onClick={() => toggleCollapsed("House facts")}
            >
              <DisclosureArrow collapsed={houseFactsCollapsed} />
              House facts
              {houseFactsCollapsed && <span className="ml-1 normal-case augur-muted">{HOUSE_FACTS.length} rows</span>}
            </button>
          </th>
        </tr>
        {!houseFactsCollapsed &&
          HOUSE_FACTS.map((fact) => (
            <tr key={fact.label} data-product-house-fact={fact.label}>
              <th className="whitespace-nowrap px-3 py-1.5 text-left font-medium augur-strong">{fact.label}</th>
              {entries.map((entry, index) => {
                const property = findProperty(bootstrap, resolvedInputs[index].propertyId);
                return (
                  <td key={entry.id} className="px-3 py-1.5 text-right augur-tabular">
                    {property ? fact.value(property) : <span className="augur-muted">—</span>}
                  </td>
                );
              })}
            </tr>
          ))}
      </>
    ) : null;

  return (
    <details
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
      className="augur-card divide-y divide-slate-200 dark:divide-slate-700 [&_summary::-webkit-details-marker]:hidden"
      data-product-scenario-editor=""
    >
      <summary className="cursor-pointer list-none px-4 py-3" data-product-editor-toggle="">
        <div className="overflow-x-auto">
          <div className="grid min-w-max items-center gap-3" style={comparisonColumns}>
            <div className="flex h-8 min-w-0 items-center gap-1.5">
              <span className="augur-eyebrow inline-flex min-w-0 shrink items-center gap-1 whitespace-nowrap">
                <DisclosureArrow collapsed={!open} />
                Scenario comparison
              </span>
              <Button
                size="xs"
                variant="subtle"
                className="shrink-0"
                title={`Reset ${activeVariant == null ? "base" : activeVariant.label}`}
                aria-label={`Reset ${activeVariant == null ? "base" : activeVariant.label}`}
                onClick={(event) => {
                  stopSummaryButton(event);
                  resetActive();
                }}
              >
                Reset
              </Button>
              {variants.length < MAX_VARIANTS && (
                <Button
                  size="xs"
                  variant="light"
                  className="shrink-0"
                  data-product-scenario-add=""
                  title="Add variant"
                  aria-label="Add variant"
                  onClick={(event) => {
                    stopSummaryButton(event);
                    onAddVariant();
                  }}
                >
                  Add
                </Button>
              )}
            </div>
            <ScenarioHeaderCells
              entries={entries}
              activeId={activeId}
              onSelect={onSelect}
              onDelete={onDeleteVariant}
              onRename={onRename}
            />
          </div>
        </div>
      </summary>

      {open && (
        <>
          <div className="px-4 py-3">
            <div className="overflow-x-auto" data-product-scenario-table="">
              <table className="w-full text-sm" style={{ minWidth: tableMinWidth, tableLayout: "fixed" }}>
                <colgroup>
                  <col style={{ width: settingColumnWidth }} />
                  {entries.map((entry) => (
                    <col key={entry.id} />
                  ))}
                </colgroup>
                <tbody>
                  {GROUPS.map((group) => {
                    if (group === "Timeline") return <React.Fragment key={group}>{timelineGroup}</React.Fragment>;
                    const groupKnobs = visibleKnobs.filter((knob) => knob.group === group);
                    if (groupKnobs.length === 0) return null;
                    const groupCollapsed = collapsed.has(group);
                    return (
                      <React.Fragment key={group}>
                        <tr>
                          <th colSpan={1 + entries.length} className="bg-slate-50 p-0 dark:bg-slate-900">
                            <button
                              type="button"
                              className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-[11px] font-semibold uppercase tracking-wide text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
                              aria-expanded={!groupCollapsed}
                              data-product-group-toggle={group}
                              onClick={() => toggleCollapsed(group)}
                            >
                              <DisclosureArrow collapsed={groupCollapsed} />
                              {group}
                              {groupCollapsed && (
                                <span className="ml-1 normal-case augur-muted">{groupKnobs.length} rows</span>
                              )}
                            </button>
                          </th>
                        </tr>
                        {!groupCollapsed &&
                          groupKnobs.map((knob) => (
                            <tr key={knob.key} data-product-knob-row={knob.key}>
                              <th className="whitespace-nowrap px-3 py-1.5 text-left font-medium augur-strong">
                                {knob.label}
                              </th>
                              {knobApplies(knob, base.input) ? (
                                <td className="px-3 py-1.5 align-top">
                                  <div className="min-w-[8rem]">
                                    <KnobCell
                                      knob={knob}
                                      value={base.input[knob.key]}
                                      ariaLabel={`${knob.label} — Base`}
                                      bootstrap={bootstrap}
                                      portfolio={portfolio}
                                      placeholder={cellPlaceholder(knob, base.input, bootstrap)}
                                      onChange={(value) => onSetBaseField(knob.key, value)}
                                    />
                                  </div>
                                </td>
                              ) : (
                                <NaCell />
                              )}
                              {variants.map((variant) => (
                                <VariantKnobCell
                                  key={variant.id}
                                  knob={knob}
                                  variant={variant}
                                  baseInput={base.input}
                                  bootstrap={bootstrap}
                                  portfolio={portfolio}
                                  onPatchVariant={onPatchVariant}
                                  onRevertKeys={onRevertKeys}
                                />
                              ))}
                            </tr>
                          ))}
                        {group === "Property" && houseFactsGroup}
                      </React.Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>

          <ProductPortfolioPanel portfolio={portfolio} error={portfolioError} />

          <div className="px-4 py-2 text-xs augur-muted" data-product-taxes="">
            Taxes: Federal + California · single filer
          </div>
        </>
      )}
    </details>
  );
}
