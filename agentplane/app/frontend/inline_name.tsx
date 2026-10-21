import { TextInput } from "@mantine/core";
import { type JSX, useState } from "react";

import "./inline_name.css";

/**
 * A name edited where it is read: the field is the name, styled as a heading, and a hover is the
 * only hint that it takes typing. Enter commits and Escape puts the stored name back; moving away
 * commits too, so a rename is never lost by clicking elsewhere -- renaming again is one edit, where
 * losing what was typed is not recoverable at all.
 *
 * While nothing is being typed the field shows the stored `name`, so a rename that arrives from
 * elsewhere shows. `onCommit` gets a changed name, trimmed, or `null` for a blank one.
 */
export function InlineName({
  label,
  name,
  placeholder,
  maxLength,
  disabled,
  className,
  onCommit,
}: {
  label: string;
  name: string | null;
  placeholder?: string;
  maxLength: number;
  disabled: boolean;
  /** The root element's, for the caller to place the field. */
  className?: string;
  onCommit: (name: string | null) => void;
}): JSX.Element {
  const [draft, setDraft] = useState<string | null>(null);

  function commit(): void {
    if (draft === null) return;
    const committed = draft.trim() || null;
    setDraft(null);
    if (committed !== name) onCommit(committed);
  }

  return (
    <TextInput
      aria-label={label}
      disabled={disabled}
      variant="unstyled"
      size="sm"
      value={draft ?? name ?? ""}
      placeholder={placeholder}
      maxLength={maxLength}
      className={className}
      classNames={{ input: "agentplane-inline-name-input" }}
      onChange={(event) => setDraft(event.currentTarget.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === "Enter") event.currentTarget.blur();
        if (event.key === "Escape") setDraft(null);
      }}
    />
  );
}
