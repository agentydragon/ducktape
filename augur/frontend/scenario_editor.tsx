import React, { useState } from "react";
import { Button } from "@mantine/core";
import { NumberField, NativeSelectField } from "./lib/controls.tsx";
import { scenarioColor, resolveVariant, HOUSING_KEYS, MAX_VARIANTS } from "./input_helpers.ts";
import { ScenarioTabs } from "./scenario_tabs.tsx";
import { PropertyPurchasePanel, SellOrderControl, ProductPortfolioPanel, propertyLabel } from "./forms.tsx";

const INDEX_DATA = [
  { value: "inflation", label: "Inflation" },
  { value: "none", label: "None" },
];

// Scalar knobs edited in the comparison spreadsheet (rows = knobs, columns = Base + variants). The
// housing cluster (`HOUSING_KEYS`) + lifecycle events are too rich for cells, so they're edited in
// the per-entity Property & timeline panel below; sell order is Base-only. Grouped so related knobs
// stay together (a flat flow split e.g. the cash-buffer trio).
const SCALAR_GROUPS = ["Spending", "Outside rent", "Cash buffer", "Private equity"];

const SCALAR_KNOBS = [
  { key: "monthlySpendUsd", label: "Monthly spend", kind: "usd", min: 1, step: 100, group: "Spending" },
  { key: "spendIndex", label: "Spend index", kind: "index", group: "Spending" },
  { key: "monthlyRentUsd", label: "Monthly rent", kind: "usd", min: 0, step: 100, group: "Outside rent" },
  { key: "rentalLocationId", label: "Rent location", kind: "location", group: "Outside rent" },
  { key: "cashBufferTriggerBelowUsd", label: "Trigger below", kind: "usd", min: 0, step: 1000, group: "Cash buffer" },
  { key: "cashBufferSaleUsd", label: "Sell amount", kind: "usd", min: 0, step: 1000, group: "Cash buffer" },
  { key: "cashBufferIndexToInflation", label: "Buffer index", kind: "boolIndex", group: "Cash buffer" },
  { key: "peLnwFloorUsd", label: "PE LNW floor", kind: "usd", min: 0, step: 10000, group: "Private equity" },
  { key: "peIndexFloorToInflation", label: "PE floor index", kind: "boolIndex", group: "Private equity" },
];

// Compact, label-less control for a spreadsheet cell. The row + column headers already name the knob
// and the scenario, so the input itself carries only an `aria-label`. `muted` dims a value the
// variant inherits from Base (vs. one it overrides); `rightSection` tucks a revert affordance inside
// the box (like a unit suffix), so an overridden cell carries its own ↩.
function KnobCell({ knob, value, ariaLabel, bootstrap, muted = false, rightSection = undefined, onChange }) {
  // Inherited (non-overridden) cells render greyed/disabled-looking (Mantine `filled` variant + a
  // slight fade) so overrides stand out, while staying editable — typing creates an override.
  const wrap = (control) => <div className={muted ? "opacity-75" : undefined}>{control}</div>;
  // Spread onto the control: the revert button as a right section (overriding a select's native
  // chevron, made clickable via pointer-events) + a filled/greyed look for inherited cells.
  const controlProps = {
    ...(rightSection ? { rightSection, rightSectionPointerEvents: "auto", rightSectionWidth: 30 } : {}),
    ...(muted ? { variant: "filled" } : {}),
  };
  if (knob.kind === "usd") {
    return wrap(
      <NumberField
        aria-label={ariaLabel}
        value={value}
        min={knob.min}
        step={knob.step}
        prefix="$"
        {...controlProps}
        onChange={onChange}
      />
    );
  }
  if (knob.kind === "index") {
    return wrap(
      <NativeSelectField
        aria-label={ariaLabel}
        value={value}
        data={INDEX_DATA}
        {...controlProps}
        onChange={(event) => onChange(event.target.value === "none" ? "none" : "inflation")}
      />
    );
  }
  if (knob.kind === "boolIndex") {
    return wrap(
      <NativeSelectField
        aria-label={ariaLabel}
        value={value ? "inflation" : "none"}
        data={INDEX_DATA}
        {...controlProps}
        onChange={(event) => onChange(event.target.value === "inflation")}
      />
    );
  }
  if (knob.kind === "property") {
    const properties = bootstrap.properties ?? [];
    return wrap(
      <NativeSelectField
        aria-label={ariaLabel}
        value={value ?? ""}
        data={[
          { value: "", label: properties.length === 0 ? "(no properties)" : "(no purchase)" },
          ...properties.map((property) => ({ value: property.id, label: propertyLabel(property) })),
        ]}
        {...controlProps}
        onChange={(event) => onChange(event.target.value || null)}
      />
    );
  }
  // location
  return wrap(
    <NativeSelectField
      aria-label={ariaLabel}
      value={value ?? ""}
      data={[
        { value: "", label: "(default)" },
        ...bootstrap.locations.map((location) => ({ value: location.id, label: location.label })),
      ]}
      {...controlProps}
      onChange={(event) => onChange(event.target.value || null)}
    />
  );
}

// Revert-to-base affordance rendered inside a cell's input as a right section (suffix). Only shown on
// overridden cells; clicking drops the override so the cell re-inherits Base.
function RevertButton({ label, title = "Revert to base", onClick }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={title}
      onClick={onClick}
      className="augur-link text-sm leading-none"
    >
      ↩
    </button>
  );
}

const TABLE_HEADER =
  "px-3 py-2 text-right text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400";

// One variant's cell for a scalar knob: editable, bound to the override value when overridden or the
// inherited Base value otherwise (editing an inherited cell creates the override). A revert control
// appears only when the cell overrides, dropping the key so it re-inherits Base.
function VariantKnobCell({ knob, variant, baseInput, bootstrap, onPatchVariant, onRevertKeys }) {
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

// One variant's "Property to buy" cell. Housing is overridden as a unit, so this cell tracks the
// whole housing override: it shows the variant's property when housing is overridden (with ↩ to
// re-inherit the entire cluster) or the inherited Base property (muted) otherwise. Picking a
// property seeds the variant's housing from Base via `onSetVariantProperty`.
function PropertyVariantCell({ variant, baseInput, bootstrap, onSetVariantProperty, onRevertKeys }) {
  const overridden = HOUSING_KEYS.some((key) => key in variant.overrides);
  const value = overridden ? variant.overrides.propertyId : baseInput.propertyId;
  return (
    <td className="px-3 py-1.5 align-top">
      <KnobCell
        knob={{ kind: "property" }}
        value={value}
        ariaLabel={`Property to buy — ${variant.label}`}
        bootstrap={bootstrap}
        muted={!overridden}
        rightSection={
          overridden ? (
            <RevertButton
              label={`Revert housing for ${variant.label} to base`}
              title="Revert housing to base"
              onClick={() => onRevertKeys(variant.id, HOUSING_KEYS)}
            />
          ) : undefined
        }
        onChange={(next) => onSetVariantProperty(variant.id, next)}
      />
    </td>
  );
}

// One-line description of an entity's housing for the inherited-housing summary.
function HousingSummary({ input, bootstrap }) {
  const property = (bootstrap.properties ?? []).find((entry) => entry.id === input.propertyId);
  if (!property) return <>No property purchase (renting).</>;
  const financing = input.financingKind === "mortgage" ? "mortgage" : "cash";
  const rented = Number(input.rentalFractionRentedPct) > 0 ? ` · ${input.rentalFractionRentedPct}% rented` : "";
  const events = input.propertyLifecycleEvents?.length ?? 0;
  const timeline = events > 0 ? ` · ${events} timeline event${events === 1 ? "" : "s"}` : "";
  return (
    <>
      {property.address || property.id} · {financing}
      {rented}
      {timeline}
    </>
  );
}

// Housing & timeline for the active entity. Base edits propagate to inheriting variants; a variant
// either inherits Base housing (summary + "Override housing") or pins its own cluster (full panel +
// "Revert to base"). Housing is overridden as a unit so a variant never half-inherits a property.
function HousingSection({
  base,
  activeVariant,
  activeColor,
  bootstrap,
  horizonMonths,
  onSetBasePatch,
  onPatchVariant,
  onRevertKeys,
  onOverrideHousing,
}) {
  if (activeVariant == null) {
    return (
      <div>
        <div className="px-4 pt-3 augur-eyebrow">Housing &amp; timeline — Base</div>
        <PropertyPurchasePanel
          bootstrap={bootstrap}
          input={base.input}
          showPropertySelect={false}
          onChange={onSetBasePatch}
          horizonMonths={horizonMonths}
        />
      </div>
    );
  }
  const overridden = HOUSING_KEYS.some((key) => key in activeVariant.overrides);
  if (!overridden) {
    return (
      <div className="px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="augur-eyebrow">
            Housing &amp; timeline —{" "}
            <span className="font-semibold" style={{ color: activeColor }}>
              {activeVariant.label}
            </span>
          </div>
          <Button size="xs" variant="default" onClick={() => onOverrideHousing(activeVariant.id)}>
            Override housing
          </Button>
        </div>
        <div className="mt-2 text-xs augur-muted">
          Inherits Base: <HousingSummary input={base.input} bootstrap={bootstrap} />
        </div>
      </div>
    );
  }
  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-3">
        <div className="augur-eyebrow">
          Housing &amp; timeline —{" "}
          <span className="font-semibold" style={{ color: activeColor }}>
            {activeVariant.label}
          </span>{" "}
          <span className="augur-muted">(overrides Base)</span>
        </div>
        <button
          type="button"
          className="augur-link text-xs font-semibold"
          onClick={() => onRevertKeys(activeVariant.id, HOUSING_KEYS)}
        >
          Revert to base housing
        </button>
      </div>
      <PropertyPurchasePanel
        bootstrap={bootstrap}
        input={resolveVariant(base.input, activeVariant.overrides)}
        showPropertySelect={false}
        onChange={(patch) => onPatchVariant(activeVariant.id, patch)}
        horizonMonths={horizonMonths}
      />
    </div>
  );
}

// Full-width scenario editor for the Base + per-variant-overrides model. The spreadsheet edits the
// scalar knobs (Base column + one column per variant; Base edits propagate to inheriting variants,
// variant cells override individually). The Property & timeline panel below edits the active
// entity's housing cluster. Sell order is Base-only. The whole thing collapses (chips stay visible)
// so the chart/results below are reachable without scrolling.
export function ScenarioEditor({
  base,
  variants,
  activeId,
  bootstrap,
  portfolio,
  portfolioError,
  horizonMonths,
  onSelect,
  onAddVariant,
  onDeleteVariant,
  onRename,
  onResetBase,
  onSetBaseField,
  onSetBasePatch,
  onPatchVariant,
  onRevertKeys,
  onOverrideHousing,
  onSetVariantProperty,
}) {
  const [open, setOpen] = useState(true);
  const entries = [{ id: "base", label: base.label }, ...variants.map((v) => ({ id: v.id, label: v.label }))];
  const activeVariant = variants.find((v) => v.id === activeId) ?? null;
  const activeIndex = Math.max(
    0,
    entries.findIndex((entry) => entry.id === activeId)
  );
  const activeLabel = entries[activeIndex]?.label ?? base.label;
  const multi = variants.length > 0;

  const resetActive = () => {
    if (activeVariant == null) onResetBase();
    else onRevertKeys(activeVariant.id, Object.keys(activeVariant.overrides));
  };

  return (
    <div className="augur-card divide-y divide-slate-200 dark:divide-slate-700" data-product-scenario-editor="">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
        <ScenarioTabs
          entries={entries}
          activeId={activeId}
          onSelect={onSelect}
          onAdd={onAddVariant}
          onDelete={onDeleteVariant}
          onRename={onRename}
          canAdd={variants.length < MAX_VARIANTS}
        />
        <div className="flex items-center gap-1">
          {open && (
            <Button size="xs" variant="subtle" onClick={resetActive}>
              Reset {activeVariant == null ? "base" : activeLabel}
            </Button>
          )}
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
      </div>

      {open && (
        <>
          <div className="px-4 py-3">
            <div className="augur-eyebrow">Scenario settings</div>
            {multi && (
              <div className="mt-1 text-xs augur-muted">
                Edit a Base cell to change it everywhere; edit a variant cell to override just that variant (↩ reverts
                it to Base). Pick a different property per scenario in the first row.
              </div>
            )}
            <div className="mt-3 overflow-x-auto" data-product-scenario-table="">
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
                  <tr>
                    <th
                      colSpan={1 + entries.length}
                      className="bg-slate-50 px-3 py-1.5 text-left text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400"
                    >
                      Property
                    </th>
                  </tr>
                  <tr data-product-knob-row="propertyId">
                    <th className="whitespace-nowrap px-3 py-1.5 text-left font-medium augur-strong">
                      Property to buy
                    </th>
                    <td className="px-3 py-1.5 align-top">
                      <div className="min-w-[8rem]">
                        <KnobCell
                          knob={{ kind: "property" }}
                          value={base.input.propertyId}
                          ariaLabel="Property to buy — Base"
                          bootstrap={bootstrap}
                          onChange={(value) => onSetBaseField("propertyId", value)}
                        />
                      </div>
                    </td>
                    {variants.map((variant) => (
                      <PropertyVariantCell
                        key={variant.id}
                        variant={variant}
                        baseInput={base.input}
                        bootstrap={bootstrap}
                        onSetVariantProperty={onSetVariantProperty}
                        onRevertKeys={onRevertKeys}
                      />
                    ))}
                  </tr>
                  {SCALAR_GROUPS.map((group) => (
                    <React.Fragment key={group}>
                      <tr>
                        <th
                          colSpan={1 + entries.length}
                          className="bg-slate-50 px-3 py-1.5 text-left text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400"
                        >
                          {group}
                        </th>
                      </tr>
                      {SCALAR_KNOBS.filter((knob) => knob.group === group).map((knob) => (
                        <tr key={knob.key} data-product-knob-row={knob.key}>
                          <th className="whitespace-nowrap px-3 py-1.5 text-left font-medium augur-strong">
                            {knob.label}
                          </th>
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
                  ))}
                </tbody>
              </table>
            </div>
            <SellOrderControl
              sellOrder={base.input.sellOrder}
              portfolio={portfolio}
              onChange={(sellOrder) => onSetBaseField("sellOrder", sellOrder)}
            />
          </div>

          <HousingSection
            base={base}
            activeVariant={activeVariant}
            activeColor={scenarioColor(activeIndex)}
            bootstrap={bootstrap}
            horizonMonths={horizonMonths}
            onSetBasePatch={onSetBasePatch}
            onPatchVariant={onPatchVariant}
            onRevertKeys={onRevertKeys}
            onOverrideHousing={onOverrideHousing}
          />

          <ProductPortfolioPanel portfolio={portfolio} error={portfolioError} />
        </>
      )}
    </div>
  );
}
