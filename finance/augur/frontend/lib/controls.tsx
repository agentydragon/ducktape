import { NativeSelect, NumberInput, Text } from "@mantine/core";

const FIELD_LABEL_CLASSES = { label: "augur-field-label mb-1 block", input: "augur-tabular" };

function numberFieldSectionWidth(section) {
  if (!section) return undefined;
  return Math.max(34, String(section).length * 8 + 24);
}

export function NumberField({
  label = undefined,
  value,
  onChange,
  min = 0,
  max = undefined,
  step = 1000,
  prefix = null,
  suffix = null,
  ...inputProps
}) {
  const formattedPrefix = prefix ? String(prefix).trim() : undefined;
  const formattedSuffix = suffix ? String(suffix).trim() : undefined;
  return (
    <NumberInput
      label={label}
      aria-label={label}
      min={min}
      max={max}
      step={step}
      value={value ?? ""}
      hideControls
      leftSection={formattedPrefix ? <Text className="augur-number-section">{formattedPrefix}</Text> : undefined}
      leftSectionPointerEvents="none"
      leftSectionWidth={numberFieldSectionWidth(formattedPrefix)}
      rightSection={formattedSuffix ? <Text className="augur-number-section">{formattedSuffix}</Text> : undefined}
      rightSectionPointerEvents="none"
      rightSectionWidth={numberFieldSectionWidth(formattedSuffix)}
      thousandSeparator=","
      classNames={FIELD_LABEL_CLASSES}
      {...inputProps}
      onChange={(nextValue) => {
        const number = typeof nextValue === "number" ? nextValue : Number(nextValue);
        onChange(Number.isFinite(number) ? number : null);
      }}
    />
  );
}

// Mantine NativeSelect with the project's standard label classNames so callsites don't repeat
// `classNames={{ label: "augur-field-label mb-1 block", input: "augur-tabular" }}` per use.
export function NativeSelectField(props) {
  return <NativeSelect classNames={FIELD_LABEL_CLASSES} {...props} />;
}
