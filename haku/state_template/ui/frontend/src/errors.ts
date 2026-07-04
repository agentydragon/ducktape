import { notifications } from "@mantine/notifications";

// How errors reach the operator, by failure kind:
//   - A *user action* failed (a save, a toggle, a note) → notifyError → a red toast. Non-blocking,
//     the surface stays put, the operator can retry. The default for mutations.
//   - A *surface can't load* its content at all → <LoadError> (load_error.tsx), replacing the region.
//
// NOTE(later): notifyError is also the choke point to ship errors to the backend for Haku to review
//   and fix — see memory/improvements/ui-error-telemetry.md.

// Render an unknown thrown value as a readable string (Error message, else String()).
export const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e));

// Surface a failed user action as a red toast. `title` names the action ("Couldn't save your
// answer"); the thrown value supplies the detail line.
export function notifyError(title: string, e: unknown): void {
  notifications.show({ color: "red", title, message: errText(e), autoClose: 8000 });
}
