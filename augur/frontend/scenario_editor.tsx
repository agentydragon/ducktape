import React, { useState } from "react";
import { Button } from "@mantine/core";
import { NumberField, NativeSelectField } from "./lib/controls.tsx";
import { scenarioColor, resolveVariant, MAX_VARIANTS } from "./input_helpers.ts";
import { ScenarioTabs } from "./scenario_tabs.tsx";
import { SellOrderControl, ProductPortfolioPanel, LifecycleEventsEditor, propertyLabel } from "./forms.tsx";

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
// agency. The lifecycle timeline is the one list-shaped knob — it stays a per-active editor below.
// Row groups, in display order. Each is independently collapsible; the finer housing split (Mortgage
// / Rental income / Management as their own groups) lets you fold away the parts that don't matter
// for the comparison you're looking at.
const GROUPS = [
  "Property",
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
  { key: "financingKind", label: "Financing", kind: "financing", group: "Property", needs: "owns" },
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
  { key: "cashBufferTriggerBelowUsd", label: "Trigger below", kind: "usd", step: 1000, group: "Cash buffer" },
  { key: "cashBufferSaleUsd", label: "Sell amount", kind: "usd", step: 1000, group: "Cash buffer" },
  { key: "cashBufferIndexToInflation", label: "Buffer index", kind: "boolIndex", group: "Cash buffer" },
  { key: "peLnwFloorUsd", label: "PE LNW floor", kind: "usd", step: 10000, group: "Private equity" },
  { key: "peIndexFloorToInflation", label: "PE floor index", kind: "boolIndex", group: "Private equity" },
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

// Whether a knob applies to a single resolved scenario. Gates both the row (shown when it applies
// to some scenario) and each cell (a non-applying scenario shows "—" instead of an inert input — so
// the mortgage rows render inputs only for the scenarios that actually buy with a mortgage, etc.).
function knobApplies(knob, input) {
  const owns = input.propertyId != null;
  const rented = owns && Number(input.rentalFractionRentedPct) > 0;
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
function KnobCell({ knob, value, ariaLabel, bootstrap, muted = false, rightSection = undefined, onChange }) {
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

const TABLE_HEADER =
  "px-3 py-2 text-right text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400";

// One variant's cell: editable, bound to the override value when overridden or the inherited Base
// value otherwise (editing an inherited cell creates the override). The revert ↩ appears (inside the
// box) only when overridden, dropping the key so the cell re-inherits Base.
function VariantKnobCell({ knob, variant, baseInput, bootstrap, onPatchVariant, onRevertKeys }) {
  if (!knobApplies(knob, resolveVariant(baseInput, variant.overrides))) return <NaCell />;
  const overridden = knob.key in variant.overrides;
  const value = overridden ? variant.overrides[knob.key] : baseInput[knob.key];
  return (
    <td className="px-3 py-1.5 align-top">
      <KnobCell
        knob={knob}
        value={value}
        ariaLabel={`${knob.label} — ${variant.label}`}
        bootstrap={bootstrap}
        muted={!overridden}
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

// Full-width scenario editor for the Base + per-variant-overrides model. Every per-scenario knob is
// a row (Base column + one column per variant); a Base cell edits everywhere, a variant cell
// overrides individually. The lifecycle timeline (too list-shaped for table cells) is a collapsible
// per-scenario section below the grid — one editor per owning scenario (Base + variants), with the
// same inherit/override semantics. Sell order is Base-only. The whole thing collapses so the
// chart/results below are reachable without scrolling.
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
  const multi = variants.length > 0;

  const resolvedInputs = [base.input, ...variants.map((v) => resolveVariant(base.input, v.overrides))];
  const visibleKnobs = KNOBS.filter((knob) => resolvedInputs.some((input) => knobApplies(knob, input)));

  // One timeline editor per scenario that buys (Base + variants); a variant's events inherit Base
  // until edited (which creates a `propertyLifecycleEvents` override, revertable). Index into
  // `entries` keeps the dot color aligned with the chart + column headers.
  const timelineCollapsed = collapsed.has("Timeline");
  const timelineEntries = [
    { id: "base", label: base.label, input: base.input, variant: null },
    ...variants.map((v) => ({ id: v.id, label: v.label, input: resolveVariant(base.input, v.overrides), variant: v })),
  ].filter((entry) => entry.input.propertyId != null);

  return (
    <div className="augur-card divide-y divide-slate-200 dark:divide-slate-700" data-product-scenario-editor="">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
        <div className="augur-eyebrow">
          Scenario comparison{multi ? ` — Base + ${variants.length} variant${variants.length === 1 ? "" : "s"}` : ""}
        </div>
        <Button
          size="xs"
          variant="default"
          data-product-editor-toggle=""
          aria-expanded={open}
          onClick={() => setOpen((previous) => !previous)}
        >
          {open ? "Hide ▴" : "Edit ▾"}
        </Button>
      </div>

      {open && (
        <>
          <div className="px-4 py-3">
            {multi && (
              <div className="mb-3 text-xs augur-muted">
                Edit a Base cell to change it everywhere; edit a variant cell to override just that variant (its ↩
                reverts it to Base). Variants can differ on any row — what each scenario buys, how it finances,
                spending.
              </div>
            )}
            <div className="overflow-x-auto" data-product-scenario-table="">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-200 dark:border-slate-700">
                    <th className="px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                      Setting
                    </th>
                    {entries.map((entry, index) => (
                      <th key={entry.id} className={TABLE_HEADER} data-product-scenario-col={entry.id}>
                        <span className="inline-flex items-center justify-end gap-1.5">
                          <span
                            className="h-2.5 w-2.5 rounded-full"
                            style={{ backgroundColor: scenarioColor(index) }}
                            aria-hidden="true"
                          />
                          <span
                            className={entry.id === activeId ? "font-semibold text-slate-700 dark:text-slate-200" : ""}
                          >
                            {entry.label}
                          </span>
                        </span>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {GROUPS.map((group) => {
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
                              <span
                                aria-hidden="true"
                                className={`text-[8px] transition-transform ${groupCollapsed ? "" : "rotate-90"}`}
                              >
                                ▶
                              </span>
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
                                  onPatchVariant={onPatchVariant}
                                  onRevertKeys={onRevertKeys}
                                />
                              ))}
                            </tr>
                          ))}
                      </React.Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <SellOrderControl
              sellOrder={base.input.sellOrder}
              portfolio={portfolio}
              onChange={(sellOrder) => onSetBaseField("sellOrder", sellOrder)}
            />
          </div>

          {timelineEntries.length > 0 && (
            <div className="px-4 py-3" data-product-timeline="">
              <button
                type="button"
                className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400"
                aria-expanded={!timelineCollapsed}
                data-product-group-toggle="Timeline"
                onClick={() => toggleCollapsed("Timeline")}
              >
                <span
                  aria-hidden="true"
                  className={`text-[8px] transition-transform ${timelineCollapsed ? "" : "rotate-90"}`}
                >
                  ▶
                </span>
                Timeline
              </button>
              {!timelineCollapsed && (
                <div className="mt-3 space-y-4">
                  {timelineEntries.map((entry) => {
                    const index = entries.findIndex((e) => e.id === entry.id);
                    const overridden = entry.variant != null && "propertyLifecycleEvents" in entry.variant.overrides;
                    return (
                      <div key={entry.id}>
                        <div className="mb-1 flex items-center justify-between gap-2">
                          <div className="augur-field-label flex items-center gap-1.5">
                            <span
                              className="h-2.5 w-2.5 rounded-full"
                              style={{ backgroundColor: scenarioColor(index) }}
                              aria-hidden="true"
                            />
                            {entry.label}
                          </div>
                          {overridden && (
                            <button
                              type="button"
                              className="augur-link text-xs font-semibold"
                              onClick={() => onRevertKeys(entry.variant.id, ["propertyLifecycleEvents"])}
                            >
                              Revert to base
                            </button>
                          )}
                        </div>
                        <LifecycleEventsEditor
                          events={entry.input.propertyLifecycleEvents ?? []}
                          horizonMonths={horizonMonths}
                          onChange={(events) =>
                            entry.variant == null
                              ? onSetBaseField("propertyLifecycleEvents", events)
                              : onPatchVariant(entry.variant.id, { propertyLifecycleEvents: events })
                          }
                        />
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          <ProductPortfolioPanel portfolio={portfolio} error={portfolioError} />
        </>
      )}
    </div>
  );
}

// The scenario selector below the comparison table: chips (add / rename / delete / select) + Reset.
// The active chip drives only the rollout results below it (histogram / events / terminal table);
// per-scenario editing — including each scenario's timeline — lives in the comparison editor above.
export function ScenarioInspector({
  base,
  variants,
  activeId,
  onSelect,
  onAddVariant,
  onDeleteVariant,
  onRename,
  onResetBase,
  onRevertKeys,
}) {
  const entries = [{ id: "base", label: base.label }, ...variants.map((v) => ({ id: v.id, label: v.label }))];
  const activeVariant = variants.find((v) => v.id === activeId) ?? null;

  const resetActive = () => {
    if (activeVariant == null) onResetBase();
    else onRevertKeys(activeVariant.id, Object.keys(activeVariant.overrides));
  };

  return (
    <div
      className="augur-card flex flex-wrap items-center justify-between gap-2 px-4 py-3"
      data-product-scenario-inspector=""
    >
      <ScenarioTabs
        entries={entries}
        activeId={activeId}
        onSelect={onSelect}
        onAdd={onAddVariant}
        onDelete={onDeleteVariant}
        onRename={onRename}
        canAdd={variants.length < MAX_VARIANTS}
      />
      <Button size="xs" variant="subtle" onClick={resetActive}>
        Reset {activeVariant == null ? "base" : activeVariant.label}
      </Button>
    </div>
  );
}
