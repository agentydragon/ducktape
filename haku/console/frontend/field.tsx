import type { ReactNode } from "react";

export function Field({ label, children, mono = false }: { label: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className="haku-shell-field">
      <dt>{label}</dt>
      <dd className={mono ? "haku-shell-mono" : ""}>{children}</dd>
    </div>
  );
}
